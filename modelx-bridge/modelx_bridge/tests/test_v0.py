"""In-process exercise of every v0 method against a real modelx model.

Run with any interpreter that can import modelx_bridge:

    python -m modelx_bridge.tests.test_v0

No test framework on purpose: this has to run inside a Pyodide kernel too, where
there is no pytest. Every assertion prints the number it checked.

This suite runs against the SYNTHETIC fixture (`termlife_synthetic`), which has
no file dependencies, so the protocol and codec are exercised without pandas,
openpyxl or shipped content. The demo model that visitors actually see is
lifelib's BasicTerm_S -- tests/test_model.py checks that one against native
ground truth, and tests/test_events.py checks the event semantics.
"""

import json
import sys
import time

from modelx_bridge import Bridge, BridgeError
from modelx_bridge.codec import Codec
from modelx_bridge.methods import _resolve

FAILURES = []
NUMBERS = {}


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    print("%s %-46s %s" % (status, label, detail))


def strict_json(payload, label):
    """json.dumps with no default= fallback: the codec must have left nothing
    that the standard encoder cannot serialise."""
    try:
        text = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        check(label + " json.dumps", False, repr(exc))
        return ""
    check(label + " json.dumps", True, "%d bytes, no default=" % len(text))
    return text


def main():
    import modelx as mx

    bridge = Bridge()
    print("modelx %s, python %s\n" % (mx.__version__, sys.version.split()[0]))

    # -- session.info / hello ---------------------------------------------
    info = bridge.dispatch("session.info", {})
    check("session.info protocol", info["protocol"] == 0, repr(info["protocol"]))
    check("session.info runtime", info["runtime"] in ("pyodide", "cpython"),
          info["runtime"])
    check("session.info no models yet", info["models"] == [], repr(info["models"]))
    check("session.info limits", info["limits"]["max_inline_bytes"] == 8192,
          json.dumps(info["limits"]))
    # The id is per INTERPRETER, not per Bridge: a frontend compares it across a
    # restart, and a second Bridge in the same kernel -- which is what a
    # bootstrap re-run produces -- must not look like a new kernel.
    check("session.info identifies the interpreter",
          isinstance(info["kernel"]["id"], str)
          and len(info["kernel"]["id"]) >= 12
          and info["kernel"]["boots"] >= 1
          and info["kernel"]["uptime"] >= 0,
          json.dumps(info["kernel"]))
    check("a second Bridge in the same kernel reports the SAME id",
          Bridge().dispatch("session.info", {})["kernel"]["id"]
          == info["kernel"]["id"], "one interpreter, one id")
    check("session.info advertises the backup and identity extensions",
          {"save.backup", "kernel.identity"} <= set(info["features"]),
          json.dumps(info["features"]))
    hello = bridge.hello()
    check("hello envelope", hello["type"] == "hello" and "result" in hello
          and "id" not in hello, sorted(hello))
    strict_json(hello, "hello")

    # -- model.open_sample -------------------------------------------------
    t0 = time.perf_counter()
    opened = bridge.dispatch("model.open_sample", {"sample": "termlife_synthetic"})
    NUMBERS["open_sample_ms"] = (time.perf_counter() - t0) * 1000
    check("model.open_sample", opened["model"] == "TermLife_S",
          "%s rev %d in %.1f ms" % (opened["model"], opened["revision"],
                                    NUMBERS["open_sample_ms"]))
    again = bridge.dispatch("model.open_sample", {"sample": "termlife_synthetic"})
    check("model.open_sample idempotent", again == opened, json.dumps(again))
    try:
        bridge.dispatch("model.open_sample", {"sample": "nope"})
        check("unknown sample -> not_found", False)
    except BridgeError as err:
        check("unknown sample -> not_found", err.code == "not_found", err.message)

    model = mx.get_models()["TermLife_S"]

    # -- tree.get ----------------------------------------------------------
    t0 = time.perf_counter()
    tree = bridge.dispatch("tree.get", {})
    NUMBERS["tree_ms"] = (time.perf_counter() - t0) * 1000
    text = strict_json(tree, "tree.get")
    NUMBERS["tree_bytes"] = len(text)
    root = tree["root"]
    check("tree root is the model", root["kind"] == "Model" and root["obj"] == "",
          root["display"])
    space = root["spaces"][0]
    check("tree space display", space["display"] == "Projection[point_id]",
          space["display"])
    check("tree space parameters", space["parameters"] == ["point_id"],
          repr(space["parameters"]))
    names = [c["name"] for c in space["cells"]]
    check("tree cells", len(names) == 13, "%d cells: %s" % (len(names), ", ".join(names)))
    claims = [c for c in space["cells"] if c["name"] == "claims"][0]
    check("tree cells display", claims["display"] == "claims(t)", claims["display"])
    check("tree cells cached=0 before evaluation", claims["cached"] == 0,
          str(claims["cached"]))
    check("tree cells has_formula", claims["has_formula"] is True)
    refs = sorted(r["name"] for r in space["refs"])
    check("tree refs", refs == ["disc_rate", "expense_pp", "model_points",
                                "mort_table", "point_id"], ", ".join(refs))
    mt = [r for r in space["refs"] if r["name"] == "mort_table"][0]
    check("tree ref value_type", mt["value_type"] == "dict", mt["value_type"])
    check("tree.get never evaluates", len(model.Projection.claims) == 0,
          "cached=%d" % len(model.Projection.claims))

    depth1 = bridge.dispatch("tree.get", {"obj": "", "depth": 1})
    check("tree.get depth=1 stops", depth1["root"]["spaces"][0]["cells"] == [],
          "%d bytes" % len(json.dumps(depth1)))
    sub = bridge.dispatch("tree.get", {"obj": "Projection"})
    check("tree.get obj subtree", sub["root"]["name"] == "Projection",
          sub["root"]["obj"])

    # -- formula.get -------------------------------------------------------
    formula = bridge.dispatch("formula.get", {"obj": "Projection.pols_if"})
    strict_json(formula, "formula.get")
    check("formula.get source verbatim",
          formula["source"].startswith("def pols_if(t):")
          and '"""' in formula["source"], repr(formula["source"][:34]))
    check("formula.get doc", formula["doc"].startswith("Policies in force"),
          repr(formula["doc"][:40]))
    try:
        bridge.dispatch("formula.get", {"obj": "Projection.nosuch"})
        check("formula.get unknown obj -> not_found", False)
    except BridgeError as err:
        check("formula.get unknown obj -> not_found", err.code == "not_found",
              err.message)

    # -- value.get ---------------------------------------------------------
    lazy = bridge.dispatch("value.get", {
        "evaluate": False,
        "nodes": [{"obj": "Projection.pv_net_cf", "args": []}]})
    entry = lazy["values"][0]
    check("value.get evaluate=false is not-computed",
          entry["ok"] and entry["cached"] is False and entry["value"] is None,
          json.dumps(entry))
    check("value.get evaluate=false does not bump revision",
          bridge.revision(model) == 1, "revision %d" % bridge.revision(model))

    model.Projection.big_table = list(range(5000))   # forces the handle path
    t0 = time.perf_counter()
    values = bridge.dispatch("value.get", {"nodes": [
        {"obj": "Projection.pv_net_cf", "args": []},
        {"obj": "Projection.claims", "args": [0]},
        {"obj": "Projection.pols_if", "args": [120]},
        {"obj": "Projection.disc_rate", "args": []},
        {"obj": "Projection.mort_table", "args": []},
        {"obj": "Projection.big_table", "args": []},
        {"obj": "Projection.claims", "args": ["not an int"]},
        {"obj": "Projection.nosuch", "args": []},
    ]})
    NUMBERS["value_ms"] = (time.perf_counter() - t0) * 1000
    strict_json(values, "value.get")
    got = values["values"]
    check("value.get returns one entry per node in order", len(got) == 8,
          "%d entries in %.1f ms" % (len(got), NUMBERS["value_ms"]))
    NUMBERS["pv_net_cf"] = got[0].get("value")
    check("value.get pv_net_cf", got[0]["ok"] and isinstance(got[0].get("value"), float),
          json.dumps(got[0])[:140])
    check("value.get claims(t=0) display",
          got[1]["display"] == "TermLife_S.Projection.claims(t=0)", got[1]["display"])
    check("value.get pols_if(120) recursed",
          got[2]["ok"] and 0.0 < got[2].get("value", -1) < 1.0,
          json.dumps(got[2])[:140])
    check("value.get reference display",
          got[3]["ok"] and got[3]["display"] == "TermLife_S.Projection.disc_rate",
          json.dumps(got[3])[:140])
    check("value.get int-keyed dict ref is a tagged dict",
          isinstance(got[4].get("value"), dict)
          and got[4]["value"].get("$t") == "dict",
          json.dumps(got[4].get("value"))[:90])
    check("value.get oversized ref becomes a handle",
          got[5]["ok"] and got[5]["value"].get("$t") == "handle"
          and got[5]["value"]["kind"] == "list",
          json.dumps({k: v for k, v in got[5]["value"].items()
                      if k != "repr"})[:110])
    check("value.get bad args -> per-node error, batch survives",
          got[6]["ok"] is False and got[6]["error"]["code"] in
          ("formula_error", "bad_request", "internal"),
          got[6]["error"]["code"] + ": " + got[6]["error"]["message"][:60])
    check("value.get unknown obj -> per-node not_found",
          got[7]["ok"] is False and got[7]["error"]["code"] == "not_found",
          got[7]["error"]["message"])
    check("value.get bumped revision once", bridge.revision(model) == 2,
          "revision %d" % bridge.revision(model))
    events = bridge.drain_events()
    check("one model.changed evt per evaluating dispatch", len(events) == 1
          and events[0]["params"]["reason"] == "evaluate", json.dumps(events))

    # -- trace.preds -------------------------------------------------------
    t0 = time.perf_counter()
    trace = bridge.dispatch("trace.preds", {"obj": "Projection.claims", "args": [3]})
    NUMBERS["trace_ms"] = (time.perf_counter() - t0) * 1000
    strict_json(trace, "trace.preds")
    node = trace["node"]
    check("trace.preds node display",
          node["display"] == "TermLife_S.Projection.claims(t=3)", node["display"])
    pred_names = [p["obj"] for p in trace["preds"]]
    check("trace.preds precedents",
          pred_names == ["Projection.model_point", "Projection.pols_death"],
          "%s in %.1f ms" % (", ".join(pred_names), NUMBERS["trace_ms"]))
    check("trace.preds carries predslen/succslen",
          all("predslen" in p and "succslen" in p for p in trace["preds"]),
          json.dumps([[p["predslen"], p["succslen"]] for p in trace["preds"]]))
    check("trace.preds echoes args", node["args"] == [3], json.dumps(node["args"]))

    # a formula that raises
    model.Projection.new_cells(name="boom", formula="def boom(t):\n    return 1 / 0\n")
    try:
        bridge.dispatch("trace.preds", {"obj": "Projection.boom", "args": [1]})
        check("formula_error", False)
    except BridgeError as err:
        check("formula_error code", err.code == "formula_error", err.message)
        check("formula_error carries modelx traceback",
              "Formula traceback" in err.data["formula_traceback"]
              and err.data["error_obj"] == "Projection.boom",
              json.dumps(err.data)[:120])

    # -- envelope / dispatcher ---------------------------------------------
    res = bridge.handle({"type": "req", "id": "c1", "method": "session.info",
                         "params": {}})
    check("handle() res envelope",
          res["type"] == "res" and res["id"] == "c1" and "result" in res
          and "error" not in res, sorted(res))
    res = bridge.handle({"type": "req", "id": "c2", "method": "nope", "params": {}})
    check("unknown method -> bad_request",
          res["error"]["code"] == "bad_request", res["error"]["message"])
    check("non-req dropped", bridge.handle({"type": "res", "id": "x"}) is None)
    check("req without id dropped",
          bridge.handle({"type": "req", "method": "session.info"}) is None)
    res = bridge.handle({"type": "req", "id": "c3", "method": "tree.get",
                         "params": "not an object"})
    check("ill-typed params -> bad_request",
          res["error"]["code"] == "bad_request", res["error"]["message"])

    # no model open at all
    fresh = Bridge()
    name = model.name
    model.close()
    try:
        fresh.dispatch("tree.get", {})
        check("no model open -> no_model", False)
    except BridgeError as err:
        check("no model open -> no_model", err.code == "no_model", err.message)
    from modelx_bridge import build_sample
    model = build_sample("termlife_synthetic")
    check("sample rebuilt after close", model.name == name, model.name)

    # two models open
    other = mx.new_model(name="Other")
    try:
        fresh.dispatch("tree.get", {})
        check("ambiguous model -> bad_request", False)
    except BridgeError as err:
        check("ambiguous model -> bad_request", err.code == "bad_request", err.message)
    check("explicit model disambiguates",
          fresh.dispatch("tree.get", {"model": "TermLife_S"})["model"] == "TermLife_S")
    other.close()

    # -- post_execute ------------------------------------------------------
    # Full event semantics, including the feedback loop, live in test_events.py.
    bridge.prime()
    bridge.drain_events()
    before = bridge.revision(model)
    bridge.on_execute()
    check("on_execute on an unchanged model emits nothing",
          bridge.drain_events() == [] and bridge.revision(model) == before,
          "revision %d" % bridge.revision(model))
    model.Projection.pv_net_cf()          # stands in for a user's Console cell
    bridge.on_execute()
    executed = bridge.drain_events()
    check("on_execute bumps and emits model.changed",
          bridge.revision(model) == before + 1 and len(executed) == 1
          and executed[0]["params"]["reason"] == "execute", json.dumps(executed))

    codec_tests()
    itemspace_tests(bridge, model)
    regression_tests(bridge, model)

    print("\n--- numbers ---")
    for key in sorted(NUMBERS):
        print("  %-18s %s" % (key, NUMBERS[key]))
    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


def codec_tests():
    import datetime
    print()
    codec = Codec()

    check("codec passes JSON natives",
          codec.encode({"a": 1, "b": [True, None, "x"], "c": 1.5})
          == {"a": 1, "b": [True, None, "x"], "c": 1.5})
    check("codec tags tuple",
          codec.encode((1, "a")) == {"$t": "tuple", "v": [1, "a"]})
    check("codec tags non-finite floats",
          codec.encode([float("nan"), float("inf"), float("-inf")])
          == [{"$t": "num", "v": "NaN"}, {"$t": "num", "v": "Infinity"},
              {"$t": "num", "v": "-Infinity"}])
    check("codec tags datetime",
          codec.encode(datetime.date(2026, 9, 22)) == {"$t": "date", "v": "2026-09-22"})
    long_str = "x" * 5000
    enc = codec.encode(long_str)
    check("codec truncates long strings",
          enc["$t"] == "str" and enc["len"] == 5000 and len(enc["v"]) == 4096,
          json.dumps({k: v for k, v in enc.items() if k != "v"}))
    enc = codec.encode({"$t": "not a tag"})
    check("codec disambiguates a dict containing $t",
          enc["$t"] == "dict" and enc["items"] == [["$t", "not a tag"]],
          json.dumps(enc))
    enc = codec.encode({(1, 2): "tuplekey"})
    check("codec tags non-string keys",
          enc["$t"] == "dict" and enc["items"][0][0] == {"$t": "tuple", "v": [1, 2]},
          json.dumps(enc))
    import decimal
    enc = codec.encode(decimal.Decimal("1.5"))
    check("codec opaque fallback",
          enc == {"$t": "opaque", "py": "decimal.Decimal", "repr": "Decimal('1.5')"},
          json.dumps(enc))

    class Exploding:
        def __repr__(self):
            raise RuntimeError("boom")

    enc = codec.encode(Exploding())
    check("codec never raises on a broken __repr__",
          enc["$t"] == "opaque" and "unreprable" in enc["repr"], enc["repr"])

    cyclic = []
    cyclic.append(cyclic)
    enc = codec.encode(cyclic)
    strict_json(enc, "codec cyclic list")

    big = list(range(5000))
    enc = codec.encode(big)
    check("oversized value becomes a handle",
          enc["$t"] == "handle" and enc["kind"] == "list", enc["repr"][:40])

    try:
        import numpy as np
    except ImportError:
        print("skip numpy codec checks (numpy not installed)")
        np = None
    if np is not None:
        enc = codec.encode(np.float64(34.18079328868595))
        check("codec tags numpy scalar",
              enc == {"$t": "np", "dtype": "float64", "v": 34.18079328868595},
              json.dumps(enc))
        enc = codec.encode(np.float64("nan"))
        check("codec re-tags a non-finite numpy scalar",
              enc == {"$t": "np", "dtype": "float64",
                      "v": {"$t": "num", "v": "NaN"}}, json.dumps(enc))
        enc = codec.encode(np.arange(6.0).reshape(3, 2))
        check("ndarray is always a handle",
              enc["$t"] == "handle" and enc["shape"] == [3, 2]
              and enc["preview"] == [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]],
              json.dumps(enc["preview"]))

    try:
        import pandas as pd
    except ImportError:
        print("skip pandas codec checks (pandas not installed)")
        return
    series = pd.Series([0.0, 0.00555, 0.00684], name="zero_spot")
    enc = codec.encode(series)
    strict_json(enc, "pandas Series handle")
    check("small Series is still a handle",
          enc["$t"] == "handle" and enc["kind"] == "Series"
          and enc["shape"] == [3] and enc["columns"] == ["zero_spot"],
          json.dumps({k: enc[k] for k in ("h", "dtype", "shape", "columns",
                                          "index_preview", "preview")}))
    frame = pd.DataFrame({"a": range(30), "b": [float(i) / 3 for i in range(30)]})
    enc = codec.encode(frame)
    check("DataFrame preview capped at 10 rows",
          enc["shape"] == [30, 2] and len(enc["preview"]) == 10,
          "%s cols=%s" % (enc["shape"], enc["columns"]))

    lru = Codec(max_handles=2)
    ids = [lru.handle(object())["h"] for _ in range(3)]
    check("handle store is an LRU of max_handles",
          list(lru.handles) == ids[1:], "kept %s of %s" % (list(lru.handles), ids))


def itemspace_tests(bridge, model):
    print()
    space = model.Projection
    item = space[1]
    item.claims(0)
    tree = bridge.dispatch("tree.get", {})
    proj = tree["root"]["spaces"][0]
    # ITEMSPACES ARE NAMED THE WAY A READER NEEDS (0.8.0). This used to assert
    # `["__Space1"]` - modelx's internal bookkeeping, which is what the
    # Explorer printed, to a visitor who had computed `Projection[1]`. It now
    # carries the repr modelx itself uses, the arguments, and an `obj` that
    # round-trips through `_resolve`; `total` is the honest count even when the
    # list is bounded. Still no recursion: reaching inside one is section 6.3's
    # deliberate refusal and is not changed here.
    items = proj["itemspaces"]
    check("tree reports itemspaces as objects, with a total",
          items["total"] == 1 and len(items["shown"]) == 1,
          json.dumps(items))
    one = items["shown"][0]
    check("...named as modelx names it, not as it stores it",
          one["display"] == "Projection[1]" and one["name"] == "__Space1",
          json.dumps(one))
    check("...carrying the arguments that identify it",
          one["args"] == [1], json.dumps(one["args"]))
    check("...and an obj the kernel can resolve back",
          _resolve(model, one["obj"])._get_repr() == "Projection[1]",
          one["obj"])
    check("and the tree still does not recurse into it",
          proj["spaces"] == [], json.dumps(proj["spaces"]))
    obj = item.claims._idstr
    check("itemspace member namedid is the internal name",
          obj == "Projection.__Space1.claims", obj)
    values = bridge.dispatch("value.get", {"nodes": [{"obj": obj, "args": [0]}]})
    entry = values["values"][0]
    check("itemspace node round-trips through the wire obj",
          entry["ok"] and entry["display"]
          == "TermLife_S.Projection[1].claims(t=0)", entry["display"])
    NUMBERS["itemspace_claims_0"] = entry["value"]


def regression_tests(bridge, model):
    """One check per fixed review finding, so none of them come back silently."""
    print()

    # [LOW] methods._display: repr_parent() is "" for a Model, and joining with a
    # bare "." put a leading dot on the wire (".TermLife_S has no formula").
    try:
        bridge.dispatch("formula.get", {"obj": ""})
        check("model-level formula.get -> not_found", False)
    except BridgeError as err:
        check("model-level formula.get has no leading dot",
              err.code == "not_found" and err.message.startswith("TermLife_S"),
              err.message)

    # [LOW] methods._make_node: _objid(model) is "", so the message opened with
    # a space (" does not take arguments").
    entry = bridge.dispatch("value.get", {"nodes": [{"obj": "", "args": []}]})
    got = entry["values"][0]
    check("model-level value.get names the model",
          got["ok"] is False
          and got["error"]["message"].startswith("TermLife_S does not take"),
          got["error"]["message"])

    # [HIGH-adjacent] params.model is nullable: the panel holds the name it got
    # from tree.get and passes it back, and has none on the first call.
    check("params.model null means 'no name yet'",
          bridge.dispatch("tree.get", {"model": None})["model"] == "TermLife_S")
    check("params.model explicit still resolves",
          bridge.dispatch("tree.get", {"model": "TermLife_S"})["model"]
          == "TermLife_S")
    try:
        bridge.dispatch("tree.get", {"model": 7})
        check("params.model wrong type -> bad_request", False)
    except BridgeError as err:
        check("params.model wrong type -> bad_request",
              err.code == "bad_request", err.message)

    # [LOW] methods._evaluate: error_args were repr() strings, which the frontend
    # cannot turn back into an addressable node. Section 4 says codec-encoded.
    model.Projection.new_cells(
        name="boom_args", formula="def boom_args(t):\n    return 1 / 0\n")
    try:
        bridge.dispatch("trace.preds", {"obj": "Projection.boom_args",
                                        "args": [1000]})
        check("formula_error error_args are codec-encoded", False)
    except BridgeError as err:
        args = err.data.get("error_args")
        check("formula_error error_args are codec-encoded",
              args == [1000], json.dumps(err.data.get("error_args")))
        strict_json(err.to_json(), "formula_error payload")

    # session.info now carries the sample gallery, so the frontend does not have
    # to keep its own copy of the list.
    samples = bridge.dispatch("session.info", {})["samples"]
    ids = [s["id"] for s in samples]
    check("session.info carries the sample gallery",
          ids == ["BasicTerm_S"], json.dumps(ids))
    check("the synthetic fixture is NOT in the gallery",
          "termlife_synthetic" not in ids, ", ".join(ids))

    # [MEDIUM] codec.encode: a value _enc already turned into a handle was being
    # promoted a second time, burning two ids for one object.
    try:
        import pandas as pd
    except ImportError:
        print("skip codec promotion checks (pandas not installed)")
        return
    codec = Codec()
    frame = pd.DataFrame({"a": range(50), "b": ["x" * 9000 for _ in range(50)]})
    enc = codec.encode(frame)
    check("an oversize handle is not promoted to a second handle",
          enc["$t"] == "handle" and list(codec.handles) == [enc["h"]],
          "handles=%s" % list(codec.handles))
    size = len(json.dumps(enc))
    check("a handle tag is bounded by construction", size < 16384,
          "%d bytes for a 50x2 frame of 9000-char strings" % size)
    cell = enc["preview"][0][1]
    check("preview strings use the preview budget, not MAX_STR_CHARS",
          isinstance(cell, dict) and cell["$t"] == "str" and len(cell["v"]) == 120,
          json.dumps({k: v for k, v in cell.items() if k != "v"}))


if __name__ == "__main__":
    sys.exit(main())
