"""The server over REAL stdio, driven by the mcp client SDK (spec 13).

    python -m modelx_mcp.tests.test_stdio

Launches `python3 -m modelx_mcp` as Claude Code does (evals/run_claude_code.py
builds the same command line) and calls every tool through the protocol, so
what is checked is what a client receives: the instructions, the tools/list
JSON (held equal to tools.json), each result's text, isError and the absence
of structuredContent. It also measures the prefix a client pays on every
request: the tools/list JSON plus the instructions.

Every session also holds stdout to the protocol: the client SDK logs a stdout
line it cannot parse and carries on, so without the message handler below a
server printing a line on every call passed every check here (measured: 13
"input_value='STRAY-STDOUT-LINE'" parse failures, then "all passed"). And a
call has a timeout, so a response glued to stray bytes fails its check
instead of hanging the suite.
"""
import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta

from modelx_mcp.tests.checks import PACKAGE, PYTHON_ROOT, SHIPPED, check, clean, finish, run_module
from modelx_mcp.tools import NO_MODEL

SIX = ["get_tree", "get_formulas", "get_map", "calculate", "get_value", "trace"]
TOOLS_JSON = os.path.join(PACKAGE, "tools.json")


def env():
    e = dict(os.environ)
    e["PYTHONPATH"] = PYTHON_ROOT + os.pathsep + e.get("PYTHONPATH", "")
    e.setdefault("PYTHONIOENCODING", "utf-8")
    return e


CALL_TIMEOUT = timedelta(seconds=30)


async def serve(args, calls, errlog, got):
    """Launch, initialize, list the tools, make `calls`, filling `got`. A call
    that raises (a timeout, a closed connection) is a result whose text starts
    "RAISED" and whose isError is None; every stdout line the client could not
    parse is in got["stray"]."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "modelx_mcp"] + args,
                                   env=env(), cwd=PYTHON_ROOT)

    async def on_message(message):
        # The SDK forwards each stdout line that is not JSON-RPC here, as the
        # exception its parse raised.
        if isinstance(message, Exception):
            got["stray"].append(str(message)[:300])
    t0 = time.time()
    async with stdio_client(params, errlog=errlog) as (r, w):
        async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=60),
                                 message_handler=on_message) as s:
            init = await s.initialize()
            got["init_s"] = time.time() - t0
            got["instructions"] = init.instructions or ""
            got["server"] = (init.serverInfo.name, init.serverInfo.version)
            listed = await s.list_tools()
            got["tools"] = [t.model_dump(exclude_none=True) for t in listed.tools]
            got["output_schemas"] = [t.outputSchema for t in listed.tools]
            for name, a in calls:
                try:
                    res = await s.call_tool(name, a, read_timeout_seconds=CALL_TIMEOUT)
                except Exception as e:
                    got["results"].append((name, a, "RAISED %s: %s" % (type(e).__name__, e), None, None, 0))
                    continue
                text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                got["results"].append((name, a, text, res.isError, res.structuredContent,
                                       len(res.content)))


def run(args, calls):
    got = {"results": [], "stray": [], "instructions": "", "tools": [], "output_schemas": [],
           "init_s": float("inf"), "server": None, "crashed": None}
    with tempfile.TemporaryFile("w+") as errlog:
        try:
            asyncio.run(serve(args, calls, errlog, got))
        except Exception as e:      # a server that died takes the session down with it
            got["crashed"] = "%s: %s" % (type(e).__name__, e)
        errlog.seek(0)
        got["stderr"] = errlog.read()
    # A result for every call, so a check reads "RAISED ..." rather than an IndexError.
    for name, a in calls[len(got["results"]):]:
        got["results"].append((name, a, "RAISED: the session ended first (%s)" % got["crashed"], None, None, 0))
    return got


def raw(args, messages, timeout=60):
    """Launch the server, write `messages` to it as JSON-RPC lines, wait for
    the answer to each one that has an id, then close its stdin ->
    (answers, stderr, exit code). No client SDK, so a request the SDK would
    never send (server/discover) goes out as Claude Code sends it. An answer
    that does not come within `timeout` is None."""
    proc = subprocess.Popen([sys.executable, "-m", "modelx_mcp"] + args, env=env(), cwd=PYTHON_ROOT,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines, err = queue.Queue(), []
    threading.Thread(target=lambda: [lines.put(x) for x in iter(proc.stdout.readline, b"")],
                     daemon=True).start()
    reader = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    reader.start()
    answers = []
    try:
        for m in messages:
            try:
                proc.stdin.write((json.dumps(m) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError:         # the server has gone: its stderr says why
                break
            if "id" in m:
                try:
                    answers.append(json.loads(lines.get(timeout=timeout)))
                except (queue.Empty, ValueError):
                    answers.append(None)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout)
    return answers, (err[0] if err else b"").decode("utf-8", "replace"), proc.returncode


INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test_stdio", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
#: What Claude Code 2.1.291 sends first on every connect (its id is the one the
#: server answered there, its params are not known); mcp 1.x does not know the
#: method, so the params do not change the answer.
DISCOVER = {"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover", "params": {}}


def stdout_clean(label, got):
    check(label + ": stdout carried only JSON-RPC (the client parsed every line)",
          not got["stray"] and not got["crashed"], (got["stray"][:2], got["crashed"]))


def main():
    P = "BasicTerm_S.Projection."
    # -- --print-tools and tools.json -----------------------------------------
    printed = subprocess.run([sys.executable, "-m", "modelx_mcp", "--print-tools"], env=env(),
                             cwd=PYTHON_ROOT, capture_output=True, text=True)
    with open(TOOLS_JSON, encoding="utf-8") as f:
        committed = f.read()
    check("--print-tools prints tools.json byte for byte, opening no model",
          printed.returncode == 0 and printed.stdout == committed and "open" not in printed.stderr,
          printed.stderr[-200:])
    v = subprocess.run([sys.executable, "-m", "modelx_mcp", "--version"], env=env(), cwd=PYTHON_ROOT,
                       capture_output=True, text=True)
    check("--version", v.stdout.strip() == "modelx-mcp 0.1.0", v.stdout)
    v = subprocess.run([sys.executable, "-m", "modelx_mcp", "--max-chars", "100"], env=env(),
                       cwd=PYTHON_ROOT, capture_output=True, text=True)
    check("--max-chars under 2,000 is refused at launch", v.returncode == 2 and "at least 2000" in v.stderr,
          v.stderr[-120:])
    # The console script is `modelx-mcp`. The first cut said "usage:
    # modelx_mcp" and described itself with a `python3 -m` line.
    v = subprocess.run([sys.executable, "-m", "modelx_mcp", "--help"], env=env(), cwd=PYTHON_ROOT,
                       capture_output=True, text=True)
    check("--help names the program modelx-mcp and shows the launch commands",
          v.returncode == 0 and v.stdout.startswith("usage: modelx-mcp ")
          and "\n    modelx-mcp --sample BasicTerm_S\n" in v.stdout
          and "\n    modelx-mcp --storage-root /path/to/models --open /MyModel\n" in v.stdout
          and "PYTHONPATH" not in v.stdout and "python3 -m" not in v.stdout, v.stdout[:300])
    # MEASURED before this: an absolute --open path with no --storage-root was
    # reported as "not a modelx model" and the server ran with nothing open.
    t0 = time.time()
    v = subprocess.run([sys.executable, "-m", "modelx_mcp", "--open", SHIPPED or os.path.abspath("MyModel")],
                       env=env(), cwd=PYTHON_ROOT, capture_output=True, text=True, timeout=60)
    check("--open without --storage-root is a usage error, before any model loads",
          v.returncode == 2 and "modelx-mcp: error: --open needs --storage-root: --storage-root is the "
          "folder that holds your saved models" in v.stderr
          and "modelx-mcp --storage-root /path/to/models --open /MyModel" in v.stderr
          and ": open " not in v.stderr and not v.stdout, "exit %s in %.2f s: %s"
          % (v.returncode, time.time() - t0, v.stderr[-200:]))

    # -- stderr: one line from the server, whatever the client probes --------
    # MEASURED before this, over this raw stdio with mcp 1.28.1: the probe
    # added 6,080 bytes of pydantic errors ("Failed to validate request: 31
    # validation errors for ClientRequest") and tools/list a "Processing
    # request of type ListToolsRequest" line. --sample twice is opened once and
    # named once (it was built twice, and stderr said "open BasicTerm_S,
    # BasicTerm_S").
    answers, err, code = raw(["--sample", "BasicTerm_S", "--sample", "BasicTerm_S"],
                             [DISCOVER, INITIALIZE, INITIALIZED, LIST])
    probe, init, listed = [a or {} for a in (answers + [None] * 3)[:3]]
    check("server/discover is answered with an error, then the server connects as usual",
          probe.get("id") == "server-discover-probe-1" and (probe.get("error") or {}).get("code") == -32602
          and ((init.get("result") or {}).get("serverInfo") or {}).get("name") == "modelx"
          and [t["name"] for t in (listed.get("result") or {}).get("tools", [])] == SIX and code == 0,
          (probe, code))
    check("after server/discover and tools/list, stderr holds only the 'open' line, naming the sample once",
          err == "modelx-mcp 0.1.0: open BasicTerm_S\n", "%d bytes: %r" % (len(err), err[:300]))
    # The filter is narrow: any other request that fails validation, a
    # malformed call or another unknown method, is still logged.
    answers, err, code = raw([], [INITIALIZE, INITIALIZED,
                                  {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": 12345}},
                                  {"jsonrpc": "2.0", "id": 8, "method": "server/discover2", "params": {}}])
    check("a malformed tools/call and an unknown method other than server/discover are still logged",
          [((a or {}).get("error") or {}).get("code") for a in answers[1:]] == [-32602, -32602]
          and err.count("Failed to validate request:") == 2 and "input_value='tools/call'" in err
          and "input_value='server/discover2'" in err, "%d bytes: %r" % (len(err), err[:200]))

    # -- the read surface ---------------------------------------------------------
    log = os.path.join(tempfile.mkdtemp(prefix="modelx-mcp-stdio-"), "journal.jsonl")
    calls = [
        ("get_tree", {}),
        ("get_formulas", {"refs": [P + "claims"], "docstrings": False}),
        ("get_map", {"cells": "claims"}),
        ("calculate", {"refs": ["Projection[2].pols_lapse(t=30)"]}),
        ("get_value", {"refs": ["Projection[2].pols_lapse(t=30)", "Projection.claims(t="]}),
        ("trace", {"ref": "BasicTerm_S.Projection[2].pols_lapse(t=30)"}),
        ("get_value", {"refs": [P + "disc_rate_ann"], "offset": 148, "rows": 2}),
        ("trace", {"ref": "BasicTerm_S.Projection"}),
        ("trace", {"ref": P + "pv_net_cf()", "direction": "down"}),
        ("calculate", {"refs": []}),
        ("get_value", {"refs": [P + "disc_rate_ann[3]", P + "disc_rate_ann.iloc[3]"]}),
    ]
    got = run(["--sample", "BasicTerm_S", "--log", log], calls)
    check("initialize within 30 s", got["init_s"] < 30, "%.2f s" % got["init_s"])
    # FastMCP 1.28.1 sends the mcp package's own version unless told otherwise.
    check("serverInfo is modelx 0.1.0, not the mcp SDK's version", got["server"] == ("modelx", "0.1.0"),
          got["server"])
    check("the instructions name the open model",
          "Open models: BasicTerm_S (the shipped sample BasicTerm_S)." in got["instructions"])
    check("the instructions say read-only", "It is read-only: no tool edits a formula or a Reference."
          in got["instructions"])
    names = [t["name"] for t in got["tools"]]
    check("tools/list is exactly the six; run_python is absent by default", names == SIX, names)
    check("no tool advertises an outputSchema", got["output_schemas"] == [None] * 6, got["output_schemas"])
    check("tools/list equals tools.json", got["tools"] == json.loads(committed))
    check("no input schema carries a pydantic 'title'", '"title"' not in json.dumps(got["tools"]))
    prefix_tools = len(json.dumps(got["tools"]))
    prefix_instr = len(got["instructions"])
    print("prefix: tools/list %d chars + instructions %d chars = %d"
          % (prefix_tools, prefix_instr, prefix_tools + prefix_instr))
    res = dict((k, r) for k, r in enumerate(got["results"]))
    for k in range(7):
        name, a, text, err, structured, n = res[k]
        check("%s over stdio: one text block, not isError, no structuredContent" % name,
              not err and structured is None and n == 1 and text, (err, structured, text[:100]))
        clean("stdio %s" % name, text)
    check("calculate over stdio creates the ItemSpace and prints the value",
          "# created ItemSpace BasicTerm_S.Projection[2] (ran the Space formula; it stays in the model)"
          in res[3][2] and "BasicTerm_S.Projection[2].pols_lapse(t=30) = 0.004125084186073208  "
                           "[computed now; reads 3; read by 0 computed]" in res[3][2], res[3][2])
    check("get_value over stdio: a value and a per-ref error in one call",
          "BasicTerm_S.Projection[2].pols_lapse(t=30) = 0.004125084186073208  [reads 3; read by 0 computed]"
          in res[4][2] and "Projection.claims(t= -> 'Projection.claims(t=' is not a ref." in res[4][2], res[4][2])
    check("trace over stdio: the recorded precedents",
          "BasicTerm_S.Projection[2].lapse_rate(t=30) = 0.060000000000000005  (reads 1)" in res[5][2])
    check("get_value paging over stdio", "rows 148-149 of 151" in res[6][2], res[6][2][:100])
    name, a, text, err, structured, n = res[7]
    check("a whole-call error is isError with one sentence and no traceback",
          err and text == "Error executing tool trace: BasicTerm_S.Projection is a Space; trace follows a "
                          "Cells node, e.g. BasicTerm_S.Projection.<cells>(...)" and "Traceback" not in text, text)
    name, a, text, err, structured, n = res[8]
    check("a schema violation is FastMCP's own isError", err and "'preds' or 'succs'" in text, text[:200])
    name, a, text, err, structured, n = res[9]
    check("an empty refs list is isError, naming an example", err and "refs is empty; pass e.g." in text, text)
    # The first cut advertised "[i]" for one element, in get_value's text and
    # in CITING, but [i] is refused on every Series (measured on disc_rate_ann).
    name, a, text, err, structured, n = res[10]
    value_text = [t["description"] for t in got["tools"] if t["name"] == "get_value"]
    check("[i] is advertised for an ndarray only, as get_value refuses it on a Series",
          "Projection.disc_rate_ann[3] -> refused: [3] on a Series is ambiguous" in text
          and P + "disc_rate_ann.iloc[3] = 0.00788" in text
          and "for one element add .loc[label] or .iloc[i] ([i] on an ndarray only)" in got["instructions"]
          and value_text and ".iloc[i] read one element ([i] on an ndarray only)" in value_text[0], text)
    check("stderr says what opened", "modelx-mcp 0.1.0: open BasicTerm_S\n" in got["stderr"],
          got["stderr"][-200:])
    stdout_clean("the read surface", got)
    with open(log, encoding="utf-8") as f:
        records = [json.loads(line) for line in f]
    check("--log: one journal record per call, errors included",
          [r["tool"] for r in records] == [c[0] for c in calls if c[1].get("direction") != "down"],
          [r["tool"] for r in records])
    calc = [r for r in records if r["tool"] == "calculate"][0]
    # The bridge reads a missing `evaluate` as true (value.get and trace.preds,
    # protocol section 6), so a reader that leaves it out evaluates: the first
    # cut's `.get("evaluate")` read it as false and stayed green on a mutant
    # whose readers omitted the key.
    check("--log: a record carries every bridge call with its result",
          all("result" in c or "error" in c for c in calc["calls"]) and
          any(c["method"] == "value.get" and c["params"].get("evaluate", True) for c in calc["calls"]))
    read = [c for r in records if r["tool"] != "calculate" for c in r["calls"]
            if c["method"] in ("value.get", "trace.preds")]
    check("--log: only calculate's records let value.get or trace.preds evaluate",
          read and not [c for c in read if c["params"].get("evaluate", True)],
          [c["params"] for c in read if c["params"].get("evaluate", True)][:2])
    check("--log: ... and the readers' records include both methods",
          set(c["method"] for c in read) == {"value.get", "trace.preds"}, sorted(set(c["method"] for c in read)))

    # -- --allow-python --------------------------------------------------------------
    # Over stdio, fds 0 and 1 were the channel. MEASURED before __main__ moved
    # the transport off them: exit() and quit() closed the protocol's input and
    # the server died unanswered (exit code 1); input() swallowed the client's
    # next request; bytes written to fd 1 without a newline were glued to the
    # response line and the call never completed.
    noisy = ("import os, subprocess, sys\n"
             "print('a', end='')\n"
             "os.write(1, b'FD1-NO-NEWLINE')\n"
             "subprocess.run([sys.executable, '-c', \"import sys; sys.stdout.write('CHILD-NO-NEWLINE')\"])\n"
             "sys.__stdout__.write('DUNDER'); sys.__stdout__.flush()\n"
             "os.write(2, b'FD2')")
    got = run(["--sample", "BasicTerm_S", "--allow-python"],
              [("run_python", {"code": "1/0"}), ("run_python", {"code": "BasicTerm_S.Projection.point_id"}),
               ("run_python", {"code": "exit()"}), ("run_python", {"code": "quit()"}),
               ("run_python", {"code": "x = input()\nx"}), ("run_python", {"code": noisy}),
               ("run_python", {"code": "import warnings\nwarnings.warn('VISIBLE-WARNING')\n6"}),
               ("run_python", {"code": "1 + 1"})])
    names = [t["name"] for t in got["tools"]]
    check("--allow-python adds run_python as a seventh tool", names == SIX + ["run_python"], names)
    rp = got["tools"][-1] if got["tools"] else {"annotations": {}}
    check("run_python is annotated destructive and not read-only",
          rp["annotations"].get("destructiveHint") is True and rp["annotations"].get("readOnlyHint") is False,
          rp["annotations"])
    check("the instructions no longer claim read-only, nor that calculate only reads",
          "Only run_python can change a model" in got["instructions"]
          and "It is read-only" not in got["instructions"]
          and "calculate computes values and can create ItemSpaces; the other tools only read."
          in got["instructions"])
    R = [r[2] for r in got["results"]]
    name, a, text, err, structured, n = got["results"][0]
    check("run_python: an exception in the code is output, not isError",
          not err and "ZeroDivisionError: division by zero" in text and structured is None, text)
    check("run_python: a value", "value: 1" in R[1], R[1])
    check("run_python: exit() is answered, as SystemExit", "\nSystemExit: None\n" in R[2], R[2][:200])
    check("run_python: quit() is answered, as SystemExit", "\nSystemExit: None\n" in R[3], R[3][:200])
    check("run_python: input() raises EOFError; it reads nothing the client sent",
          "\nEOFError: EOF when reading a line\n" in R[4], R[4][:200])
    check("run_python: fds 1 and 2, a child process and sys.__stdout__ are its output, in order",
          "stdout:\naFD1-NO-NEWLINECHILD-NO-NEWLINEDUNDERFD2\n" in R[5], R[5][:300])
    check("run_python: a warning is its output, not the server's stderr",
          "stdout:\n<run_python>:2: UserWarning: VISIBLE-WARNING\nvalue: 6" in R[6]
          and "VISIBLE-WARNING" not in got["stderr"], R[6][:200])
    check("run_python: the server still answers after all of that", "value: 2" in R[7], R[7][:200])
    stdout_clean("--allow-python", got)
    check("stderr says run_python is enabled", "; run_python ENABLED" in got["stderr"])
    py_tools = len(json.dumps(got["tools"]))
    print("prefix with run_python: tools/list %d chars + instructions %d chars = %d"
          % (py_tools, len(got["instructions"]), py_tools + len(got["instructions"])))

    # -- nothing opens ------------------------------------------------------------------
    root = tempfile.mkdtemp(prefix="modelx-mcp-root-")
    got = run(["--storage-root", root, "--open", "/Nope"],
              [("get_tree", {}), ("calculate", {"refs": ["Projection.pv_net_cf()"]})])
    check("a failed open is in the instructions, with its reason",
          "Open models: none; failed to open: /Nope (" in got["instructions"], got["instructions"][:400])
    check("stderr names the failure", "modelx-mcp: could not open /Nope: " in got["stderr"], got["stderr"][-300:])
    name, a, text, err, structured, n = got["results"][0]
    check("no_model, whole call: get_tree is isError", err and text == "Error executing tool get_tree: " + NO_MODEL,
          text)
    name, a, text, err, structured, n = got["results"][1]
    # calculate heads its text with every ref that gave no value (review
    # finding 19; the same text test_synthetic pins).
    check("no_model, per ref: calculate answers inside the batch",
          not err and text == "NO VALUE for 1 of the 1 refs; each one's reason is its ' -> ' line below: "
                              "[\"Projection.pv_net_cf()\"]\nProjection.pv_net_cf() -> " + NO_MODEL, text)
    stdout_clean("nothing opens", got)

    # -- a model whose formulas write to stdout ----------------------------------------
    # Session.dispatch redirects sys.stdout only; a formula reaches fd 1 too.
    # MEASURED before isolate_stdio: calculate of fdwrite(1) and shell(1) never
    # completed (their bytes were glued to the response), and dunder(1) put a
    # line on the channel that the client could not parse.
    made = subprocess.run([sys.executable, "-c", NOISY_MODEL, root], env=env(), cwd=PYTHON_ROOT,
                          capture_output=True, text=True)
    check("the noisy model is written", made.returncode == 0, made.stderr[-300:])
    refs = ["S.printed(1)", "S.fdwrite(1)", "S.dunder(1)", "S.shell(1)"]
    got = run(["--storage-root", root, "--open", "/Noisy"], [("calculate", {"refs": [r]}) for r in refs])
    want = ["Noisy.S.printed(t=1) = 1.0", "Noisy.S.fdwrite(t=1) = 2.0", "Noisy.S.dunder(t=1) = 3.0",
            "Noisy.S.shell(t=1) = 0.0"]
    for (name, a, text, err, structured, n), w in zip(got["results"], want):
        check("a formula that writes to stdout: calculate %s is answered" % a["refs"][0],
              err is False and w in text, text[:200])
    stdout_clean("a noisy model", got)
    check("what the formulas wrote went to stderr",
          all(x in got["stderr"] for x in ("PRINTED 1", "FD1-NO-NEWLINE", "DUNDER-STDOUT", "CHILD-NO-NEWLINE")),
          got["stderr"][-300:])
    return finish()


#: A model whose formulas write to stdout four ways: print, fd 1 with no
#: newline, sys.__stdout__, and a child process with no newline.
NOISY_MODEL = r'''
import os, sys
import modelx as mx
m = mx.new_model("Noisy")
s = m.new_space("S")
s.new_cells("printed", formula="def printed(t):\n    print('PRINTED', t)\n    return t * 1.0")
s.new_cells("fdwrite", formula="def fdwrite(t):\n    import os\n    os.write(1, b'FD1-NO-NEWLINE')\n    return t * 2.0")
s.new_cells("dunder", formula="def dunder(t):\n    import sys\n    sys.__stdout__.write('DUNDER-STDOUT\\n')\n    sys.__stdout__.flush()\n    return t * 3.0")
s.new_cells("shell", formula="def shell(t):\n    import subprocess, sys\n    return float(subprocess.run([sys.executable, '-c', \"import sys; sys.stdout.write('CHILD-NO-NEWLINE')\"]).returncode)")
m.write(os.path.join(sys.argv[1], "Noisy"))
'''


if __name__ == "__main__":
    run_module(main)
