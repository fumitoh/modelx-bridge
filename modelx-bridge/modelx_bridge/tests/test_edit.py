"""formula.set: the first method that changes a model, against the real one.

    python -m modelx_bridge.tests.test_edit

Every claim in `Bridge.formula_set`'s docstring is asserted here against
lifelib's BasicTerm_S, because the whole method is a set of statements about
what modelx does when a formula is replaced, and a statement that is not checked
is a guess with better formatting.

THE ONE THAT MATTERS MOST is `edit marks the model dirty`. A formula edit moves
neither dirty detector -- `_fingerprint` is (computed nodes, reference edges,
structure size) and `_touch_key` is (structure, Reference identities), and on a
freshly-read model an edit leaves both exactly where they were. The two checks
that prove this file is earning its place are the pair that assert the bridge
marks the model dirty anyway, and that `model.close` then refuses: without them,
a visitor's rewritten formula is something the kernel would throw away while
calling the model untouched.

The headline number is the demo itself: BasicTerm_S's pv_net_cf() is
910.92066093366, and doubling `claims` makes it 2576.451603396318. That pair is
the "edit a formula, watch the number change" loop, checked end to end through
the wire methods rather than through modelx directly.
"""

import json
import os
import sys

from .shipped import sample_dir

FAILURES = []
NUMBERS = {}

SHIPPED = sample_dir()

PV_NET_CF = 910.92066093366
PV_NET_CF_DOUBLED_CLAIMS = 2576.451603396318
TOL = 1e-12


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-54s %s" % ("ok  " if condition else "FAIL", label, detail))


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
    # prime() adopts the model as it is on disk; formula.set is then the only
    # thing in this file that can make it dirty, which is what the dirty checks
    # below are measuring.
    bridge._baseline_now(model)
    bridge._dirty.discard("BasicTerm_S")

    def call(method, params=None):
        return bridge.dispatch(method, params or {})

    def refused(method, params):
        """Dispatch expecting a BridgeError, and hand it back."""
        try:
            call(method, params)
        except BridgeError as err:
            return err
        return None

    def value(obj, args=None):
        got = call("value.get", {"nodes": [{"obj": obj, "args": args or []}]})
        entry = got["values"][0]
        if not entry.get("ok"):
            return None
        return scalar(entry.get("value"))

    claims = "Projection.claims"

    print("--- the method is advertised ---")
    info = call("session.info")
    check("session.info lists formula.set", "formula.set" in info["features"],
          ", ".join(info["features"]))
    # Compared as numbers: "0.10.0" >= "0.5.0" is False as strings.
    check("bridge version bumped for a mutating method",
          tuple(map(int, info["bridge"].split("."))) >= (0, 5, 0),
          info["bridge"])
    check("the model starts clean", info["models"][0]["dirty"] is False,
          json.dumps(info["models"][0]))

    print("\n--- baseline ---")
    base_pv = value("Projection.pv_net_cf")
    NUMBERS["pv_net_cf before"] = base_pv
    check("pv_net_cf() is the native figure", close(base_pv, PV_NET_CF),
          repr(base_pv))
    source = call("formula.get", {"obj": claims})["source"]
    check("formula.get returns the claims source",
          source.startswith("def claims(t):"), repr(source[:24]))

    print("\n--- an unchanged source is reported, not applied (fact 4) ---")
    # Re-applying identical text still clears this Cells and everything below
    # it; measured at 260 of 1,832 computed nodes. The method must decline.
    traced_before = len(model._impl.tracegraph)
    bridge.drain_events()        # the baseline read above evaluated, and said so
    same = call("formula.set", {"obj": claims, "source": source})
    check("changed is False", same["changed"] is False, json.dumps(same["changed"]))
    check("cleared is 0", same["cleared"] == 0, repr(same["cleared"]))
    check("nothing was invalidated",
          len(model._impl.tracegraph) == traced_before,
          "%d nodes, unchanged" % traced_before)
    check("the model is still clean", same["dirty"] is False, repr(same["dirty"]))
    check("no model.changed was emitted", bridge.drain_events() == [], "")

    print("\n--- the edit, end to end ---")
    edited_source = source.replace(
        "return claim_pp(t) * pols_death(t)",
        "return 2 * claim_pp(t) * pols_death(t)")
    check("the test's own edit really differs", edited_source != source,
          "%d chars" % len(edited_source))
    result = call("formula.set",
                  {"obj": claims, "source": edited_source, "expect": source})
    check("changed is True", result["changed"] is True, "")
    check("cleared counts the invalidated nodes", result["cleared"] > 200,
          "%d nodes" % result["cleared"])
    NUMBERS["cleared by editing claims"] = result["cleared"]
    check("the source comes back as modelx stored it",
          result["source"].startswith("def claims(t):"), "")
    check("the docstring is parsed out of the new source",
          (result["doc"] or "").startswith("Claims"), repr((result["doc"] or "")[:16]))
    check("parameters are reported", result["parameters"] == ["t"],
          json.dumps(result["parameters"]))
    check("an unrenamed edit reports no rename",
          result["renamed_from"] is None and result["name"] == "claims", "")
    check("it did not override an inherited Cells",
          result["overrode"] is False, "")

    print("\n--- the number moved (this IS the demo) ---")
    after_pv = value("Projection.pv_net_cf")
    NUMBERS["pv_net_cf after"] = after_pv
    check("pv_net_cf() recomputed to the doubled-claims figure",
          close(after_pv, PV_NET_CF_DOUBLED_CLAIMS), repr(after_pv))
    check("and it is not the old number", not close(after_pv, PV_NET_CF), "")

    print("\n--- THE DIRTY MARK (fact 1) ---")
    # Both detectors are blind to this edit. Prove that first, so the checks
    # below are measuring the bridge's own bookkeeping and not a lucky side
    # effect of something modelx happened to move.
    check("the edit is dirty per the bridge", result["dirty"] is True, "")
    check("session.info now reports the model dirty",
          call("session.info")["models"][0]["dirty"] is True, "")
    check("an edit event was emitted with reason 'edit'",
          any(e["params"].get("reason") == "edit"
              for e in bridge.drain_events()), "")
    blocked = refused("model.close", {"model": "BasicTerm_S"})
    check("model.close now refuses without force",
          blocked is not None and blocked.code == "bad_request",
          blocked.message if blocked else "IT CLOSED")
    check("and says the model has changed",
          blocked is not None and "has changed" in blocked.message, "")

    print("\n--- _touch_key really is blind to it (why the mark is needed) ---")
    from modelx_bridge.methods import _touch_key, _fingerprint
    clean = mx.read_model(SHIPPED, name="BlindCheck")
    key_before, fp_before = _touch_key(clean), _fingerprint(clean)
    clean.Projection.claims.formula = "def claims(t):\n    return 42.0"
    check("_touch_key does not move on a formula edit",
          _touch_key(clean) == key_before, "unchanged, as measured")
    check("_fingerprint does not move either (nothing computed)",
          _fingerprint(clean) == fp_before, repr(fp_before))
    clean.close()

    print("\n--- optimistic concurrency ---")
    stale = refused("formula.set", {"obj": claims, "source": edited_source,
                                    "expect": source})
    check("a stale expect is refused",
          stale is not None and stale.code == "bad_request",
          stale.message if stale else "IT WROTE")
    check("the refusal is flagged as a conflict",
          stale is not None and (stale.data or {}).get("conflict") is True, "")
    check("and carries the current source so the editor can show it",
          stale is not None
          and (stale.data or {}).get("current", "").startswith("def claims"), "")
    fresh = call("formula.get", {"obj": claims})["source"]
    ok = call("formula.set", {"obj": claims, "source": source, "expect": fresh})
    check("expect matching the live source is accepted",
          ok["changed"] is True, "")
    check("and the original formula is back",
          close(value("Projection.pv_net_cf"), PV_NET_CF), "")

    print("\n--- a rejected edit changes nothing (fact 2) ---")
    before_bad = call("formula.get", {"obj": claims})["source"]
    syntax = refused("formula.set",
                     {"obj": claims, "source": "def claims(t):\nreturn 1"})
    check("a syntax error is bad_request, not internal",
          syntax is not None and syntax.code == "bad_request",
          syntax.code if syntax else "IT PASSED")
    check("it does not leak a traceback",
          syntax is not None and "traceback" not in (syntax.data or {}), "")
    check("it carries the line the editor should point at",
          syntax is not None and (syntax.data or {}).get("lineno") == 2,
          json.dumps((syntax.data or {}).get("lineno")))
    check("it carries the column",
          syntax is not None and (syntax.data or {}).get("offset") is not None, "")
    check("the formula is untouched",
          call("formula.get", {"obj": claims})["source"] == before_bad, "")
    check("and the value still computes",
          close(value("Projection.pv_net_cf"), PV_NET_CF), "")

    print("\n--- a formula is exactly one def or lambda (fact 3) ---")
    for label, bad_source in [
        ("an import above the def", "import os\ndef claims(t):\n    return 1.0"),
        ("two defs", "def helper(x):\n    return x\ndef claims(t):\n    return 1.0"),
        ("a bare expression", "1 + 1"),
        ("an empty source", ""),
    ]:
        err = refused("formula.set", {"obj": claims, "source": bad_source})
        check("refused: " + label,
              err is not None and err.code == "bad_request"
              and "exactly one" in err.message,
              err.message[:48] if err else "IT PASSED")
    lam = call("formula.set", {"obj": claims, "source": "lambda t: 3.0"})
    check("a lambda is accepted", lam["changed"] is True, lam["source"])
    check("and evaluates", value("Projection.claims", [0]) == 3.0,
          repr(value("Projection.claims", [0])))
    call("formula.set", {"obj": claims, "source": source})

    print("\n--- a formula may change the parameter list ---")
    widened = call("formula.set",
                   {"obj": claims,
                    "source": "def claims(t, kind='death'):\n    return 1.0"})
    check("the new parameters are reported",
          widened["parameters"] == ["t", "kind"],
          json.dumps(widened["parameters"]))
    check("the tree agrees", _find_params(call("tree.get", {}), "Projection.claims")
          == ["t", "kind"], "")
    call("formula.set", {"obj": claims, "source": source})

    print("\n--- modelx REWRITES the def name, so the bridge says so ---")
    # Measured: `def something_else(t)` set on `claims` is STORED as
    # `def claims(t)`. In an editor that re-reads the stored source that looks
    # like the rename quietly undoing itself, and it is the one thing here a
    # person cannot work out from what they can see.
    renamed = call("formula.set",
                   {"obj": claims, "source": "def something_else(t):\n    return 1.0"})
    check("renamed_from carries the name that was typed",
          renamed["renamed_from"] == "something_else", repr(renamed["renamed_from"]))
    check("the Cells keeps its own name", renamed["name"] == "claims", "")
    check("and the stored source carries the Cells name, not the typed one",
          renamed["source"].startswith("def claims(t):"),
          repr(renamed["source"][:24]))
    check("a lambda reports no rename",
          call("formula.set", {"obj": claims, "source": "lambda t: 1.0"}
               )["renamed_from"] is None, "")
    call("formula.set", {"obj": claims, "source": source})

    print("\n--- what is not editable, said by name ---")
    for label, obj, needle in [
        ("a Reference", "Projection.point_id", "Reference"),
        ("a Space", "Projection", "Cells formulas only"),
        ("the Model", "", "Model has no formula"),
    ]:
        err = refused("formula.set", {"obj": obj, "source": "lambda: 1"})
        check("refused: " + label,
              err is not None and err.code == "bad_request" and needle in err.message,
              err.message[:56] if err else "IT PASSED")
    missing = refused("formula.set", {"obj": "Projection.nope", "source": "lambda: 1"})
    check("an unknown obj is not_found",
          missing is not None and missing.code == "not_found",
          missing.code if missing else "IT PASSED")

    print("\n--- parameter validation ---")
    for label, params, needle in [
        ("source must be a string", {"obj": claims}, "params.source"),
        ("source must be a string, not a number",
         {"obj": claims, "source": 3}, "params.source"),
        ("obj is required", {"source": "lambda: 1"}, "params.obj"),
        ("expect must be a string",
         {"obj": claims, "source": "lambda: 1", "expect": 7}, "params.expect"),
        ("a huge paste is refused",
         {"obj": claims, "source": "x" * 20001}, "character limit"),
    ]:
        err = refused("formula.set", params)
        check("refused: " + label,
              err is not None and err.code == "bad_request" and needle in err.message,
              err.message[:48] if err else "IT PASSED")

    print("\n--- a derived Cells becomes an override ---")
    inh = mx.new_model(name="Inherit")
    base = inh.new_space("Base")
    base.new_cells(name="a", formula="def a(x):\n    return x * 2")
    inh.new_space("Sub", bases=base)
    bridge2 = Bridge()
    bridge2.prime()
    sub = bridge2.dispatch("formula.set",
                           {"model": "Inherit", "obj": "Sub.a",
                            "source": "def a(x):\n    return x * 3"})
    check("overrode is reported", sub["overrode"] is True, "")
    check("and the Cells is no longer derived", sub["derived"] is False, "")
    check("the base keeps its own formula", base.a(2) == 4, repr(base.a(2)))
    check("the override took effect", inh.Sub.a(2) == 6, repr(inh.Sub.a(2)))
    inh.close()

    print("\n--- the edit survives a save and a re-read ---")
    import tempfile
    call("formula.set", {"obj": claims, "source": edited_source})
    target = os.path.join(tempfile.mkdtemp(prefix="ll-edit-"), "Edited")
    mx.write_model(model, target, backup=False)
    reread = mx.read_model(target, name="Reread")
    try:
        check("the saved model carries the edited formula",
              "2 * claim_pp(t)" in reread.Projection.claims.formula.source, "")
        again = reread.Projection.pv_net_cf()
        NUMBERS["pv_net_cf after save+read"] = again
        check("and recomputes to the same edited figure",
              close(again, PV_NET_CF_DOUBLED_CLAIMS), repr(again))
    finally:
        reread.close()

    print("\n--- numbers ---")
    for key in sorted(NUMBERS):
        print("  %-28s %s" % (key, NUMBERS[key]))
    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


def _find_params(tree, obj):
    """The `parameters` the tree reports for one obj, or None."""
    stack = [tree["root"]]
    while stack:
        node = stack.pop()
        if node.get("obj") == obj:
            return node.get("parameters")
        stack.extend(node.get("spaces") or [])
        stack.extend(node.get("cells") or [])
    return None


if __name__ == "__main__":
    sys.exit(main())
