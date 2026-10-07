"""What modelx-mcp needs from the bridge (0.10.0, protocol section 18).

    python -m modelx_bridge.tests.test_mcp_support

SEVEN ADDITIONS, AND THE PROPERTY THEY SHARE IS THAT THE SHIPPED FRONTEND SEES
NOTHING DIFFERENT. Each is an OPTIONAL result field or an OPTIONAL param whose
default is the 0.9.0 behaviour, so section [J] asserts the default replies
directly, key by key, instead of trusting that nothing moved.

  B3 session.computed     session.info.models[].computed, ItemSpaces included
  B4 stats.extremes       argmin / argmax and their labels on every stats block
  B5 trace.values         trace.* with values: false mints NO handle
  B6 cells.page.element   one element of each cached Series, as a column
  B7 table.get.label      a page that starts at an index label
  B8 error.display        the failing node's display on formula_error
  B9 handle.index_name    what a Series' or DataFrame's labels are

B1 (a revision bump on a failed evaluation) and B2 (a canonical `display` on
value.get) were considered with these and DEFERRED: they change behaviour the
shipped UI relies on -- model.changed handling and display strings -- and need a
browser run of their own. Section [I] pins the behaviour they would change, so
whoever lands them sees this suite move and has to look at the frontend too.

Every read here is measured against both models' tracegraphs, the way
test_cells_page measures cells.page: section [K] asserts that no call that
reads added a node. The models: the shipped BasicTerm_S, opened as the sample,
and a synthetic one built below.
"""

import json
import sys

from .shipped import sample_dir

FAILURES = []

SHIPPED = sample_dir()

#: Measured 2026-10-05 on the shipped model (modelx 0.33.0, pandas 3.0.6).
TRACED = 1832                       # after pv_net_cf()
ITEM_CACHED = 3632                  # cached values inside Projection[2]
FAILED_LEFTOVER = 4                 # what claims(9999) adds after pv_net_cf()
CLAIMS_ARGMIN, CLAIMS_ARGMAX = 120, 108
PV_CLAIMS_PREDS = 123
DISC_RATE_10 = [0.01188, 0.01226]   # disc_rate_ann, years 10 and 11

#: The seven names this version adds, and the two it deliberately does not.
ADDED = ["session.computed", "stats.extremes", "trace.values",
         "cells.page.element", "table.get.label", "error.display",
         "handle.index_name"]
DEFERRED = ["eval.failure.bump", "value.display.canonical"]


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-62s %s" % ("ok  " if condition else "FAIL", label, detail))


def refused(label, code, call):
    """`call` must raise a BridgeError with `code`; returns its message."""
    try:
        call()
    except Exception as exc:
        got = getattr(exc, "code", None)
        message = str(getattr(exc, "message", exc))
        check(label, got == code, "%s: %s" % (got, message[:70]))
        return message
    check(label, False, "did not raise")
    return ""


def np_tag(value):
    return {"$t": "np", "dtype": "int64", "v": value}


def main():
    import numpy as np
    import pandas as pd
    import modelx as mx
    from modelx_bridge import Bridge
    from modelx_bridge.methods import FEATURES, VERSION

    for open_model in list(mx.get_models().values()):
        open_model.close()

    bridge = Bridge()
    opened = bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"})
    model = mx.get_models()[opened["model"]]
    name = model.name

    # EVERY Cells of the synthetic model is created here, before anything is
    # computed: a structural edit clears the model's whole cache, so a fixture
    # that interleaves new_cells with computation hands later checks an empty
    # one (test_cells_page found that the hard way).
    synth = mx.new_model("Synth")
    space = synth.new_space("S")
    space.pd = pd
    space.np = np
    space.new_cells("f", formula=lambda t, kind=None: t * 2.0)
    space.new_cells("v", formula=lambda t: pd.Series(
        [t * 1.0, -t * 2.0], index=pd.Index([1, 2], name="point")))
    # `f` is the Cells above: a formula's names resolve in its Space.
    space.new_cells("g", formula=lambda: f(3))  # noqa: F821
    space.new_cells("ragged", formula=lambda t: pd.Series(
        [t * 1.0, 5.0] if t % 2 == 0 else [t * 1.0],
        index=pd.Index([1, 2] if t % 2 == 0 else [1])))
    space.new_cells("twice", formula=lambda t: pd.Series(
        [1.0, 2.0, 3.0], index=[1, 2, 2]))
    space.new_cells("frames", formula=lambda t: pd.DataFrame({"a": [t]}))
    space.new_cells("arrays", formula=lambda t: np.arange(3.0) + t)
    space.new_cells("never", formula=lambda t: pd.Series([t], index=[1]))
    space.new_cells("uncached", formula=lambda t: t)
    space.new_cells("peaks", formula=lambda: pd.Series(
        [np.nan, 1.0, 5.0, 5.0], index=pd.Index([10, 20, 30, 40], name="k")))
    space.new_cells("masked", formula=lambda: pd.DataFrame(
        {"gaps": pd.array([10, 20, None, 40], dtype="Int64")}))
    space.new_cells("allnan", formula=lambda: pd.Series([np.nan, np.nan]))
    space.new_cells("labels", formula=lambda: pd.Index([3, 1, 2], name="ix"))
    space.new_cells("multi", formula=lambda: pd.Series(
        [1.0, 2.0, 3.0], index=pd.MultiIndex.from_tuples(
            [(1, "a"), (1, "b"), (2, "a")], names=["n", "s"])))
    space.new_cells("half", formula=lambda: pd.Series(
        [1.0], index=pd.MultiIndex.from_tuples([(1, "a")], names=[None, "s"])))
    space.new_cells("repeats", formula=lambda: pd.Series(
        [1.0, 2.0, 3.0, 4.0], index=[5, 7, 7, 9]))
    # 7 at rows 0 and 201: get_loc answers a mask, and a 100-row page holds one.
    space.new_cells("apart", formula=lambda: pd.Series(
        range(202), index=[7] + list(range(1000, 1200)) + [7], dtype=float))
    # Keys and labels whose encoding is over max_inline_bytes (8,192), or that
    # `_enc` would make a handle of: an extreme's label must mint none.
    space.new_cells("bigkey", formula=lambda x, y: float(len(x) + len(y)))
    space.new_cells("longlabels", formula=lambda: pd.Series(
        [1.0, 9.0], index=pd.MultiIndex.from_tuples(
            [("a" * 4500, "b" * 4500), ("c" * 4500, "d" * 4500)])))
    space.new_cells("arraylabels", formula=lambda: pd.Series(
        [1.0, 5.0], index=pd.Index([np.array([1, 2]), np.array([3])],
                                   dtype=object)))
    space.new_cells("levels", formula=lambda t: pd.Series(
        [t * 1.0, t * 2.0], index=pd.MultiIndex.from_tuples(
            [(1, "x"), (2, "y")], names=["pt", "kind"])))
    space.new_cells("pairs", formula=lambda t: pd.Series(
        [t * 1.0, t * 2.0], index=pd.Index([(1, 2), (3, 4)],
                                           tupleize_cols=False)))
    space.uncached.is_cached = False
    bridge.prime()

    def traced(m=model):
        return len(m._impl.tracegraph)

    def call(method, params, model_name=None):
        params = dict(params)
        params.setdefault("model", model_name or name)
        out = bridge.dispatch(method, params)
        bridge.take_buffers()
        return out

    # Every call that READS, with both tracegraphs measured around it.
    deltas = []

    def read(method, params, model_name=None):
        before = (traced(model), traced(synth), bridge.codec._counter)
        out = call(method, params, model_name)
        after = (traced(model), traced(synth), bridge.codec._counter)
        deltas.append((method, params.get("obj") or params.get("h"),
                       after[0] - before[0], after[1] - before[1]))
        out_minted = after[2] - before[2]
        return out, out_minted

    def info(model_name):
        models = bridge.dispatch("session.info", {})["models"]
        return [m for m in models if m["name"] == model_name][0]

    def handle(obj, model_name=None, args=()):
        entry = call("value.get", {"nodes": [{"obj": obj, "args": list(args)}],
                                   "evaluate": True}, model_name)["values"][0]
        return entry["value"]

    def tree_cached(obj=""):
        root = call("tree.get", {"obj": obj})["root"]
        total, stack = 0, [root]
        while stack:
            node = stack.pop()
            total += sum(c["cached"] for c in node.get("cells", []))
            stack.extend(node.get("spaces", []))
        return total

    print("modelx %s, pandas %s, bridge %s" % (mx.__version__, pd.__version__,
                                              VERSION))

    # -- [A] -------------------------------------------------------------------
    print("\n[A] the additions are announced, and the deferred two are not")
    check("the version is 0.10.0 or later, compared as numbers",
          tuple(map(int, VERSION.split("."))) >= (0, 10, 0), VERSION)
    features = bridge.dispatch("session.info", {})["features"]
    check("all seven 0.10.0 names are in session.info.features",
          all(f in features for f in ADDED),
          ", ".join(f for f in ADDED if f not in features) or "all seven")
    check("...and FEATURES itself", all(f in FEATURES for f in ADDED), "")
    check("B1 and B2 are NOT announced: they did not land",
          not any(f in features for f in DEFERRED), ", ".join(DEFERRED))

    # -- [B] -------------------------------------------------------------------
    print("\n[B] B3 session.computed: the total that counts inside ItemSpaces")
    check("every open model carries computed",
          all("computed" in m for m in
              bridge.dispatch("session.info", {})["models"]), "")
    check("the fresh sample has computed 0", info(name)["computed"] == 0,
          repr(info(name)["computed"]))
    call("value.get", {"nodes": [{"obj": "Projection.pv_net_cf", "args": []}]})
    computed = info(name)["computed"]
    check("pv_net_cf() makes it the measured 1,832", computed == TRACED,
          str(computed))
    check("...which is the tree's own sum while no ItemSpace exists",
          computed == tree_cached(), "tree sums to %d" % tree_cached())
    item = handle("Projection", args=[2])
    check("value.get on Projection [2] creates the ItemSpace",
          isinstance(item, dict) and item.get("$t") == "mx"
          and item.get("display") == "Projection[2]", json.dumps(item))
    call("value.get", {"nodes": [{"obj": item["obj"] + ".pv_net_cf",
                                  "args": []}]})
    computed = info(name)["computed"]
    inside = tree_cached(item["obj"])
    check("inside Projection[2], the measured 3,632 cached values",
          inside == ITEM_CACHED, str(inside))
    check("COMPUTED COUNTS THEM, PLUS ONE NODE FOR THE ITEMSPACE ITSELF",
          computed == TRACED + ITEM_CACHED + 1,
          "%d = %d + %d + 1" % (computed, TRACED, ITEM_CACHED))
    check("while the tree, which does not recurse into ItemSpaces, still "
          "sums to 1,832", tree_cached() == TRACED, str(tree_cached()))
    check("computed is len(tracegraph), read as the model has it",
          computed == traced(), "%d / %d" % (computed, traced()))

    check("the synthetic model starts at 0", info("Synth")["computed"] == 0,
          repr(info("Synth")["computed"]))
    for t in range(5):
        call("value.get", {"nodes": [{"obj": "S.v", "args": [t]}]}, "Synth")
    call("value.get", {"nodes": [{"obj": "S.f", "args": [3]}]}, "Synth")
    check("five v and one f are 6: a node that reads no other node counts",
          info("Synth")["computed"] == 6, str(info("Synth")["computed"]))
    space.uncached(1)
    check("a Cells that keeps no values adds none",
          info("Synth")["computed"] == 6, str(info("Synth")["computed"]))

    # -- [G] B8 sits here: it is the failure that moves `computed` -------------
    print("\n[G] B8 error.display: the failing node, named")
    bridge.drain_events()
    before_computed, before_rev = computed, bridge.revision(model)
    failed = call("value.get", {"nodes": [{"obj": "Projection.claims",
                                           "args": [9999]}]})["values"][0]
    data = failed.get("error", {}).get("data", {})
    after_computed = info(name)["computed"]
    check("claims(9999) fails as formula_error",
          failed["ok"] is False and failed["error"]["code"] == "formula_error",
          failed.get("error", {}).get("message", ""))
    check("error_display names the node that raised",
          data.get("error_display") == name + ".Projection.mort_rate(t=9999)",
          repr(data.get("error_display")))
    check("...beside the opaque address it was always given",
          data.get("error_obj") == "Projection.mort_rate"
          and data.get("error_args") == [9999],
          "%s %s" % (data.get("error_obj"), data.get("error_args")))
    check("a failure keeps what it computed, and computed says so",
          after_computed == before_computed + FAILED_LEFTOVER,
          "%d -> %d" % (before_computed, after_computed))

    # Here, before anything else evaluates and bumps for its own reasons.
    print("\n[I] B1, a revision bump on a failed evaluation, is DEFERRED "
          "(protocol section 18.8): the behaviour it would change")
    check("B1 DEFERRED, still open: that failure moved no revision",
          bridge.revision(model) == before_rev and not bridge.drain_events(),
          "revision %d with computed +%d"
          % (bridge.revision(model), after_computed - before_computed))
    bridge.on_execute()
    events = [e["params"] for e in bridge.drain_events()]
    check("B1 DEFERRED, still open: so the next post_execute says 'execute'",
          [e.get("reason") for e in events if e.get("model") == name]
          == ["execute"], json.dumps(events))
    check("B1 DEFERRED, still open: ...and marks the model dirty",
          info(name)["dirty"] is True,
          "protocol section 18.8 describes the candidate fix")

    print("\n[G] ...continued")
    try:
        call("trace.preds", {"obj": "Projection.claims", "args": [9999]})
        check("trace.preds' evaluating failure carries it too", False,
              "it returned")
    except Exception as exc:
        check("trace.preds' evaluating failure carries it too",
              getattr(exc, "data", {}).get("error_display")
              == name + ".Projection.mort_rate(t=9999)",
              repr(getattr(exc, "data", {}).get("error_display")))
    lost = handle("Projection", args=[99999])
    failed = call("value.get", {"nodes": [{"obj": lost["obj"] + ".pv_net_cf",
                                           "args": []}]})["values"][0]
    data = failed.get("error", {}).get("data", {})
    check("inside an ItemSpace error_obj is modelx's internal name",
          "__Space" in data.get("error_obj", ""), data.get("error_obj"))
    check("ERROR_DISPLAY NAMES IT BY ITS ARGUMENTS INSTEAD",
          data.get("error_display")
          == name + ".Projection[99999].model_point()",
          repr(data.get("error_display")))
    check("...and it carries no internal name",
          "__Space" not in (data.get("error_display") or "__Space"), "")

    # -- [C] -------------------------------------------------------------------
    print("\n[C] B4 stats.extremes: where the extremes are")
    page, minted = read("cells.page", {"obj": "Projection.claims"})
    stats = page["stats"]
    check("claims: argmin 120 and argmax 108, the measured positions",
          stats["argmin"] == CLAIMS_ARGMIN and stats["argmax"] == CLAIMS_ARGMAX,
          "%s / %s" % (stats["argmin"], stats["argmax"]))
    column = page["page"]["columns"][1]["values"]
    check("...and the values there ARE the column's min and max",
          column[stats["argmin"]] == stats["min"]
          and column[stats["argmax"]] == stats["max"],
          "%s, %s" % (column[stats["argmin"]], column[stats["argmax"]]))
    check("the labels are the arguments, codec-encoded",
          stats["argmin_label"] == np_tag(120)
          and stats["argmax_label"] == np_tag(108),
          json.dumps([stats["argmin_label"], stats["argmax_label"]]))
    check("cells.page still mints no handle", minted == 0, str(minted))

    rate = handle("Projection.disc_rate_ann")
    got, _ = read("table.stats", {"h": rate["h"]})
    check("disc_rate_ann: argmin 0 and argmax 150",
          got["stats"]["argmin"] == 0 and got["stats"]["argmax"] == 150,
          json.dumps([got["stats"]["argmin"], got["stats"]["argmax"]]))
    # Which index pandas builds for these Excel tables depends on its version,
    # and the label follows the index (section 18.2). MEASURED 2026-10-06:
    # pandas 3.0.6 reads disc_rate_ann's `year` and model_point_table's
    # `point_id` as RangeIndexes, whose labels are plain ints; 2.2.3 and 2.3.3
    # read the same .xlsx as int64 Indexes, whose labels are np.int64. This
    # pinned plain ints alone, and failed 2 checks on pandas 2 for that.
    def label(index, value):
        return value if isinstance(index, pd.RangeIndex) else np_tag(value)

    rate_index = model.Projection.disc_rate_ann.index
    check("...labelled from its index: plain ints from a RangeIndex, else np tags",
          got["stats"]["argmin_label"] == label(rate_index, 0)
          and got["stats"]["argmax_label"] == label(rate_index, 150),
          "%s: %s" % (type(rate_index).__name__,
                      json.dumps([got["stats"]["argmin_label"],
                                  got["stats"]["argmax_label"]])))
    table = handle("Projection.model_point_table")
    source = model.Projection.model_point_table["sum_assured"]
    got, _ = read("table.stats", {"h": table["h"], "col": 4})
    check("sum_assured over 10,000 rows: the labels are pandas' idxmin/idxmax",
          got["stats"]["argmin_label"] == label(source.index, int(source.idxmin()))
          and got["stats"]["argmax_label"]
          == label(source.index, int(source.idxmax())),
          "point_id %s / %s, from its %s" % (source.idxmin(), source.idxmax(),
                                             type(source.index).__name__))
    # The rule itself, on both index types whatever pandas reads Excel into.
    from modelx_bridge import tables
    pinned = {}
    for index in (pd.RangeIndex(1, 4, name="r"), pd.Index([1, 2, 3], name="i")):
        block = {"argmin": 2, "argmax": 1}
        tables.label_extremes(block, index, bridge.codec)
        pinned[type(index).__name__] = [block["argmin_label"],
                                        block["argmax_label"]]
    check("a RangeIndex labels with plain ints and an int64 Index with np tags",
          pinned == {"RangeIndex": [3, 2], "Index": [np_tag(3), np_tag(2)]},
          json.dumps(pinned))
    check("...one row before the label, because point_id starts at 1",
          got["stats"]["argmin"] == int(source.idxmin()) - 1
          and got["stats"]["argmax"] == int(source.idxmax()) - 1,
          json.dumps([got["stats"]["argmin"], got["stats"]["argmax"]]))
    text, _ = read("table.stats", {"h": table["h"], "col": 1})
    check("a text column carries no extremes at all",
          "argmin" not in text["stats"] and "argmin_label" not in text["stats"],
          text["stats"]["kind"])
    factors = handle("Projection.disc_factors")
    got, _ = read("table.stats", {"h": factors["h"]})
    check("an ndarray has positions (argmin 120, argmax 0) and no labels",
          got["stats"]["argmin"] == 120 and got["stats"]["argmax"] == 0
          and got["stats"]["argmin_label"] is None
          and got["stats"]["argmax_label"] is None,
          json.dumps({k: got["stats"][k] for k in
                      ("argmin", "argmax", "argmin_label")}))

    peaks = handle("S.peaks", "Synth")
    got, _ = read("table.stats", {"h": peaks["h"]}, "Synth")
    check("a leading NaN is skipped and a tie goes to the FIRST maximum",
          got["stats"]["argmin"] == 1 and got["stats"]["argmax"] == 2,
          json.dumps([got["stats"]["argmin"], got["stats"]["argmax"]]))
    check("...and the labels are that index's, 20 and 30",
          got["stats"]["argmin_label"] == np_tag(20)
          and got["stats"]["argmax_label"] == np_tag(30), "")
    masked = handle("S.masked", "Synth")
    got, _ = read("table.stats", {"h": masked["h"]}, "Synth")
    check("A MASKED COLUMN COUNTS POSITIONS WITH ITS NULL IN PLACE",
          got["stats"]["argmax"] == 3 and got["stats"]["argmin"] == 0,
          "[10, 20, <NA>, 40]: max at %s (the null-dropped array says 2)"
          % got["stats"]["argmax"])
    nan = handle("S.allnan", "Synth")
    got, _ = read("table.stats", {"h": nan["h"]}, "Synth")
    check("an all-NaN column points nowhere: null, not 0",
          got["stats"]["argmin"] is None
          and got["stats"]["argmax_label"] is None,
          json.dumps({k: got["stats"][k] for k in ("count", "argmin",
                                                   "argmax_label")}))
    ix = handle("S.labels", "Synth")
    got, _ = read("table.stats", {"h": ix["h"]}, "Synth")
    check("an Index has positions only: its labels are null",
          got["stats"]["argmin"] == 1 and got["stats"]["argmax"] == 0
          and got["stats"]["argmin_label"] is None, "")

    # The first cut encoded a label with `codec.encode`, which makes a handle of
    # an encoding over 8,192 bytes: cells.page then minted one, against 13.2.
    space.bigkey("a" * 4500, "b" * 4500)
    space.bigkey("c" * 4500, "d" * 4501)
    space.bigkey("e" * 10, "f" * 10)
    got, minted = read("cells.page", {"obj": "S.bigkey"}, "Synth")
    big, small = got["stats"]["argmax_label"], got["stats"]["argmin_label"]
    check("A LABEL OVER 8 KB: cells.page STILL MINTS NO HANDLE",
          minted == 0, "%d minted for argmax %s" % (minted, got["stats"]["argmax"]))
    check("...the label is an opaque tag with its repr, not a handle",
          isinstance(big, dict) and big.get("$t") == "opaque"
          and big.get("py") == "builtins.tuple"
          and big.get("repr", "").startswith("('cccc"), json.dumps(big)[:60])
    check("...and a short label is unchanged: a tuple tag",
          small == {"$t": "tuple", "v": ["e" * 10, "f" * 10]},
          json.dumps(small)[:60])
    back, _ = read("cells.page", {"obj": "S.bigkey", "around": small["v"]},
                   "Synth")
    check("...whose elements, sent back as around, land on argmin",
          back["focus_row"] == got["stats"]["argmin"],
          "focus_row %s, argmin %s" % (back["focus_row"], got["stats"]["argmin"]))
    for cells, py in (("longlabels", "builtins.tuple"),
                      ("arraylabels", "numpy.ndarray")):
        labelled = handle("S." + cells, "Synth")
        got, minted = read("table.stats", {"h": labelled["h"]}, "Synth")
        check("table.stats over %s mints no handle either" % cells,
              minted == 0 and got["stats"]["argmax_label"].get("$t") == "opaque"
              and got["stats"]["argmax_label"].get("py") == py,
              "%d minted, %s" % (minted, json.dumps(
                  got["stats"]["argmax_label"])[:50]))

    # -- [D] -------------------------------------------------------------------
    print("\n[D] B5 trace.values: neighbours without values mint no handle")
    full, minted_full = read("trace.preds", {"obj": "Projection.pv_claims",
                                             "evaluate": False})
    bare, minted_bare = read("trace.preds", {"obj": "Projection.pv_claims",
                                             "evaluate": False,
                                             "values": False})
    check("pv_claims(): the measured 123 precedents either way",
          len(full["preds"]) == PV_CLAIMS_PREDS
          and len(bare["preds"]) == PV_CLAIMS_PREDS,
          "%d / %d" % (len(full["preds"]), len(bare["preds"])))
    check("with values, every entry carries one",
          all("value" in p for p in full["preds"]) and "value" in full["node"],
          "")
    check("WITHOUT, NO ENTRY AND NOT THE NODE CARRIES THE KEY",
          not any("value" in p for p in bare["preds"])
          and "value" not in bare["node"], "")
    check("AND THE CODEC'S HANDLE COUNTER DID NOT MOVE",
          minted_bare == 0, "%d minted (with values: %d, for disc_factors())"
          % (minted_bare, minted_full))
    strip = [dict((k, v) for k, v in p.items() if k != "value")
             for p in full["preds"]]
    check("everything else is identical, predslen and succslen included",
          strip == bare["preds"], "")
    check("...and the reply is smaller",
          len(json.dumps(bare)) < len(json.dumps(full)),
          "%d -> %d bytes" % (len(json.dumps(full)), len(json.dumps(bare))))
    down, minted = read("trace.succs", {"obj": "Projection.disc_factors",
                                        "args": [], "values": False})
    check("trace.succs takes it too: no value, no handle",
          down["cached"] is True and down["succs"]
          and not any("value" in s for s in down["succs"]) and minted == 0,
          "%d dependents" % len(down["succs"]))
    cold, _ = read("trace.preds", {"obj": "Projection.claims", "args": [500],
                                   "evaluate": False, "values": False})
    check("an uncomputed node: cached false, still no value key",
          cold["cached"] is False and "value" not in cold["node"]
          and "preds" not in cold, json.dumps(cold["node"])[:60])
    synth_before = traced(synth)
    warm = call("trace.preds", {"obj": "S.g", "args": [], "values": False},
                "Synth")
    check("the evaluating trace.preds honours it as well",
          warm["cached"] is True and "value" not in warm["node"]
          and not any("value" in p for p in warm["preds"])
          and traced(synth) > synth_before, "it computed g() and answered bare")
    refused("a non-boolean values is bad_request", "bad_request",
            lambda: call("trace.succs", {"obj": "Projection.claims",
                                         "args": [3], "values": "no"}))
    # The first cut read it through `_flag`, which takes null as the default:
    # `values: null` answered WITH every value, while `evaluate: null` beside
    # it is refused.
    for method, more in (("trace.succs", {}),
                         ("trace.preds", {"evaluate": False})):
        refused("values: null is bad_request on %s, as evaluate: null is"
                % method, "bad_request",
                lambda m=method, p=dict(more, obj="Projection.claims",
                                        args=[3], values=None): call(m, p))

    # -- [E] -------------------------------------------------------------------
    print("\n[E] B6 cells.page.element: one element of each cached value")
    got, minted = read("cells.page", {"obj": "S.v", "element": 2}, "Synth")
    values = got["page"]["columns"][1]["values"]
    check("element 2 of v(0..4) is one scalar per t",
          values == [0.0, -2.0, -4.0, -6.0, -8.0]
          and got["scalar_values"] is True and got["value_dtype"] == "float64",
          json.dumps(values))
    check("with the whole column's stats and where the minimum is",
          got["stats"]["sum"] == -20.0 and got["stats"]["argmin"] == 4
          and got["stats"]["argmin_label"] == np_tag(4),
          json.dumps({k: got["stats"][k] for k in ("sum", "min", "argmin")}))
    check("the reply says which element, and that none was missing",
          got["element"] == 2 and got["element_missing"] == 0
          and got["n_cached"] == 5,
          json.dumps({k: got[k] for k in ("element", "element_missing")}))
    check("keys are the Cells' own: t = 0..4",
          got["keys"] == {"integer": True, "min": "0", "max": "4", "gaps": 0},
          json.dumps(got["keys"]))
    check("it minted no handle", minted == 0, str(minted))
    got, _ = read("cells.page", {"obj": "S.v", "element": 9}, "Synth")
    check("an element no value carries: all 5 missing, count 0",
          got["element_missing"] == 5 and got["stats"]["count"] == 0
          and got["stats"]["argmin"] is None,
          json.dumps({k: got["stats"][k] for k in ("n", "count", "nulls")}))
    for t in range(4):
        space.ragged(t)
    got, _ = read("cells.page", {"obj": "S.ragged", "element": 2}, "Synth")
    check("a value without the label is a NaN in the column, and counted",
          got["element_missing"] == 2 and got["stats"]["count"] == 2
          and got["stats"]["n"] == 4,
          "missing for t = 1, 3: %s" % json.dumps(
              got["page"]["columns"][1]["values"]))
    refused("a label that cannot be one is bad_request, not internal",
            "bad_request",
            lambda: call("cells.page", {"obj": "S.v", "element": [1, 2]},
                         "Synth"))
    refused("a Cells of scalars is bad_request", "bad_request",
            lambda: call("cells.page", {"obj": "S.f", "element": 1}, "Synth"))
    refused("the shipped claims is a Cells of scalars too", "bad_request",
            lambda: call("cells.page", {"obj": "Projection.claims",
                                        "element": 1}))
    for cells, why in (("twice", "a label that selects two rows"),
                       ("frames", "DataFrame values"),
                       ("arrays", "ndarray values")):
        getattr(space, cells)(0)
        message = refused("%s is refused by name" % why, "bad_request",
                          lambda c=cells: call("cells.page",
                                               {"obj": "S." + c, "element": 2},
                                               "Synth"))
        check("...and the sentence names what it found",
              ("2 rows" in message) if cells == "twice"
              else (("DataFrame" in message) if cells == "frames"
                    else "ndarray" in message), message[:60])
    # A leading-level label of a MultiIndex matching ONE row: the first cut
    # refused it as "label 2 selects 1 rows ..., not one element".
    for t in range(3):
        space.levels(t)
        space.pairs(t)
    message = refused("a partial MultiIndex key is refused", "bad_request",
                      lambda: call("cells.page", {"obj": "S.levels",
                                                  "element": 2}, "Synth"))
    check("...AS A PARTIAL KEY, naming the levels, even for one row",
          "partial key" in message and "(pt, kind)" in message
          and "selects 1 row of" in message, message[:70])
    got, _ = read("cells.page", {"obj": "S.levels",
                                 "element": {"$t": "tuple", "v": [2, "y"]}},
                  "Synth")
    check("...and the full label it asks for reads the element",
          got["page"]["columns"][1]["values"] == [0.0, 2.0, 4.0],
          json.dumps(got["page"]["columns"][1]["values"]))
    try:
        got, _ = read("cells.page", {"obj": "S.pairs",
                                     "element": {"$t": "tuple", "v": [1, 2]}},
                      "Synth")
        check("a tuple label on a FLAT index of tuples reads the element",
              got["page"]["columns"][1]["values"] == [0.0, 1.0, 2.0],
              json.dumps(got["page"]["columns"][1]["values"]))
    except Exception as exc:
        # `.loc[(1, 2)]` raised IndexingError there: `internal`, not a refusal.
        check("a tuple label on a FLAT index of tuples reads the element",
              False, "%s: %s" % (type(exc).__name__, str(exc)[:50]))
    try:
        got, _ = read("cells.page", {"obj": "S.never", "element": 1}, "Synth")
        check("AN EMPTY CACHE IS AN EMPTY COLUMN, NOT A REFUSAL",
              got["n_cached"] == 0 and got["element_missing"] == 0
              and got["page"]["rows"] == 0,
              "the first cut blamed the values' type")
    except Exception as exc:
        check("AN EMPTY CACHE IS AN EMPTY COLUMN, NOT A REFUSAL", False,
              str(exc)[:70])
    got, _ = read("cells.page", {"obj": "S.g", "element": 1}, "Synth")
    check("a zero-parameter Cells: the note, element echoed, missing null",
          got["page"] is None and "note" in got and got["element"] == 1
          and got["element_missing"] is None, "")
    plain, _ = read("cells.page", {"obj": "S.v"}, "Synth")
    nulled, _ = read("cells.page", {"obj": "S.v", "element": None}, "Synth")
    check("element: null is the request without it",
          nulled == plain and "element" not in plain, "")

    # -- [F] -------------------------------------------------------------------
    print("\n[F] B7 table.get.label: a page that starts at a label")
    got, _ = read("table.get", {"h": rate["h"], "label": 10, "rows": 2})
    check("disc_rate_ann label 10 is row 10, and focus_row says so",
          got["row"] == 10 and got["focus_row"] == 10,
          json.dumps({k: got[k] for k in ("row", "rows", "focus_row")}))
    check("...one row carries it: label_matches 1",
          got.get("label_matches") == 1, repr(got.get("label_matches")))
    check("its values are years 10 and 11",
          got["columns"][0]["values"] == DISC_RATE_10
          and got["index"]["values"] == [10, 11],
          json.dumps(got["columns"][0]["values"]))
    got, _ = read("table.get", {"h": table["h"], "label": 7, "rows": 1})
    check("model_point_table label 7 is row 6: labels are not positions",
          got["row"] == 6 and got["index"]["values"] == [7]
          and got["columns"][0]["values"]
          == [int(model.Projection.model_point_table.loc[7, "age_at_entry"])],
          json.dumps(got["index"]["values"]))
    message = refused("a label not in the index is not_found", "not_found",
                      lambda: call("table.get", {"h": rate["h"],
                                                 "label": 999}))
    check("...naming it", "999" in message, message)
    refused("label with row is bad_request", "bad_request",
            lambda: call("table.get", {"h": rate["h"], "label": 10, "row": 3}))
    refused("an ndarray has no labels: bad_request", "bad_request",
            lambda: call("table.get", {"h": factors["h"], "label": 3}))
    refused("nor does an Index", "bad_request",
            lambda: call("table.get", {"h": ix["h"], "label": 3}, "Synth"))
    refused("a label that cannot be one is bad_request, not internal",
            "bad_request",
            lambda: call("table.get", {"h": rate["h"], "label": [1, 2]}))
    multi = handle("S.multi", "Synth")
    got, _ = read("table.get", {"h": multi["h"], "rows": 1,
                                "label": {"$t": "tuple", "v": [2, "a"]}},
                  "Synth")
    check("a MultiIndex label is a tuple tag: (2, 'a') is row 2",
          got["focus_row"] == 2, repr(got["focus_row"]))
    got, _ = read("table.get", {"h": multi["h"], "label": 1}, "Synth")
    check("a leading-level label lands on the FIRST row it matches",
          got["focus_row"] == 0 and got.get("label_matches") == 2,
          "focus_row %r, label_matches %r" % (got["focus_row"],
                                              got.get("label_matches")))
    repeats = handle("S.repeats", "Synth")
    got, _ = read("table.get", {"h": repeats["h"], "label": 7}, "Synth")
    check("so does a repeated label: 7 is rows 1 and 2, the page starts at 1",
          got["focus_row"] == 1 and got["index"]["values"][:2] == [7, 7]
          and got.get("label_matches") == 2,
          json.dumps(got["index"]["values"]))
    # The first cut said "the page shows the rest". Repeats far apart do not
    # share a page, and nothing in the reply said there were more.
    apart = handle("S.apart", "Synth")
    got, _ = read("table.get", {"h": apart["h"], "label": 7}, "Synth")
    check("REPEATS FAR APART: the page holds one 7, label_matches says 2",
          got["focus_row"] == 0 and got["index"]["values"].count(7) == 1
          and got.get("label_matches") == 2,
          "rows %d..%d, %d 7s, label_matches %r"
          % (got["row"], got["row"] + got["rows"] - 1,
             got["index"]["values"].count(7), got.get("label_matches")))

    # -- [H] -------------------------------------------------------------------
    print("\n[H] B9 handle.index_name: what the labels are")
    check("disc_rate_ann's labels are years", rate.get("index_name") == "year",
          repr(rate.get("index_name")))
    check("model_point_table's are point_id",
          table.get("index_name") == "point_id", repr(table.get("index_name")))
    point = handle("Projection.model_point")
    check("model_point() is a Series with an UNNAMED index: null, present",
          "index_name" in point and point["index_name"] is None,
          "%s, %s" % (point["kind"], point.get("index_name")))
    check("disc_factors() is an ndarray: it has no index, and no key",
          factors["kind"] == "ndarray" and "index_name" not in factors, "")
    check("v(1) of the synthetic model: point",
          handle("S.v", "Synth", [1]).get("index_name") == "point", "")
    check("a MultiIndex: its names in level order",
          multi.get("index_name") == "n, s", repr(multi.get("index_name")))
    check("an unnamed level keeps its place",
          handle("S.half", "Synth").get("index_name") == "None, s",
          repr(handle("S.half", "Synth").get("index_name")))
    check("an Index handle carries none", "index_name" not in ix, "")

    # -- [I] continued -------------------------------------------------------
    print("\n[I] B2, a canonical display on value.get, is DEFERRED (protocol "
          "section 18.8): one node, two displays, one node")
    entry = call("value.get", {"nodes": [{"obj": "S.f", "args": [3]}],
                               "evaluate": False}, "Synth")["values"][0]
    named = call("value.get", {"nodes": [{"obj": "S.f", "args": [3, None]}],
                               "evaluate": False}, "Synth")["values"][0]
    preds = call("trace.preds", {"obj": "S.g", "args": [],
                                 "evaluate": False}, "Synth")["preds"]
    check("B2 DEFERRED, still open: value.get [3] displays f(t=3)",
          entry["display"] == "Synth.S.f(t=3)" and entry["args"] == [3],
          "%s %s" % (entry["display"], entry["args"]))
    check("B2 DEFERRED, still open: the trace displays f(t=3, kind=None)",
          [p["display"] for p in preds] == ["Synth.S.f(t=3, kind=None)"]
          and preds[0]["args"] == [3, None],
          json.dumps([(p["display"], p["args"]) for p in preds]))
    check("BOTH FORMS ADDRESS ONE NODE: [3, null] reads the value [3] cached",
          named["cached"] is True and named["value"] == entry["value"]
          and named["display"] == preds[0]["display"],
          "%s = %s" % (named["display"], json.dumps(named["value"])))
    short = call("trace.succs", {"obj": "S.f", "args": [3]}, "Synth")
    long = call("trace.succs", {"obj": "S.f", "args": [3, None]}, "Synth")
    check("...and trace.succs answers the same node for either",
          short["node"] == long["node"] and short["succs"] == long["succs"]
          and short["node"]["args"] == [3, None],
          short["node"]["display"])

    # -- [J] -------------------------------------------------------------------
    print("\n[J] the defaults the shipped frontend reads are unchanged")
    entry = call("value.get", {"nodes": [{"obj": "Projection.claims",
                                          "args": [3]}]})["values"][0]
    check("a value.get entry has exactly its 0.9.0 keys",
          sorted(entry) == ["args", "cached", "display", "ok", "predslen",
                            "succslen", "value"], ", ".join(sorted(entry)))
    trace = call("trace.preds", {"obj": "Projection.claims", "args": [3]})
    check("a trace entry has exactly its 0.9.0 keys",
          all(sorted(p) == ["args", "display", "obj", "predslen",
                            "succslen", "value"] for p in trace["preds"]), "")
    paged = call("table.get", {"h": rate["h"], "rows": 2})
    check("table.get without label: no focus_row or label_matches, row 0",
          "focus_row" not in paged and "label_matches" not in paged
          and paged["row"] == 0, "")
    nulled = call("table.get", {"h": rate["h"], "rows": 2, "label": None})
    check("...and label: null is the same request", nulled == paged, "")
    page = call("cells.page", {"obj": "Projection.claims"})
    check("cells.page without element: no element keys",
          "element" not in page and "element_missing" not in page, "")
    entry = info(name)
    check("session.info's model entry: the 0.9.0 keys plus computed",
          sorted(entry) == ["computed", "dirty", "name", "path", "revision",
                            "sample"], ", ".join(sorted(entry)))

    # -- [K] -------------------------------------------------------------------
    print("\n[K] the evaluation policy, over every read this suite made")
    moved = [row for row in deltas if row[2] or row[3]]
    check("NOT ONE READ ADDED A TRACEGRAPH NODE TO EITHER MODEL",
          moved == [], "%d reads; %d moved %s"
          % (len(deltas), len(moved), json.dumps(moved[:4])))

    synth.close()
    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
