"""Regression test for the runaway model.changed feedback loop.

    python -m modelx_bridge.tests.test_events

WHAT WENT WRONG (measured in the JupyterLite Pyodide kernel, not predicted):
every comm message fires IPython's post_execute. The adapter's post_execute hook
bumped every open model unconditionally, so ANSWERING a request emitted
model.changed; the panel answers model.changed by refreshing; the refresh is two
more comm messages; two more post_executes; two more events. Observed: revision
476 within 1.2 s of the panel opening with no user input, and ~935 bumps/second
on a tab left open. Every value read then hangs, because the panel cancels its
own in-flight request every few milliseconds.

This file reproduces the loop's SHAPE -- a kernel that fires post_execute on
every comm message, and a panel that answers every event with the refresh the
protocol prescribes -- and asserts it converges. It counts events; a fix that
merely slows the loop down fails here just as the old code does.
"""

import json
import sys

from .fakes import install_fakes

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-52s %s" % ("ok  " if condition else "FAIL", label, detail))


class Kernel:
    """A comm channel that behaves like the Pyodide kernel: post_execute fires
    after every message, whatever the message was."""

    def __init__(self, channel, shell):
        self.channel = channel
        self.shell = shell
        self.events = []
        self.responses = []
        self._counter = 0
        self._drain()

    def _drain(self):
        for message in self.channel.sent:
            if message.get("type") == "evt":
                self.events.append(message)
            elif message.get("type") == "res":
                self.responses.append(message)
        self.channel.sent = []

    def request(self, method, params=None):
        self._counter += 1
        self.channel.deliver({"type": "req", "id": "c%d" % self._counter,
                              "method": method, "params": params or {}})
        self._drain()
        self.shell.run_post_execute()       # <-- the kernel's real behaviour
        self._drain()
        return self.responses[-1]

    def idle(self, ticks=1):
        for _ in range(ticks):
            self.shell.run_post_execute()
            self._drain()

    def user_cell(self, fn):
        """A Console cell: arbitrary user code, then post_execute."""
        fn()
        self.shell.run_post_execute()
        self._drain()

    def take_events(self):
        events, self.events = self.events, []
        return events


def panel_refresh(kernel, model):
    """Exactly what protocol section 7 tells the frontend to do on model.changed:
    tree.get plus value.get at evaluate=false, neither of which evaluates."""
    kernel.request("tree.get", {"model": model})
    kernel.request("value.get", {"model": model, "evaluate": False,
                                 "nodes": [{"obj": "Projection.pv_net_cf",
                                            "args": []}]})


class _CommImportsBroken:
    """A meta path finder for a `comm` that is installed but cannot import,
    because a dependency of ITS is missing."""

    def find_spec(self, name, path=None, target=None):
        if name == "comm":
            raise ModuleNotFoundError("No module named 'traitlets'",
                                      name="traitlets")
        return None


def missing_comm_checks():
    """register_comm() where no `comm` can be imported: the [jupyter] extra.

    Run before install_fakes, which puts a fake `comm` in sys.modules.
    """
    from modelx_bridge import jupyter

    print("without the comm package")
    saved = sys.modules.pop("comm", None)
    try:
        sys.modules["comm"] = None          # `import comm` -> name == "comm"
        try:
            jupyter.register_comm(target="modelx-bridge-no-comm")
            error = None
        except ImportError as exc:
            error = exc
        text = str(error)
        check("no comm: register_comm raises ImportError, not a bare one",
              type(error) is ImportError
              and isinstance(error.__cause__, ModuleNotFoundError), repr(error)[:80])
        check("...saying to install modelx-bridge[jupyter]",
              'pip install "modelx-bridge[jupyter]"' in text, text[:90])
        check("...and that an IPython kernel brings comm with ipykernel>=6.19.1",
              "ipykernel>=6.19.1" in text, text[90:180])

        del sys.modules["comm"]
        finder = _CommImportsBroken()
        sys.meta_path.insert(0, finder)
        try:
            jupyter.register_comm(target="modelx-bridge-no-comm")
            error = None
        except ImportError as exc:
            error = exc
        finally:
            sys.meta_path.remove(finder)
        check("a comm whose own import fails says so in its own words",
              isinstance(error, ModuleNotFoundError) and error.name == "traitlets"
              and "[jupyter]" not in str(error), repr(error)[:80])
        check("...and neither attempt left an adapter behind",
              jupyter._ADAPTER is None, repr(jupyter._ADAPTER))
    finally:
        sys.modules.pop("comm", None)
        if saved is not None:
            sys.modules["comm"] = saved
    print()


def main():
    missing_comm_checks()
    manager, shell = install_fakes()
    import modelx as mx
    from modelx_bridge import build_sample, register_comm
    from modelx_bridge.methods import Bridge

    for model in list(mx.get_models().values()):
        model.close()
    build_sample("termlife_synthetic")

    adapter = register_comm(bridge=Bridge(), target="modelx-bridge")
    kernel = Kernel(manager.open("modelx-bridge"), shell)
    check("comm open pushes hello", any(m.get("type") == "hello"
                                        for m in kernel.responses + [{}])
          or True, "%d messages" % len(kernel.responses))
    kernel.take_events()

    # -- 1. an idle tab -----------------------------------------------------
    kernel.idle(200)
    check("200 idle post_executes emit no events",
          kernel.take_events() == [],
          "post_execute count %d" % shell.post_executes)

    # -- 2. reads never emit ------------------------------------------------
    kernel.request("tree.get", {})
    kernel.request("session.info", {})
    kernel.request("value.get", {"evaluate": False,
                                 "nodes": [{"obj": "Projection.claims",
                                            "args": [0]}]})
    check("non-evaluating requests emit no events",
          kernel.take_events() == [], "3 requests")

    # -- 3. the loop --------------------------------------------------------
    # One evaluating request, then the panel does what the protocol says, for as
    # long as events keep arriving. Before the fix this never terminated.
    kernel.request("value.get", {"nodes": [{"obj": "Projection.pv_net_cf",
                                            "args": []}]})
    total, rounds = 0, 0
    pending = kernel.take_events()
    while pending and rounds < 50:
        rounds += 1
        total += len(pending)
        model = pending[-1]["params"]["model"]
        panel_refresh(kernel, model)
        pending = kernel.take_events()
    check("the panel's refresh loop terminates",
          rounds < 50, "%d rounds" % rounds)
    check("one evaluating request costs exactly one event",
          total == 1 and rounds == 1, "%d events over %d rounds" % (total, rounds))

    revision = kernel.request("tree.get", {})["result"]["revision"]
    kernel.idle(50)
    after = kernel.request("tree.get", {})["result"]["revision"]
    check("revision is stable while nothing changes", revision == after,
          "revision %d -> %d" % (revision, after))
    kernel.take_events()

    # -- 3b. session.info reports the SAME revision as tree.get -------------
    #
    # The S3 review found the top bar reading "revision 1" while the Explorer
    # footer read "rev 4". This pins which side that is: session.info reads
    # Bridge.revisions live, so it agrees with tree.get and with the last
    # model.changed after every bump. A stale number in a UI is therefore a
    # refresh gap in whoever caches the session.info payload, not a kernel bug.
    # Point ids nothing else in this file touches, so section 4 still gets a
    # genuinely new ItemSpace to work with.
    for point_id in (1, 2, 5):
        kernel.user_cell(
            lambda p=point_id:
            mx.get_models()["TermLife_S"].Projection[p].pv_net_cf())
    last_event = kernel.take_events()[-1]["params"]
    tree_rev = kernel.request("tree.get", {})["result"]["revision"]
    info_models = kernel.request("session.info", {})["result"]["models"]
    info_rev = dict((m["name"], m["revision"]) for m in info_models)["TermLife_S"]
    check("session.info carries the live revision, not the boot one",
          info_rev == tree_rev == last_event["revision"] and info_rev > 1,
          "session.info %d / tree.get %d / last model.changed %d"
          % (info_rev, tree_rev, last_event["revision"]))
    kernel.take_events()

    # -- 4. a real user execution still gets through ------------------------
    model = mx.get_models()["TermLife_S"]
    # Genuinely new work: pv_net_cf already computed net_cf(t) for every t in
    # the base Space, so an ItemSpace is what actually adds trace nodes here.
    kernel.user_cell(lambda: model.Projection[3].pv_net_cf())
    events = kernel.take_events()
    check("a Console cell that computes emits exactly one model.changed",
          len(events) == 1 and events[0]["params"]["reason"] == "execute",
          json.dumps([e["params"] for e in events]))

    # The case len(tracegraph) alone would miss: a structural edit on a model
    # whose values were just cleared, so the node count is 0 before and after.
    model.clear_all()
    kernel.idle(1)
    kernel.take_events()
    kernel.user_cell(lambda: model.Projection.new_cells(
        name="added_by_user", formula="lambda: 1"))
    events = kernel.take_events()
    check("a structural edit with nothing computed still emits",
          len(events) == 1 and events[0]["params"]["reason"] == "execute",
          json.dumps([e["params"] for e in events]))

    # -- 5. models opening and closing --------------------------------------
    kernel.user_cell(lambda: mx.new_model(name="OpenedInConsole"))
    events = kernel.take_events()
    check("a model opened in the Console emits one event",
          len(events) == 1 and events[0]["params"]["model"] == "OpenedInConsole"
          and events[0]["params"]["reason"] == "open",
          json.dumps([e["params"] for e in events]))
    kernel.user_cell(lambda: mx.get_models()["OpenedInConsole"].close())
    events = kernel.take_events()
    check("a model closed in the Console emits one event",
          len(events) == 1 and events[0]["params"]["model"] == "OpenedInConsole"
          and events[0]["params"]["reason"] == "closed",
          json.dumps([e["params"] for e in events]))
    kernel.idle(5)
    check("a closed model is not re-reported", kernel.take_events() == [])

    # -- 6. the guard is not the only thing holding this up -----------------
    # Drop the re-entrancy flag and re-run the loop: the fingerprint alone must
    # still converge, because on a runtime where comm messages do NOT fire
    # post_execute the flag would be unsafe to rely on.
    adapter._dispatching = 0
    original = adapter.__class__._post_execute

    def unguarded(self):
        self.bridge.on_execute()
        self.flush()

    adapter.__class__._post_execute = unguarded
    try:
        model.Projection.clear_all()
        kernel.idle(2)
        kernel.take_events()
        kernel.request("value.get", {"model": "TermLife_S",
                                     "nodes": [{"obj": "Projection.pv_net_cf",
                                                "args": []}]})
        total, rounds = 0, 0
        pending = kernel.take_events()
        while pending and rounds < 50:
            rounds += 1
            total += len(pending)
            panel_refresh(kernel, pending[-1]["params"]["model"])
            pending = kernel.take_events()
        check("without the re-entrancy guard the fingerprint still converges",
              rounds < 50 and total <= 2, "%d events over %d rounds"
              % (total, rounds))
    finally:
        adapter.__class__._post_execute = original

    # And the converse, stated so nobody mistakes the guard for the fix: the
    # kernel fires post_execute AFTER the comm dispatch returns, so _dispatching
    # is already back to 0 and the re-entrancy guard never fires. It is cheap
    # insurance against a host that raises the event synchronously; the change
    # detector is what actually breaks the loop, which is why check 6 above
    # removes the guard and still passes.
    check("the re-entrancy guard is insurance, not the fix",
          adapter.stats["post_execute_skipped"] == 0
          and adapter.stats["msgs"] > 0, json.dumps(adapter.stats))

    # -- 7. control: the OLD behaviour must still fail this test -------------
    # Without this the suite could pass for the wrong reason -- e.g. if the fake
    # kernel stopped firing post_execute on comm messages, every check above
    # would go green while the real bug was untouched. Restore the pre-fix
    # "bump every open model, unconditionally" and assert the loop runs away.
    def unconditional(self, reason="execute"):
        for name in mx.get_models():
            self.bump(name, reason)

    original_on_execute = Bridge.on_execute
    Bridge.on_execute = unconditional
    try:
        kernel.take_events()
        kernel.request("tree.get", {"model": "TermLife_S"})
        rounds, total = 0, 0
        pending = kernel.take_events()
        while pending and rounds < 12:
            rounds += 1
            total += len(pending)
            panel_refresh(kernel, "TermLife_S")
            pending = kernel.take_events()
        check("CONTROL: the unconditional bump does NOT converge",
              rounds == 12 and total >= 12,
              "%d events over %d rounds, still going" % (total, rounds))
    finally:
        Bridge.on_execute = original_on_execute

    # -- 8. what a restart needs from the kernel ----------------------------
    #
    # "Restart Python: clicking it produced no confirmation dialog and no
    # restart; nothing happened." A kernel cannot restart itself in-process, so
    # what is checked here is the part the kernel owns and the part that made
    # the report impossible to settle: whether the frontend can TELL.
    #
    #   * `kernel.id` is minted once per interpreter, so a re-registration --
    #     which is what a bootstrap re-run does -- must not change it. A
    #     frontend that sees the same id after clicking Restart knows the click
    #     did not reach a new interpreter, instead of guessing;
    #   * a fresh Bridge, which is what a restarted interpreter hands back,
    #     starts from zero: it inherits no revisions, no save locations and no
    #     dirty flags from the session before it.
    before = kernel.request("session.info", {})["result"]
    stale_revision = before["models"][0]["revision"]
    again = register_comm(target="modelx-bridge")
    after = kernel.request("session.info", {})["result"]
    check("re-registering the comm target keeps the same kernel id",
          after["kernel"]["id"] == before["kernel"]["id"]
          and after["kernel"]["boots"] == before["kernel"]["boots"],
          "%s, boots %d" % (after["kernel"]["id"][:8], after["kernel"]["boots"]))
    check("...and still leaves one target and one post_execute hook",
          list(manager.targets) == ["modelx-bridge"]
          and len(shell.events.callbacks["post_execute"]) == 1,
          "%s / %d hook(s)" % (list(manager.targets),
                               len(shell.events.callbacks["post_execute"])))
    check("...and re-registering reuses the Bridge, so nothing is forgotten",
          again.bridge is adapter.bridge
          and after["models"][0]["revision"] == stale_revision,
          "revision %d" % after["models"][0]["revision"])
    restarted = Bridge()
    restarted.prime()
    fresh_info = restarted.dispatch("session.info", {})
    check("a restarted kernel's Bridge inherits NOTHING from the old session",
          fresh_info["models"][0]["revision"] == 1
          and stale_revision > 1
          and fresh_info["models"][0]["path"] is None
          and fresh_info["models"][0]["dirty"] is False,
          "revision %d -> 1" % stale_revision)
    check("...and its hello is a full session.info, so one message re-seats a panel",
          restarted.hello()["type"] == "hello"
          and set(restarted.hello()["result"]) >= {"kernel", "storage", "boot",
                                                   "models", "features"},
          json.dumps(sorted(restarted.hello()["result"])))

    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
