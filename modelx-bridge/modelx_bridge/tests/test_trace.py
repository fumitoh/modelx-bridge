"""The Formula tab's, the Trace tab's and the Model map's kernel methods (0.9.0).

    python -m modelx_bridge.tests.test_trace

THREE READS, AND THE ONE PROPERTY THEY SHARE IS THAT NONE OF THEM EVALUATES.
Each panel follows the selection, so each request fires whenever the visitor
clicks something, and a click must never start a calculation the visitor did
not ask for (UI-DESIGN 4.2). Every section below that could evaluate checks the
tracegraph before and after.

WHY THE MAP IS READ FROM SOURCE, measured here rather than argued. The shipped
BasicTerm_S has nothing computed on a fresh load. Its 40 formulas NAME each
other 82 times, in one cycle group of four; the tracegraph after
`result_pv()` + `result_cf()` holds 77 of those links and none that the source
does not - so the source is a superset of that calculation, and complete before
anything is computed. Section [D] pins both numbers.
"""

import sys

from .shipped import sample_dir

FAILURES = []

SHIPPED = sample_dir()

#: Measured 2026-09-26 against the shipped model. Exact, so a change is loud.
MAP_CELLS = 40
MAP_LINKS = 82
MAP_CYCLE = ["pols_death", "pols_if", "pols_lapse", "pols_maturity"]
TRACED_LINKS = 77
#: The five links a result_pv() + result_cf() calculation never reaches.
SOURCE_ONLY = [["disc_factors", "check_pv_net_cf"], ["model_point", "sex"],
               ["net_cf", "check_pv_net_cf"], ["proj_len", "check_pv_net_cf"],
               ["pv_net_cf", "check_pv_net_cf"]]


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-60s %s" % ("ok  " if condition else "FAIL", label, detail))


def cycles(names, edges):
    """Strongly connected groups of more than one member (Tarjan, iterative)."""
    succ = dict((n, []) for n in names)
    for a, b in edges:
        succ[a].append(b)
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]

    def visit(root):
        work = [(root, iter(succ[root]))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(succ[nxt])))
                    advanced = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    top = stack.pop()
                    on.discard(top)
                    group.append(top)
                    if top == node:
                        break
                if len(group) > 1:
                    out.append(sorted(group))

    for n in names:
        if n not in index:
            visit(n)
    return out


def main():
    import modelx as mx
    from modelx_bridge.methods import Bridge, FEATURES, VERSION

    bridge = Bridge()
    model = mx.read_model(SHIPPED)
    name = model.name
    proj = model.spaces["Projection"]
    graph = model._impl.tracegraph

    def call(method, params):
        params = dict(params)
        params.setdefault("model", name)
        return bridge.dispatch(method, params)

    print("\n[A] the methods are announced")
    check("trace.succs is in FEATURES", "trace.succs" in FEATURES)
    check("map.get is in FEATURES", "map.get" in FEATURES)
    # Compared as numbers: "0.10.0" >= "0.9.0" is False as strings.
    check("the bridge version moved with them",
          tuple(map(int, VERSION.split("."))) >= (0, 9, 0), VERSION)

    print("\n[B] formula.get says what the editor used to read off the tree")
    got = call("formula.get", {"obj": "Projection.claims"})
    check("it still carries the source and the doc",
          got["source"].startswith("def claims") and "Claims" in got["doc"])
    check("...and the doc is doc.get's, character for character",
          got["doc"] == call("doc.get", {"obj": "Projection.claims"})["doc"])
    check("a defined Cells is not derived", got["derived"] is False,
          repr(got["derived"]))
    check("...and not dynamic", got["dynamic"] is False, repr(got["dynamic"]))
    check("its parameters come with it", got["parameters"] == ["t"],
          repr(got["parameters"]))
    check("and its kind", got["kind"] == "Cells", got["kind"])
    check("formula.get computed nothing", len(graph) == 0, str(len(graph)))

    print("\n[C] trace.preds / trace.succs with nothing computed")
    before = len(graph)
    cold = call("trace.preds", {"obj": "Projection.claims", "args": [0],
                                "evaluate": False})
    check("an uncomputed node answers cached: false", cold["cached"] is False,
          str(cold.get("cached")))
    check("...with NO preds key, not an empty list that reads as an input",
          "preds" not in cold, str(sorted(cold.keys())))
    check("...and names the node it is about",
          cold["node"]["display"].endswith("claims(t=0)"), cold["node"]["display"])
    check("trace.preds with evaluate: false computed nothing",
          len(graph) == before, "%d -> %d" % (before, len(graph)))
    cold = call("trace.succs", {"obj": "Projection.claims", "args": [0]})
    check("trace.succs on an uncomputed node is cached: false too",
          cold["cached"] is False and "succs" not in cold,
          str(sorted(cold.keys())))
    check("trace.succs computed nothing", len(graph) == before,
          "%d -> %d" % (before, len(graph)))
    try:
        call("trace.preds", {"obj": "Projection.claims", "args": [0],
                             "evaluate": "no"})
        check("a non-boolean evaluate is refused", False, "it returned")
    except Exception as exc:
        check("a non-boolean evaluate is refused", True, type(exc).__name__)

    print("\n[C2] ...and once something IS computed")
    call("value.get", {"nodes": [{"obj": "Projection.pv_net_cf", "args": []}],
                       "evaluate": True})
    after_calc = len(graph)
    warm = call("trace.preds", {"obj": "Projection.claims", "args": [3],
                                "evaluate": False})
    check("a computed node answers cached: true", warm["cached"] is True)
    check("...with its precedents", sorted(p["display"].split(".")[-1]
                                           for p in warm["preds"])
          == ["claim_pp(t=3)", "pols_death(t=3)"],
          str([p["display"] for p in warm["preds"]]))
    check("...each carrying its value",
          all(p["value"] is not None for p in warm["preds"]))
    succs = call("trace.succs", {"obj": "Projection.claims", "args": [3]})
    # pv_net_cf() reaches claims through pv_claims, never through net_cf:
    # a dependent is what has been COMPUTED reading this node.
    check("and its dependents", [s["display"].split(".")[-1]
                                 for s in succs["succs"]] == ["pv_claims()"],
          str([s["display"] for s in succs["succs"]]))
    top = call("trace.succs", {"obj": "Projection.pv_net_cf", "args": []})
    check("the top of the model has no dependents, and says so with a list",
          top["cached"] is True and top["succs"] == [], str(top.get("succs")))
    check("neither read computed anything", len(graph) == after_calc,
          "%d -> %d" % (after_calc, len(graph)))
    legacy = call("trace.preds", {"obj": "Projection.claims", "args": [3]})
    check("trace.preds without evaluate still answers as before",
          len(legacy["preds"]) == 2 and legacy["cached"] is True)

    print("\n[D] map.get: the Space as its formulas name it")
    for c in proj.cells.values():
        c.clear()
    cleared = len(graph)
    got = call("map.get", {"obj": "Projection"})
    names = [c["name"] for c in got["cells"]]
    links = got["edges"]
    check("every Cells is on the map", len(names) == MAP_CELLS, str(len(names)))
    check("with the measured number of links", len(links) == MAP_LINKS,
          str(len(links)))
    check("and exactly one cycle group, the policy decrements",
          cycles(names, links) == [MAP_CYCLE], str(cycles(names, links)))
    check("pols_if's recursion through time is a self-loop, not an edge",
          got["recursive"] == ["pols_if"]
          and ["pols_if", "pols_if"] not in links, str(got["recursive"]))
    check("claims reads what its formula names",
          sorted(a for a, b in links if b == "claims")
          == ["claim_pp", "pols_death"])
    check("references are listed, not drawn as Cells",
          "point_id" in [r["name"] for r in got["refs"]]
          and "point_id" not in names)
    check("...and which formulas read them",
          ["point_id", "model_point"] in got["ref_edges"], str(got["ref_edges"]))
    check("nothing failed to parse", got["unread"] == [], str(got["unread"]))
    check("each Cells carries what a selection needs",
          all("obj" in c and "parameters" in c for c in got["cells"]))
    check("map.get computed nothing", len(graph) == cleared,
          "%d -> %d" % (cleared, len(graph)))

    print("\n[E] ...and it is a superset of a real calculation")
    proj.point_id = 1
    proj.result_pv()
    proj.result_cf()
    traced = set()
    for u, v in graph.edges():
        a, b = u[0].name, v[0].name
        if a != b:
            traced.add((a, b))
    source = set(tuple(e) for e in links)
    check("the tracegraph after result_pv() + result_cf() has the measured links",
          len(traced) == TRACED_LINKS, str(len(traced)))
    check("and every one of them is on the map", traced <= source,
          str(sorted(traced - source)))
    check("the map's extra links are the ones that calculation never reaches",
          sorted(list(e) for e in source - traced) == SOURCE_ONLY,
          str(sorted(source - traced)))

    print("\n[F] what the map refuses, by name")
    whole = call("map.get", {"obj": ""})
    check("the Model maps its only Space", whole["obj"] == "Projection"
          and len(whole["edges"]) == MAP_LINKS, whole["obj"])
    two = mx.new_model(name="TwoSpaces")
    two.new_space("A")
    two.new_space("B")
    try:
        bridge.dispatch("map.get", {"model": "TwoSpaces", "obj": ""})
        check("a Model with two Spaces is refused", False, "it returned")
    except Exception as exc:
        check("a Model with two Spaces is refused, naming them",
              "A, B" in str(exc), str(exc)[:70])
    for obj, what in (("Projection.point_id", "a Reference"),
                      ("Projection.claims", "a Cells")):
        try:
            call("map.get", {"obj": obj})
            check("map.get on %s is refused" % what, False, "it returned")
        except Exception as exc:
            check("map.get on %s is refused" % what,
                  "not a Space" in str(exc), str(exc)[:60])

    print("\n[G] names a formula binds are not reads")
    from modelx_bridge.methods import _names_read
    check("a parameter that shadows a Cells is not a read",
          _names_read("def f(claims):\n    return claims + 1\n", ["claims"])
          == set())
    check("nor is a comprehension variable",
          _names_read("def f():\n    return sum(t for t in range(3))\n", [])
          == {"sum", "range"})
    check("nor an assignment target",
          _names_read("def f(t):\n    x = premiums(t)\n    return x\n", ["t"])
          == {"premiums"})
    check("a lambda formula parses too",
          _names_read("lambda t: claims(t) * 2", ["t"]) == {"claims"})
    check("and a source that does not parse says so rather than guessing",
          _names_read("def f(:\n", []) is None)

    print("")
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
