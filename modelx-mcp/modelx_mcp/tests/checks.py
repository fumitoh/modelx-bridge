"""Plain checks, as in python/modelx_bridge/tests: no pytest.

Each suite prints one line per check, "ok  " or "FAIL", and exits 1 if any
failed. A suite that cannot run here prints a line starting "skip " with the
reason and exits 0; run_all.py counts it as skipped, never as passed.
"""
import os
import re
import sys

from modelx_bridge.samples import shipped_model_dir

FAILURES = []

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.dirname(HERE)
#: The folder that holds modelx_mcp/: lifelib Studio's python/, or the public
#: repo's modelx-mcp/. Suites launch subprocesses from it and put it on their
#: PYTHONPATH; in the public layout modelx_bridge must already be importable.
PYTHON_ROOT = os.path.dirname(PACKAGE)
#: The shipped BasicTerm_S, found the way modelx_bridge finds it: lifelib
#: Studio's apps/lite/files, the bridge package's own models/ folder in the
#: public repo, or a copy named by $MODELX_BRIDGE_MODELS.
SHIPPED = shipped_model_dir("BasicTerm_S")

#: Neither may reach a model: a handle id means nothing to a reader and is
#: evicted within a few calls; __SpaceN is modelx's internal ItemSpace name,
#: which names nothing a person can cite (spec 6.1).
LEAK = re.compile(r"__Space|\bh\d+\b")


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-64s %s" % ("ok  " if condition else "FAIL", label,
                           str(detail).replace("\n", "\\n")[:160]))


def skip(reason):
    print("skip " + reason)


def finish():
    print("\n%d failed" % len(FAILURES) if FAILURES else "\nall passed")
    for label in FAILURES:
        print("  FAILED: " + label)
    return 1 if FAILURES else 0


def clean(label, text, max_chars=12000):
    """Every tool output: no handle id, no __SpaceN, within the bound."""
    leak = LEAK.search(text)
    check(label + ": no handle id or __SpaceN", leak is None,
          "found %r" % leak.group(0) if leak else "")
    check(label + ": within %d chars" % max_chars, len(text) <= max_chars, "%d chars" % len(text))


def call(fn, *args, **kwargs):
    """A tool call as the server makes it: (text, is_error)."""
    from modelx_mcp.refs import RefError
    from modelx_mcp.tools import ToolError, WireError
    try:
        return fn(*args, **kwargs), False
    except (ToolError, RefError) as e:
        return str(e), True
    except WireError as e:
        return "%s: %s" % (e.code, e.message), True


def safe(fn, *args, **kwargs):
    """call()'s text, or an exception no tool may raise, as text. A check of a
    whole-call crash ("internal error: KeyError") then FAILS with what went
    wrong, and the suite goes on to its other checks."""
    try:
        return call(fn, *args, **kwargs)[0]
    except Exception as e:
        return "UNCAUGHT %s: %s" % (type(e).__name__, e)


def state(session, model):
    """(computed nodes, revision, ItemSpaces) read straight from the bridge.
    The evaluation guard holds a reader to leaving all three unchanged."""
    info = [m for m in session.bridge.dispatch("session.info", {})["models"]
            if m["name"] == model][0]
    tree = session.bridge.dispatch("tree.get", {"model": model})
    stack, items = list(tree["root"].get("spaces") or []), 0
    while stack:
        sp = stack.pop()
        items += (sp.get("itemspaces") or {}).get("total", 0)
        stack.extend(sp.get("spaces") or [])
    session.bridge.drain_events()
    return (info["computed"], info["revision"], items)


def run_module(main):
    sys.exit(main())
