"""ref.set: changing what a Reference holds, against the real model.

    python -m modelx_bridge.tests.test_ref

THE NUMBER THIS FILE EXISTS FOR is `cleared`. A formula edit clears what
depended on that formula — 260 of BasicTerm_S's 1,832 computed nodes. A
reference edit clears **all 1,832**, and the two checks that prove it is not a
coincidence of this model are in the "blast radius" section: a reference nothing
reads clears the model anyway, and in a two-branch model changing `k` clears the
branch that only reads `j`. Everything the UI says about the cost of this click
rests on those two.

The headline pair is the demo: `point_id` 1 → 3 moves `pv_net_cf()` from
910.92066093366 to 2026.1231145798447, checked through the wire methods.
"""

import json
import os
import sys

from .shipped import sample_dir

FAILURES = []
NUMBERS = {}

SHIPPED = sample_dir()

PV_POINT_1 = 910.92066093366
PV_POINT_3 = 2026.1231145798447
TRACED_POINT_1 = 1832
TOL = 1e-12


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-56s %s" % ("ok  " if condition else "FAIL", label, detail))


def close(got, want, tol=TOL):
    return abs(got - want) / abs(want) <= tol if want else abs(got) <= tol


def scalar(encoded):
    while isinstance(encoded, dict) and encoded.get("$t") in ("np", "num"):
        encoded = encoded.get("v")
    return encoded


def main():
    from modelx_bridge import Bridge, BridgeError
    import modelx as mx

    bridge = Bridge()
    model = mx.read_model(SHIPPED, name="BasicTerm_S")
    bridge.prime()
    bridge._baseline_now(model)
    bridge._dirty.discard("BasicTerm_S")

    def call(method, params=None):
        return bridge.dispatch(method, params or {})

    def refused(method, params):
        try:
            call(method, params)
        except BridgeError as err:
            return err
        return None

    def value(obj, args=None):
        entry = call("value.get",
                     {"nodes": [{"obj": obj, "args": args or []}]})["values"][0]
        return scalar(entry.get("value")) if entry.get("ok") else None

    point = "Projection.point_id"

    print("--- the method is advertised ---")
    info = call("session.info")
    check("session.info lists ref.set", "ref.set" in info["features"],
          ", ".join(info["features"]))
    # Compared as numbers: "0.10.0" >= "0.6.0" is False as strings.
    check("bridge version bumped",
          tuple(map(int, info["bridge"].split("."))) >= (0, 6, 0),
          info["bridge"])
    check("the model starts clean", info["models"][0]["dirty"] is False, "")

    print("\n--- baseline ---")
    check("point_id is 1", value(point) == 1, repr(value(point)))
    pv = value("Projection.pv_net_cf")
    NUMBERS["pv_net_cf point 1"] = pv
    check("pv_net_cf() is the native figure", close(pv, PV_POINT_1), repr(pv))
    traced = len(model._impl.tracegraph)
    NUMBERS["computed nodes"] = traced
    check("the model has computed the expected amount", traced == TRACED_POINT_1,
          "%d nodes" % traced)
    bridge.drain_events()

    print("\n--- the same value is declined, and that matters here ---")
    # Applying it would cost the model's ENTIRE computation for no change.
    same = call("ref.set", {"obj": point, "value": 1})
    check("changed is False", same["changed"] is False, "")
    check("cleared is 0", same["cleared"] == 0, repr(same["cleared"]))
    check("nothing was invalidated",
          len(model._impl.tracegraph) == traced, "%d nodes, unchanged" % traced)
    check("the model is still clean", same["dirty"] is False, "")
    check("no model.changed was emitted", bridge.drain_events() == [], "")
    check("and pv_net_cf is still cached",
          close(value("Projection.pv_net_cf"), PV_POINT_1), "")

    print("\n--- 1 is not 1.0 and not True ---")
    # type-first comparison: a float or a bool that happens to be equal is a
    # different reference value, and declining it would be wrong.
    changed_float = call("ref.set", {"obj": point, "value": 1.0})
    check("assigning 1.0 over 1 is a change", changed_float["changed"] is True,
          repr(changed_float["value"]))
    call("ref.set", {"obj": point, "value": 1})

    print("\n--- the edit, end to end (this IS the demo) ---")
    call("value.get", {"nodes": [{"obj": "Projection.pv_net_cf", "args": []}]})
    before = len(model._impl.tracegraph)
    result = call("ref.set", {"obj": point, "value": 3, "expect": 1})
    check("changed is True", result["changed"] is True, "")
    check("value comes back", result["value"] == 3, repr(result["value"]))
    check("value_type is reported", result["value_type"] == "int",
          repr(result["value_type"]))
    check("display names the reference",
          result["display"] == "BasicTerm_S.Projection.point_id", result["display"])
    check("parent is the owning Space", result["parent"] == "Projection",
          result["parent"])
    check("refmode is preserved", result["refmode"] == "auto",
          repr(result["refmode"]))
    check("it did not override an inherited reference",
          result["overrode"] is False, "")
    after_pv = value("Projection.pv_net_cf")
    NUMBERS["pv_net_cf point 3"] = after_pv
    check("pv_net_cf() recomputed for point 3", close(after_pv, PV_POINT_3),
          repr(after_pv))
    check("sum_assured followed too", value("Projection.sum_assured") == 799000,
          repr(value("Projection.sum_assured")))

    print("\n--- THE BLAST RADIUS: a reference clears the WHOLE model ---")
    NUMBERS["cleared by point_id"] = result["cleared"]
    check("cleared is the model's entire computation",
          result["cleared"] == before, "%d of %d" % (result["cleared"], before))
    check("...which is far more than a formula edit clears",
          result["cleared"] > 1000, "%d nodes" % result["cleared"])

    # Not a property of point_id being upstream of everything: a reference that
    # NOTHING reads clears the model just the same.
    unused = mx.read_model(SHIPPED, name="Unused")
    bridge_u = Bridge()
    bridge_u.prime()
    unused.Projection.pv_net_cf()
    unused.Projection.spare = 1              # create one nothing reads
    seeded = len(unused._impl.tracegraph)
    if seeded == 0:
        unused.Projection.pv_net_cf()
        seeded = len(unused._impl.tracegraph)
    spare = bridge_u.dispatch("ref.set", {"model": "Unused",
                                          "obj": "Projection.spare", "value": 2})
    check("a reference NOTHING reads still clears the model",
          spare["cleared"] == seeded and seeded > 0,
          "%d of %d" % (spare["cleared"], seeded))

    # And the two-branch case, which is the one that rules out "it only looked
    # global because everything in BasicTerm_S reads point_id".
    two = mx.new_model(name="TwoBranch")
    space = two.new_space("S")
    space.k = 2
    space.j = 100
    space.new_cells(name="uses_k", formula="def uses_k(x):\n    return x * k")
    space.new_cells(name="uses_j", formula="def uses_j(x):\n    return x * j")
    space.uses_k(1); space.uses_k(2); space.uses_j(1); space.uses_j(2)
    bridge_t = Bridge()
    bridge_t.prime()
    check("both branches are cached",
          len(space.uses_k) == 2 and len(space.uses_j) == 2, "")
    bridge_t.dispatch("ref.set", {"model": "TwoBranch", "obj": "S.k", "value": 3})
    check("changing k clears the branch that only reads j",
          len(space.uses_j) == 0, "%d cached" % len(space.uses_j))
    two.close()
    unused.close()

    print("\n--- the dirty mark ---")
    check("the edit is dirty per the bridge", result["dirty"] is True, "")
    check("session.info reports the model dirty",
          call("session.info")["models"][0]["dirty"] is True, "")
    check("an edit event was emitted",
          any(e["params"].get("reason") == "edit"
              for e in bridge.drain_events()), "")
    blocked = refused("model.close", {"model": "BasicTerm_S"})
    check("model.close now refuses without force",
          blocked is not None and blocked.code == "bad_request",
          blocked.message[:52] if blocked else "IT CLOSED")

    print("\n--- optimistic concurrency ---")
    stale = refused("ref.set", {"obj": point, "value": 5, "expect": 1})
    check("a stale expect is refused",
          stale is not None and stale.code == "bad_request",
          stale.message if stale else "IT WROTE")
    check("flagged as a conflict",
          stale is not None and (stale.data or {}).get("conflict") is True, "")
    check("and carries the value it really holds",
          stale is not None and (stale.data or {}).get("current") == 3,
          repr((stale.data or {}).get("current")))
    ok = call("ref.set", {"obj": point, "value": 1, "expect": 3})
    check("expect matching the live value is accepted", ok["changed"] is True, "")
    check("and the original number is back",
          close(value("Projection.pv_net_cf"), PV_POINT_1), "")

    print("\n--- only values that can cross the wire ---")
    for label, obj, needle in [
        ("a DataFrame", "Projection.model_point_table", "DataFrame"),
        ("a Series", "Projection.disc_rate_ann", "Series"),
        ("a module", "Projection.np", "module"),
    ]:
        err = refused("ref.set", {"obj": obj, "value": 3})
        check("refused: " + label,
              err is not None and err.code == "bad_request" and needle in err.message,
              err.message[:56] if err else "IT WROTE")
        check("  ...and says what would make one editable",
              err is not None and "number, string, boolean" in err.message, "")
    check("the DataFrame is untouched",
          type(model.Projection.model_point_table).__name__ == "DataFrame", "")

    bad_value = refused("ref.set", {"obj": point,
                                    "value": {"$t": "handle", "h": "h1"}})
    check("a handle cannot be sent as a value",
          bad_value is not None and bad_value.code == "bad_request",
          bad_value.message[:48] if bad_value else "IT WROTE")

    print("\n--- an existing Reference only ---")
    for label, obj, needle in [
        ("a Cells", "Projection.claims", "not a Reference"),
        ("a Space", "Projection", "not a Reference"),
        ("the Model", "", "not a Reference"),
    ]:
        err = refused("ref.set", {"obj": obj, "value": 1})
        check("refused: " + label,
              err is not None and err.code == "bad_request" and needle in err.message,
              err.message[:48] if err else "IT WROTE")
    missing = refused("ref.set", {"obj": "Projection.brand_new", "value": 1})
    check("an unknown name is not_found, not a silent create",
          missing is not None and missing.code == "not_found",
          missing.code if missing else "IT CREATED ONE")
    check("and nothing was created",
          "brand_new" not in model.Projection.refs, "")

    print("\n--- refmode is validated here, because modelx does not ---")
    # MEASURED: set_ref(name, value, refmode="sideways") stores "sideways".
    bad_mode = refused("ref.set", {"obj": point, "value": 2,
                                   "refmode": "sideways"})
    check("a nonsense refmode is refused",
          bad_mode is not None and "auto, absolute, relative" in bad_mode.message,
          bad_mode.message[:48] if bad_mode else "IT WROTE")
    check("and the value was not written", value(point) == 1, repr(value(point)))
    explicit = call("ref.set", {"obj": point, "value": 2, "refmode": "absolute"})
    check("an explicit mode is accepted and reported",
          explicit["refmode"] == "absolute", repr(explicit["refmode"]))
    kept = call("ref.set", {"obj": point, "value": 1})
    check("and is then preserved by a later edit with no mode",
          kept["refmode"] == "absolute", repr(kept["refmode"]))

    print("\n--- the value types a UI can type ---")
    probe = "Projection.probe"
    model.Projection.probe = 0
    bridge._baseline_now(model)
    for label, sent, want in [
        ("float", 2.5, 2.5),
        ("string", "BEF_FEE", "BEF_FEE"),
        ("bool", True, True),
        ("null", None, None),
        ("list", [1, 2, 3], [1, 2, 3]),
        ("dict", {"a": 1}, {"a": 1}),
    ]:
        got = call("ref.set", {"obj": probe, "value": sent})
        check("set a %s" % label, got["value"] == want,
              json.dumps(got["value"]))
    tup = call("ref.set", {"obj": probe, "value": {"$t": "tuple", "v": [1, 2]}})
    check("a tuple round-trips through its tag",
          tup["value"] == {"$t": "tuple", "v": [1, 2]}, json.dumps(tup["value"]))

    print("\n--- parameter validation ---")
    for label, params, needle in [
        ("value is required", {"obj": point}, "params.value"),
        ("obj is required", {"value": 1}, "params.obj"),
    ]:
        err = refused("ref.set", params)
        check("refused: " + label,
              err is not None and err.code == "bad_request" and needle in err.message,
              err.message[:48] if err else "IT PASSED")

    print("\n--- an inherited reference becomes an override ---")
    inh = mx.new_model(name="Inherit")
    base = inh.new_space("Base")
    base.new_cells(name="a", formula="def a(x):\n    return x * k")
    base.k = 2
    inh.new_space("Sub", bases=base)
    bridge_i = Bridge()
    bridge_i.prime()
    check("Sub.a(3) starts at 6", inh.Sub.a(3) == 6, repr(inh.Sub.a(3)))
    sub = bridge_i.dispatch("ref.set", {"model": "Inherit", "obj": "Sub.k",
                                        "value": 10})
    check("overrode is reported", sub["overrode"] is True, "")
    check("and it is no longer derived", sub["derived"] is False, "")
    check("the override took effect", inh.Sub.a(3) == 30, repr(inh.Sub.a(3)))
    check("the base keeps its own value", base.k == 2 and base.a(3) == 6, "")

    print("\n--- a Model-level reference ---")
    inh.top = 5
    bridge_m = Bridge()
    bridge_m.prime()
    top = bridge_m.dispatch("ref.set", {"model": "Inherit", "obj": "top",
                                        "value": 6})
    check("it can be set", top["value"] == 6 and top["changed"] is True, "")
    check("its parent is reported as the model", top["parent"] == "",
          repr(top["parent"]))
    check("and it has no refmode", top["refmode"] is None, repr(top["refmode"]))
    mode_err = None
    try:
        bridge_m.dispatch("ref.set", {"model": "Inherit", "obj": "top",
                                      "value": 7, "refmode": "auto"})
    except BridgeError as err:
        mode_err = err
    check("passing a refmode for one is refused",
          mode_err is not None and "no refmode" in mode_err.message,
          mode_err.message[:48] if mode_err else "IT ACCEPTED ONE")
    inh.close()

    print("\n--- the edit survives a save and a re-read ---")
    import tempfile
    call("ref.set", {"obj": point, "value": 3})
    target = os.path.join(tempfile.mkdtemp(prefix="ll-ref-"), "Edited")
    mx.write_model(model, target, backup=False)
    reread = mx.read_model(target, name="RereadRef")
    try:
        check("the saved model carries the new reference",
              reread.Projection.point_id == 3, repr(reread.Projection.point_id))
        again = reread.Projection.pv_net_cf()
        NUMBERS["pv_net_cf after save+read"] = again
        check("and recomputes to the point-3 figure", close(again, PV_POINT_3),
              repr(again))
    finally:
        reread.close()

    print("\n--- numbers ---")
    for key in sorted(NUMBERS):
        print("  %-28s %s" % (key, NUMBERS[key]))
    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
