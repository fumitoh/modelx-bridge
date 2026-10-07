"""Golden replay (spec 11.2, 13): what tools.py prints depends on the wire
alone.

    python -m modelx_mcp.tests.test_golden

Every call of capture_golden.CALLS is captured on the shipped BasicTerm_S with
its bridge calls and their results, then replayed through Tools over a FAKE
dispatch and a fake clock. The replay must:

  - ask for exactly the recorded (method, params), in order;
  - consume every recorded call;
  - print the recorded text byte for byte (a recorded error is re-raised).

The clock advances by each recorded call's ms, so "(0.02 s)" replays too. Any
tests/golden/*.jsonl present (the files a TypeScript port would share) are
replayed the same way.
"""
import glob
import json
import os

from modelx_mcp.refs import RefError
from modelx_mcp.tests.capture_golden import capture
from modelx_mcp.tests.checks import HERE, check, clean, finish, run_module
from modelx_mcp.tools import ToolError, Tools, WireError


class ReplayDispatch(object):

    def __init__(self, record):
        self.calls = list(record["calls"])
        self.now = 0.0
        self.mismatch = None

    def clock(self):
        return self.now

    def dispatch(self, method, params):
        if not self.calls:
            self.mismatch = "an extra call: %s" % method
            raise AssertionError(self.mismatch)
        c = self.calls.pop(0)
        if c["method"] != method or json.dumps(c["params"], sort_keys=True) != json.dumps(params, sort_keys=True):
            self.mismatch = "asked %s %s, recorded %s %s" % (method, json.dumps(params)[:80],
                                                             c["method"], json.dumps(c["params"])[:80])
            raise AssertionError(self.mismatch)
        self.now += c["ms"] / 1000.0
        if "error" in c:
            raise WireError(c["error"]["code"], c["error"]["message"], c["error"].get("data"))
        return c["result"]


def replay(record, label):
    name, args, kwargs = record["call"]
    fake = ReplayDispatch(json.loads(json.dumps(record)))
    tools = Tools(fake.dispatch, clock=fake.clock)
    try:
        text = getattr(tools, name)(*args, **kwargs)
    except (ToolError, RefError) as e:
        text = None
        same = str(e) == record.get("error")
    except AssertionError:
        text, same = None, False
    else:
        same = text == record.get("text")
    check("%s: byte-identical" % label, same, fake.mismatch or "")
    check("%s: every recorded call consumed" % label, not fake.calls and fake.mismatch is None,
          "%d left" % len(fake.calls))
    return text


def main():
    records = capture()
    methods = set()
    for k, r in enumerate(records):
        label = "%02d %s %s" % (k + 1, r["call"][0], json.dumps(r["call"][1])[:40])
        replay(r, label)
        methods.update(c["method"] for c in r["calls"])
        if "text" in r:
            clean(label, r["text"])
    check("the goldens cover every tool",
          set(r["call"][0] for r in records) >= {"get_tree", "get_formulas", "get_map",
                                                 "calculate", "get_value", "trace"})
    check("the goldens include a recorded bridge error (re-raised on replay)",
          any("error" in c for r in records for c in r["calls"]))
    # The bridge reads a missing `evaluate` as true (value.get and trace.preds,
    # protocol section 6). The first cut read it with `.get("evaluate")`, looked
    # at value.get alone and put the inclusion under one all(). It stayed green
    # on a mutant whose readers omitted the key, on one whose trace.preds did,
    # and on a CALLS with no calculate (all() of nothing is True).
    evaluating = [(r["call"][0], c["method"]) for r in records for c in r["calls"]
                  if c["method"] in ("value.get", "trace.preds") and c["params"].get("evaluate", True)]
    check("the goldens include evaluate: true",
          any(m == "value.get" for _, m in evaluating),
          "value.get with evaluate true: %d" % sum(m == "value.get" for _, m in evaluating))
    check("... from calculate only: no reader's value.get or trace.preds evaluates",
          all(tool == "calculate" for tool, _ in evaluating),
          sorted(set(e for e in evaluating if e[0] != "calculate")))
    read = set(c["method"] for r in records if r["call"][0] != "calculate" for c in r["calls"])
    check("... and the readers' calls include both methods",
          read >= {"value.get", "trace.preds"}, sorted(read))
    print("bridge methods in the goldens: %s" % ", ".join(sorted(methods)))
    files = sorted(glob.glob(os.path.join(HERE, "golden", "*.jsonl")))
    print("committed golden files: %d" % len(files))
    for path in files:
        with open(path, encoding="utf-8") as f:
            for k, line in enumerate(f):
                replay(json.loads(line), "%s:%d" % (os.path.basename(path), k + 1))
    return finish()


if __name__ == "__main__":
    run_module(main)
