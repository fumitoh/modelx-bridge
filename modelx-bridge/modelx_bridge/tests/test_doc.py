"""doc.get, and the precedent/dependent counts that now ride with a value.

    python -m modelx_bridge.tests.test_doc

WHY `doc.get` IS ITS OWN METHOD, measured here rather than argued. On the
shipped BasicTerm_S the Projection space's docstring is 6,824 characters and all
40 of its Cells carry one, 6,552 more on the browser's Python (7,372 on CPython
3.12 and earlier; see CELLS_DOC_CHARS) - about 13 KB that would be added to
`tree.get`, a payload re-read on every `model.changed`, which fires on every
recalculation. This file pins those sizes, so the day someone proposes folding
`doc` into the tree the cost is on the record.

WHY THE COUNTS MOVED. `predslen` and `succslen` were computed only by
`_node_payload`, which serves `trace.preds`. So the Inspector could show "this
node's precedent/dependent counts" - a Phase 1 promise - only after the visitor
clicked Precedents, and `succslen` was on the wire with nothing reading it.
They now ride with the `value.get` entry the node bar reads anyway.

THE RULE BOTH MUST OBEY is section 7's: introspection does not evaluate.
`doc.get` on an uncomputed model must leave it uncomputed, and that is the last
section here.
"""

import inspect
import sys

from .shipped import sample_dir

FAILURES = []

SHIPPED = sample_dir()

#: Measured 2026-09-26 against the shipped model. Exact, so a change is loud.
PROJECTION_DOC_CHARS = 6824
#: THE CELLS' TOTAL DEPENDS ON THE INTERPRETER, not on modelx or the checkout.
#: CPython 3.13 strips the common leading indentation from every docstring at
#: compile time (gh-81283); 3.12 and earlier keep it. With the same modelx 0.33.0
#: and the same LF source, measured 2026-09-27: 6,552 on 3.13.12 and 3.14.0rc2,
#: 7,372 on 3.11.15 and 3.12.3 -- no CR in any of them. The browser runs Pyodide
#: 314, which is Python 3.14, so 6,552 is what tree.get would have carried there.
#: The Space's docstring is a module docstring at column 0, with no indentation
#: to lose: 6,824 on all four.
CELLS_DOC_CHARS = 6552 if sys.version_info >= (3, 13) else 7372
#: inspect.cleandoc over the same 40 docstrings: 6,488 on all four interpreters,
#: which is what shows the two totals above differ by whitespace and nothing else.
CELLS_CLEANDOC_CHARS = 6488
CELLS_WITH_DOCS = 40


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-58s %s" % ("ok  " if condition else "FAIL", label, detail))


def main():
    import modelx as mx
    from modelx_bridge.methods import Bridge, FEATURES, VERSION, _ref_names

    bridge = Bridge()
    model = mx.read_model(SHIPPED)
    name = model.name

    def call(method, params):
        params = dict(params)
        params.setdefault("model", name)
        return bridge.dispatch(method, params)

    print("\n[A] the method is announced")
    check("doc.get is in FEATURES", "doc.get" in FEATURES, str(VERSION))
    # Compared as numbers: "0.10.0" >= "0.8.0" is False as strings.
    check("the bridge version moved with it",
          tuple(map(int, VERSION.split("."))) >= (0, 8, 0), VERSION)

    print("\n[B] what it answers, per kind")
    space = call("doc.get", {"obj": "Projection"})
    check("a Space has a docstring", isinstance(space["doc"], str),
          repr(space["doc"])[:40])
    check("...and it is the module docstring, entire",
          len(space["doc"]) == PROJECTION_DOC_CHARS,
          "%d chars, expected %d" % (len(space["doc"]), PROJECTION_DOC_CHARS))
    check("...reported with its kind", space["kind"] == "UserSpace",
          space["kind"])
    check("...and echoing the obj it was asked about", space["obj"] == "Projection",
          space["obj"])

    cells = call("doc.get", {"obj": "Projection.claims"})
    check("a Cells has one too", isinstance(cells["doc"], str),
          repr(cells["doc"])[:40])
    check("...starting with its summary line",
          cells["doc"].startswith("Claims"), repr(cells["doc"][:20]))
    check("...and it is reStructuredText, which the UI must render",
          "``t``" in cells["doc"], repr(cells["doc"][:70]))

    root = call("doc.get", {"obj": ""})
    check("the model answers rather than failing", root["kind"] == "Model",
          root["kind"])
    check("...with null, because this model has no docstring",
          root["doc"] is None, repr(root["doc"]))

    ref_name = list(_ref_names(model.spaces["Projection"]))[0]
    ref = call("doc.get", {"obj": "Projection." + ref_name})
    check("a Reference answers rather than failing",
          "doc" in ref, str(sorted(ref.keys())))
    check("...with null when it has no docstring", ref["doc"] is None,
          repr(ref["doc"]))

    print("\n[C] the size that decided this is a method and not a tree field")
    total = 0
    cleaned = 0
    with_docs = 0
    for cell in model.spaces["Projection"].cells.values():
        doc = getattr(cell, "doc", None) or ""
        total += len(doc)
        cleaned += len(inspect.cleandoc(doc))
        with_docs += 1 if doc else 0
    check("every Cells in the demo model carries a docstring",
          with_docs == CELLS_WITH_DOCS, "%d of %d" % (with_docs, CELLS_WITH_DOCS))
    check("and they weigh what tree.get would have had to carry",
          total == CELLS_DOC_CHARS,
          "%d chars on Python %d.%d, expected %d"
          % (total, sys.version_info[0], sys.version_info[1], CELLS_DOC_CHARS))
    check("...the same text on every Python, indentation aside",
          cleaned == CELLS_CLEANDOC_CHARS,
          "%d chars after inspect.cleandoc, expected %d"
          % (cleaned, CELLS_CLEANDOC_CHARS))
    check("which with the Space is over 13 KB on every model.changed",
          total + PROJECTION_DOC_CHARS > 13000,
          "%d chars" % (total + PROJECTION_DOC_CHARS))

    print("\n[D] a bad obj fails like every other method")
    try:
        call("doc.get", {"obj": "Projection.nope"})
        check("an unknown obj is refused", False, "it returned instead")
    except Exception as exc:
        check("an unknown obj is refused", True, type(exc).__name__)

    print("\n[E] the counts ride with the value the node bar already reads")
    entry = call("value.get", {"nodes": [{"obj": "Projection.pv_net_cf",
                                          "args": []}],
                               "evaluate": True})["values"][0]
    check("a computed entry carries predslen", "predslen" in entry,
          str(sorted(entry.keys())))
    check("...and succslen, which nothing used to send outside trace.preds",
          "succslen" in entry, str(sorted(entry.keys())))
    check("pv_net_cf has the precedents it has",
          entry["predslen"] == 4, str(entry["predslen"]))
    check("...and nothing depends on it - it is the top of the model",
          entry["succslen"] == 0, str(entry["succslen"]))

    mid = call("value.get", {"nodes": [{"obj": "Projection.claims",
                                        "args": [3]}],
                             "evaluate": True})["values"][0]
    check("a node in the middle has both", mid["predslen"] == 2 and
          mid["succslen"] == 1,
          "preds %s succs %s" % (mid["predslen"], mid["succslen"]))

    print("\n[F] and neither of them evaluates - section 7")
    # A SYNTHETIC MODEL, not a second read of the shipped one. Two traps there,
    # both hit on the way to this file: `mx.read_model` on a path whose name is
    # already open RENAMES the open one to `BasicTerm_S_BAK1` (so the bridge
    # built above would be talking about a model that had moved), and
    # `Model.name` has no setter at all - assigning it raises
    # `TypeError: 'NoneType' object is not callable` from the property.
    def probe(t):
        """Probe ``t``, with a docstring worth fetching."""
        return 1 if t == 0 else probe(t - 1) + 1

    fresh = mx.new_model(name="DocProbe")
    fresh_space = fresh.new_space("S")
    fresh_space.new_cells(name="probe", formula=probe)
    fresh_bridge = Bridge()

    def fresh_call(method, params):
        params = dict(params)
        params.setdefault("model", "DocProbe")
        return fresh_bridge.dispatch(method, params)

    before = len(fresh_space.cells["probe"])
    check("the fresh model has nothing computed", before == 0, str(before))
    fetched = fresh_call("doc.get", {"obj": "S.probe"})
    check("doc.get answers for it", "Probe" in (fetched["doc"] or ""),
          repr(fetched["doc"]))
    fresh_call("doc.get", {"obj": "S"})
    after = len(fresh_space.cells["probe"])
    check("doc.get computed nothing", after == 0, str(after))

    uncached = fresh_call("value.get", {"nodes": [{"obj": "S.probe",
                                                   "args": [3]}],
                                        "evaluate": False})["values"][0]
    check("an uncached entry still refuses to evaluate",
          uncached["cached"] is False and uncached["value"] is None,
          str(uncached))
    check("...and carries no counts to invent, rather than zeros that lie",
          "predslen" not in uncached, str(sorted(uncached.keys())))
    check("and the model is still uncomputed afterwards",
          len(fresh_space.cells["probe"]) == 0,
          str(len(fresh_space.cells["probe"])))

    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
