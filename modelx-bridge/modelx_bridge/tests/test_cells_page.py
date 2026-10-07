"""cells.page and table.stats: reading a column without computing one.

    python -m modelx_bridge.tests.test_cells_page

THE ASSERTION THIS FILE EXISTS FOR is `len(model._impl.tracegraph)` identical
before and after every call — on a populated cache, a partial cache, an EMPTY
cache, a zero-parameter Cells, a multi-parameter Cells and an `is_cached=False`
Cells. That assertion IS the evaluation policy; the docstring in `cells_page` is
only its comment. It must never be deleted. The two traps it is aimed at do not
look like evaluators: `claims[130]` adds 8 tracegraph nodes and grows
`len(cells)` 121 → 122, and `to_frame()` / `to_frame(*args)` are one name with
opposite semantics.

The second load-bearing pair is about honesty of numbers rather than of reads.
On BasicTerm_S `claims` the whole column is sum=5814.680788 min=0.0
max=64.478472 while its first 100 rows give sum=4562.909698 min=31.015247
max=61.562898 — a Sum 22% low and BOTH extremes wrong. Every statistic here is
checked against the whole column, and `table.stats` is checked against pandas'
own reduction over all 10,000 rows of `model_point_table`.

The third is a handle count. An object-dtype value column paged raw mints ONE
HANDLE PER CELL (measured: 10 rows → 10 handles), so a 200-row page would evict
the entire 64-entry LRU and break the Explorer's grid as collateral. BasicTerm_S
cannot reach that path — all three of its array-valued Cells take no arguments —
so the fixture is hand-built and this check is the only thing between the
product and that bug.
"""

import json
import sys

from .shipped import sample_dir

FAILURES = []
NUMBERS = {}

SHIPPED = sample_dir()

CLAIMS_N = 121
CLAIMS_SUM = 5814.680787725242
CLAIMS_MIN = 0.0
CLAIMS_MAX = 64.47847187567388
FIRST_100_SUM = 4562.909698          # what a page-scoped statistic would print
TRACED = 1832
SUM_ASSURED_SUM = 5060517000
TOL = 1e-12


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-58s %s" % ("ok  " if condition else "FAIL", label, detail))


def fails(label, code, call):
    try:
        call()
    except Exception as exc:
        got = getattr(exc, "code", None)
        check(label, got == code, "%s: %s" % (got, str(getattr(exc, "message", exc))[:60]))
        return
    check(label, False, "did not raise")


def close(got, want, tol=TOL):
    return abs(got - want) / abs(want) <= tol if want else abs(got) <= tol


def main():
    import numpy as np
    import pandas as pd
    import modelx as mx
    from modelx_bridge import Bridge, BridgeError
    from modelx_bridge import tables
    from modelx_bridge.methods import FEATURES, _METHODS

    bridge = Bridge()
    model = mx.read_model(SHIPPED, name="BasicTerm_S")
    bridge.prime()
    model.Projection.pv_net_cf()
    traced = len(model._impl.tracegraph)

    def traced_now(m=model):
        return len(m._impl.tracegraph)

    # Every delta any cells.page in this suite produced. The named checks below
    # cover the cases the plan lists, one at a time; this is the whole-suite
    # version, so a NEW case added later is covered the moment it is written.
    deltas = []

    def page(params, m=model, b=bridge):
        """One cells.page, with the evaluation policy measured around it."""
        before, handles = traced_now(m), len(b.codec.handles)
        result = b.dispatch("cells.page", params)
        b.take_buffers()
        result["_traced_delta"] = traced_now(m) - before
        result["_handle_delta"] = len(b.codec.handles) - handles
        deltas.append((params.get("obj"), result["_traced_delta"],
                       result["_handle_delta"]))
        return result

    print("modelx %s, pandas %s\n" % (mx.__version__, pd.__version__))

    # -- the method is advertised, and the two lists agree -----------------
    print("--- capability hygiene ---")
    info = bridge.dispatch("session.info")
    check("session.info lists cells.page", "cells.page" in info["features"],
          ", ".join(info["features"][-3:]))
    check("session.info lists table.stats", "table.stats" in info["features"], "")
    # Nothing checked this before: a method that works but is not listed is
    # treated as absent by a client that detects before calling, and a name in
    # FEATURES with no method behind it fails after the UI has promised a table.
    # Exempt: names that announce a group of methods, a behaviour, a result
    # field or a param rather than one method.
    missing = sorted(set(FEATURES) - set(_METHODS) - {"buffers", "files",
                                                      "storage", "open.conflict",
                                                      "save.backup",
                                                      "kernel.identity",
                                                      # 0.10.0 (section 18)
                                                      "session.computed",
                                                      "stats.extremes",
                                                      "trace.values",
                                                      "cells.page.element",
                                                      "table.get.label",
                                                      "error.display",
                                                      "handle.index_name"})
    check("every method-shaped FEATURE has a method", missing == [],
          repr(missing))
    check("limits advertise max_series_rows",
          info["limits"]["max_series_rows"] == tables.MAX_SERIES_ROWS,
          repr(info["limits"]["max_series_rows"]))
    check("the model starts at the expected computation", traced == TRACED,
          "%d nodes" % traced)

    # -- the headline: a populated cache ----------------------------------
    print("\n--- claims(t), a populated cache ---")
    r = page({"obj": "Projection.claims", "around": [0], "format": "binary"})
    check("READING DOES NOT EVALUATE", r["_traced_delta"] == 0,
          "%d tracegraph nodes added" % r["_traced_delta"])
    check("and len(claims) did not move", len(model.Projection.claims) == CLAIMS_N,
          "%d" % len(model.Projection.claims))
    check("the reply carries NO handle", "h" not in r and "h" not in r["page"],
          ", ".join(sorted(k for k in r if not k.startswith("_"))))
    check("and minted none", r["_handle_delta"] == 0 and len(bridge.codec.handles) == 0,
          "%d in the store" % len(bridge.codec.handles))
    check("params, value name and dtype",
          r["params"] == ["t"] and r["value_name"] == "claims"
          and r["value_dtype"] == "float64",
          json.dumps([r["params"], r["value_name"], r["value_dtype"]]))
    check("n_cached and retains", r["n_cached"] == CLAIMS_N and r["retains"] is True,
          "%d cached, retains %s" % (r["n_cached"], r["retains"]))
    check("focus_row addresses the series, not the page", r["focus_row"] == 0,
          repr(r["focus_row"]))
    check("scalar_values and no too_large",
          r["scalar_values"] is True and r["too_large"] is None, "")

    stats = r["stats"]
    NUMBERS["claims sum (whole column)"] = stats["sum"]
    check("THE STATISTIC IS OVER THE WHOLE COLUMN",
          stats["n"] == CLAIMS_N and stats["count"] == CLAIMS_N
          and close(stats["sum"], CLAIMS_SUM),
          "count %d sum %.6f" % (stats["count"], stats["sum"]))
    check("and it says so on the block itself", stats["scope"] == "column",
          repr(stats["scope"]))
    check("BOTH EXTREMES ARE THE COLUMN'S, NOT THE PAGE'S",
          stats["min"] == CLAIMS_MIN and close(stats["max"], CLAIMS_MAX),
          "min %s max %s" % (stats["min"], stats["max"]))
    # If this ever passes, a page-scoped aggregate has crept back in.
    check("a page-scoped sum would have been visibly wrong",
          not close(stats["sum"], FIRST_100_SUM, 1e-6),
          "first 100 rows sum to %.6f, 22%% low" % FIRST_100_SUM)
    check("kind is numeric", stats["kind"] == "numeric", stats["kind"])

    keys = r["keys"]
    check("keys report the run and its holes",
          keys == {"integer": True, "min": "0", "max": "120", "gaps": 0},
          json.dumps(keys))

    pg = r["page"]
    check("the page geometry describes the SERIES, not the window",
          pg["total_rows"] == CLAIMS_N and pg["row"] == 0
          and pg["rows"] == CLAIMS_N and pg["complete"] is True,
          json.dumps({k: pg[k] for k in ("row", "rows", "total_rows", "complete")}))
    names = [c["name"] for c in pg["columns"]]
    check("columns are the hand-built k0 / v", names == ["k0", "v"],
          json.dumps(names))
    check("both are clean typed columns",
          pg["columns"][0]["js"] == "BigInt64Array"
          and pg["columns"][1]["js"] == "Float64Array",
          json.dumps([c.get("js") for c in pg["columns"]]))

    # -- pols_maturity: the audit fact nothing else surfaces ---------------
    print("\n--- pols_maturity, 120 values over t = 1..120 ---")
    pm = page({"obj": "Projection.pols_maturity"})
    check("it does not evaluate either", pm["_traced_delta"] == 0, "")
    check("its run starts at 1 where claims starts at 0",
          pm["keys"] == {"integer": True, "min": "1", "max": "120", "gaps": 0},
          json.dumps(pm["keys"]))
    check("and it is 120 long, not 121", pm["n_cached"] == 120,
          "%d" % pm["n_cached"])

    # -- a zero-parameter Cells is never worth a round trip ----------------
    print("\n--- a zero-parameter Cells ---")
    z = page({"obj": "Projection.pv_net_cf"})
    check("it does not evaluate", z["_traced_delta"] == 0, "")
    check("page and stats are null, with a note",
          z["page"] is None and z["stats"] is None and "note" in z,
          z.get("note", "")[:56])
    check("and the NaN-indexed one-row series was never built",
          z["params"] == [] and z["value_dtype"] is None, "")

    # -- the container Cells: object dtype, but zero-parameter -------------
    print("\n--- the array-valued Cells BasicTerm_S actually has ---")
    for name in ("disc_factors", "disc_rate_mth", "model_point"):
        c = page({"obj": "Projection." + name})
        check("%s reads without evaluating" % name, c["_traced_delta"] == 0
              and c["page"] is None, "zero-parameter, no page")

    # -- an evicted / unknown handle, and table.stats over a real column ---
    print("\n--- table.stats over the 10000x5 model_point_table ---")
    before = traced_now()
    tag = bridge.dispatch("value.get", {
        "nodes": [{"obj": "Projection.model_point_table"}]})["values"][0]["value"]
    h = tag["h"]
    source = model.Projection.model_point_table

    def stats_call(params):
        b4 = traced_now()
        out = bridge.dispatch("table.stats", params)
        out["_traced_delta"] = traced_now() - b4
        return out

    sa = stats_call({"h": h, "col": 4})
    check("table.stats does not evaluate", sa["_traced_delta"] == 0, "")
    check("it names the column it reduced",
          sa["name"] == "sum_assured" and sa["col"] == 4 and sa["dtype"] == "int64",
          json.dumps({k: sa[k] for k in ("name", "col", "dtype")}))
    check("THE COLUMN IS PULLED WHOLE, NOT SLICED TO A PAGE",
          sa["stats"]["n"] == 10000 and sa["stats"]["count"] == 10000,
          "n %d over %d rows" % (sa["stats"]["n"], sa["total_rows"]))
    check("the sum equals pandas' own over all 10000 rows",
          sa["stats"]["sum"] == str(int(source["sum_assured"].sum()))
          == str(SUM_ASSURED_SUM), sa["stats"]["sum"])
    check("an integer column's sum/min/max are DECIMAL STRINGS",
          all(isinstance(sa["stats"][k], str) for k in ("sum", "min", "max")),
          json.dumps({k: sa["stats"][k] for k in ("sum", "min", "max")}))
    check("its mean is a float, because a mean is an estimate",
          isinstance(sa["stats"]["mean"], float), repr(sa["stats"]["mean"]))
    NUMBERS["sum_assured (10000 rows)"] = sa["stats"]["sum"]

    sex = stats_call({"h": h, "col": 1})
    check("a string column branches to text, not to a broken sum",
          sex["stats"]["kind"] == "text" and sex["stats"]["sum"] is None
          and sex["stats"]["unique"] == 2, json.dumps(sex["stats"]))
    check("col defaults to 0", stats_call({"h": h})["col"] == 0, "")
    fails("a column past the end is bad_request", "bad_request",
          lambda: bridge.dispatch("table.stats", {"h": h, "col": 99}))
    fails("an evicted handle is not_found", "not_found",
          lambda: bridge.dispatch("table.stats", {"h": "h9999"}))

    # -- strictness --------------------------------------------------------
    print("\n--- what cells.page refuses ---")
    fails("an unknown param is REJECTED, not dropped", "bad_request",
          lambda: bridge.dispatch("cells.page", {"obj": "Projection.claims",
                                                 "arround": [0]}))
    fails("a Reference is bad_request", "bad_request",
          lambda: bridge.dispatch("cells.page", {"obj": "Projection.disc_rate_ann"}))
    fails("a Space is bad_request", "bad_request",
          lambda: bridge.dispatch("cells.page", {"obj": "Projection"}))
    fails("the Model is bad_request", "bad_request",
          lambda: bridge.dispatch("cells.page", {"obj": ""}))
    fails("an unknown obj is not_found", "not_found",
          lambda: bridge.dispatch("cells.page", {"obj": "Projection.nope"}))
    fails("a negative row is bad_request", "bad_request",
          lambda: bridge.dispatch("cells.page", {"obj": "Projection.claims",
                                                 "row": -1}))
    check("and none of that evaluated anything", traced_now() == before,
          "%d nodes" % traced_now())

    # -- fixed blocks, and an argument step that costs nothing --------------
    print("\n--- fixed blocks (the reason an argument step can be free) ---")
    # EVERY cells and reference this fixture needs is created HERE, before
    # anything is computed. Adding a Cells is a structural edit and assigning a
    # Reference is an edit too, and either CLEARS the whole model's cache -- so a
    # fixture that interleaves `new_cells` with computation silently hands the
    # later checks an empty cache. That is how the `too_large` check below first
    # read `len(cells)` as 0 instead of 400 and passed straight through the guard
    # it exists to prove.
    scratch = mx.new_model("CellsPageProbe")
    space = scratch.new_space("S")
    space.np = np
    space.pd = pd
    space.new_cells(name="long", formula=lambda t: float(t) * 1.5)
    space.new_cells(name="never_run", formula=lambda t: t + 1)
    space.new_cells(name="uncached", formula=lambda t: t + 1)
    space.new_cells(name="holes", formula=lambda t: t * 1.0)
    space.new_cells(name="two", formula=lambda i, j: i * 10 + j)
    space.new_cells(name="bykey", formula=lambda k: len(k))
    space.new_cells(name="arrays", formula=lambda t: np.arange(t + 1, dtype=float))
    space.new_cells(name="allnan", formula=lambda t: float("nan"))
    space.new_cells(name="huge", formula=lambda t: 9007199254740993 + t)
    # pandas' MASKED integer, which is what `pd.read_csv` hands back for an
    # integer column with one blank cell. Its dtype kind is "i", so it reaches
    # the integer branch of the reduction, where `np.asarray` on it RAISES
    # `ValueError: cannot convert float NaN to integer` -- measured, and it came
    # back from `table.stats` as an `internal` error on an ordinary column.
    space.new_cells(name="masked", formula=lambda: pd.DataFrame({
        "gaps": pd.array([10, 20, None, 40], dtype="Int64"),
        "whole": pd.array([1, 2, 3, 4], dtype="Int64")}))
    space.uncached.is_cached = False

    for t in range(400):
        space.long(t)
    space.uncached(3)
    for t in (0, 1, 5):
        space.holes(t)
    for i, j in ((1, 2), (3, 4), (0, 0)):
        space.two(i, j)
    space.bykey("abc")
    space.bykey("de")
    for t in range(10):
        space.arrays(t)
    for t in range(5):
        space.allnan(t)
    for t in range(3):
        space.huge(t)
    space.masked()

    def spage(params):
        before_s = len(scratch._impl.tracegraph)
        handles = len(bridge.codec.handles)
        out = bridge.dispatch("cells.page", dict(params, model="CellsPageProbe"))
        bridge.take_buffers()
        out["_traced_delta"] = len(scratch._impl.tracegraph) - before_s
        out["_handle_delta"] = len(bridge.codec.handles) - handles
        deltas.append((params.get("obj"), out["_traced_delta"],
                       out["_handle_delta"]))
        return out

    blk = spage({"obj": "S.long", "around": [250], "rows": 200})
    check("around t=250 with rows=200 opens the block at row 200",
          blk["page"]["row"] == 200, repr(blk["page"]["row"]))
    check("and focus_row is the SERIES position, 250", blk["focus_row"] == 250,
          repr(blk["focus_row"]))
    check("the block is 200 long out of 400, and is the last one",
          blk["page"]["rows"] == 200 and blk["page"]["total_rows"] == 400
          and blk["page"]["complete"] is True,
          json.dumps({k: blk["page"][k] for k in ("rows", "total_rows", "complete")}))
    first = spage({"obj": "S.long", "rows": 200})
    check("no around opens at row 0", first["page"]["row"] == 0, "")
    # The frame handed to `tables.page` IS the window, so the page block would
    # otherwise report total_rows 200 for a 400-value column and a pager reading
    # it would print "rows 1-200 of 200".
    check("and the first block knows there is more after it",
          first["page"]["total_rows"] == 400
          and first["page"]["complete"] is False,
          json.dumps({k: first["page"][k] for k in ("total_rows", "complete")}))
    check("an explicit row wins over around",
          spage({"obj": "S.long", "around": [250], "row": 0,
                 "rows": 200})["page"]["row"] == 0, "")
    # A client that remembered `row: 2000` and re-read after an edit CLEARED the
    # cache -- the Re-read path this whole method exists to make honest -- used to
    # be handed an EMPTY window reported as `complete: true` at a row that does
    # not exist. A pager reading it prints "rows 2001-2000 of 400".
    past = spage({"obj": "S.long", "row": 2000, "rows": 200})
    check("A ROW PAST THE END LANDS ON THE LAST BLOCK, NOT ON NOTHING",
          past["page"]["row"] == 200 and past["page"]["rows"] == 200,
          json.dumps({k: past["page"][k] for k in ("row", "rows")}))
    check("and `complete` is never true over an empty window",
          past["page"]["rows"] > 0 and past["page"]["complete"] is True,
          "complete %s over %d rows" % (past["page"]["complete"],
                                        past["page"]["rows"]))
    check("paging a 400-value column mints no handles",
          len(bridge.codec.handles) == 1, "%d (the model_point_table one)"
          % len(bridge.codec.handles))

    uncached = spage({"obj": "S.long", "around": [9999], "rows": 200})
    check("AROUND AN UNCACHED KEY IS focus_row null, WITH A PAGE STILL",
          uncached["focus_row"] is None and uncached["page"]["row"] == 0
          and uncached["page"]["rows"] == 200, "t=9999 has not been computed")
    check("and asking for it did not compute it", uncached["_traced_delta"] == 0
          and len(space.long) == 400, "%d cached" % len(space.long))

    # -- the empty states, which need different sentences -------------------
    print("\n--- honest empty states ---")
    empty = spage({"obj": "S.never_run"})
    check("NOTHING COMPUTED YET is n_cached 0 with retains TRUE",
          empty["n_cached"] == 0 and empty["retains"] is True,
          "retains means it KEEPS values, not that it HAS any")
    check("it still returns a page, not an error",
          empty["page"] is not None and empty["page"]["rows"] == 0, "")
    check("its keys block is null, because there is no run to describe",
          empty["keys"] is None, "")
    check("and its statistics are NaN, not zero",
          empty["stats"]["count"] == 0
          and empty["stats"]["mean"] == {"$t": "num", "v": "NaN"},
          json.dumps(empty["stats"]["mean"]))
    check("reading an empty cache does not evaluate", empty["_traced_delta"] == 0, "")

    nk = spage({"obj": "S.uncached"})
    check("KEEPS NO VALUES is a DIFFERENT reply: retains FALSE",
          nk["retains"] is False and nk["n_cached"] == 0,
          "both arrive as an empty grid and need different sentences")
    check("and reading one does not evaluate", nk["_traced_delta"] == 0, "")

    # -- gaps ---------------------------------------------------------------
    print("\n--- gaps ---")
    holes = spage({"obj": "S.holes"})
    check("a column with holes reports them",
          holes["keys"] == {"integer": True, "min": "0", "max": "5", "gaps": 3},
          json.dumps(holes["keys"]))

    # -- the size guard fires without paying for it -------------------------
    print("\n--- too_large refuses off len(cells), never off .series ---")
    saved_limit = tables.MAX_SERIES_ROWS
    cells_class = type(space.long)
    saved_series = cells_class.series

    def exploding(self):
        raise AssertionError("cells.series was materialised behind the limit")

    tables.MAX_SERIES_ROWS = 10
    cells_class.series = property(exploding)
    # Drop the memo first, or this check proves nothing: `_series` answers a
    # memo hit WITHOUT touching the property, so if the memo still held S.long
    # the exploding property would stay silent with the guard DELETED. Measured
    # -- with the guard neutralised and the memo warm, the property was never
    # touched. Today the intervening pages happen to have evicted the one entry;
    # this line means the check does not depend on that ordering.
    bridge._series_memo = None
    try:
        big = bridge.dispatch("cells.page", {"model": "CellsPageProbe",
                                             "obj": "S.long"})
        check("it refuses with a count and the limit",
              big["too_large"] == {"cached": 400, "limit": 10},
              json.dumps(big["too_large"]))
        check("and carries no page or stats",
              big["page"] is None and big["stats"] is None, "")
        check("THE SERIES WAS NEVER TOUCHED", True,
              "the monkeypatched property would have raised")
    except AssertionError as exc:
        check("THE SERIES WAS NEVER TOUCHED", False, str(exc))
    finally:
        cells_class.series = saved_series
        tables.MAX_SERIES_ROWS = saved_limit

    # -- the MultiIndex case BasicTerm_S cannot reach -----------------------
    print("\n--- a 2-parameter Cells (no BasicTerm_S cell takes two) ---")
    two = spage({"obj": "S.two", "format": "binary", "around": [3, 4]})
    check("it does not evaluate", two["_traced_delta"] == 0, "")
    cols = two["page"]["columns"]
    check("it pages as k0 / k1 / v",
          [c["name"] for c in cols] == ["k0", "k1", "v"],
          json.dumps([c["name"] for c in cols]))
    check("ALL THREE ARE CLEAN TYPED COLUMNS",
          all(c.get("js") == "BigInt64Array" and c["dtype"] == "int64"
              for c in cols), json.dumps([c.get("js") for c in cols]))
    check("a tuple around finds its row", two["focus_row"] == 1,
          repr(two["focus_row"]))
    check("its params carry the real labels", two["params"] == ["i", "j"],
          json.dumps(two["params"]))
    check("and keys is null: a cross-product run is undefined",
          two["keys"] is None, "")
    bridge.take_buffers()

    # Side by side with the path this method exists to avoid, so the reason the
    # frame is hand-built lives in the suite rather than only in a comment.
    from modelx_bridge.codec import Codec
    raw = tables.page(Codec(), space.two.frame,
                      {"row": 0, "rows": 10, "col": 0, "cols": 50,
                       "format": "json"})
    opaque = json.dumps(raw["index"])
    check("where .frame renders the SAME keys as opaque tuple blobs",
          '"$t": "opaque"' in opaque and '"$t": "tuple"' in opaque,
          opaque[:72] + "...")

    # -- a string-keyed Cells ----------------------------------------------
    print("\n--- a string-keyed Cells ---")
    sk = spage({"obj": "S.bykey", "format": "binary"})
    kcol = sk["page"]["columns"][0]
    check("k0 comes back as a JSON column, v stays binary",
          kcol.get("values") == ["abc", "de"]
          and "buffer" in sk["page"]["columns"][1], json.dumps(kcol))
    check("and its keys block is null: a string run has no gaps",
          sk["keys"] is None, "")
    bridge.take_buffers()

    # -- THE OBJECT-DTYPE GUARD --------------------------------------------
    print("\n--- an object-dtype value column (the 64-entry LRU is at stake) ---")
    handles_before = len(bridge.codec.handles)
    arr = spage({"obj": "S.arrays", "format": "binary"})
    check("it does not evaluate", arr["_traced_delta"] == 0, "")
    check("scalar_values is FALSE", arr["scalar_values"] is False, "")
    check("ZERO HANDLES MINTED, where paging it raw minted one per cell",
          len(bridge.codec.handles) == handles_before,
          "measured 10 rows -> 10 handles without the guard")
    vcol = arr["page"]["columns"][1]
    check("the value column carries short reprs",
          vcol.get("values", [None])[0] == "array([0.])",
          json.dumps(vcol.get("values", [])[:2]))
    check("and its statistics are kind 'other' with a note, not a wrong number",
          arr["stats"]["kind"] == "other" and arr["stats"]["sum"] is None
          and "note" in arr["stats"], arr["stats"].get("note", "")[:50])
    bridge.take_buffers()

    # -- NaN, and why it must not reach _check_size -------------------------
    print("\n--- an all-NaN column ---")
    nan = spage({"obj": "S.allnan"})
    tagged = {"$t": "num", "v": "NaN"}
    check("its mean is a tagged NaN, NOT a bad_request from allow_nan=False",
          nan["stats"]["mean"] == tagged and nan["stats"]["min"] == tagged,
          json.dumps(nan["stats"]["mean"]))
    check("and count is 0 against n of 5",
          nan["stats"]["count"] == 0 and nan["stats"]["n"] == 5
          and nan["stats"]["nulls"] == 5, json.dumps(
              {k: nan["stats"][k] for k in ("n", "count", "nulls")}))

    # -- integers past 2^53 -------------------------------------------------
    print("\n--- integers a browser cannot parse ---")
    huge = spage({"obj": "S.huge", "format": "binary"})
    check("sum/min/max survive as exact decimal strings",
          huge["stats"]["sum"] == "27021597764222982"
          and huge["stats"]["min"] == "9007199254740993"
          and huge["stats"]["max"] == "9007199254740995",
          json.dumps({k: huge["stats"][k] for k in ("sum", "min", "max")}))
    # The same value through a double -- which is what JSON.parse hands a browser
    # for a bare JSON number -- comes back one short. That is the whole argument
    # for the decimal string, so it is measured here rather than asserted in a
    # comment.
    check("and a double could not have carried it",
          int(float(huge["stats"]["min"])) == 9007199254740992,
          "JSON.parse('9007199254740993') is 9007199254740992 in every browser")
    check("and the values themselves ride as BigInt64Array",
          huge["page"]["columns"][1].get("js") == "BigInt64Array", "")
    NUMBERS["huge sum"] = huge["stats"]["sum"]
    bridge.take_buffers()

    # -- the column shape a spreadsheet produces and the reduction did not ---
    print("\n--- a masked integer column: an ordinary CSV, not an exotic case ---")
    masked_h = bridge.dispatch("value.get", {
        "model": "CellsPageProbe",
        "nodes": [{"obj": "S.masked"}]})["values"][0]["value"]["h"]
    gapped = bridge.dispatch("table.stats", {"h": masked_h, "col": 0})["stats"]
    # `pd.read_csv` gives dtype Int64 for an integer column with one blank cell.
    # Its kind is "i", so it reaches the integer branch, and `np.asarray` on it
    # raises ValueError: cannot convert float NaN to integer -- which left this
    # method answering `internal` on a column header click.
    check("A MASKED INTEGER COLUMN REDUCES INSTEAD OF RAISING",
          gapped["kind"] == "integer" and gapped["n"] == 4
          and gapped["count"] == 3 and gapped["nulls"] == 1,
          json.dumps({k: gapped[k] for k in ("kind", "n", "count", "nulls")}))
    check("its sum is over the values that exist, and still exact",
          gapped["sum"] == "70" and gapped["min"] == "10"
          and gapped["max"] == "40",
          json.dumps({k: gapped[k] for k in ("sum", "min", "max")}))
    check("and the mean divides by the count, not by n",
          gapped["mean"] == 70.0 / 3, repr(gapped["mean"]))
    whole = bridge.dispatch("table.stats", {"h": masked_h, "col": 1})["stats"]
    check("a masked column with no nulls reads like a plain one",
          whole["count"] == 4 and whole["nulls"] == 0 and whole["sum"] == "10",
          json.dumps({k: whole[k] for k in ("count", "nulls", "sum")}))
    bridge.take_buffers()

    # -- the memo, and the revision that keeps it honest --------------------
    print("\n--- the single-entry series memo ---")
    bridge.dispatch("cells.page", {"model": "BasicTerm_S",
                                   "obj": "Projection.claims"})
    bridge.take_buffers()
    memo = bridge._series_memo
    check("it is keyed on (model, obj, revision, n_cached)",
          memo is not None and memo[0] == ("BasicTerm_S", "Projection.claims",
                                           bridge.revision(model), CLAIMS_N),
          json.dumps(list(memo[0])) if memo else "no memo")
    first = memo[1]
    bridge.dispatch("cells.page", {"model": "BasicTerm_S",
                                   "obj": "Projection.claims"})
    bridge.take_buffers()
    check("a second read at the same revision reuses the same Series object",
          bridge._series_memo[1] is first, "")
    bridge.dispatch("cells.page", {"model": "CellsPageProbe", "obj": "S.long"})
    bridge.take_buffers()
    check("and reading another column replaces the ONE entry",
          bridge._series_memo[0][1] == "S.long", bridge._series_memo[0][1])

    bridge.dispatch("ref.set", {"model": "BasicTerm_S",
                                "obj": "Projection.point_id", "value": 3})
    after = page({"model": "BasicTerm_S", "obj": "Projection.claims"})
    check("A REVISION BUMP CANNOT SERVE PRE-EDIT NUMBERS",
          bridge._series_memo[0][2] == bridge.revision(model)
          and after["n_cached"] == 0,
          "the reference edit cleared all %d cached values" % TRACED)
    check("and the panel's honest answer is 'nothing computed yet'",
          after["retains"] is True and after["stats"]["count"] == 0, "")

    # -- the memo that the revision alone did NOT keep honest ---------------
    #
    # The revision is a CONSERVATIVE change detector, not proof of equality --
    # `on_execute`'s own docstring says it can miss a change that leaves all
    # three fingerprint counts identical. Anything that moves the cache without
    # bumping leaves the memo holding the column as it was, and the reply then
    # mixes a fresh `len(cells)` with a stale series. MEASURED before the count
    # went into the key: `n_cached: 122` over `stats.n: 121`, a page claiming
    # `total_rows: 122` while carrying 121 rows, and `keys.gaps: -1`.
    #
    # Computing here rather than through the bridge is the point: a bridge
    # evaluation bumps, so it would test the revision and not the count.
    print("\n--- the memo cannot outlive the cache it describes ---")
    warm = spage({"obj": "S.long", "rows": 200})
    before_rev = bridge.revision(scratch)
    space.long(400)
    check("the cache moved with the revision standing still",
          len(space.long) == 401 and bridge.revision(scratch) == before_rev,
          "%d cached at revision %d" % (len(space.long), before_rev))
    again = spage({"obj": "S.long", "rows": 200})
    check("THE STATISTIC IS OVER THE SERIES THE REPLY COUNTS",
          again["n_cached"] == 401 and again["stats"]["n"] == 401,
          "n_cached %d, stats over %d" % (again["n_cached"], again["stats"]["n"]))
    check("and the sum moved with it", again["stats"]["sum"]
          > warm["stats"]["sum"], "%.1f -> %.1f"
          % (warm["stats"]["sum"], again["stats"]["sum"]))
    check("the page geometry counts the same series",
          again["page"]["total_rows"] == 401, repr(again["page"]["total_rows"]))
    check("GAPS CANNOT BE NEGATIVE: it is counted off the index it measured",
          again["keys"] == {"integer": True, "min": "0", "max": "400",
                            "gaps": 0}, json.dumps(again["keys"]))
    check("and the memo key carries the count that invalidated it",
          bridge._series_memo[0] == ("CellsPageProbe", "S.long",
                                     before_rev, 401),
          json.dumps(list(bridge._series_memo[0])))

    # -- value.get says which node each entry is for ------------------------
    print("\n--- args on every value.get entry ---")
    got = bridge.dispatch("value.get", {
        "model": "BasicTerm_S",
        "nodes": [{"obj": "Projection.claims", "args": [3]},
                  {"obj": "Projection.disc_rate_ann", "args": []}]})["values"]
    check("a Cells entry carries the arguments it was read at",
          got[0]["args"] == [3], json.dumps(got[0]["args"]))
    check("a Reference entry carries [], whatever was sent",
          got[1]["args"] == [], json.dumps(got[1]["args"]))
    uncomputed = bridge.dispatch("value.get", {
        "model": "BasicTerm_S",
        "nodes": [{"obj": "Projection.claims", "args": [7]}],
        "evaluate": False})["values"][0]
    check("and a NOT-CACHED entry carries them too — it is the frame that lies",
          uncomputed["cached"] is False and uncomputed["args"] == [7],
          json.dumps(uncomputed))

    scratch.close()

    print("\n--- the evaluation policy, over every call this suite made ---")
    moved = [row for row in deltas if row[1] or row[2]]
    check("NOT ONE cells.page ADDED A TRACEGRAPH NODE OR A HANDLE",
          moved == [], "%d calls; %d moved %s"
          % (len(deltas), len(moved), json.dumps(moved[:4])))

    print("\n--- numbers ---")
    for key in sorted(NUMBERS):
        print("  %-28s %s" % (key, NUMBERS[key]))
    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
