"""The six tools on the shipped BasicTerm_S, in one fresh process.

    python -m modelx_mcp.tests.test_basicterm_s

Values are pinned as measured (modelx 0.33.0, pandas 3.0.6, CPython 3.11;
spec 13). Four properties are held over the whole run rather than per call:

  THE EVALUATION GUARD. No tool but calculate changes (computed nodes,
  revision, ItemSpaces), measured on the bridge around every reader call, on
  a fresh model and on a computed one with an ItemSpace -- and no reader puts
  `evaluate: true` on the wire.
  THE ROUND TRIP. Every computed Cells node's display, as the bridge prints
  it, resolves back to the same obj and args through dispatch only.
  NO LEAKS. No output contains a handle id or __SpaceN.
  THE BOUND. Every output is within max_chars, and every cut names the call
  that returns the rest -- followed here until nothing is left.
"""
import json
import re
import time

from modelx_mcp.tests.checks import call, check, clean, finish, run_module, safe, state
from modelx_mcp.tools import Tools, verify_citations

M = "BasicTerm_S"
P = M + ".Projection."

#: Measured 2026-10-05 (spec 8, 13).
PV_NET_CF = "910.92066093366"
CLAIMS_0 = "34.18079328868595"
TRACED = 1832                    # computed nodes after pv_net_cf()
FAILED_LEFTOVER = 4              # what claims(t=9999) adds at this point
B7_BEST = 0.17301358164048888    # Projection[10]'s pv_net_cf / pv_premiums
PROJECTION_DOC = 6824            # characters, on every CPython (test_doc.py)

OUTPUTS = []


def reader_calls(T):
    """The readers of the evaluation guard: every non-computing tool, over
    plain nodes, ranges, a Cells listing, pages, accessors, ItemSpaces that
    exist and ones that do not, both trace directions and depth 2."""
    return [
        (T.get_tree, (), {}), (T.get_tree, (), {"filter": "term|lapse"}),
        (T.get_tree, (), {"path": "Projection"}),
        (T.get_formulas, ([M + ".Projection", P + "pv_net_cf", P + "disc_rate_ann"],), {}),
        (T.get_formulas, ([M + ".Projection"],), {"doc_offset": 2000}),
        (T.get_formulas, ([M + ".Projection[3].pv_net_cf", M],), {}),
        (T.get_map, (), {}), (T.get_map, (), {"cells": "pv_net_cf", "depth": -1}),
        (T.get_value, ([P + "pv_net_cf()", P + "claims(t=0)", P + "point_id"],), {}),
        (T.get_value, ([P + "premiums(t=range(0, 12))"],), {}),
        (T.get_value, ([P + "premiums"],), {}),
        (T.get_value, ([M + ".Projection[3].pv_net_cf()", M + ".Projection[2].pv_net_cf()"],), {}),
        (T.get_value, ([P + "disc_factors()"],), {"offset": 2, "rows": 3}),
        (T.get_value, ([P + "disc_factors()[0]", P + "model_point().iloc[0]"],), {}),
        (T.trace, (P + "pv_net_cf()",), {}), (T.trace, (P + "pv_net_cf()",), {"depth": 2}),
        (T.trace, (P + "premiums(t=1)",), {"direction": "succs"}),
        (T.trace, (M + ".Projection[5].pv_net_cf()",), {"direction": "succs"}),
        (T.trace, (M + ".Projection[2].pv_premiums()",), {}),
        (T.trace, (P + "pv_premiums()",), {}),
    ]


def guard(S, label):
    T = S.tools
    calls = reader_calls(T)
    moved, evaluating = [], []
    for fn, a, k in calls:
        s0 = state(S, M)
        text, _ = call(fn, *a, **k)
        s1 = state(S, M)
        OUTPUTS.append(("guard %s %s" % (label, fn.__name__), text))
        if s0 != s1:
            moved.append("%s%r: %s -> %s" % (fn.__name__, a, s0, s1))
        for c in T.calls:
            p = c["params"]
            if (c["method"] == "value.get" and p.get("evaluate", True)) or \
                    (c["method"] == "trace.preds" and p.get("evaluate", True)):
                evaluating.append("%s%r: %s" % (fn.__name__, a, c["method"]))
    s0 = state(S, M)
    verify_citations(T, [{"ref": P + "pv_net_cf()", "value": 1.0},
                         {"ref": M + ".Projection[4].pv_net_cf()", "value": 1.0},
                         {"ref": P + "claims(t=7)", "value": "1"}])
    if state(S, M) != s0:
        moved.append("verify_citations")
    check("guard (%s): %d reader calls and verify_citations leave (computed, revision, "
          "ItemSpaces) unchanged" % (label, len(calls)), not moved, moved[:2])
    check("guard (%s): no reader sent evaluate: true" % label, not evaluating, evaluating[:2])


def out(label, text):
    OUTPUTS.append((label, text))
    return text


def follow(T, label, first, pattern, again):
    """Follow a cut's continuation until there is none; -> every page."""
    pages = [first]
    for _ in range(50):
        m = re.search(pattern, pages[-1])
        if not m:
            break
        pages.append(out(label, again(int(m.group(1)))))
    return pages


def main():
    from modelx_mcp.session import Session
    S = Session(samples=[M])
    T = S.tools
    check("the sample opened", S.opened == [M] and not S.failed, (S.opened, S.failed))
    check("fresh: computed 0, revision 1, no ItemSpace", state(S, M) == (0, 1, 0), state(S, M))
    guard(S, "fresh")

    # -- fresh reads -----------------------------------------------------------
    t = out("tree", T.get_tree())
    check("get_tree: header", t.startswith("BasicTerm_S rev 1, nothing computed\nSpace Projection[point_id]"), t[:80])
    check("get_tree: 40 Cells, none computed", "Cells (40; none computed; values cached in this Space after ':'):" in t)
    check("get_tree: References with their types", "disc_rate_ann: Series, model_point_table: DataFrame" in t)
    t = out("tree filter", T.get_tree(filter="term"))
    check("get_tree filter='term': policy_term only (never the model name)",
          "Cells (1 of 40; none computed" in t and t.rstrip().endswith("    policy_term()"), t)
    t = out("value fresh", T.get_value([P + "pv_net_cf()", P + "claims(t=0)", P + "disc_rate_ann", P + "point_id"]))
    check("get_value fresh: NOT COMPUTED, never 0",
          P + "pv_net_cf() = NOT COMPUTED" in t and P + "claims(t=0) = NOT COMPUTED" in t)
    check("get_value fresh: one footer naming the exact calculate call",
          t.count("NOT COMPUTED means") == 1 and
          'calculate(["%spv_net_cf()", "%sclaims(t=0)"]) computes them.' % (P, P) in t)
    check("get_value: a Series with its index name and whole-column extremes",
          "Series float64 [151] year {0: 0.0, 1: 0.00555" in t and
          "min 0.0 at year=0, max 0.03056000000000001 at year=150" in t)
    check("get_value: the base-Space note names point_id = 1",
          "its values are for the item that the Reference point_id = 1 selects" in t)
    t = out("trace fresh succs", T.trace(P + "claims(t=3)", direction="succs"))
    check("trace of an uncomputed node: UNKNOWN, not none, and no count",
          "= NOT COMPUTED, so its recorded dependents are UNKNOWN, not none." in t
          and "read by 0" not in t and "formula level (read from source" in t)
    t = out("trace fresh preds", T.trace(P + "claims(t=3)"))
    check("trace preds of an uncomputed node ends with the formula body",
          "recorded precedents are UNKNOWN" in t and t.rstrip().endswith("return claim_pp(t) * pols_death(t)"))

    # -- calculate -------------------------------------------------------------
    t = out("calc", T.calculate([P + "pv_net_cf()", P + "claims(t=0)"]))
    check("calculate: rev 1->2 and computed 0 -> 1832 from session.info",
          "BasicTerm_S rev 1->2: 2 nodes: 2 computed now (" in t and
          "Computed nodes in the model: 0 -> %d (+%d)." % (TRACED, TRACED) in t, t.splitlines()[1])
    check("calculate: [computed now] with the trace counts",
          P + "pv_net_cf() = %s  [computed now; reads 4; read by 0 computed]" % PV_NET_CF in t)
    check("calculate: claims(t=0)", P + "claims(t=0) = %s  [computed now" % CLAIMS_0 in t)
    check("state after calculate", state(S, M) == (TRACED, 2, 0), state(S, M))
    t = out("calc again", T.calculate([P + "pv_net_cf()", P + "pv_net_cf"]))
    check("calculate again: nothing computed, [cached], revision unchanged",
          "Nothing was computed." in t and "[cached; reads 4; read by 0 computed]" in t
          and state(S, M) == (TRACED, 2, 0), t.splitlines()[1])
    check("a zero-parameter Cells without parentheses reads as a call",
          t.count(P + "pv_net_cf() = " + PV_NET_CF) == 2)
    t = out("cells listing", T.get_value([P + "claims"]))
    check("get_value(Cells): 121 cached, t = 0..120 contiguous",
          t.startswith(P + "claims: 121 cached values, t = 0..120 contiguous; read from the cache"), t[:120])
    check("get_value(Cells): whole-column statistics with where",
          "whole column (all 121): sum 5814.680787725242, mean 48.05521312169622, min 0.0 at t=120, "
          "max 64.47847187567388 at t=108" in t)
    check("get_value(Cells): 20 rows and the next call",
          "rows 0-19 of 121:" in t and 'next rows: get_value(["%sclaims"], offset=20)' % P in t)
    t = out("label on scalars", T.get_value([P + "claims"], label=1))
    check("bad_request per ref: label= on a Cells of scalars",
          t.startswith(P + "claims -> bad_request: element picks one element out of each cached "
                           "value, and this Cells' values are scalars (dtype float64)"), t)
    t = out("range", T.calculate([P + "pols_if[0:121]"]))
    check("a slice is 121 nodes, with statistics over exactly them",
          P + "pols_if(t=range(0, 121)): 121 nodes (121 cached)" in t and
          "over these 121 nodes: sum 90.93342021765827, min 0.0 at t=120, max 1 at t=0" in t)
    t = out("item create", T.calculate([M + ".Projection[2].pols_lapse(t=30)"]))
    check("calculate creates an ItemSpace and says so",
          "# created ItemSpace BasicTerm_S.Projection[2] (ran the Space formula; it stays in the model)" in t
          and M + ".Projection[2].pols_lapse(t=30) = 0.004125084186073208  [computed now" in t, t)
    check("one ItemSpace now", state(S, M)[2] == 1, state(S, M))
    t = out("trace item", T.trace(M + ".Projection[2].pols_lapse(t=30)"))
    check("S6 b1: the three recorded precedents and their values",
          M + ".Projection[2].pols_if(t=30) = 0.8021015012045919  (reads 5)" in t and
          M + ".Projection[2].pols_death(t=30) = 2.6079469533144335e-05  (reads 2)" in t and
          M + ".Projection[2].lapse_rate(t=30) = 0.060000000000000005  (reads 1)" in t, t)
    check("inside an ItemSpace there is no base-Space note", "# BasicTerm_S.Projection takes" not in t)
    s0 = state(S, M)
    t = out("missing item", T.get_value([M + ".Projection[3].pv_net_cf()"]))
    check("a reader names a missing ItemSpace and creates nothing",
          t.startswith(M + ".Projection[3].pv_net_cf() -> NOT COMPUTED: ItemSpace BasicTerm_S.Projection[3] "
                           "does not exist yet") and state(S, M) == s0, t)
    t = out("missing item formula", T.get_formulas([M + ".Projection[3].pv_net_cf"]))
    check("get_formulas shows the base Space's formula for a missing ItemSpace",
          "# ItemSpace BasicTerm_S.Projection[3] does not exist yet; its formulas are its base Space's" in t
          and "def pv_net_cf():" in t)

    # -- every per-ref error path in one call; a formula error ------------------
    s0 = state(S, M)
    t = out("errors", T.calculate(["Projection.claimz(t=0)", "Projection.claims", "Projection.claims(t=0, x=1)",
                                   "Projection.claims(t=9999)", "PV_NET_CF()",
                                   "Projection.claims(t=range(0, 500))", "Projection.claims(t=",
                                   "Projection.claims(t=range(0, 2), t2=range(0, 2))", "Projection"]))
    s1 = state(S, M)
    check("not_found per ref: did you mean, and the get_tree call",
          "Projection.claimz(t=0) -> no 'claimz' in BasicTerm_S.Projection; did you mean claims, claim_pp, "
          "pv_claims? get_tree(model=\"BasicTerm_S\", filter=\"clai\") lists names" in t)
    check("a parameterised Cells without arguments names a call and a range",
          "Projection.claims -> claims takes (t): give arguments, e.g. claims(t=0), or a range: "
          "claims(t=range(0, 12))" in t)
    check("an unknown keyword", "Projection.claims(t=0, x=1) -> claims takes (t); there is no parameter 'x'" in t)
    check("formula_error: message, chain, raising line, and the next call",
          "Projection.claims(t=9999) -> formula error: KeyError: np.int64(880)" in t and
          "    call chain: BasicTerm_S.Projection.claims(t=9999) -> BasicTerm_S.Projection.pols_death(t=9999)" in t and
          "    raised at line 10 of BasicTerm_S.Projection.mort_rate(t=9999): return "
          "mort_table[str(max(min(5, duration(t)),0))][age(t)]" in t and
          '    get_formulas(["BasicTerm_S.Projection.mort_rate"]) shows that formula' in t, t)
    check("a case-insensitive match is resolved and disclosed",
          "# 'PV_NET_CF' resolved to BasicTerm_S.Projection.pv_net_cf (case-insensitive)" in t)
    check("a range over 200 nodes is refused with the split",
          "is 500 nodes; at most 200 per call. Split it: range(0, 200) first" in t)
    check("bad_request per ref: a malformed ref", "Projection.claims(t= -> 'Projection.claims(t=' is not a ref." in t)
    check("a Space has no value", "Projection -> a Space has no value to calculate" in t)
    check("a failed calculation keeps what it computed, from session.info",
          s1[0] - s0[0] == FAILED_LEFTOVER and
          "Computed nodes in the model: %d -> %d (+%d). A failed calculation keeps what it computed "
          "before it failed." % (s0[0], s1[0], FAILED_LEFTOVER) in t, (s0, s1))
    check("B1 DEFERRED, still open: a failed evaluation leaves the revision where it was",
          s1[1] == s0[1] and ("BasicTerm_S rev %d: " % s0[1]) in t, (s0, s1))
    t = out("loc errors", T.get_value([P + "disc_rate_ann.loc[999]", P + "claims(t=0).loc[3]",
                                       P + "disc_rate_ann[3]", P + "disc_rate_ann.loc[10]",
                                       P + "disc_rate_ann.iloc[3]", P + "disc_rate_ann.iloc[999]"]))
    check("not_found per ref: the bridge's own message (B7)",
          P + "disc_rate_ann.loc[999] -> not_found: no row labelled 999 in this Series" in t)
    check("an accessor on a scalar is refused, with the value",
          P + "claims(t=0).loc[3] -> refused: BasicTerm_S.Projection.claims(t=0) is 34.18079328868595, "
              "not a Series, DataFrame or ndarray" in t)
    check("[k] on a Series is refused as ambiguous",
          "[3] on a Series is ambiguous; write .loc[label] or .iloc[position]" in t)
    check(".loc[label] reads one element, with its label and position",
          P + "disc_rate_ann.loc[10] = 0.01188  (year 10, position 10)" in t)
    check(".iloc[i]", P + "disc_rate_ann.iloc[3] = 0.00788  (year 3, position 3)" in t)
    check(".iloc past the end", "position 999 is past the end (151 rows)" in t)
    t = out("rows", T.get_value([M + ".Projection.model_point_table.loc[3]",
                                 M + '.Projection.model_point_table["sum_assured"].loc[3]',
                                 M + '.Projection.model_point_table["age_at_entry"]']))
    check("a DataFrame row with no column is the whole row, never its first cell",
          M + ".Projection.model_point_table.loc[3] = row {'age_at_entry': 51, 'sex': 'F', 'policy_term': 10, "
              "'policy_count': 1, 'sum_assured': 799000}  (point_id 3, position 2)" in t, t)
    check("one cell by column and label",
          M + '.Projection.model_point_table["sum_assured"].loc[3] = 799000  (point_id 3, position 2)' in t, t)
    check("a whole column with its statistics",
          'column "age_at_entry" of 10000 rows' in t and "min 20 at point_id=55, max 59 at point_id=9" in t, t)
    t = out("row operand", T.calculate([M + ".Projection.model_point_table.loc[3] * 2"]))
    check("a row is refused as an operand, naming the fix",
          "is a DataFrame row, not a number; pick one column with [\"<column>\"]" in t, t)
    t, err = call(T.get_tree, model="Nope")
    check("not_found, whole call: an unknown model", err and t == "no open model 'Nope'; open: BasicTerm_S", t)
    t, err = call(T.get_map, cells="claimz")
    check("not_found, whole call: get_map(cells=) with did you mean",
          err and t == "no Cells 'claimz' in BasicTerm_S.Projection; did you mean claims, claim_pp, pv_claims", t)
    t, err = call(T.trace, M + ".Projection")
    check("trace on a Space is refused, naming a Cells form",
          err and t == "BasicTerm_S.Projection is a Space; trace follows a Cells node, e.g. "
                       "BasicTerm_S.Projection.<cells>(...)", t)
    t, err = call(T.trace, P + "claims")
    check("trace of a Cells without arguments names one node",
          err and "claims takes (t); trace one node, e.g. BasicTerm_S.Projection.claims(t=0)" in t, t)
    t, err = call(T.trace, P + "pv_net_cf()", direction="down")
    check("trace direction is checked", err and "\"preds\" (what it reads) or \"succs\"" in t, t)
    t, err = call(T.trace, P + "claims(t=range(0, 3))")
    check("trace refuses a range", err and "trace follows one node" in t, t)
    t, err = call(T.calculate, [])
    check("an empty refs list is a whole-call error naming an example", err and "refs is empty; pass e.g." in t, t)
    t, err = call(T.calculate, ["x"] * 51)
    check("over 50 refs is a whole-call error", err and t == "51 refs; at most 50 per call", t)
    t, err = call(T.get_map, depth=0)
    check("get_map depth=0 is refused, not answered 'none'", err and "depth is 1 or more" in t, t)

    # -- formulas, docstrings, the map ------------------------------------------
    t = out("formulas Space", T.get_formulas([M + ".Projection"]))
    check("the Projection docstring whole in one call (S6 b5's answer is in it)",
          "docstring (%d chars):" % PROJECTION_DOC in t and "basic_term_sample.xlsx" in t
          and "more chars" not in t)
    check("the Space block summarises the map",
          "formulas name each other in 82 links (read from source; get_map has them)" in t
          and "recursive: pols_if" in t)
    t = out("formulas", T.get_formulas([P + "claims", P + "disc_rate_ann"], docstrings=False))
    check("a Cells block: links read from source, and the source without its docstring",
          "## BasicTerm_S.Projection.claims(t) - Cells; 121 values cached" in t and
          "formula names: claim_pp, pols_death\nnamed by the formulas of: net_cf, pv_claims, result_cf\n"
          "def claims(t):\n    return claim_pp(t) * pols_death(t)" in t, t[:300])
    check("a Reference block: its value and the paragraph of its Space's docstring",
          "## BasicTerm_S.Projection.disc_rate_ann - Reference (Series)" in t and
          "documented in BasicTerm_S.Projection's docstring:\n  disc_rate_ann: Annual discount rates" in t)
    t = out("formulas 13", T.get_formulas([P + "claims"] * 13))
    check("get_formulas names the refs it dropped",
          "[1 refs not shown, to stay under 12000 chars and 12 refs: get_formulas([" in t)
    t = out("map", T.get_map())
    check("get_map: 40 Cells, 82 links, 8 reference reads, read from SOURCE",
          t.startswith("BasicTerm_S.Projection rev ") and
          ": 40 Cells, 82 links between Cells, 8 reads of References. Read from formula SOURCE" in t)
    t = out("map claims", T.get_map(cells="claims", depth=-1))
    deps = t.split("claims is named by (its formula-level dependents), all levels:")[1]
    levels = [int(x) for x in re.findall(r"level \d+ \((\d+)\)", deps)]
    check("get_map(cells='claims', depth=-1): dependents by level (3, 4, 1, 1, 2, 1)",
          levels == [3, 4, 1, 1, 2, 1], levels)

    # -- trace on computed nodes -------------------------------------------------
    t = out("trace", T.trace(P + "pv_net_cf()"))
    check("trace: the four recorded precedents with values",
          P + "pv_premiums() = 8252.085855522228  (reads 123)" in t and
          P + "pv_claims() = 5501.194898364312  (reads 123)" in t and
          "B. formula level (read from source): pv_net_cf names pv_claims, pv_commissions, pv_expenses, "
          "pv_premiums" in t and t.rstrip().endswith(
              "return pv_premiums() - pv_claims() - pv_expenses() - pv_commissions()"))
    t = out("trace depth 2", T.trace(P + "pv_net_cf()", depth=2))
    check("trace depth 2: a group of 121 collapsed, and (shown above)",
          P + "premiums(t=0..120)  x121 nodes; first (t=0) = 94.84, last (t=120) = 0.0" in t
          and P + "proj_len() (shown above)" in t)
    t = out("trace depth 5", T.trace(P + "pv_net_cf()", depth=5))
    check("a depth over 3 is read as 3 and says so", "\n# depth=5 read as 3: trace expands 1 to 3 levels\n" in t,
          t[:200])
    t = out("trace succs", T.trace(P + "claims(t=3)", direction="succs"))
    check("trace succs: recorded vs named, two labelled answers",
          "A. recorded dependents - the 1 computed node recorded READING this value so far" in t and
          P + "pv_claims() = 5501.194898364312  (read by " in t and
          "B. formula level (read from source): named by the formulas of net_cf, pv_claims, result_cf" in t and
          "named in a formula but not in A: net_cf, result_cf - nothing computed from them has read" in t, t)

    # -- derived values, S6 b7 ---------------------------------------------------
    s0 = state(S, M)
    refs = ["%s.Projection[%d].pv_net_cf() / %s.Projection[%d].pv_premiums()" % (M, k, M, k)
            for k in range(1, 11)]
    t = out("b7", T.calculate(refs))
    ratios = dict((int(k), float(v)) for k, v in re.findall(
        r"Projection\[(\d+)\]\.pv_net_cf\(\) / BasicTerm_S\.Projection\[\d+\]\.pv_premiums\(\) = (\S+)  \[derived",
        t))
    check("S6 b7: ten ratios, point 10 the largest at 0.17301358164048888",
          len(ratios) == 10 and ratios[10] == B7_BEST and max(ratios, key=ratios.get) == 10, ratios.get(10))
    check("ItemSpaces created for derived operands are announced",
          t.count("# created ItemSpace") == 9 and "# created ItemSpace BasicTerm_S.Projection[10]" in t,
          t.count("# created ItemSpace"))
    check("ItemSpaces: 10 now", state(S, M)[2] == 10 and s0[2] == 1, (s0, state(S, M)))
    t = out("derived errors", T.calculate(["Projection.pv_claims() / Projection.pv_premiums()",
                                           "Projection.model_point() * 2", "Projection.pv_claims() ** 2",
                                           "Projection.pv_net_cf() / 0", "Projection.claimz() + 1"]))
    check("a derived value prints its operands",
          P + "pv_claims() / " + P + "pv_premiums() = 0.6666429548455265  [derived here from the 2 values below" in t
          and "    " + P + "pv_claims() = 5501.194898364312 [" in t)
    check("a vector operand is refused", "Projection.model_point() * 2 -> refused: BasicTerm_S.Projection."
                                         "model_point() is Series object [5], not a number" in t, t)
    check("** is refused", "Projection.pv_claims() ** 2 -> a derived value uses + - * / only" in t)
    check("division by zero is per ref", "Projection.pv_net_cf() / 0 -> division by zero" in t)
    check("an unknown operand is per ref", "Projection.claimz() + 1 -> refused: Projection.claimz(): no 'claimz'" in t)

    t = out("item failure", T.calculate([M + ".Projection[99999].pv_net_cf()"]))
    check("a failure inside an ItemSpace is named by its display (B8), never by __SpaceN",
          "formula error: KeyError" in t and
          re.search(r"raised at line \d+ of BasicTerm_S\.Projection\[99999\]\.model_point\(\): ", t)
          and '    get_formulas(["BasicTerm_S.Projection[99999].model_point"]) shows that formula' in t, t)
    h0 = S.bridge.codec._counter
    t = out("trace handles", T.trace(P + "pv_premiums()"))
    minted = S.bridge.codec._counter - h0
    check("trace mints only the handles of the vectors it prints (B5 values: false)",
          P + "premiums(t=0..120)  x121 nodes" in t and P + "disc_factors() = ndarray" in t and minted == 1,
          "minted %d" % minted)

    # -- verify_citations (spec 8.7) ----------------------------------------------
    s0 = state(S, M)
    rows = verify_citations(T, [
        {"ref": P + "pv_net_cf()", "value": float(PV_NET_CF)},
        {"ref": "`" + P + "pv_net_cf()` = 910.92", "value": "910.92"},
        {"ref": P + "claims(t=0)", "value": CLAIMS_0},
        {"ref": P + "pv_net_cf()", "value": 911.0},
        {"ref": P + "claims(t=500)", "value": 1.0},
        {"ref": M + ".Projection[11].pv_net_cf()", "value": 1.0},
        {"ref": M + ".Projection", "value": 1.0}])
    verdicts = [r["verdict"] for r in rows]
    check("verify_citations: MATCH x3, MISMATCH, NOT COMPUTED x2, CANNOT CHECK",
          verdicts == ["MATCH"] * 3 + ["MISMATCH", "NOT COMPUTED", "NOT COMPUTED", "CANNOT CHECK"], verdicts)
    check("verify_citations never evaluates", state(S, M) == s0, (s0, state(S, M)))

    # -- the review of 2026-10-05: each check failed on the code it was found in --
    import modelx as mx
    model = mx.get_models()[M]
    I = M + ".Projection[%d]."

    s0 = state(S, M)
    t = out("refused, nothing created", safe(T.calculate, [
        I % 21 + "pv_netcf()", I % 22 + "claims(1, 2)", M + ".Projection[23]", I % 24 + "claims",
        I % 25 + "claims(t=range(0, 300))", I % 26 + "pv_net_cf() / " + I % 27 + "pv_premiumz()"]))
    check("6/20: a refused ref creates no ItemSpace and computes nothing",
          state(S, M) == s0 and "created ItemSpace" not in t, (s0, state(S, M)))
    check("6/20 and 19: the refused refs are named at the top", t.startswith("NO VALUE for 6 of the 6 refs; "), t[:200])
    t = out("one good, one refused", safe(T.calculate, [I % 28 + "pv_net_cf()", I % 29 + "claimz(t=0)"]))
    check("6/20: only the ref that passed every check creates its ItemSpace, and says so",
          state(S, M)[2] == s0[2] + 1 and "# created ItemSpace BasicTerm_S.Projection[28]" in t
          and "# created ItemSpace BasicTerm_S.Projection[29]" not in t, (s0, state(S, M), t[:300]))

    t = out("empty ranges", safe(T.calculate, [P + "pv_net_cf()", P + "claims(t=range(5, 5))", P + "claims[120:0]"]))
    check("5: an empty range or slice is refused per ref, and the other refs are still reported",
          P + "pv_net_cf() = " + PV_NET_CF in t and
          "'%sclaims(t=range(5, 5))' is empty: range(5, 5) holds no values" % P in t and
          "range(120, 0) holds no values; a descending range needs a negative step, e.g. range(120, 0, -1)" in t, t)
    t = out("empty slice read", safe(T.get_value, [P + "claims[3:3]"]))
    check("5: get_value refuses an empty slice rather than print '0 nodes ()'",
          "is empty: range(3, 3) holds no values" in t and "0 nodes" not in t, t)

    t0 = time.time()
    t = out("huge range", safe(T.calculate, [P + "pv_net_cf()", P + "claims(t=range(0, 100000000))"]))
    check("12: a 10**8-node range is refused without building it (it took 19 s and 3.9 GB)",
          time.time() - t0 < 2 and "is 100000000 nodes; at most 200 per call" in t and PV_NET_CF in t,
          "%.2f s" % (time.time() - t0))
    t = out("stepped refusal", safe(T.calculate, [P + "claims(t=range(0, 1000, 2))"]))
    check("12: the split a refusal names keeps the step", "Split it: range(0, 400, 2) first" in t, t)

    # In an ItemSpace: the bounds checks below count the base Space's 121 claims.
    for ref, step in ((I % 15 + "claims(t=range(0, 400, 2))", ", 2)"), (I % 15 + "claims[199:0:-1]", ", -1)")):
        t = out("range rest", safe(T.calculate, [ref]))
        m = re.search(r"\[(\d+) more rows: get_value\(\[\"(.*?)\"\]\)\]", t)
        rest = out("range rest followed", safe(T.get_value, [m.group(2)])) if m else ""
        check("11: a cut range names its rest with the step, and the rest is exactly the rows cut (%s)" % ref[-18:],
              m is not None and m.group(2).endswith(step + ")") and
              (": %s nodes (%s cached)" % (m.group(1), m.group(1))) in rest, (m and m.group(0), rest[:200]))

    t = out("chain", safe(T.calculate, [P + "claims(t=-1)"]))
    # The chain is as long as modelx's call-stack limit, which depends on the
    # interpreter: modelx 0.33.0's CallStack.default_maxdepth is 65,000 before
    # CPython 3.12 (50,000 on Windows) and 100,000 from 3.12. MEASURED 2026-10-06
    # from the built wheels: 3.11 printed "... 64990 more ... -> pols_if(t=-64995)",
    # 3.12 and 3.13 "... 99990 more ... -> pols_if(t=-99995)"; a pinned 65,000
    # failed on both.
    depth = mx.get_recursion()
    check("10: a chain modelx elided counts the frames it hid (%s frames, 11 shown)" % format(depth + 1, ","),
          "pols_if(t=-4) -> ... %d more ... -> BasicTerm_S.Projection.pols_if(t=-%d)" % (depth - 10, depth - 5)
          in t, t[:600])

    t = out("overlap", safe(T.calculate, [I % 14 + "claims(t=range(0, 10))", I % 14 + "claims(t=range(5, 15))",
                                          I % 14 + "claims(t=7)", I % 14 + "claims(7)"]))
    check("25: overlapping refs count each node once",
          "15 nodes (7 more named again in the refs, counted once): 15 computed now" in t
          and len(model.Projection[14].claims) == 15, t[:300])

    t = out("read by", safe(T.calculate, [I % 12 + "claims(t=3)", I % 12 + "pv_claims()"]))
    check("21: 'read by' is counted at the end of the call (pv_claims read claims(t=3) after it)",
          I % 12 + "claims(t=3) = 19.879575366427847  [computed now; reads 2; read by 1 computed]" in t, t)

    out("one item claim", safe(T.calculate, [I % 13 + "claims(t=3)"]))
    n13, nbase = len(model.Projection[13].claims), len(model.Projection.claims)
    t = out("item formulas", safe(T.get_formulas, [I % 13 + "claims", M + ".Projection[13]", I % 42 + "claims"],
                                  docstrings=False))
    check("8/18: get_formulas counts an ItemSpace's own cache, not its base Space's (%d vs %d)" % (n13, nbase),
          n13 == 1 and nbase > 1 and "## BasicTerm_S.Projection[13].claims(t) - Cells; 1 value cached" in t, t[:200])
    check("8: an ItemSpace's header gives no count of its base Space's ItemSpaces",
          "## BasicTerm_S.Projection[13] - ItemSpace; 40 Cells, 6 References\n" in t, t)
    check("8/18: a missing ItemSpace has nothing cached, and says why",
          "## BasicTerm_S.Projection[42].claims(t) - Cells; nothing cached (the ItemSpace does not exist)" in t, t)
    t = out("item tree", safe(T.get_tree, path="Projection[13]", filter="claims"))
    check("18: get_tree(path=ItemSpace) prints the ItemSpace's own counts, by its ref",
          "\nSpace Projection[13]\n" in t and "claims(t):1, pv_claims()" in t, t)
    t = out("missing item tree", safe(T.get_tree, path="Projection[42]"))
    check("18: get_tree(path=a missing ItemSpace) says it does not exist and prints no counts",
          t.startswith("ItemSpace BasicTerm_S.Projection[42] does not exist yet") and ":121" not in t, t)

    t = out("map item", safe(T.get_map, space="Projection[2]"))
    check("9: get_map on an ItemSpace is headed by its ref", t.startswith("BasicTerm_S.Projection[2] rev "), t[:80])
    t = out("map item error", safe(T.get_map, space="Projection[2]", cells="nonexistent_x"))
    check("9: and its refusal names it the same way", "no Cells 'nonexistent_x' in BasicTerm_S.Projection[2]" in t, t)

    for tool, a in ((T.get_value, ([M + ".Projection[2.0].pols_lapse(t=30)"],)),
                    (T.trace, (M + ".Projection[2.0].pols_lapse(t=30)",))):
        t = out("float key", safe(tool, *a))
        check("7: an ItemSpace key written as a float gets no base-Space note (%s)" % tool.__name__,
              "selects" not in t and "0.004125084186073208" in t, t[:300])

    t = out("row then column", safe(T.get_value, [
        M + '.Projection.model_point_table.loc[3]["sum_assured"]', M + '.Projection.model_point_table.loc[3]["nope"]',
        P + 'disc_rate_ann.loc[10]["x"]']))
    check("15: df.loc[k][\"col\"] picks the column too",
          M + '.Projection.model_point_table["sum_assured"].loc[3] = 799000  (point_id 3, position 2)' in t, t)
    check("15: a column the row lacks is refused, never ignored", "refused: no column 'nope'" in t, t)
    check("15: nothing follows one element silently",
          "refused: .loc[10] selects one element, so [\"x\"] cannot follow it" in t, t)
    t = out("row then column derived", safe(T.calculate, [M + '.Projection.model_point_table.loc[3]["sum_assured"] / 1000']))
    check("15: and calculate derives from it", "= 799.0  [derived here" in t, t)

    t = out("reference trace", safe(T.trace, P + "point_id"))
    check("23: trace on a Reference says modelx does not trace one, and names no <cells> under it",
          "is a Reference, and modelx does not trace References" in t and "<cells>" not in t, t)
    t = out("missing item cells", safe(T.get_value, [I % 42 + "claims"]))
    check("23: the NOT COMPUTED footer never offers a calculate call calculate refuses",
          'calculate(["%sclaims"])' % (I % 42) not in t and
          "e.g. BasicTerm_S.Projection[42].claims(t=0)" in t, t)

    t = out("signature forms", safe(T.get_value, [M + ".Projection[point_id]", P + "claims(t)", P + "claims(x)"]))
    check("28: get_tree's printed Space and Cells signatures resolve back",
          M + ".Projection[point_id] -> a Space has no value" in t and P + "claims: " in t, t[:300])
    check("28: a bare name that is not the signature is still refused",
          "cannot read 'x' in 'BasicTerm_S.Projection.claims(x)' as an argument" in t, t)
    t, err = call(T.get_tree, path="Projection[point_id]", filter="claims")
    check("28: get_tree(path=) takes the Space as get_tree prints it", not err and "Space Projection[point_id]" in t, t)

    small2 = Tools(S.dispatch, max_chars=2000)
    t = out("small refused", safe(small2.calculate, [P + "claims(t=range(0, 121))", P + "premiums(t=range(0, 79))",
                                                     P + "claims(t=9999)", P + "premiums(t=range(79, 121))"]))
    head = t.split("\n" + P + "claims(t=range(0, 121))")[0]
    check("19: refused refs are named above the cut, and the cut does not say everything was computed",
          'NO VALUE for 2 of the 4 refs' in head and P + "claims(t=9999)" in head and
          "Everything was computed" not in t and "NOT everything was computed" in t.splitlines()[-1], t[-300:])

    # 19, residual: a ref whose node computed but whose ACCESSOR gave no value
    # was not in NO VALUE. At the default bound, with 30 refs before it, its
    # ' -> ' line was cut and the cut line said "Everything was computed".
    many = ([P + "%s(t=range(%d, %d))" % (c, 6 * k, 6 * k + 6) for c in ("claims", "premiums") for k in range(14)]
            + [P + "mort_table", P + "model_point_table"])
    for extra in (P + "model_point().loc['no_such_field']", P + "model_point()[99]"):
        t = out("accessor cut", safe(T.calculate, many + [extra]))
        check("19: at the default bound, 30 refs and %s: NO VALUE names it, and the cut says not everything "
              "was computed" % extra[len(P):],
              len(many) == 30 and "[output bound 12000 chars reached" in t and extra + " -> " not in t
              and "NO VALUE for 1 of the 31 refs; each one's reason is its ' -> ' line below: %s"
              % json.dumps([extra]) in t and "Everything was computed" not in t,
              t[:400] + " ... " + t[-300:])
    for ref in (P + "claims(t=range(0, 3))[1]", P + "claims(t=range(0, 3)).loc[1]", P + "claims[0:3].iloc[1]"):
        t = out("range accessor", safe(T.calculate, [P + "pv_net_cf()", ref]))
        check("19: an accessor on a range is refused, never dropped, and NO VALUE names it (%s)" % ref[len(P):],
              "NO VALUE for 1 of the 2 refs; each one's reason is its ' -> ' line below: %s" % json.dumps([ref]) in t
              and ref + " -> " in t and "picks from one value, and this ref names 3 nodes; nothing was read" in t
              and "3 nodes (" not in t, t)
        t = out("range accessor read", safe(T.get_value, [ref]))
        check("19: and get_value refuses it the same way (%s)" % ref[len(P):],
              t.startswith(ref + " -> ") and "picks from one value" in t and "3 nodes (" not in t, t)

    # -- the guard on a computed model with ItemSpaces -----------------------------
    guard(S, "computed, with ItemSpaces")

    # -- bounds: every cut names the call that returns the rest ---------------------
    small = Tools(S.dispatch, max_chars=2000)
    # The 40 Cells fit at the --max-chars minimum, so the Cells-list cut is
    # driven through Tools directly at 1,000.
    tiny = Tools(S.dispatch, max_chars=1000)
    first = out("small tree", tiny.get_tree())
    pages = follow(tiny, "small tree", first, r"get_tree\(model=\"BasicTerm_S\", offset=(\d+)\)\]",
                   lambda k: tiny.get_tree(offset=k))
    names = set()
    for p in pages:
        for line in p.splitlines():
            if line.startswith("    ") and "(" in line and ":" not in line.split("(")[0]:
                names.update(x.strip().split("(")[0] for x in line.split(",") if x.strip())
    check("get_tree at 1,000 chars: the Cells list continues by offset until all 40 are shown",
          len(pages) > 1 and len(names) == 40, (len(pages), len(names)))
    first = out("small map", small.get_map())
    pages = follow(small, "small map", first, r"get_map\(model=\"BasicTerm_S\", offset=(\d+)\)\]",
                   lambda k: small.get_map(offset=k))
    lines = [x for p in pages for x in p.splitlines() if " <- " in x and not x.startswith("each line")]
    check("get_map at 2,000 chars: lines continue by offset until all 40 Cells",
          len(pages) > 1 and len(lines) == 40, (len(pages), len(lines)))
    first = out("small doc", small.get_formulas([M + ".Projection"]))
    pages = follow(small, "small doc", first, r"doc_offset=(\d+)\)\]",
                   lambda k: small.get_formulas([M + ".Projection"], doc_offset=k))
    spans = [tuple(map(int, m)) for p in pages[1:] for m in re.findall(r"docstring, chars (\d+)-(\d+) of", p)]
    check("a docstring at 2,000 chars pages by doc_offset to its last character",
          len(pages) > 2 and spans[-1][1] == PROJECTION_DOC and
          all(a[1] == b[0] for a, b in zip(spans, spans[1:])), spans)
    t = out("small range", small.calculate([P + "pols_if[0:121]"]))
    m = re.search(r"\[(\d+) more rows: get_value\(\[\"BasicTerm_S\.Projection\.pols_if\(t=range\((\d+), 121\)\)\"\]\)\]", t)
    check("a range table stops at half the bound and names the rest",
          m is not None and int(m.group(1)) + int(m.group(2)) == 121 and "over these 121 nodes" in t, t[-200:])
    first = out("small listing", small.get_value([P + "claims"], rows=200))
    pages = follow(small, "small listing", first, r"offset=(\d+)\)$",
                   lambda k: small.get_value([P + "claims"], offset=k, rows=200))
    rows_seen = sum(len(re.findall(r"^\s+\d+\s+\S+$", p, re.M)) for p in pages)
    check("a Cells listing fits its rows to the bound and continues by offset",
          len(pages) > 1 and rows_seen == 121 and all("output bound" not in p for p in pages),
          (len(pages), rows_seen))
    t = out("paging two refs", T.get_value([P + "disc_rate_ann", P + "disc_factors()"], offset=5))
    check("offset= with several refs says it paged nothing", t.count("page a vector only when it is "
                                                                     "the call's one ref") == 2)
    t = out("label not applied", T.get_value([P + "pv_net_cf()"], label=3))
    check("label= on a node says it was not applied", "it was not applied to " + P + "pv_net_cf()" in t)

    # -- the round trip: what a tool prints, a tool accepts ---------------------------
    import modelx as mx
    from modelx_bridge.methods import _display, _objid
    from modelx_mcp.tools import enc
    model = mx.get_models()[M]
    T.begin()
    ok, bad, cells = 0, [], 0
    for nd in list(model._impl.tracegraph.nodes):
        try:
            iface = nd[0].interface
        except Exception:
            continue
        if type(iface).__name__ != "Cells":
            continue
        cells += 1
        args = tuple(nd[1]) if len(nd) > 1 else ()
        text = _display(iface, args)
        try:
            n = T.resolve(text)
        except Exception as e:
            bad.append((text, str(e)))
            continue
        if n.obj == _objid(iface) and [enc(a) for a in (n.args or [])] == [enc(a) for a in args]:
            ok += 1
        else:
            bad.append((text, n.obj, n.args))
    check("round trip: every computed Cells node's display resolves to its obj and args (%d nodes)" % cells,
          cells > 5000 and ok == cells and not bad, bad[:2])
    check("the round trip covered nodes inside ItemSpaces",
          any("[" in _display(nd[0].interface, ()) for nd in model._impl.tracegraph.nodes
              if type(getattr(nd[0], "interface", None)).__name__ == "Cells"))

    # -- every output ------------------------------------------------------------
    for label, text in OUTPUTS:
        clean(label, text, 2000 if label.startswith("small") else 12000)
    return finish()


if __name__ == "__main__":
    run_module(main)
