"""The tools on lifelib's own models: vectorised (_ME) and with ItemSpaces (_SE).

    MODELX_MCP_MODELS=/path/to/models python -m modelx_mcp.tests.test_lifelib

SKIPPED unless MODELX_MCP_MODELS names a folder holding three of lifelib
0.17.1's own models, each as lifelib ships it: BasicTerm_ME and BasicTerm_SE
from its basiclife library and CashValue_ME from savings. lifelib.create(
"basiclife", path) and lifelib.create("savings", path) write those libraries
out; lifelib Studio's evals/prepare_models.py stages all three in one folder.
Neither package depends on lifelib. lifelib Studio's CI does not install it,
so there this suite reports itself skipped, by design; modelx-bridge's CI has
a job that installs lifelib and runs it.

Each model runs in a process of its own, opened the way S6 opens it
(--storage-root and --open). Values as measured for spec 13 and the S6 task
truths (evals/s6/dev).
"""
import math
import os
import re
import subprocess
import sys

from modelx_mcp.tests.checks import PYTHON_ROOT, call, check, clean, finish, safe, skip, state

ROOT = os.environ.get("MODELX_MCP_MODELS")

#: How far a computed value may print from its pin. The last digits depend on
#: the CPU, not on the code. MEASURED 2026-10-06: numpy's exp, log and power
#: give different last bits under its AVX-512 and AVX2 dispatch (sum and nansum
#: do not). With every AVX-512 dispatch target turned off through
#: NPY_DISABLE_CPU_FEATURES, the path a CPU without AVX-512 takes, numpy 2.0.2,
#: 2.3.0, 2.4.6 and 2.5.3 all print the S6 s8 total as 215146132.0684811, not
#: 215146132.06825686 (1.0e-12 relative); numpy 2.0.2 with AVX-512 prints
#: ...06825688. While these pins were repr substrings, 7 checks here failed
#: there, and 1 on numpy 2.0.2 with AVX-512. The largest move among them was
#: 2.9e-12 relative (S6 s6's max, 23.19979903678737 -> 23.199799036719952), so
#: 1e-9 leaves about 300 times that; a pin moved by 1e-8 fails every check that
#: uses near().
REL_TOL = 1e-9

#: A number as the tools print one: repr of a float or an int.
NUMBER = r"(-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)"


def near(text, template, *values):
    """Whether `text` holds `template` with each "{}" in it a printed number
    within REL_TOL of the matching value. Everything else in the template, the
    labels and positions, must match exactly."""
    pattern = NUMBER.join(re.escape(part) for part in template.split("{}"))
    for m in re.finditer(pattern, text):
        if all(math.isclose(float(g), v, rel_tol=REL_TOL) for g, v in zip(m.groups(), values)):
            return True
    return False


def reader_calls(T, m):
    """The 19 reader calls of the evaluation guard for a vectorised model."""
    P = m + ".Projection."
    claims0 = P + ("claims(t=0, kind=None)" if m == "CashValue_ME" else "claims(t=0)")
    return [
        (T.get_tree, (), {}), (T.get_tree, (), {"filter": "term|lapse"}),
        (T.get_formulas, ([m + ".Projection", P + "pv_net_cf", P + "disc_rate_ann"],), {}),
        (T.get_formulas, ([m + ".Projection"],), {"doc_offset": 2000}),
        (T.get_map, (), {}), (T.get_map, (), {"cells": "pv_net_cf", "depth": -1}),
        (T.get_value, ([P + "pv_net_cf()", claims0],), {}),
        (T.get_value, ([P + "premiums(t=range(0, 12))"],), {}),
        (T.get_value, ([P + "premiums"],), {}),
        (T.get_value, ([P + "premiums"],), {"label": 1}),
        (T.get_value, ([P + "pv_net_cf()"],), {"offset": 2, "rows": 3}),
        (T.get_value, ([P + "pv_net_cf()[0]", P + "pv_net_cf().loc[1]"],), {}),
        (T.trace, (P + "pv_net_cf()",), {}), (T.trace, (P + "pv_net_cf()",), {"depth": 2}),
        (T.trace, (P + "premiums(t=1)",), {"direction": "succs"}),
        (T.trace, (P + "pv_claims()",), {"direction": "succs"}),
        (T.trace, (P + "pv_premiums()",), {}),
        (T.trace, (P + "model_point()",), {"direction": "succs"}),
        (T.get_value, ([P + "model_point()"],), {}),
    ]


def guard(S, m, label):
    T = S.tools
    moved = []
    for fn, a, k in reader_calls(T, m):
        s0 = state(S, m)
        text, _ = call(fn, *a, **k)
        clean("%s guard %s" % (label, fn.__name__), text)
        if state(S, m) != s0:
            moved.append("%s%r" % (fn.__name__, a))
    check("guard (%s): 19 reader calls leave (computed, revision, ItemSpaces) unchanged" % label,
          not moved, moved[:2])


def round_trip(S, m):
    import modelx as mx
    from modelx_bridge.methods import _display, _objid
    from modelx_mcp.tools import enc
    T = S.tools
    T.begin()
    ok = cells = 0
    bad = []
    for nd in list(mx.get_models()[m]._impl.tracegraph.nodes):
        iface = getattr(nd[0], "interface", None)
        if type(iface).__name__ != "Cells":
            continue
        cells += 1
        args = tuple(nd[1]) if len(nd) > 1 else ()
        try:
            n = T.resolve(_display(iface, args))
            same = n.obj == _objid(iface) and [enc(a) for a in (n.args or [])] == [enc(a) for a in args]
        except Exception as e:
            same, n = False, e
        ok += same
        if not same:
            bad.append(_display(iface, args))
    return ok, cells, bad


def session(m):
    from modelx_mcp.session import Session
    S = Session(storage_root=ROOT, paths=["/" + m])
    check("%s opened from the storage root" % m, S.opened == [m], S.failed)
    return S


def case_me():
    m, P = "BasicTerm_ME", "BasicTerm_ME.Projection."
    check("near(): the s8 total as numpy prints it without AVX-512 matches its pin; 1e-8 off does not",
          near("sum 215146132.0684811, mean", "sum {}, mean", 215146132.06825686)
          and not near("sum 215146134.22, mean", "sum {}, mean", 215146132.06825686)
          and not near("sum 215146132.06825686, max", "sum {}, mean", 215146132.06825686))
    S = session(m)
    T = S.tools
    guard(S, m, "fresh")
    t = T.calculate([P + "pv_net_cf()"])
    check("S6 s8: the total, and where the extremes are, aligned with model_point()",
          near(t, "whole column (all 10000): sum {}, ", 215146132.06825686) and
          near(t, "min {} at [5279] (policy_id 5280), max {} at [1369] (policy_id 1370)",
               -1121080.0788651237, 1836852.4938259614)
          and "positions are BasicTerm_ME.Projection.model_point() rows (same length 10000; an ndarray "
              "carries no labels)" in t, t)
    t = T.get_value([P + "pv_net_cf().loc[7342]", P + "pv_net_cf()[7342]"])
    check("S6 s8: policy 7342 by label is position 7341; [7342] is another policy",
          near(t, P + "pv_net_cf()[7341] = {}  (policy_id 7342: position 7341 of "
                      "BasicTerm_ME.Projection.model_point(), the same length; an ndarray carries no labels)",
               -52686.43614498088) and
          near(t, P + "pv_net_cf()[7342] = {}  ", 82614.44604522981), t)
    t = T.get_value([P + "claims"], label=7342, rows=3)
    check("label=7342: one model point across t, with its peak", near(t, "max {} at t=48", 13361.252415664678), t)
    t = T.get_value([P + "premiums(t=0)", P + "premiums(t=0).loc[7342]"])
    check("a Series[10000] and one element by label (B7)",
          P + "premiums(t=0).loc[7342] = 11859.2  (policy_id 7342, position 7341)" in t, t)
    S.bridge.codec.max_handles = 6
    try:
        t = T.calculate([P + "pv_net_cf()", P + "claims[0:10]", P + "pv_premiums()", P + "pv_net_cf().loc[7342]"])
    finally:
        S.bridge.codec.max_handles = 64
    check("with an LRU of 6, evicted handles (the value's and model_point()'s) are re-read",
          "not_found" not in t and near(t, "max {} at [1369] (policy_id 1370)", 1836852.4938259614) and
          near(t, P + "pv_net_cf()[7341] = {}  (policy_id 7342", -52686.43614498088), t[-300:])
    # -- the review of 2026-10-05: each check failed on the code it was found in --
    t = safe(T.calculate, [P + "model_point()"])
    t = safe(T.get_value, [P + "pv_net_cf()"], offset=10000)
    check("13: paging an aligned 10,000-row ndarray at its end says so (was KeyError 10000)",
          "no rows at offset 10000: there are 10000" in t, t[:300])
    t = safe(T.get_value, [P + 'model_point().loc[7342]["sum_assured"]', P + 'model_point().loc[7342]["nope"]'])
    check("15: model_point().loc[7342][\"sum_assured\"] is the cell, not the whole row",
          P + 'model_point()["sum_assured"].loc[7342] = 688000  (policy_id 7342, position 7341)' in t
          and "refused: no column 'nope'" in t, t)
    s0 = state(S, m)
    t = safe(T.calculate, ["Projection[2].pv_net_cf()", "Projection[1, 2].pv_net_cf()"])
    check("22: Projection[k] where Projection takes no parameters is refused, never internal",
          t.count("BasicTerm_ME.Projection takes no parameters, so it has no ItemSpaces") == 2
          and "internal" not in t and state(S, m) == s0, t)
    guard(S, m, "computed")
    ok, cells, bad = round_trip(S, m)
    check("round trip: every computed Cells node (%d)" % cells, cells == 5280 and ok == cells, bad[:2])


def case_cv():
    m, P = "CashValue_ME", "CashValue_ME.Projection."
    S = session(m)
    T = S.tools
    guard(S, m, "fresh")
    s0 = state(S, m)
    t = safe(T.calculate, ["Projection.av_pp_at", "Projection.claims", "Projection.pv_claims"])
    check("23: an argument hint invents no value (claims(t=0, kind=0) raised 'invalid kind')",
          "e.g. av_pp_at(t=0, timing=<timing>), or a range: av_pp_at(t=range(0, 12), timing=<timing>)" in t
          and "e.g. claims(t=0, kind=<kind>), or a range: claims(t=range(0, 12), kind=<kind>)" in t
          and "e.g. pv_claims(kind=<kind>); get_formulas" in t and "kind=0" not in t and "timing=0" not in t
          and "kind=range" not in t and state(S, m) == s0, t)
    t = safe(T.get_value, ["Projection[2].pv_net_cf()"])
    check("22: Projection[2] on CashValue_ME is refused, never internal",
          "CashValue_ME.Projection takes no parameters, so it has no ItemSpaces" in t, t)
    t = T.calculate([P + "av_pp_at(t=120, timing='BEF_PREM')"])
    check("S6 s3: av_pp_at for poind_id 3", near(t, ", 3: {}, 4: ", 114090.32894719542), t)
    t = T.calculate([P + "policy_term()", P + "mort_table_last_age()", P + "is_wl()"])
    check("S6 s5: policy_term() and mort_table_last_age()",
          "poind_id {1: 10, 2: 20, 3: 95, 4: 65}" in t and P + "mort_table_last_age() = 115" in t, t)
    t = T.calculate([P + "margin_mortality[0:121]"])
    check("S6 s6: the peak for poind_id 1 is t=87",
          near(t, "poind_id 1: over these 121 nodes: sum {}, min {} at t=28, max {} at t=87\n",
               1153.9307793949074, -2.3391804668884646, 23.19979903678737), t[:600])
    cut = re.search(r'\[(\d+) more rows: get_value\(\["CashValue_ME\.Projection\.margin_mortality'
                    r'\(t=range\((\d+), 121\)\)"\]\)\]', t)
    check("the range table stops at half the bound and names the rest",
          cut is not None and int(cut.group(1)) + int(cut.group(2)) == 121 and len(t) <= 7000,
          "%s, %d chars" % (cut.groups() if cut else None, len(t)))
    t = T.calculate([P + "net_cf(t=87)"])
    check("S6 s6: net_cf(t=87) for poind_id 1", near(t, "{1: {}, 2: ", 22902.90933755436), t)
    t = T.calculate([P + "result_pols()"])
    check("S6 s7: per-column statistics of a DataFrame",
          near(t, "pols_lapse, whole column (all 1141): sum {}, ", 223.62669902394197), t)
    t = T.get_value([P + 'result_pols()["pols_lapse"]'])
    check("a DataFrame column by name", near(t, "\n    whole column (all 1141): sum {}, ", 223.62669902394197), t)
    t0 = T.calculate([P + "pv_net_cf()"])
    check("the 8-second projection, positions aligned with model_point()",
          "positions 0..3 are the rows of CashValue_ME.Projection.model_point(), poind_id 1, 2, 3, 4" in t0, t0)
    t = T.get_value([P + "pv_claims()", P + "pv_claims(kind=None)"])
    lines = [x for x in t.splitlines() if " = ndarray" in x]
    check("B2 DEFERRED: pv_claims() and pv_claims(kind=None) print differently, read the same",
          len(lines) == 2 and lines[0].startswith(P + "pv_claims() = ") and
          lines[1].startswith(P + "pv_claims(kind=None) = ") and
          lines[0].split(" = ", 1)[1] == lines[1].split(" = ", 1)[1], lines)
    h0 = S.bridge.codec._counter
    t = T.trace(P + "pv_premiums()")
    minted = S.bridge.codec._counter - h0
    check("trace of 1,143 precedents: 3 lines, and only the printed vectors' handles (B5)",
          "the 1143 nodes modelx recorded" in t and P + "premiums(t=0..1140)  x1141 nodes" in t
          and minted <= 8, "minted %d" % minted)
    t = T.trace(P + "model_point()", direction="succs")
    check("trace of 4,575 dependents, grouped", "the 4575 computed nodes" in t and len(t) < 12000, len(t))
    t = safe(T.calculate, ["Projection.av_pp_at"])
    ex = re.search(r"e\.g\. (av_pp_at\([^)]*\))", t)
    got = safe(T.get_value, [P + ex.group(1)]) if ex else ""
    check("23: once av_pp_at is cached, its hint is a cached key", ex is not None and "timing='" in ex.group(1)
          and " = " in got and "NOT COMPUTED" not in got and "->" not in got, (t, got))
    guard(S, m, "computed")
    ok, cells, bad = round_trip(S, m)
    check("round trip: every computed Cells node (%d)" % cells, cells > 46000 and ok == cells, bad[:2])


def case_se():
    m, P = "BasicTerm_SE", "BasicTerm_SE.Projection."
    S = session(m)
    T = S.tools
    t = T.calculate(["BasicTerm_SE.Projection[11].premium_pp()"])
    check("S6 b4: Projection[11].premium_pp()", "BasicTerm_SE.Projection[11].premium_pp() = 176.27" in t, t)
    t = T.get_value([P + "premium_table.loc[(53, 10)]"])
    check("S6 b4: a MultiIndex label",
          P + "premium_table.loc[(53, 10)] = 0.0002286208629422971  (age_at_entry, policy_term (53, 10), "
              "position 99)" in t, t)
    t = safe(T.get_value, [P + "premium_table", P + "premium_table.iloc[99]"])
    check("14: a MultiIndex label prints as the tuple .loc[] takes",
          "{(20, 10): 4.640975421603339e-05, " in t and "(age_at_entry, policy_term (53, 10), position 99)" in t
          and "builtins" not in t, t[:400])
    t = safe(T.get_value, [P + "premium_table"], offset=95, rows=5)
    check("14: and in a page", "(51, 20)  0.0003119028987913396" in t and "builtins" not in t, t[:500])
    t = T.get_map(cells="duration_mth")
    check("S6 b2: the six formulas that name duration_mth",
          "  level 1 (6): duration, is_active, pols_if_init, pols_maturity, pols_new_biz, proj_len" in t, t)
    t = T.trace("BasicTerm_SE.Projection[11].premium_pp()")
    check("S6 b4: trace names premium_table as a Reference it reads",
          re.search(r"References it names \(modelx does not trace References\): .*premium_table", t), t)


CASES = {"me": case_me, "cv": case_cv, "se": case_se}


def main(argv):
    if not ROOT or not os.path.isdir(ROOT):
        skip("MODELX_MCP_MODELS is not set to a folder holding lifelib 0.17.1's "
             "BasicTerm_ME, BasicTerm_SE (basiclife library) and CashValue_ME (savings "
             "library), copied from lifelib's own libraries (lifelib.create) or staged "
             "by lifelib Studio's evals/prepare_models.py; lifelib is not a dependency "
             "of either package")
        return 0
    if argv:
        CASES[argv[0]]()
        return finish()
    code = 0
    for name in CASES:
        env = dict(os.environ)
        env["PYTHONPATH"] = PYTHON_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-m", "modelx_mcp.tests.test_lifelib", name],
                              cwd=PYTHON_ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        lines = proc.stdout.splitlines()
        for line in lines:
            if line.startswith(("ok  ", "FAIL")):
                print(line)
        if proc.returncode:
            code = 1
            if not any(x.startswith("FAIL") for x in lines):
                print("FAIL [%s] the case did not finish" % name)
                print((proc.stdout + proc.stderr)[-1500:])
    print("\n" + ("some case failed" if code else "all passed"))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
