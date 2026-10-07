"""Capture golden records: tool calls on the shipped BasicTerm_S, each with
every bridge call it made and the result it got, and the text it printed.

    python -m modelx_mcp.tests.capture_golden OUT.jsonl

A record is one journal line (spec 3.5). Replaying it through a fake dispatch
must give the same text byte for byte (test_golden.py), which is what holds a
port of tools.py over the same `dispatch` -- the Phase 3 TypeScript one --
equal to this one. Only the shipped sample is used, so CI recaptures without
lifelib. Run it in a fresh process: the calls assume a fresh model.
"""
import json
import sys

P = "BasicTerm_S.Projection."

#: (tool, args, kwargs), in order, on one fresh BasicTerm_S. Every tool, the
#: per-ref and whole-call errors, an ItemSpace created, a formula error, a
#: derived value, a range, a Cells listing, a docstring page.
CALLS = [
    ("get_tree", (), {}),
    ("get_tree", (), {"filter": "term"}),
    ("get_formulas", (["BasicTerm_S.Projection"],), {}),
    ("get_formulas", ([P + "claims", P + "disc_rate_ann"],), {"docstrings": False}),
    ("get_map", (), {}),
    ("get_map", (), {"cells": "claims", "depth": -1}),
    ("get_value", ([P + "pv_net_cf()", P + "claims(t=0)", P + "disc_rate_ann", P + "point_id"],), {}),
    ("trace", (P + "claims(t=3)",), {"direction": "succs"}),
    ("calculate", ([P + "pv_net_cf()", P + "claims(t=0)"],), {}),
    ("trace", (P + "pv_net_cf()",), {}),
    ("trace", (P + "pv_net_cf()",), {"depth": 2}),
    ("trace", (P + "claims(t=3)",), {"direction": "succs"}),
    ("get_value", ([P + "claims"],), {}),
    ("calculate", ([P + "pols_if[0:121]"],), {}),
    ("calculate", (["BasicTerm_S.Projection[2].pols_lapse(t=30)"],), {}),
    ("trace", ("BasicTerm_S.Projection[2].pols_lapse(t=30)",), {}),
    # An ItemSpace's own counts (its own tree.get) and its ref, never __SpaceN.
    ("get_formulas", (["BasicTerm_S.Projection[2].pols_lapse", "BasicTerm_S.Projection[2]"],),
     {"docstrings": False}),
    ("get_tree", (), {"path": "Projection[2]", "filter": "lapse"}),
    ("get_map", (), {"space": "Projection[2]"}),
    ("get_value", (["BasicTerm_S.Projection[3].pv_net_cf()"],), {}),
    ("calculate", (["Projection.claimz(t=0)", "Projection.claims", "Projection.claims(t=0, x=1)",
                    "Projection.claims(t=9999)", "PV_NET_CF()"],), {}),
    # Refused refs create nothing and are named at the top; overlaps count once.
    ("calculate", ([P + "pv_net_cf()", P + "claims[3:3]", "BasicTerm_S.Projection[7].claimz(t=0)"],), {}),
    ("calculate", ([P + "pols_lapse(t=range(0, 10))", P + "pols_lapse(t=range(5, 15))"],), {}),
    ("get_value", ([P + "mort_table"],), {}),
    ("get_tree", (), {}),
    ("get_value", ([P + "claims"],), {"label": 1}),
    ("get_value", ([P + "disc_rate_ann.loc[999]", P + "claims(t=0).loc[3]",
                    P + "disc_rate_ann.loc[10]", P + "disc_rate_ann.iloc[3]"],), {}),
    ("get_value", ([P + "disc_rate_ann"],), {"offset": 140, "rows": 5}),
    ("get_formulas", (["BasicTerm_S.Projection"],), {"doc_offset": 3000}),
    ("calculate", (["BasicTerm_S.Projection[%d].pv_net_cf() / BasicTerm_S.Projection[%d].pv_premiums()"
                    % (k, k) for k in range(1, 4)],), {}),
    ("calculate", (["Projection.pv_claims() / Projection.pv_premiums()", "Projection.model_point() * 2",
                    "Projection.pv_claims() ** 2"],), {}),
    ("get_value", ([P + "pols_if(t=range(0, 6))", P + "net_cf(t=range(0, 3))"],), {}),
    ("get_value", (["Projection.claims(t="],), {}),
]


def capture(calls=CALLS):
    """-> list of journal records, one per call, on a fresh BasicTerm_S."""
    from modelx_mcp.refs import RefError
    from modelx_mcp.session import Session
    from modelx_mcp.tools import ToolError
    records = []
    session = Session(samples=["BasicTerm_S"], journal=records.append)
    for name, args, kwargs in calls:
        before = len(records)
        try:
            getattr(session.tools, name)(*args, **kwargs)
        except (ToolError, RefError) as e:
            # A whole-call error is journaled too: the replay must raise it.
            records.append({"tool": name, "args": {}, "calls": session.tools.calls,
                            "error": str(e)})
        if len(records) == before:
            raise AssertionError("%s journaled nothing" % name)
        records[-1]["call"] = [name, list(args), kwargs]
    return records


def main(argv):
    if len(argv) != 1:
        print(__doc__)
        return 2
    records = capture()
    with open(argv[0], "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print("wrote %d records to %s" % (len(records), argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
