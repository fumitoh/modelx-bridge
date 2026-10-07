"""Drive modelx_bridge.register_comm() through a REAL Jupyter kernel.

    python .github/jupyter_roundtrip.py [--expect-ipykernel 6.19.1]

The suites replace `comm` and IPython with fakes, so they cannot tell whether
the `jupyter` extra's ipykernel bound still holds. This does what a Jupyter
frontend does, over ZMQ, against the ipykernel installed beside the wheel:

1. start a kernel and check it runs this same environment;
2. in the kernel, `modelx_bridge.register_comm()`;
3. open a comm on the target `modelx-bridge` and read the `hello`;
4. send model.open_sample and value.get, and check pv_net_cf;
5. run user code that computes a new node, and wait for the model.changed
   event the post_execute hook pushes (protocol sections 1, 2 and 7).

The last line is "ROUND TRIP OK ..." and the exit status 0, or "ROUND TRIP
FAILED ..." and 1. Run it with the working directory outside this checkout.
"""

import argparse
import math
import queue
import sys
import time

from jupyter_client.manager import start_new_kernel

COMM = "roundtrip-comm-1"
TIMEOUT = 120
#: BasicTerm_S's pv_net_cf(), as every suite in this repository pins it.
PV_NET_CF = 910.92066093366


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--expect-ipykernel", help="fail unless the kernel runs this ipykernel")
    args = ap.parse_args(argv)

    problems = []
    km, kc = start_new_kernel(kernel_name="python3", startup_timeout=TIMEOUT)
    pending = []
    kernel_ipykernel = "?"
    try:
        def execute(code):
            """Run code in the kernel: its status, and what it printed or the error."""
            msg_id = kc.execute(code)
            printed = []
            while True:
                msg = kc.get_iopub_msg(timeout=TIMEOUT)
                if msg["msg_type"] == "comm_msg":
                    pending.append(msg)       # post_execute's event, for comm_data()
                    continue
                if msg["parent_header"].get("msg_id") != msg_id:
                    continue
                if msg["msg_type"] == "stream":
                    printed.append(msg["content"]["text"])
                if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                    break
            reply = kc.get_shell_msg(timeout=TIMEOUT)
            while reply["parent_header"].get("msg_id") != msg_id:
                reply = kc.get_shell_msg(timeout=TIMEOUT)
            content = reply["content"]
            if content["status"] == "error":
                return "error", "%s: %s" % (content.get("ename"), content.get("evalue"))
            return content["status"], "".join(printed).strip()

        def comm_data():
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                if pending:
                    msg = pending.pop(0)
                else:
                    msg = kc.get_iopub_msg(timeout=max(0.1, deadline - time.time()))
                if msg["msg_type"] == "comm_msg" and msg["content"]["comm_id"] == COMM:
                    return msg["content"]["data"]
            raise TimeoutError("no comm_msg on %s within %d s" % (COMM, TIMEOUT))

        def send(msg_type, content):
            kc.shell_channel.send(kc.session.msg(msg_type, content))

        status, printed = execute("import sys, ipykernel, modelx_bridge\n"
                                  "print(sys.prefix, ipykernel.__version__, modelx_bridge.VERSION)")
        prefix, kernel_ipykernel, bridge = printed.rsplit(" ", 2)
        print("kernel: %s, ipykernel %s, modelx-bridge %s" % (prefix, kernel_ipykernel, bridge))
        if prefix != sys.prefix:
            problems.append("the kernel runs %s, not this environment (%s)" % (prefix, sys.prefix))
        if args.expect_ipykernel and kernel_ipykernel != args.expect_ipykernel:
            problems.append("ipykernel %s, expected %s" % (kernel_ipykernel, args.expect_ipykernel))

        status, printed = execute("adapter = modelx_bridge.register_comm()\n"
                                  "print(adapter.target)")
        print("register_comm:", status, printed)
        if status != "ok" or printed != "modelx-bridge":
            # Nothing further can work, so do not wait for a hello that cannot come.
            raise RuntimeError("register_comm() in the kernel gave %s: %s" % (status, printed))

        send("comm_open", {"comm_id": COMM, "target_name": "modelx-bridge",
                           "data": {"protocol": 0, "client": "jupyter-roundtrip/0"}})
        hello = comm_data()
        print("hello:", hello.get("type"), "bridge", hello.get("result", {}).get("bridge"))
        if hello.get("type") != "hello" or hello.get("result", {}).get("bridge") != bridge:
            problems.append("the first message on the comm is not this bridge's hello: %r"
                            % (hello,))

        def request(rid, method, params):
            send("comm_msg", {"comm_id": COMM, "data": {"type": "req", "id": rid,
                                                        "method": method, "params": params}})
            while True:
                data = comm_data()
                if data.get("type") == "res" and data.get("id") == rid:
                    return data

        opened = request("r1", "model.open_sample", {"sample": "BasicTerm_S"})
        print("model.open_sample:", opened.get("result"))
        res = request("r2", "value.get", {"model": "BasicTerm_S",
                                          "nodes": [{"obj": "Projection.pv_net_cf", "args": []}]})
        value = res.get("result", {}).get("values", [{}])[0].get("value", {})
        print("value.get pv_net_cf:", value)
        if not (isinstance(value, dict) and isinstance(value.get("v"), float)
                and math.isclose(value["v"], PV_NET_CF, rel_tol=1e-9)):
            problems.append("pv_net_cf came back as %r, not %r" % (value, PV_NET_CF))

        status, _ = execute("import modelx as mx\n"
                            "mx.get_models()['BasicTerm_S'].Projection[2].pv_net_cf()")
        event = comm_data()
        print("after user code:", status, event)
        if not (event.get("type") == "evt" and event.get("event") == "model.changed"
                and event.get("params", {}).get("model") == "BasicTerm_S"):
            problems.append("user code that computed did not push model.changed: %r" % (event,))
    except queue.Empty:
        problems.append("the kernel sent nothing for %d s" % TIMEOUT)
    except Exception as exc:
        problems.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)

    for line in problems:
        print("FAIL " + line)
    if problems:
        print("ROUND TRIP FAILED (%d problem(s))" % len(problems))
        return 1
    print("ROUND TRIP OK with ipykernel %s" % kernel_ipykernel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
