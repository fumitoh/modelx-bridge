"""What BasicTerm_S cannot show, on models built here with mx.new_model.

    python -m modelx_mcp.tests.test_synthetic

  [A] no model open: every tool's no_model answer, whole-call or per ref
  [B] ONE NODE, TWO PRINTED FORMS. Without bridge B2 (deferred, protocol
      18.8) value.get echoes the arguments as requested, so f(t=3) prints as
      `Synth.S.f(t=3)` from calculate and as `Synth.S.f(t=3, kind=None)` in a
      trace. Both must name the same node: same value, same counts, same
      trace, the same verify_citations verdict, and calculating one leaves the
      other [cached].
  [C] trace grouping: a run collapsed, three kinds listed, 45 groups paged
  [D] Series values: per-label range statistics, label= (B6) with a missing
      label, an empty cache; an ndarray with nothing to align it to
  [E] internal errors and an unreadable `computed`: never a traceback, never 0
  [F] several models: inference, ambiguity, model=
  [G] the server instructions, built from what is really open
  [H] the review of 2026-10-05, on a third model built after [F] and [G]:
      ItemSpace keys with a dot, a Space without parameters, mixed and
      MultiIndex ranges, big containers, a 2-D ndarray, an aligned ndarray
      paged past its end, a Cells read through another Space, and argument
      hints that invent no value. Each check failed on the code it was found in.
"""
import asyncio
import json
import os
import re
import tempfile

from modelx_mcp.tests.checks import call, check, clean, finish, run_module, safe, state
from modelx_mcp.tools import NO_MODEL, Tools, verify_citations

OUTPUTS = []


def out(label, text):
    OUTPUTS.append((label, text))
    return text


def build():
    import modelx as mx
    import numpy as np
    import pandas as pd
    m = mx.new_model("Synth")
    s = m.new_space("S")
    s.pd, s.np = pd, np
    for src in ("def f(t, kind=None):\n    return t * 2.0 if kind is None else t * 3.0",
                "def g():\n    return f(3)",
                "def v(t):\n    return pd.Series([t * 1.0, -t * 2.0], index=pd.Index([1, 2], name='point'))",
                "def h(t):\n    return t * 1.5",
                "def sumh():\n    return sum(h(t) for t in range(10))",
                "def k(t, kind):\n    return {'A': 1.0, 'B': 2.0, 'C': 3.0}[kind] * t",
                "def ktot(t):\n    return sum(k(t, x) for x in 'ABC')",
                "def base():\n    return 1.0",
                "def boom(t):\n    raise ValueError('boom at %d' % t)",
                "def arr():\n    return np.arange(12.0)",
                "def nothing(t):\n    return t"):
        s.new_cells(src.split("(")[0][4:], formula=src)
    for j in range(45):
        s.new_cells("c%d" % j, formula="def c%d():\n    return base() + %d" % (j, j))
    m2 = mx.new_model("Synth2")
    s2 = m2.new_space("S")
    s2.new_cells("f", formula="def f(t):\n    return t")
    s2.new_cells("only2", formula="def only2():\n    return 2")
    q = m2.new_space("Q", formula="def _formula(i):\n    return None")
    q.i = 1
    q.new_cells("y", formula="def y():\n    return i * 2")
    return m, m2


def build_adv():
    """[H]'s model. Built after [F]: a third model with a Space S would make
    [F]'s inference and ambiguity checks about two models false."""
    import modelx as mx
    import numpy as np
    import pandas as pd
    m = mx.new_model("Adv")
    s = m.new_space("S")
    s.pd, s.np = pd, np
    series = "pd.Series([t * 1.0, t * 2.0], index=pd.Index([1, 2], name='pid'))"
    for src in ("def dk():\n    return {i: float(i) for i in range(30)}",
                "def mixed(t):\n    return 0.0 if t == 0 else " + series,
                "def mixed2(t):\n    return 0.0 if t == 2 else " + series,
                "def sizes(t):\n    return pd.Series(range(30 if t == 0 else 20), dtype=float)",
                "def biglist():\n    return list(range(2000))",
                "def bigdict():\n    return {i: i for i in range(2000)}",
                "def grid():\n    return np.arange(12.0).reshape(3, 4)",
                "def wide():\n    return np.arange(60.0).reshape(3, 20)",
                "def mi(t):\n    return pd.Series([t * 1.0, t * 2.0], index=pd.MultiIndex.from_tuples("
                "[(1, 2), (3, 4)], names=['a', 'b']))",
                "def model_point():\n    return pd.DataFrame({'x': [1.0, 2.0, 3.0]}, "
                "index=pd.Index([101, 102, 103], name='policy_id'))",
                "def arr3():\n    return np.arange(3.0) * 10",
                "def pair(t, kind):\n    return t * {'A': 1.0, 'B': 2.0}[kind]",
                "def zerod():\n    return np.array(5.0)",
                "def cube():\n    return np.zeros((2, 3, 4))",
                "def lists(t):\n    return list(range(2000 + t))",
                "def strs():\n    return ['a, b, c' * 40 for _ in range(300)]",
                "def pairlist():\n    return [1.0, 2.0]",
                "def ridx():\n    return pd.RangeIndex(5)"):
        s.new_cells(src.split("(")[0][4:], formula=src)
    r = m.new_space("Rate", formula="def _formula(rate):\n    return None")
    r.rate = 0.01
    r.new_cells("y", formula="def y():\n    return rate * 100")
    lb = m.new_space("Lbl", formula="def _formula(tag):\n    return None")
    lb.tag = "a"
    lb.new_cells("z", formula="def z():\n    return len(tag)")
    bad = m.new_space("Bad", formula="def _formula(k):\n    if k == 3:\n        raise ValueError('no item 3')\n"
                                     "    return None")
    bad.k = 0
    bad.new_cells("w", formula="def w():\n    return 1")
    a = m.new_space("Assumptions")
    a.new_cells("rate", formula="def rate():\n    return 0.05")
    res = m.new_space("Results")
    res.new_cells("total", formula="def total():\n    return 100 * Assumptions.rate()")
    res.Assumptions = a
    # A Space whose parameter is no Reference of its base: only an item has i.
    o = m.new_space("Outer", formula="def _formula(i):\n    return None")
    o.new_cells("a", formula="def a(t):\n    return i * t")
    o.new_space("Kid").new_cells("c", formula="def c(t):\n    return t + 1")
    return m


def main():
    from modelx_mcp import server
    from modelx_mcp.session import Session

    # -- [A] nothing open ---------------------------------------------------------
    root = tempfile.mkdtemp(prefix="modelx-mcp-test-")
    S0 = Session(storage_root=root, paths=["/Nope"])
    T0 = S0.tools
    check("[A] a launch open that fails is kept with its reason",
          S0.opened == [] and len(S0.failed) == 1 and S0.failed[0][0] == "/Nope", S0.failed)
    for name, fn, a in (("get_tree", T0.get_tree, ()), ("get_map", T0.get_map, ()),
                        ("trace", T0.trace, ("S.f(t=1)",))):
        t, err = call(fn, *a)
        check("[A] no_model, whole call: %s" % name, err and t == NO_MODEL, t)
    for name, fn in (("calculate", T0.calculate), ("get_value", T0.get_value),
                     ("get_formulas", T0.get_formulas)):
        t, err = call(fn, ["S.f(t=1)"])
        # calculate names every ref that gave no value above everything else
        # (review finding 19), so a cut can never hide that one was refused.
        want = ("NO VALUE for 1 of the 1 refs; each one's reason is its ' -> ' line below: "
                "[\"S.f(t=1)\"]\n" if name == "calculate" else "") + "S.f(t=1) -> " + NO_MODEL
        check("[A] no_model, per ref: %s" % name, not err and t.strip() == want, t)
    models, items, names = S0.describe()
    check("[A] the instructions name the failure and its reason",
          models.startswith("none; failed to open: /Nope (") and items == [] and names == [], models)

    # -- build the models ------------------------------------------------------------
    build()
    S = Session()
    T = S.tools
    check("a Session over models built in the process sees both", sorted(S.describe()[0].split(", "))
          == ["Synth (no file)", "Synth2 (no file)"], S.describe()[0])

    # -- [B] one node, two printed forms ----------------------------------------------
    t = out("calc f(3)", T.calculate(["Synth.S.f(3)"]))
    check("[B] calculate prints the node as requested", "Synth.S.f(t=3) = 6.0  [computed now; reads 0; "
                                                       "read by 0 computed]" in t, t)
    out("calc g", T.calculate(["Synth.S.g()"]))
    t = out("trace g", T.trace("Synth.S.g()"))
    check("[B] trace prints the same node with its default filled",
          "Synth.S.f(t=3, kind=None) = 6.0  (reads 0)" in t, t)
    forms = ["Synth.S.f(t=3)", "Synth.S.f(t=3, kind=None)", "Synth.S.f(3, None)", "Synth.S.f(3)",
             "Synth.S.f(kind=None, t=3)"]
    t = out("value forms", T.get_value(forms))
    got = re.findall(r"^(Synth\.S\.f\([^)]*\)) = (\S+)  \[(reads \d+; read by \d+ computed)\]$", t, re.M)
    check("[B] every printed form reads the same cached value and counts",
          len(got) == 5 and len(set((v, c) for _, v, c in got)) == 1 and got[0][1:] == ("6.0", "reads 0; read by 1 computed"),
          got)
    check("[B2 DEFERRED, still open] value.get echoes the arguments as requested",
          "Synth.S.f(t=3) = 6.0" in t and "Synth.S.f(t=3, kind=None) = 6.0" in t, t)
    a, b = T.resolve("Synth.S.f(t=3)"), T.resolve("Synth.S.f(t=3, kind=None)")
    check("[B] both resolve to the same obj", a.obj == b.obj == "S.f", (a.obj, b.obj))
    r = S.dispatch("value.get", {"model": "Synth", "evaluate": False,
                                 "nodes": [{"obj": a.obj, "args": a.args}, {"obj": b.obj, "args": b.args}]})
    e1, e2 = r["values"]
    check("[B] the bridge reads one node for both: same value and counts",
          e1["cached"] and e2["cached"] and e1["value"] == e2["value"]
          and (e1["predslen"], e1["succslen"]) == (e2["predslen"], e2["succslen"]), (e1, e2))
    t1 = out("succs f(t=3)", T.trace("Synth.S.f(t=3)", direction="succs"))
    t2 = out("succs f(t=3, kind=None)", T.trace("Synth.S.f(t=3, kind=None)", direction="succs"))
    check("[B] trace of either form records the same dependents",
          t1.split("\n", 1)[1] == t2.split("\n", 1)[1].replace("f(t=3, kind=None)", "f(t=3)")
          and "Synth.S.g() = 6.0  (read by 0 computed)" in t1, (t1, t2))
    rows = verify_citations(T, [{"ref": x, "value": 6.0} for x in forms])
    check("[B] verify_citations: MATCH for every form", [r["verdict"] for r in rows] == ["MATCH"] * 5,
          [r["verdict"] for r in rows])
    t = out("calc other form", T.calculate(["Synth.S.f(t=3, kind=None)"]))
    check("[B] calculating the other form computes nothing: it is [cached]",
          "Nothing was computed." in t and "Synth.S.f(t=3, kind=None) = 6.0  [cached;" in t, t)
    out("calc f(4) filled", T.calculate(["Synth.S.f(t=4, kind=None)"]))
    t = out("value f(4) short", T.get_value(["Synth.S.f(4)"]))
    check("[B] and the reverse: computed with the default written, read without it",
          t == "Synth.S.f(t=4) = 8.0  [reads 0; read by 0 computed]", t)
    t = out("kind given", T.calculate(["Synth.S.f(t=3, kind='X')"]))
    check("[B] a non-default argument is a different node", "Synth.S.f(t=3, kind='X') = 9.0  [computed now" in t, t)

    # -- [C] trace grouping -----------------------------------------------------------
    out("calc sumh", T.calculate(["Synth.S.sumh()", "Synth.S.ktot(t=2)"]))
    t = out("trace sumh", T.trace("Synth.S.sumh()"))
    check("[C] four or more neighbours of one Cells collapse to a run",
          "Synth.S.h(t=0..9)  x10 nodes; first (t=0) = 0.0, last (t=9) = 13.5  "
          "(one: trace(\"Synth.S.h(t=0)\"))" in t, t)
    t = out("trace ktot", T.trace("Synth.S.ktot(t=2)"))
    check("[C] fewer than four are listed one by one (no kind hidden)",
          all("Synth.S.k(t=2, kind='%s') = %s  (reads 0)" % (x, v) in t
              for x, v in (("A", "2.0"), ("B", "4.0"), ("C", "6.0"))), t)
    t = out("two ranges", T.calculate(["Synth.S.k(t=range(0, 2), kind=range(0, 2))", "Synth.S.k(range(0, 3), 'A')"]))
    check("[C] two ranges in one ref are refused; one range in any position expands",
          "Synth.S.k(t=range(0, 2), kind=range(0, 2)) -> only one argument may be a range()" in t and
          "Synth.S.k(t=range(0, 3), kind='A'): 3 nodes" in t, t)
    out("calc c", T.calculate(["Synth.S.c%d()" % j for j in range(45)]))
    t = out("trace base", T.trace("Synth.S.base()", direction="succs"))
    check("[C] 45 groups: 40 shown, the rest named with offset=40",
          t.count("(read by 0 computed)") == 40 and
          "[5 more groups (5 nodes) not shown: trace(\"Synth.S.base()\", direction=\"succs\", offset=40)]" in t, t[-300:])
    check("[C] part B never lists a name the next page holds", "named in a formula but not in A" not in t, t[-300:])
    t = out("trace base 2", T.trace("Synth.S.base()", direction="succs", offset=40))
    check("[C] offset=40 prints the other 5", t.count("(read by 0 computed)") == 5 and "more groups" not in t
          and "named in a formula but not in A" not in t, t)
    tiny = Tools(S.dispatch, max_chars=2000)
    t = out("small trace base", tiny.trace("Synth.S.base()", direction="succs"))
    m = re.search(r"\[(\d+) more groups \((\d+) nodes\) not shown: .*offset=(\d+)\)\]", t)
    check("[C] at 2,000 chars the page stops at the bound and its offset is the next group",
          m is not None and int(m.group(1)) + int(m.group(3)) == 45
          and t.count("(read by 0 computed)") == int(m.group(3)) and "output bound" not in t, t[-200:])

    # -- [D] vectors ------------------------------------------------------------------
    t = out("v range", T.calculate(["Synth.S.v(t=range(0, 5))"]))
    check("[D] a range of short Series: statistics per label over exactly these nodes",
          "point 2: over these 5 nodes: sum -20.0, min -8.0 at t=4, max 0.0 at t=0" in t and
          "point 1: over these 5 nodes: sum 10.0, min 0.0 at t=0, max 4.0 at t=4" in t, t)
    t = out("v label", T.get_value(["Synth.S.v"], label=2))
    check("[D] label= (B6): one element of each cached value, with where the extremes are",
          "Synth.S.v: 5 cached values, t = 0..4 contiguous; element 2 of each value" in t and
          "sum -20.0, mean -4.0, min -8.0 at t=4, max 0.0 at t=0" in t, t)
    t = out("v label missing", T.get_value(["Synth.S.v"], label=9))
    check("[D] a label no value carries: counted, and no numbers rather than zeros",
          "(5 values lack it)" in t and "no numbers (all 5 values are null)" in t, t)
    t = out("v no label", T.get_value(["Synth.S.v"]))
    check("[D] Series values without label= name the call that gives statistics",
          "each value is a vector (dtype object)" in t and 'get_value(["Synth.S.v"], label=<id>)' in t, t)
    check("[D] the server reads label='2' as the literal 2", server.label_literal("2") == 2 and
          server.label_literal("'A'") == "A" and server.label_literal("A") == "A")
    t = out("empty label", T.get_value(["Synth.S.nothing"], label=1))
    check("[D] a Cells with nothing cached: unknown, not zero, and the calculate call",
          "Synth.S.nothing: 0 cached values: NOTHING COMPUTED yet (unknown, not zero). calculate computes "
          "values, e.g. calculate([\"Synth.S.nothing(t=0)\"])" in t, t)
    t = out("v one", T.get_value(["Synth.S.v(t=3)", "Synth.S.v(t=3).loc[2]", "Synth.S.v(t=3)[0]"]))
    check("[D] a short Series prints whole, with its index name and sum (B9)",
          "Synth.S.v(t=3) = Series float64 [2] point {1: 3.0, 2: -6.0}  sum -3.0" in t, t)
    check("[D] .loc[label] on a Series", "Synth.S.v(t=3).loc[2] = -6.0  (point 2, position 1)" in t, t)
    S.bridge.codec.max_handles = 4
    try:
        t = out("evicted", T.calculate(["Synth.S.v(t=7)", "Synth.S.v(t=range(10, 20))",
                                        "Synth.S.v(t=7).loc[2] * 2"]))
    finally:
        S.bridge.codec.max_handles = 64
    check("[D] a handle evicted between reading and rendering is re-read, not reported",
          "Synth.S.v(t=7) = Series float64 [2] point {1: 7.0, 2: -14.0}  sum -7.0  [computed now" in t
          and "Synth.S.v(t=7).loc[2] * 2 = -28.0  [derived here" in t and "not_found" not in t, t)
    t = out("arr", T.calculate(["Synth.S.arr()"]))
    check("[D] a long ndarray: five, whole-column statistics, positions only",
          "Synth.S.arr() = ndarray float64 [12] [0.0, 1.0, 2.0, 3.0, 4.0, ...]" in t and
          "whole column (all 12): sum 66.0, mean 5.5, min 0.0 at [0], max 11.0 at [11]" in t and
          "model_point" not in t, t)
    t = out("arr loc", T.get_value(["Synth.S.arr().loc[3]", "Synth.S.arr()[3]"]))
    check("[D] .loc on an ndarray with nothing to align it to is refused; [i] reads by position",
          "Synth.S.arr().loc[3] -> refused: an ndarray has no labels; read it by position: [i]" in t
          and "Synth.S.arr()[3] = 3.0" in t, t)
    t = out("boom", T.calculate(["Synth.S.boom(t=7)"]))
    check("[D] formula_error from a model's own raise",
          "Synth.S.boom(t=7) -> formula error: ValueError: boom at 7" in t and
          "get_formulas([\"Synth.S.boom\"]) shows that formula" in t, t)

    # -- [E] internal errors, an unreadable computed ----------------------------------------
    real = S.bridge.dispatch

    def broken(method, params=None, buffers=None):
        if method == "table.stats":
            raise ValueError("stats broke")
        return real(method, params, buffers)
    S.bridge.dispatch = broken
    try:
        t = out("internal per ref", T.get_value(["Synth.S.v(t=3)", "Synth.S.f(t=3)"]))
        check("[E] internal, per ref: the bridge adapter's WireError, and the batch goes on",
              "Synth.S.v(t=3) -> internal: ValueError: stats broke" in t and "Synth.S.f(t=3) = 6.0" in t, t)
        t = out("internal calc", T.calculate(["Synth.S.v(t=3)"]))
        check("[E] internal while rendering a computed value stays per ref",
              "Synth.S.v(t=3) -> internal: ValueError: stats broke" in t and "already cached" in t, t)
    finally:
        S.bridge.dispatch = real

    def no_computed(method, params):
        r = S.dispatch(method, params)
        if method == "session.info":
            r = json.loads(json.dumps(r))
            for m in r["models"]:
                m["computed"] = None
        return r
    U = Tools(no_computed)
    t = out("unknown tree", U.get_tree(model="Synth"))
    check("[E] computed null is printed as unknown, never as nothing computed",
          t.startswith("Synth rev ") and "computed nodes unknown (the bridge could not read modelx's graph)" in t
          and "nothing computed" not in t.splitlines()[0], t.splitlines()[0])
    t = out("unknown calc", U.calculate(["Synth.S.f(t=11)"]))
    check("[E] calculate with computed null says unknown",
          "Computed nodes in the model: unknown (the bridge could not read modelx's graph)." in t
          and "Nothing was computed" not in t, t.splitlines()[0])
    t = out("unknown list", U.get_tree())
    check("[E] the model list says unknown too", t.count("computed nodes unknown") == 2, t)

    class Fake(object):
        opened, failed, journal = [], [], None

        def __init__(self):
            def dispatch(method, params):
                raise KeyError("no such thing")
            self.tools = Tools(dispatch)

        def describe(self):
            return "none", [], []
    mcp = server.build(Fake())
    try:
        asyncio.run(mcp.call_tool("get_tree", {}))
        check("[E] an exception that is not the tool's is an internal error", False, "no error")
    except Exception as e:
        check("[E] an exception that is not the tool's is an internal error, never a traceback",
              str(e) == "Error executing tool get_tree: internal error in modelx-mcp: KeyError: 'no such thing'"
              and "Traceback" not in str(e), str(e))
    journal = []
    Fake.journal = journal.append
    mcp = server.build(Fake())
    try:
        asyncio.run(mcp.call_tool("trace", {"ref": "S.f(t=1)"}))
    except Exception:
        pass
    check("[E] a whole-call error is journaled with the calls it made",
          len(journal) == 1 and journal[0]["tool"] == "trace" and journal[0]["error"].startswith("internal error")
          and journal[0]["calls"] == [], journal)

    # -- [F] several models -----------------------------------------------------------
    t, err = call(T.get_tree)
    check("[F] get_tree with two models lists them and asks for model=",
          not err and t.startswith("2 models are open; pass model= to get_tree:"), t)
    t, err = call(T.get_map)
    check("[F] get_map with two models asks for model=", err and "pass model=" in t, t)
    t = out("infer", T.calculate(["S.only2()"]))
    check("[F] a ref that resolves in one model only: inferred, and said",
          "# model Synth2 inferred: the only open model where 'S.only2()' resolves" in t
          and "Synth2.S.only2() = 2  [computed now" in t, t)
    t = out("ambiguous", T.get_value(["S.f(t=1)"]))
    check("[F] a ref that resolves in two: refused, with each prefixed ref",
          "S.f(t=1) -> 'S.f(t=1)' resolves in 2 open models; start it with one: Synth.S.f(t=1), Synth2.S.f(t=1)" in t, t)
    t = out("item forms", T.calculate(["Synth2.Q[3].y()", "Synth2.Q(3).y()", "Synth2.Q(i=3).y()"]))
    check("[F] Q[3], Q(3) and Q(i=3) are one ItemSpace, created once",
          t.count("# created ItemSpace Synth2.Q[3]") == 1 and t.count("Synth2.Q[3].y() = 6") == 3, t)
    t = out("item base", T.get_value(["Synth2.Q.y()"]))
    check("[F] the base Space's value carries the note naming its Reference",
          "# Synth2.Q takes (i): its values are for the item that the Reference i = 1 selects" in t, t)

    # -- [G] the server instructions ---------------------------------------------------
    models, items, names = S.describe()
    text = server.instructions(models, items, False, names)
    check("[G] the REFS examples say they are BasicTerm_S's when it is not open",
          "(these examples are from the BasicTerm_S sample)" in text)
    check("[G] ITEMS is built from the Spaces that take parameters",
          "ITEMS. Synth2.Q[i] holds one ItemSpace per argument: Synth2.Q[2].x is item 2. Synth2.Q.x, with "
          "no [k], is the base Space: the item its Reference of the same name (i) selects." in text, text)
    check("[G] read-only and calculate-only without run_python",
          "It is read-only: no tool edits a formula or a Reference." in text and
          "calculate is the only tool that computes" in text)
    text = server.instructions(models, items, True, names)
    check("[G] with run_python the instructions do not claim read-only",
          "read-only: no tool edits" not in text and "Only run_python can change a model" in text and
          "run_python can compute too, but cite only what calculate or get_value prints" in text)
    check("[G] no parameterised Space: ITEMS says there are no ItemSpaces",
          server.items_text([], True) == "ITEMS. No Space in the open models takes parameters, so there "
                                         "are no ItemSpaces (Space[k]).")

    # -- [H] the review of 2026-10-05 -----------------------------------------------
    build_adv()
    S3 = Session()
    H = S3.tools

    t = out("H rate item", safe(H.calculate, ["Adv.Rate[0.03].y()", "Adv.Lbl['x.y'].z()"]))
    check("[H] 7: a key with a dot (a rate, a dotted string) gets no base-Space note",
          "selects" not in t and "Adv.Rate[0.03].y() = 3.0  [computed now" in t
          and "Adv.Lbl['x.y'].z() = 3  [computed now" in t, t)
    t = out("H rate item read", safe(H.get_value, ["Adv.Rate[0.03].y()"]))
    check("[H] 7: get_value too", t == "Adv.Rate[0.03].y() = 3.0  [reads 0; read by 0 computed]", t)
    t = out("H rate base", safe(H.calculate, ["Adv.Rate.y()"]))
    check("[H] 7: the base Space keeps its note",
          "# Adv.Rate takes (rate): its values are for the item that the Reference rate = 0.01 selects" in t, t)

    s0 = state(S3, "Adv")
    t = out("H item fails", safe(H.calculate, ["Adv.Bad[1].w() + Adv.Bad[3].w()"]))
    check("[H] 6/20: an ItemSpace made before a later operand's failed is announced, with its model's header",
          state(S3, "Adv")[2] == s0[2] + 1 and "# created ItemSpace Adv.Bad[1] (ran the Space formula" in t
          and t.startswith("# created ItemSpace") and "\nAdv rev " in t and "ValueError: no item 3" in t, t)

    for ref in ("Adv.S[2].dk()", "Adv.S(2).dk()"):
        t = out("H no params", safe(H.get_value, [ref]))
        check("[H] 22: Space[k] on a Space without parameters is refused, never internal (%s)" % ref,
              t == "%s -> Adv.S takes no parameters, so it has no ItemSpaces: write Adv.S.<name>, "
                   "with no [k]" % ref, t)
    t = out("H examples", safe(H.get_value, ["Adv.S.dk(t=0..3)"]))
    shown = re.findall(r"[A-Za-z_][\w.]*\.\w+(?:\([^)]*\))?", t.split("Write refs like ", 1)[-1])
    resolved = []
    for x in shown:
        try:
            resolved.append(H.resolve(x).kind)
        except Exception as e:
            resolved.append("REFUSED %s" % e)
    check("[H] 22: a refusal's examples come from an open model, and each one resolves",
          "BasicTerm_S" not in t and len(shown) == 3 and resolved == ["Cells", "Cells", "Reference"],
          (t, resolved))

    t = out("H mixed", safe(H.calculate, ["Adv.S.mixed(t=range(0, 4))", "Adv.S.mixed2(t=range(0, 4))"]))
    check("[H] 16: a range of scalars and vectors says so row by row, scalar first or later",
          t.count("mixed values: 1 scalars, 3 vectors (Series float64 [2])") == 2
          and "each value is" not in t and "\n  2                 0.0\n" in t and "\n  0                 0.0\n" in t, t)
    t = out("H sizes", safe(H.calculate, ["Adv.S.sizes(t=range(0, 3))"]))
    check("[H] 16: vectors of different sizes are not described as the first one",
          "the values differ: Series float64 [30] (1 nodes), Series float64 [20] (2 nodes)" in t
          and "each value is" not in t, t)
    t = out("H mi", safe(H.calculate, ["Adv.S.mi(t=range(0, 3))", "Adv.S.mi(t=2)"]))
    check("[H] 14: a MultiIndex label prints as the tuple .loc[] takes, in a range and a value",
          "a, b (3, 4): over these 3 nodes: sum 6.0" in t and "{(1, 2): 2.0, (3, 4): 4.0}" in t
          and "builtins" not in t, t)
    t = out("H dict", safe(H.calculate, ["Adv.S.dk()"]))
    check("[H] 17: a dict with int keys says what it cut", "18: 18.0, 19: 19.0, ... 10 more}" in t, t)
    t = out("H big", safe(H.calculate, ["Adv.S.biglist()", "Adv.S.bigdict()"]))
    check("[H] 33: a list or dict too big to inline is never printed as empty",
          "None []" not in t and "Adv.S.biglist() = list (too large to send inline; its size is not sent); "
                                 "its repr's first 300 chars, cut at that length even inside an element: "
                                 "[0, 1, 2, " in t and "Adv.S.bigdict() = dict (too large" in t, t)
    # Finding 33's residuals, each failed on the code it was found in.
    t = out("H zero-d", safe(H.calculate, ["Adv.S.zerod()", "Adv.S.cube()", "Adv.S.strs()", "Adv.S.lists(t=0)",
                                           "Adv.S.ridx()"]))
    check("[H] 33: a 1-D value of another kind (a RangeIndex) prints its values, not its head alone",
          "Adv.S.ridx() = RangeIndex int64 [5] [0, 1, 2, 3, 4]  sum 10  [computed now" in t, t)
    check("[H] 33: a 0-d ndarray prints the one value it holds, said to be read from its repr",
          "Adv.S.zerod() = ndarray float64 0-d (one value): 5.0, read from its repr array(5.)  [computed now" in t, t)
    check("[H] 33: a 3-D ndarray says how many values it has, that none is shown, and what can read them",
          "Adv.S.cube() = ndarray float64 [2x3x4]: 24 values, none shown, and no tool here reads part of it "
          "(table.get pages values of 1 and 2 dimensions only); run_python can, and only when the server "
          "was started with --allow-python  [computed now" in t, t)
    want = repr(["a, b, c" * 40 for _ in range(300)])[:300]
    check("[H] 33: a repr is cut at a fixed length and says so, never at a ', ' inside a string",
          "Adv.S.strs() = list (too large to send inline; its size is not sent); its repr's first 300 chars, "
          "cut at that length even inside an element: " + want + "  [computed now" in t, t)
    t = out("H no reader", safe(H.get_value, ["Adv.S.lists(t=0)[3]", "Adv.S.lists(t=0).iloc[3]",
                                              "Adv.S.zerod()[0]", "Adv.S.cube()[0]"]))
    check("[H] 33: a part no tool reads is refused without advising a call that fails, and names run_python",
          t.count("and no tool here reads part of it") == 4 and t.count("--allow-python") == 4
          and ".iloc[position]" not in t and "bad_request" not in t, t)
    t = out("H no reader derived", safe(H.calculate, ["Adv.S.lists(t=1) + 1", "Adv.S.zerod() * 2",
                                                      "Adv.S.pairlist() * 2"]))
    check("[H] 33: and a derived operand that is not a number advises no [i] or .loc[label] that fails",
          "pick one element" not in t and t.count("no tool here reads part of it") == 3
          and "NO VALUE for 3 of the 3 refs" in t, t)
    t = out("H list range", safe(H.calculate, ["Adv.S.lists(t=range(0, 3))"]))
    check("[H] 33: a range of lists offers no label= (it picks from Series values only)",
          "each value is list (too large to send inline; its size is not sent); read one with "
          "get_value([\"Adv.S.lists(t=0)\"])" in t and "label=" not in t, t)

    # Finding 8's residual: every count in an ItemSpace's header is its own.
    out("H outer", safe(H.calculate, ["Adv.Outer[1].Kid.c(t=range(0, 5))", "Adv.Outer[1].a(t=2)"]))
    t = out("H outer formulas", safe(H.get_formulas, ["Adv.Outer[1]", "Adv.Outer[1].Kid", "Adv.Outer"],
                                     docstrings=False))
    tree = out("H outer tree", safe(H.get_tree, model="Adv", path="Outer[1]"))
    check("[H] 8: an ItemSpace's header counts its own References (its parameter i), as get_tree does",
          "## Adv.Outer[1] - ItemSpace; 1 Cells, 1 References\n" in t
          and "## Adv.Outer[1].Kid - DynamicSpace; 1 Cells, 1 References\n" in t
          and "## Adv.Outer - UserSpace; 1 Cells, 0 References, 1 ItemSpaces\n" in t
          and tree.count("References (1):") == 2 and tree.count("i: int") == 2, t + "\n" + tree)

    t = out("H grid", safe(H.calculate, ["Adv.S.grid()", "Adv.S.wide()"]))
    check("[H] 37: a 2-D ndarray prints its values by column position",
          "        2  8.0  9.0  10.0  11.0" in t and "columns shown: 0 of" not in t, t)
    check("[H] 37: and a wide one names the col= that shows the rest",
          'columns shown: 10 of 20; get_value(["Adv.S.wide()"], col=10) shows the rest' in t, t)

    out("H align", safe(H.calculate, ["Adv.S.model_point()", "Adv.S.arr3()"]))
    t = out("H past end", safe(H.get_value, ["Adv.S.arr3()"], offset=3))
    check("[H] 13: paging an aligned ndarray at its end says there are no rows, never KeyError",
          "no rows at offset 3: there are 3" in t, t)
    t = out("H aligned page", safe(H.get_value, ["Adv.S.arr3()"], offset=1))
    check("[H] 13: and a page inside it is still aligned", "policy_id" in t and "20.0" in t, t)

    t = out("H scoped", safe(H.get_formulas, ["Adv.Assumptions.rate", "Adv.Assumptions"], docstrings=False))
    check("[H] 24: a Cells named only from another Space is not called an output",
          "none (an output)" not in t and "named by the formulas of: none in this Space (formulas in other "
                                          "Spaces are not read here" in t
          and "outputs (no formula in this Space names them; formulas in other Spaces are not read here): rate" in t, t)
    out("H total", safe(H.calculate, ["Adv.Results.total()"]))
    t = out("H scoped trace", safe(H.trace, "Adv.Assumptions.rate()", direction="succs"))
    check("[H] 24: trace part B says its scope beside the reader part A recorded",
          "Adv.Results.total() = 5.0" in t and "named by the formulas of no other Cells in this Space" in t, t)

    t = out("H pair fresh", safe(H.get_value, ["Adv.S.pair"]))
    check("[H] 23: NOTHING COMPUTED names a call with no invented value",
          'calculate(["Adv.S.pair(t=0, kind=<kind>)"]); get_formulas(["Adv.S.pair"]) shows what kind takes' in t, t)
    t = out("H pair hint", safe(H.calculate, ["Adv.S.pair"]))
    check("[H] 23: no 0 for a non-t parameter, and range() only on t",
          "e.g. pair(t=0, kind=<kind>), or a range: pair(t=range(0, 12), kind=<kind>)" in t and "kind=0" not in t, t)
    out("H pair", safe(H.calculate, ["Adv.S.pair(t=5, kind='B')"]))
    t = out("H pair cached hint", safe(H.calculate, ["Adv.S.pair"]))
    check("[H] 23: with a cached key, the example is that key",
          "e.g. pair(t=5, kind='B'), or a range: pair(t=range(0, 12), kind='B')" in t, t)
    t = out("H pair trace", safe(H.trace, "Adv.S.pair"))
    check("[H] 23: trace's example too", "trace one node, e.g. Adv.S.pair(t=5, kind='B')" in t, t)

    for label, text in OUTPUTS:
        clean(label, text, 2000 if label.startswith("small") else 12000)
    return finish()


if __name__ == "__main__":
    run_module(main)
