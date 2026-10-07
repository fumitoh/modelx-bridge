"""table.get and the binary buffers (docs/bridge-protocol-v0.md section 10).

    python -m modelx_bridge.tests.test_tables

Runs against BasicTerm_S's real 10000x5 model_point_table -- 4 int64 columns and
one string column, which is exactly the mix that makes the binary path
interesting: a binary page must still carry the string column as JSON.

The load-bearing check here is not the shape of the reply, it is that the BYTES
decode to the same numbers. Every binary column is read back with numpy and
compared element-wise against the source, because a column-major layout, a
dtype string and a byte order are three independent chances to ship a table that
looks right and is wrong.
"""

import json
import math
import sys

from modelx_bridge import Bridge, BridgeError, samples, split_buffers
from modelx_bridge.tables import DTYPE_JS, MAX_PAGE_CELLS

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-52s %s" % ("ok  " if condition else "FAIL", label, detail))


def fails(label, code, call):
    try:
        call()
    except BridgeError as err:
        check(label, err.code == code, "%s: %s" % (err.code, err.message[:70]))
        return
    except Exception as exc:
        check(label, False, "raised %r instead of BridgeError" % (exc,))
        return
    check(label, False, "did not raise")


def get_handle(bridge, obj):
    result = bridge.dispatch("value.get", {"nodes": [{"obj": obj, "args": []}]})
    return result["values"][0]["value"]


def main():
    import numpy as np
    import pandas as pd

    bridge = Bridge()
    model = samples.build_basicterm_s()
    model.Projection.point_id = 1
    print("modelx model %s, python %s\n" % (model.name, sys.version.split()[0]))

    # -- a handle is what value.get gives for a DataFrame ------------------
    tag = get_handle(bridge, "Projection.model_point_table")
    check("value.get returns a handle for the 10000x5 table",
          tag["$t"] == "handle" and tag["shape"] == [10000, 5],
          json.dumps({k: tag[k] for k in ("$t", "h", "kind", "shape")}))
    h = tag["h"]
    source = model.Projection.model_point_table

    # -- json format -------------------------------------------------------
    page = bridge.dispatch("table.get", {"h": h, "rows": 4, "format": "json"})
    check("table.get json: one page, no buffers",
          page["format"] == "json" and page["buffers"] == 0
          and not bridge.take_buffers(),
          json.dumps({k: page[k] for k in ("format", "rows", "cols", "buffers")}))
    check("table.get json: geometry is the whole table, the page is the slice",
          page["shape"] == [10000, 5] and page["rows"] == 4 and page["cols"] == 5
          and page["complete"] is False,
          json.dumps({k: page[k] for k in ("shape", "rows", "cols", "complete")}))
    check("table.get json: the index is a column like any other",
          page["index"]["name"] == "point_id"
          and page["index"]["values"] == [1, 2, 3, 4],
          json.dumps(page["index"]))
    names = [c["name"] for c in page["columns"]]
    check("table.get json: column names in source order",
          names == list(source.columns), json.dumps(names))
    check("table.get json: values match the DataFrame",
          [c["values"] for c in page["columns"]]
          == [list(source[n][:4]) for n in names],
          json.dumps([c["values"][:2] for c in page["columns"]]))
    check("table.get json: cells are plain JSON scalars, not np tags",
          all(not isinstance(v, dict) for c in page["columns"] for v in c["values"]),
          json.dumps(page["columns"][0]["values"]))
    check("table.get json result is strictly serialisable",
          isinstance(json.dumps(page, allow_nan=False), str),
          "%d bytes" % len(json.dumps(page, allow_nan=False)))

    # -- binary format: DECODE THE BYTES AND COMPARE -----------------------
    page = bridge.dispatch("table.get", {"h": h, "rows": 200, "format": "binary"})
    buffers = bridge.take_buffers()
    check("table.get binary: one buffer per numeric column plus the index",
          page["format"] == "binary" and page["buffers"] == len(buffers) == 5,
          "%d buffers: %s" % (len(buffers), [len(b) for b in buffers]))
    check("table.get binary: the string column stayed JSON",
          "values" in page["columns"][1] and "buffer" not in page["columns"][1],
          json.dumps(page["columns"][1]["values"][:3]))
    check("table.get binary: the JSON envelope is tiny",
          len(json.dumps(page)) < 2000, "%d bytes" % len(json.dumps(page)))

    mismatches = []
    for column in [page["index"]] + page["columns"]:
        if "buffer" not in column:
            continue
        raw = buffers[column["buffer"]]
        if len(raw) != column["bytes"]:
            mismatches.append("%s: %d bytes, said %d"
                              % (column["name"], len(raw), column["bytes"]))
            continue
        decoded = np.frombuffer(raw, dtype=np.dtype(column["dtype"]).newbyteorder("<"))
        expected = (source.index.to_numpy()[:200] if column["name"] == "point_id"
                    else source[column["name"]].to_numpy()[:200])
        if not np.array_equal(decoded, expected):
            mismatches.append("%s: values differ" % column["name"])
    check("table.get binary: EVERY buffer decodes to the source values",
          not mismatches, ", ".join(mismatches) or
          "%d columns x 200 rows verified element-wise" % (len(buffers)))
    check("table.get binary: js names a real TypedArray constructor",
          all(c.get("js") in DTYPE_JS.values()
              for c in [page["index"]] + page["columns"] if "buffer" in c),
          json.dumps([c.get("js") for c in page["columns"] if "buffer" in c]))
    check("table.get binary: int64 maps to BigInt64Array, not a lossy double",
          page["index"]["js"] == "BigInt64Array", page["index"]["js"])

    # -- paging ------------------------------------------------------------
    tail = bridge.dispatch("table.get", {"h": h, "row": 9998, "rows": 100})
    bridge.take_buffers()
    check("a page past the end is clipped, not an error",
          tail["rows"] == 2 and tail["index"]["values"] == [9999, 10000],
          json.dumps(tail["index"]["values"]))
    check("the last page reports complete",
          tail["complete"] is True, json.dumps(tail["complete"]))
    cols = bridge.dispatch("table.get", {"h": h, "col": 3, "cols": 1, "rows": 2})
    bridge.take_buffers()
    check("column paging selects the right column",
          [c["name"] for c in cols["columns"]] == ["policy_count"],
          json.dumps([c["name"] for c in cols["columns"]]))

    # -- auto --------------------------------------------------------------
    small = bridge.dispatch("table.get", {"h": h, "rows": 10, "format": "auto"})
    bridge.take_buffers()
    big = bridge.dispatch("table.get", {"h": h, "rows": 1000, "format": "auto"})
    bridge.take_buffers()
    check("auto picks json for a small page and binary for a large one",
          small["format"] == "json" and big["format"] == "binary",
          "%s / %s" % (small["format"], big["format"]))

    # -- Series, Index, ndarray --------------------------------------------
    series = get_handle(bridge, "Projection.disc_rate_ann")
    page = bridge.dispatch("table.get", {"h": series["h"], "rows": 5})
    bridge.take_buffers()
    check("table.get pages a Series as one column",
          page["kind"] == "Series" and page["total_cols"] == 1
          and len(page["columns"]) == 1,
          json.dumps({"kind": page["kind"], "name": page["columns"][0]["name"]}))
    check("the Series values match the model",
          page["columns"][0]["values"]
          == [float(v) for v in model.Projection.disc_rate_ann[:5]],
          json.dumps(page["columns"][0]["values"][:3]))

    codec = bridge.codec
    idx = codec.handle(pd.Index([10, 20, 30], name="year"))
    page = bridge.dispatch("table.get", {"h": idx["h"]})
    bridge.take_buffers()
    check("table.get pages an Index",
          page["kind"] == "Index" and page["columns"][0]["values"] == [10, 20, 30],
          json.dumps(page["columns"][0]))

    arr = codec.handle(np.arange(6, dtype="float32").reshape(3, 2))
    page = bridge.dispatch("table.get", {"h": arr["h"], "format": "binary"})
    buffers = bridge.take_buffers()
    decoded = [np.frombuffer(buffers[c["buffer"]], dtype="<f4").tolist()
               for c in page["columns"]]
    check("table.get pages a 2-D ndarray column-major",
          page["kind"] == "ndarray" and decoded == [[0.0, 2.0, 4.0], [1.0, 3.0, 5.0]],
          json.dumps(decoded))
    check("float32 maps to Float32Array",
          page["columns"][0]["js"] == "Float32Array", page["columns"][0]["js"])

    one_d = codec.handle(np.array([1.5, 2.5]))
    page = bridge.dispatch("table.get", {"h": one_d["h"]})
    bridge.take_buffers()
    check("a 1-D ndarray is one column",
          page["total_cols"] == 1 and page["columns"][0]["values"] == [1.5, 2.5],
          json.dumps(page["columns"][0]["values"]))

    cube = codec.handle(np.zeros((2, 2, 2)))
    fails("a 3-D array is refused, naming its shape", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": cube["h"]}))
    scalarish = codec.handle({"not": "a table"})
    fails("a handle over a non-container is refused", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": scalarish["h"]}))

    # -- non-finite floats survive the json path ---------------------------
    nan = codec.handle(pd.Series([1.0, float("nan"), float("inf")]))
    page = bridge.dispatch("table.get", {"h": nan["h"], "format": "json"})
    bridge.take_buffers()
    values = page["columns"][0]["values"]
    check("json cells tag NaN and Infinity rather than emitting bare literals",
          values[0] == 1.0 and values[1] == {"$t": "num", "v": "NaN"}
          and values[2] == {"$t": "num", "v": "Infinity"},
          json.dumps(values))
    check("a page with NaN is still strict JSON",
          isinstance(json.dumps(page, allow_nan=False), str), "allow_nan=False")
    page = bridge.dispatch("table.get", {"h": nan["h"], "format": "binary"})
    raw = bridge.take_buffers()[page["columns"][0]["buffer"]]
    decoded = np.frombuffer(raw, dtype="<f8")
    check("binary carries NaN and Infinity as IEEE-754 bits",
          math.isnan(decoded[1]) and math.isinf(decoded[2]), str(decoded.tolist()))

    # -- limits and handle lifetime ----------------------------------------
    fails("an unknown handle is not_found", "not_found",
          lambda: bridge.dispatch("table.get", {"h": "h999999"}))
    fails("a non-string handle is bad_request", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": 7}))
    fails("an unknown format is bad_request", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": h, "format": "protobuf"}))
    fails("a negative row offset is bad_request", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": h, "row": -1}))
    # A `rows` larger than the table is harmlessly clipped; the cell cap is
    # about a window that really is that big.
    clipped = bridge.dispatch("table.get", {"h": h, "rows": MAX_PAGE_CELLS})
    bridge.take_buffers()
    check("rows past the end of the table is clipped, not refused",
          clipped["rows"] == 10000 and clipped["complete"] is True,
          "%d rows" % clipped["rows"])
    huge = bridge.codec.handle(np.zeros((1000, 300)))
    fails("a genuinely oversized page is refused before it is built",
          "bad_request",
          lambda: bridge.dispatch("table.get", {"h": huge["h"], "rows": 1000,
                                                "cols": 300}))

    # The claim section 10.4 rests on: for a whole float64 projection, binary is
    # not an optimisation, it is the difference between the method working and
    # not. 10043x6 float64 is ~1.2 MB as JSON -- over max_message_bytes -- and
    # ~563 KB as buffers with a sub-kilobyte JSON envelope.
    floats = bridge.codec.handle(
        pd.DataFrame(np.random.default_rng(0).random((10043, 6)) * 1000))
    fails("a whole float64 table does NOT fit in one JSON message", "bad_request",
          lambda: bridge.dispatch("table.get", {"h": floats["h"], "rows": 10043,
                                                "cols": 6, "format": "json"}))
    page = bridge.dispatch("table.get", {"h": floats["h"], "rows": 10043,
                                         "cols": 6, "format": "binary"})
    binary_buffers = bridge.take_buffers()
    check("...and the same page DOES fit as binary buffers",
          len(binary_buffers) == 7
          and sum(len(x) for x in binary_buffers) == 10043 * 7 * 8
          and len(json.dumps(page)) < 1000,
          "%d buffers, %d bytes + %d byte envelope"
          % (len(binary_buffers), sum(len(x) for x in binary_buffers),
             len(json.dumps(page))))

    before = list(bridge.codec.handles)
    bridge.dispatch("table.get", {"h": before[0], "rows": 1})
    bridge.take_buffers()
    check("paging touches the handle so a long scroll cannot evict it",
          list(bridge.codec.handles)[-1] == before[0],
          "%s is now most-recent of %d" % (before[0], len(bridge.codec.handles)))

    # -- the whole envelope, through Bridge.handle --------------------------
    envelope = bridge.handle({"type": "req", "id": "t1", "method": "table.get",
                              "params": {"h": h, "rows": 50, "format": "binary"}})
    data, out = split_buffers(envelope)
    check("Bridge.handle carries buffers beside the JSON, not inside it",
          "buffers" not in data and len(out) == 5
          and data["result"]["buffers"] == 5,
          "%d buffers, %d bytes of json" % (len(out), len(json.dumps(data))))
    check("the res still echoes the request id",
          data["id"] == "t1" and data["type"] == "res", json.dumps(data["id"]))
    envelope = bridge.handle({"type": "req", "id": "t2", "method": "table.get",
                              "params": {"h": "gone"}})
    check("a failed table.get sends no buffers",
          "buffers" not in envelope and envelope["error"]["code"] == "not_found",
          envelope["error"]["code"])

    model.close()
    print("\n%d checks failed" % len(FAILURES))
    for label in FAILURES:
        print("  " + label)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
