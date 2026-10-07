"""Reading a handle beyond its preview: ``table.get`` and the binary buffers
(docs/bridge-protocol-v0.md section 10).

v0 hands the frontend a `handle` tag for every DataFrame, Series, Index and
ndarray and then offers no way to read one -- the preview is 10x10 and that is
all. This module is the paging half, and the place the protocol's reserved
`buffers` field is finally used.

**One result shape, two fillings.** A page is always
``{index: <column>, columns: [<column>, ...]}`` where a column is either

    {"name": "sum_assured", "dtype": "int64", "js": "BigInt64Array",
     "buffer": 3, "bytes": 800}                        # format "binary"
    {"name": "sex", "dtype": "string", "values": ["M", "F", ...]}   # format "json"

so the frontend has exactly one branch -- ``"buffer" in col`` -- and a binary
page may still carry JSON columns, because a column of strings has no typed-array
representation and pretending otherwise would mean inventing an encoding.

**Layout is column-major, deliberately.** A DataFrame's columns each have one
dtype; its rows do not. Row-major binary would need a struct layout and a
per-row unpack in JS, where column-major is ``new Float64Array(buffer)`` and
nothing else.

**Byte order.** Every typed-array constructor in JS reads the platform's byte
order, which is little-endian on every browser target that exists. A big-endian
array is byte-swapped here rather than flagged, because a flag the frontend
would have to honour is a bug waiting for hardware nobody has.

**Precision.** This is the one place binary is not merely faster: JSON carries an
int64 as a JSON number, and the browser parses that to a double, so anything past
2^53 is silently wrong. ``BigInt64Array`` is exact. A client that wants plain JS
numbers should ask for ``json`` and know what it is trading.

**And, since 0.7.0, the two builders `cells.page` needs** (section 13):
``stats()``, the one place a column is reduced, and ``key_frame()``, which builds
a Cells' cached keys and values into a page frame by hand. They are here rather
than in methods.py because ``table.stats`` reduces through the same ``stats()``
-- a statistic is always over the whole column and never over the visible page,
and that rule is worth exactly one implementation.

**And, since 0.10.0** (section 18), where a column's extremes are
(``_extremes``, ``label_extremes``) and the two reads by label: ``locate`` for
``table.get {label}`` and ``pick_element`` for ``cells.page {element}``.
"""

import sys

from .errors import bad_request, not_found

#: numpy dtype -> the JS typed-array constructor that reads it. A dtype absent
#: from this map has no typed-array form and falls back to JSON cells.
DTYPE_JS = {
    "float64": "Float64Array", "float32": "Float32Array",
    "int64": "BigInt64Array", "int32": "Int32Array",
    "int16": "Int16Array", "int8": "Int8Array",
    "uint64": "BigUint64Array", "uint32": "Uint32Array",
    "uint16": "Uint16Array", "uint8": "Uint8Array",
    "bool": "Uint8Array",
}

DEFAULT_ROWS = 100
DEFAULT_COLS = 50

#: Refuse a page bigger than this many cells before building anything. The byte
#: limits below catch the rest, but only after the data exists. Note the
#: requested window is clipped to the container first, so a careless
#: `rows: 10000000` on a 10000-row table is not an error -- this fires only when
#: the caller really did ask for that many cells of a container that has them.
MAX_PAGE_CELLS = 250000

#: Total bytes of binary buffers one message may carry. Reported in `limits`.
MAX_BUFFER_BYTES = 8388608

#: How many cells a page must have before `format: "auto"` picks binary.
#: Below it the buffer bookkeeping costs more than the JSON it saves.
AUTO_BINARY_CELLS = 512

#: How many cached values `cells.page` will page at all. Checked against the
#: FREE `len(cells)` before `.series` is ever touched, so the refusal costs
#: nothing and the client can say "too many to page here" from the reply instead
#: of discovering it by freezing the one kernel thread.
#:
#: MEASURED on CPython 3.13 / pandas 3.0.6: `cells.series` is 0.06 ms at 121
#: cached values, 2.6 ms at 10,000 and 15 ms at 50,000, so the limit costs ~15 ms
#: of frozen kernel thread in the worst case here. It is 50,000 rather than
#: 200,000 (~60 ms measured) because Pyodide/WASM is typically 3-10x slower with
#: pandas 3.0.2: at 200,000 that is a 200-600 ms freeze on a single click, which
#: presents as a hung kernel. NOT YET measured in the browser -- see the probe
#: named in protocol section 13.
MAX_SERIES_ROWS = 50000

#: Rows one `cells.page` window carries by default. 200 rather than `DEFAULT_ROWS`
#: because the whole of BasicTerm_S's longest projection (121 values) then lands
#: in one block, and a client that pages in fixed blocks can answer an argument
#: step from the page it already holds instead of asking again.
DEFAULT_SERIES_ROWS = 200

#: How long a repr in the value column of a non-scalar page may be.
MAX_CELL_REPR_CHARS = 120

FORMATS = ("json", "binary", "auto")


def _int(params, key, default):
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise bad_request("params.%s must be an integer" % key)
    if value < 0:
        raise bad_request("params.%s must not be negative" % key)
    return value


def describe(value):
    """(kind, total_rows, total_cols) for a handled container, or raise."""
    pd = sys.modules.get("pandas")
    np = sys.modules.get("numpy")
    if pd is not None and isinstance(value, pd.DataFrame):
        return "DataFrame", int(value.shape[0]), int(value.shape[1])
    if pd is not None and isinstance(value, pd.Series):
        return "Series", int(len(value)), 1
    if pd is not None and isinstance(value, pd.Index):
        return "Index", int(len(value)), 1
    if np is not None and isinstance(value, np.ndarray):
        if value.ndim == 1:
            return "ndarray", int(value.shape[0]), 1
        if value.ndim == 2:
            return "ndarray", int(value.shape[0]), int(value.shape[1])
        raise bad_request(
            "table.get supports 1- and 2-dimensional arrays; this handle is %d-"
            "dimensional with shape %s -- slice it in the Console first"
            % (value.ndim, list(value.shape)), ndim=value.ndim,
            shape=list(value.shape))
    raise bad_request(
        "table.get cannot page a %s; handles over other values carry their "
        "repr and preview only" % type(value).__name__, py=type(value).__name__)


def _slice(value, kind, row, rows, col, cols):
    """(index_name, index_values, [(column name, 1-D values), ...]) for one page."""
    pd = sys.modules.get("pandas")
    np = sys.modules.get("numpy")

    if kind == "DataFrame":
        block = value.iloc[row:row + rows, col:col + cols]
        columns = [(_name(block.columns[i]), block.iloc[:, i].to_numpy())
                   for i in range(block.shape[1])]
        return _name(block.index.name, ""), block.index.to_numpy(), columns

    if kind == "Series":
        block = value.iloc[row:row + rows]
        columns = ([] if col > 0 or cols == 0
                   else [(_name(value.name, "value"), block.to_numpy())])
        return _name(block.index.name, ""), block.index.to_numpy(), columns

    if kind == "Index":
        block = value[row:row + rows]
        columns = ([] if col > 0 or cols == 0
                   else [(_name(value.name, "index"), block.to_numpy()
                          if hasattr(block, "to_numpy") else np.asarray(block))])
        return "", np.arange(row, row + len(block)), columns

    block = value[row:row + rows]
    if value.ndim == 1:
        columns = [] if col > 0 or cols == 0 else [("0", block)]
    else:
        block = block[:, col:col + cols]
        columns = [(str(col + i), block[:, i]) for i in range(block.shape[1])]
    return "", np.arange(row, row + len(block)), columns


def _name(value, default=""):
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _binary(values):
    """(dtype, js ctor, bytes) for a numeric column, or None if it has no
    typed-array form (object, string, datetime, categorical, ...)."""
    np = sys.modules.get("numpy")
    if np is None or not isinstance(values, np.ndarray):
        return None
    js = DTYPE_JS.get(str(values.dtype))
    if js is None:
        return None
    array = values
    try:
        if array.dtype.byteorder == ">":
            array = array.astype(array.dtype.newbyteorder("<"))
        array = np.ascontiguousarray(array)
        return str(values.dtype), js, array.tobytes()
    except Exception:
        return None


def _json_column(codec, name, values):
    """Preview-grade cells: numpy scalars unwrapped to Python ones, strings
    clipped at 120 characters with a `str` tag when they are. An `np` tag per
    cell would triple a numeric column for no information -- the column's dtype
    is carried once, on the column."""
    return {"name": name, "dtype": _dtype_name(values),
            "values": codec._preview_row(values)}


def _dtype_name(values):
    dtype = getattr(values, "dtype", None)
    return str(dtype) if dtype is not None else "object"


def page(codec, value, params, sink=None, max_buffer_bytes=MAX_BUFFER_BYTES):
    """Build one page of ``value``. Returns the result dict.

    ``sink`` is a list that binary buffers are appended to; a column's ``buffer``
    field is its index in that list. With no sink, ``binary`` is impossible and
    the page is built as JSON -- which is what makes this function usable from a
    transport that has no buffer channel at all.
    """
    kind, total_rows, total_cols = describe(value)
    row = _int(params, "row", 0)
    rows = _int(params, "rows", DEFAULT_ROWS)
    col = _int(params, "col", 0)
    cols = _int(params, "cols", DEFAULT_COLS)
    fmt = params.get("format", "json")
    if fmt not in FORMATS:
        raise bad_request("params.format must be one of %s"
                          % ", ".join(FORMATS), format=fmt)

    want_rows = max(0, min(rows, total_rows - row))
    want_cols = max(0, min(cols, total_cols - col))
    if want_rows * max(want_cols, 1) > MAX_PAGE_CELLS:
        raise bad_request(
            "a page of %d rows x %d columns is %d cells, over the %d cell limit; "
            "ask for fewer rows" % (want_rows, want_cols,
                                    want_rows * want_cols, MAX_PAGE_CELLS),
            cells=want_rows * want_cols, limit=MAX_PAGE_CELLS)

    index_name, index_values, columns = _slice(value, kind, row, rows, col, cols)

    if fmt == "auto":
        fmt = ("binary" if sink is not None
               and want_rows * max(want_cols, 1) >= AUTO_BINARY_CELLS else "json")
    if fmt == "binary" and sink is None:
        fmt = "json"

    budget = [max_buffer_bytes]

    def emit(name, values):
        if fmt == "binary":
            binary = _binary(values)
            if binary is not None:
                dtype, js, data = binary
                if len(data) <= budget[0]:
                    budget[0] -= len(data)
                    sink.append(data)
                    return {"name": name, "dtype": dtype, "js": js,
                            "buffer": len(sink) - 1, "bytes": len(data)}
                raise bad_request(
                    "this page needs more than the %d byte buffer limit; ask for "
                    "fewer rows" % max_buffer_bytes, limit=max_buffer_bytes)
        return _json_column(codec, name, values)

    result = {
        "kind": kind,
        "shape": [total_rows, total_cols],
        "row": row, "rows": len(index_values),
        "col": col, "cols": len(columns),
        "total_rows": total_rows, "total_cols": total_cols,
        "index": emit(index_name, index_values),
        "columns": [emit(name, values) for name, values in columns],
        "format": fmt,
        "complete": row + len(index_values) >= total_rows
                    and col + len(columns) >= total_cols,
    }
    result["buffers"] = len(sink) if sink is not None else 0
    return result


# --- whole-column statistics and the hand-built key frame (section 13) ----
#
# Both `cells.page` and `table.stats` reduce through `stats()`, so the honest-
# statistic rule -- a statistic is ALWAYS over the whole column and NEVER over
# the visible page -- is implemented once. MEASURED on BasicTerm_S `claims`
# after the projection is computed: the whole column is count=121
# sum=5814.680788 mean=48.055213 min=0.0 max=64.478472, while its first 100 rows
# give count=100 sum=4562.909698 min=31.015247 max=61.562898 -- a Sum 22% low
# and BOTH extremes wrong, on a projection column that rises and then falls.
# That is why the reduction is here and not in the frontend.


def _stat_kind(values, dtype):
    """Which statistics are defined for this column, decided from the WHOLE column.

    Never from a sample of the first value: a column whose first element is a
    float and whose rest are arrays would then be reduced as numeric.

    MEASURED, and the reason this cannot branch on ``dtype.kind`` alone: under
    pandas 3.0.6 a string column has dtype ``str`` whose ``.kind`` is ``"O"`` --
    the SAME kind as an object column of ndarrays. ``pd.api.types.is_string_dtype``
    is no help either; measured True for an object column of ndarrays. So ``str``
    is recognised by name and a genuine object column is settled by
    ``infer_dtype``, which scans every element and costs 1.28 ms at 200,000 rows.
    """
    pd = sys.modules.get("pandas")
    kind = getattr(dtype, "kind", "O")
    if kind == "f":
        return "numeric"
    if kind in "iub":
        return "integer"
    if str(dtype) == "str":
        return "text"
    if kind == "O" and pd is not None:
        try:
            inferred = pd.api.types.infer_dtype(values, skipna=True)
        except Exception:
            return "other"
        if inferred in ("string", "empty"):
            return "text"
    return "other"


def _nulls(values):
    """Null count via ``pd.isna``, never ``np.isnan``, which RAISES on a string
    or object column."""
    pd = sys.modules.get("pandas")
    if pd is None:
        return 0
    try:
        return int(pd.isna(values).sum())
    except Exception:
        return 0


def _plain(values, kind):
    """The non-null values as a plain numpy array, or None if there is no such form.

    MEASURED, and the reason this is not a bare ``np.asarray``: ``pd.read_csv``
    hands back dtype ``Int64`` -- pandas' MASKED integer -- for an integer column
    with one blank cell. Its ``.kind`` is ``"i"``, so it reaches the integer
    branch below, and ``np.asarray`` on it raises ``ValueError: cannot convert
    float NaN to integer``. That escaped `table.stats` as an `internal` error on
    an ordinary spreadsheet column: nothing in the reduction is wrong, the column
    simply never reaches it.

    Dropping the nulls here rather than reducing around them is what keeps the
    integer sum EXACT -- `pd.NA` has no int form, so the alternative is a float
    accumulator and the 2^53 problem the decimal strings exist to avoid.
    """
    np = sys.modules.get("numpy")
    pd = sys.modules.get("pandas")
    try:
        dtype = getattr(values, "dtype", None)
        if (pd is not None and dtype is not None
                and isinstance(dtype, pd.api.extensions.ExtensionDtype)):
            series = values if isinstance(values, pd.Series) else pd.Series(values)
            series = series.dropna()
            return series.to_numpy(dtype="float64" if kind == "numeric" else "int64")
        return np.asarray(values)
    except Exception:
        return None


def stats(values, dtype=None, scope="column"):
    """The reduction block both `cells.page` and `table.stats` return.

    ``scope`` rides on the block itself so a client cannot print the number
    without being handed what it is over -- the label is data, not a UI
    convention a narrow dock panel might drop.

    The numbers are shaped for the wire, not for Python:

    - EVERY float goes through ``codec._scalar_float``. ``_check_size`` dumps the
      result with ``allow_nan=False``, so a bare NaN -- which ``nanmean`` of an
      all-NaN column returns -- would fail the ENTIRE request with a confusing
      size error instead of showing "NaN".
    - INTEGER sum/min/max are DECIMAL STRINGS and the sum accumulates in Python
      ints. MEASURED: ``np.arange(1, 200001) * 10**13`` sums to
      1400752841041379328 through ``ndarray.sum()`` -- silently wrapped -- and to
      200001000000000000000000 through ``astype(object).sum()``, which costs
      6.4 ms at 200,000 rows and 0.24 ms at 10,000. And
      ``JSON.parse("9007199254740993")`` is 9007199254740992 in every browser, so
      a decimal string is the only exact wire form. The mean stays a float; it is
      an estimate by nature.
    - A ``text`` column reports count / nulls / unique only, and ``other``
      reports nothing at all plus a note. MEASURED: pandas ``.sum()`` on an object
      column of ragged ndarrays raises ``TypeError: operands could not be
      broadcast together with shapes (2,) (3,)``, and on equal-length ones returns
      an ARRAY -- neither is a statistic. ``describe()`` is not the answer either:
      it upcasts int64 to float64, so an integer column would report
      ``min 47.000000``.
    - ``argmin`` / ``argmax`` (0.10.0) on every ``numeric`` and ``integer``
      block say WHERE the extremes are, as positions -- see `_extremes`. Null
      when the column has no number to point at.
    """
    from .codec import _scalar_float

    np = sys.modules.get("numpy")
    if dtype is None:
        dtype = getattr(values, "dtype", None)
    n = int(len(values))
    kind = _stat_kind(values, dtype)
    block = {"scope": scope, "kind": kind, "n": n, "count": 0, "nulls": 0,
             "sum": None, "mean": None, "min": None, "max": None}

    if kind == "other":
        block["nulls"] = _nulls(values)
        block["count"] = n - block["nulls"]
        block["note"] = ("these values are not numbers or text, so statistics "
                         "are not defined for them")
        return block

    if kind == "text":
        block["nulls"] = _nulls(values)
        block["count"] = n - block["nulls"]
        try:
            block["unique"] = int(sys.modules["pandas"].Series(values).nunique())
        except Exception:
            block["unique"] = None
        return block

    array = _plain(values, kind)
    if array is None:
        # A numeric-looking column with no plain form. Say nothing rather than
        # guess: `internal` on a column header click is the one answer a panel
        # cannot render, and a wrong number is worse than a missing one.
        block["nulls"] = _nulls(values)
        block["count"] = n - block["nulls"]
        block["note"] = ("this column has no plain numeric form, so statistics "
                         "are not defined for it")
        return block

    if kind == "numeric":
        # `len(array)`, not `n`: a masked column arrives here with its nulls
        # already dropped, where a plain float64 column arrives whole with its
        # nulls still in it as NaN. Both routes give the same count.
        count = int(len(array) - np.isnan(array).sum())
        block["nulls"] = n - count
        block["count"] = count
        if count:
            total = float(np.nansum(array))
            block["sum"] = _scalar_float(total)
            block["mean"] = _scalar_float(total / count)
            block["min"] = _scalar_float(float(np.nanmin(array)))
            block["max"] = _scalar_float(float(np.nanmax(array)))
        else:
            # Computing them would be `nanmin` over an empty slice: a
            # RuntimeWarning and a bare NaN. Tag it here instead, so an all-NaN
            # column reads as NaN in the UI rather than failing in _check_size.
            nan = _scalar_float(float("nan"))
            block["sum"] = block["mean"] = block["min"] = block["max"] = nan
        block["argmin"], block["argmax"] = (_extremes(values, array, kind)
                                            if count else (None, None))
        return block

    # integer, unsigned and bool
    count = int(len(array))
    block["nulls"] = n - count
    block["count"] = count
    if count:
        exact = int(array.astype(object).sum())
        block["sum"] = str(exact)
        block["mean"] = _scalar_float(float(exact) / count)
        block["min"] = str(int(array.min()))
        block["max"] = str(int(array.max()))
    block["argmin"], block["argmax"] = (_extremes(values, array, kind)
                                        if count else (None, None))
    return block


def _extremes(values, array, kind):
    """``(argmin, argmax)``: the POSITIONS of the first minimum and the first
    maximum, nulls skipped, counted over the column AS GIVEN (0.10.0).

    The statistic says what the extremes are; this says where, which is the
    question a peak always comes with. MEASURED on BasicTerm_S `claims` (121
    cached values over t = 0..120): argmin 120, argmax 108 -- the column rises
    and then falls, so neither is at an end a reader would guess.

    Positions are counted over the column a client pages, never over `array`
    when `_plain` dropped nulls from it: a masked column's array has its nulls
    removed, and a position in it would point past the right row by one for
    every null before the extreme. Measured on `[10, 20, <NA>, 40]` as Int64:
    the maximum is position 3, where the null-dropped array says 2.

    The cost is two more passes. MEASURED at 200,000 rows on CPython 3.11: the
    whole `stats()` block went from 0.75 to 1.36 ms for float64 (nanargmin and
    nanargmax are the slower pair) and from 7.7 to 7.9 ms for int64, where the
    exact sum dominates.

    None for both, rather than a guess, if locating them fails: a client must
    never print a position it was not given.
    """
    np = sys.modules.get("numpy")
    try:
        if len(array) == len(values):
            # Nothing was dropped, so a position in `array` IS a position in
            # the column. A plain float column keeps its NaN here, so it is
            # skipped with nanargmin; a plain integer column has no null.
            if kind == "numeric":
                return int(np.nanargmin(array)), int(np.nanargmax(array))
            return int(np.argmin(array)), int(np.argmax(array))
        series = sys.modules["pandas"].Series(values).reset_index(drop=True)
        return int(series.idxmin(skipna=True)), int(series.idxmax(skipna=True))
    except Exception:
        return None, None


def label_extremes(block, index, codec):
    """Add ``argmin_label`` / ``argmax_label`` to a block from `stats` (0.10.0).

    The index label at each position, codec-encoded (section 5) -- for a
    one-parameter Cells that is the argument: `claims`' maximum is at t=108,
    which arrives as ``{"$t":"np","dtype":"int64","v":108}``. Null where there
    are no labels (``index`` None: an ndarray, an Index) or no position. A
    block without ``argmin`` (text, other) is left alone.

    Through ``codec.encode_inline``, NEVER ``codec.encode``: `encode` turns a
    label encoding over 8,192 bytes into a handle, so the first cut minted one
    on `cells.page` -- which mints none (section 13.2) -- for a Cells keyed by
    two 4,500-character strings, measured. Such a label is an `opaque` tag.
    """
    if not block or "argmin" not in block:
        return
    for key in ("argmin", "argmax"):
        label = None
        if index is not None and block[key] is not None:
            try:
                label = codec.encode_inline(index[block[key]])
            except Exception:
                label = None
        block[key + "_label"] = label


def key_frame(series):
    """``(frame, scalar_values)`` -- one window's rows, built BY HAND.

    Columns are always ``k0..kn-1`` then ``v``. Fixed names cannot collide with a
    parameter name or with the Cells' own name, and the caller carries the real
    labels separately.

    NOT ``reset_index()``: it gives a 1-parameter Cells and an n-parameter Cells
    two DIFFERENT layouts (the extra RangeIndex column) and leaves an
    object-dtype value column untouched. MEASURED on a 2-parameter Cells, the
    hand-built frame pages as three clean typed columns (k0 int64 / k1 int64 /
    v int64, all BigInt64Array, 0 handles) where `_slice` on the same Cells'
    ``.frame`` renders the index as
    ``{"$t":"tuple","v":[{"$t":"opaque","py":"builtins.int","repr":"1"},...]}``.

    THE OBJECT-DTYPE GUARD IS THE LOAD-BEARING HALF, and it branches on the
    SERIES dtype, never on a sample. MEASURED: a 10-row object-dtype series of
    ndarrays paged through `page()` minted TEN HANDLES, one per cell, because
    `_json_column` -> `codec._preview_row` -> `_preview_cell` calls
    ``_enc(value, MAX_DEPTH)`` and `_enc`'s guard is ``depth > MAX_DEPTH``, so at
    exactly MAX_DEPTH it falls through to the ndarray branch and returns
    ``self.handle(value)``. A 200-row page would mint 200 handles against a
    64-entry LRU and evict every handle the Explorer's grid is paging. With the
    repr fallback: 0 handles, measured.

    Under pandas 3.0 a column of strings is dtype ``str``, not ``object``, and
    pages as a JSON column minting nothing -- so ``object`` here means "these are
    not scalars" and the guard needs no further inspection.
    """
    pd = sys.modules["pandas"]
    index = series.index
    frame = pd.DataFrame(index=pd.RangeIndex(len(series)))
    if index.nlevels > 1:
        for level in range(index.nlevels):
            frame["k%d" % level] = index.get_level_values(level).to_numpy()
    else:
        frame["k0"] = index.to_numpy()

    scalar_values = str(getattr(series, "dtype", "object")) != "object"
    if scalar_values:
        frame["v"] = series.to_numpy()
    else:
        frame["v"] = [_short_repr(value) for value in series.to_numpy()]
    return frame, scalar_values


def _short_repr(value):
    try:
        text = repr(value)
    except Exception as exc:
        text = "<unreprable %s: %s>" % (type(value).__name__, exc)
    text = " ".join(text.split())        # an ndarray repr carries newlines
    if len(text) > MAX_CELL_REPR_CHARS:
        text = text[:MAX_CELL_REPR_CHARS] + "..."
    return text


def column(value, kind, col):
    """``(name, the WHOLE column)`` for one column of a handled container.

    Rows are NOT sliced. `_slice` takes a window of both axes, so reusing it for
    a statistic would materialise a 200,000-row column twice -- once clipped to
    the page, once for the reduction -- and would reduce the wrong half anyway.
    ``col`` is indexed exactly as `table.get`'s ``col``.
    """
    pd = sys.modules.get("pandas")
    total_cols = describe(value)[2]
    if col >= total_cols:
        raise bad_request(
            "column %d is past the end of this %s, which has %d column(s)"
            % (col, kind, total_cols), col=col, total_cols=total_cols)

    if kind == "DataFrame":
        return _name(value.columns[col]), value.iloc[:, col]
    if kind == "Series":
        return _name(value.name, "value"), value
    if kind == "Index":
        return _name(value.name, "index"), pd.Series(value) if pd else value
    if value.ndim == 1:
        return "0", value
    return str(col), value[:, col]


def handle_value(codec, h):
    """The live object behind a handle id, or not_found for an evicted one."""
    if not isinstance(h, str):
        raise bad_request("params.h must be a handle id string")
    if h not in codec.handles:
        raise not_found(
            "handle %r is not in this session's store; handles are an LRU of %d "
            "and this one has been evicted -- re-read the value to get a fresh "
            "handle" % (h, codec.max_handles), h=h)
    # Touch it: paging a table keeps it alive, or a long scroll evicts the very
    # handle being scrolled.
    codec.handles.move_to_end(h)
    return codec.handles[h]


# --- 0.10.0: reading by label (section 18) -------------------------------


def locate(value, label):
    """``(row, matches)`` for ``table.get {label}``: the position of the FIRST
    row of a Series' or DataFrame's index carrying ``label``, and how many rows
    carry it.

    ``Index.get_loc``, a lookup: nothing is scanned or materialised. A label
    that matches several rows -- a repeated label, or the leading levels of a
    MultiIndex -- lands on the FIRST of them. get_loc answers those with a
    slice, or with a boolean mask when the repeats are not adjacent
    (measured: [5, 7, 9, 7] gives a mask), and then the rest need NOT be on
    the page: measured, label 7 at rows 0 and 1001 of a 1,002-row Series gave
    a page of rows 0..99 holding one 7. The first cut said "the page shows the
    rest", which a reader trusting it would take as every match. So
    ``matches`` is counted from what get_loc returned -- a slice's length, a
    mask's count -- and a client compares it with the page.
    MEASURED on BasicTerm_S `disc_rate_ann` (151 rows over year 0..150): label
    10 is row 10, label 999 is `not_found`.
    """
    kind = describe(value)[0]
    if kind not in ("Series", "DataFrame"):
        raise bad_request(
            "a label needs an index, and the rows of this %s have positions "
            "only; page it by row" % kind, kind=kind)
    try:
        where = value.index.get_loc(label)
    except KeyError:
        raise not_found("no row labelled %r in this %s" % (label, kind),
                        kind=kind)
    except Exception as exc:
        # A label that cannot be one. A JSON array decodes to a list, and
        # pandas answers that with InvalidIndexError (measured on a RangeIndex,
        # an Index and a MultiIndex) -- neither a KeyError nor a TypeError, so
        # catching those alone lets it out as `internal`. A MultiIndex label is
        # a tuple tag (section 5).
        raise bad_request("%r cannot be an index label here (%s)"
                          % (label, type(exc).__name__), kind=kind)
    row, matches = _rows_of(where, len(value.index))
    if not matches:
        raise not_found("no row labelled %r in this %s" % (label, kind),
                        kind=kind)
    return row, matches


def _rows_of(where, n):
    """``(first row, how many rows)`` from what ``Index.get_loc`` returned: an
    int for a label carried once, a slice for adjacent matches, a boolean mask
    for scattered ones. The count is the slice's length or the mask's count:
    get_loc has already done the lookup, and nothing looks again."""
    np = sys.modules.get("numpy")
    if isinstance(where, slice):
        span = range(*where.indices(n))
        return (span[0] if len(span) else 0), len(span)
    if np is not None and isinstance(where, np.ndarray):
        hits = np.flatnonzero(where)
        return (int(hits[0]) if len(hits) else 0), int(len(hits))
    return int(where), 1


def pick_element(series, label):
    """``(series, missing)`` for ``cells.page {element}``: ``value.loc[label]``
    of each cached value, as a column of scalars over the same keys.

    For a Cells whose values are Series over model points -- every lifelib _ME
    model -- this is one model point across the Cells' arguments, so its
    whole-run statistics and its peak come from `stats` like any other column.
    It reads values that are already cached and computes nothing.

    ``missing`` counts the values that do not carry the label; each is a NaN
    in the column, so `stats` leaves it out of the count. Anything that is not
    one scalar per value is REFUSED by name rather than paged as something
    else: a Cells of scalars (nothing to pick from), a value that is not a
    Series (a DataFrame row is not an element; an ndarray has no labels), a
    label that selects several rows of a value, and a PARTIAL key of a
    MultiIndex, which selects a sub-Series however many rows it has.

    The partial key is refused by its own sentence. The first cut read
    ``value.loc[label]`` and refused any Series it got as "selects N rows",
    which for a leading-level label matching ONE row read "label 2 selects 1
    rows of the value cached for 0, not one element" -- measured, and no hint
    that the full tuple label, which works, was what it wanted.

    ``Index.get_loc`` and ``iloc``, not ``.loc``: the same element where both
    answer, counted by the `_rows_of` that `locate` uses. MEASURED under
    pandas 3.0.6: on a flat index of tuples, ``.loc[(1, 2)]`` raises
    ``IndexingError: Too many indexers`` -- an `internal` error -- where
    ``get_loc((1, 2))`` is row 0.

    An EMPTY cache is answered with the empty column, not refused. The first
    cut tested the dtype first, and a Cells with nothing cached -- whose empty
    series is float64 -- was refused as "values are scalars", blaming the
    values' type for a run that had not happened.
    """
    from .codec import _index_name

    pd = sys.modules["pandas"]
    np = sys.modules["numpy"]
    if len(series) == 0:
        return series, 0
    if str(series.dtype) != "object":
        raise bad_request(
            "element picks one element out of each cached value, and this "
            "Cells' values are scalars (dtype %s); page it without element"
            % series.dtype, dtype=str(series.dtype))
    out, missing = [], 0
    for key, value in zip(series.index, series.to_numpy()):
        if not isinstance(value, pd.Series):
            raise bad_request(
                "element picks by label from Series values, and the value "
                "cached for %s is a %s" % (key, type(value).__name__),
                kind=type(value).__name__)
        index = value.index
        try:
            present = label in index
        except TypeError as exc:
            raise bad_request("%r cannot be an index label here: %s"
                              % (label, exc))
        if not present:
            missing += 1
            out.append(np.nan)
            continue
        try:
            row, rows = _rows_of(index.get_loc(label), len(index))
        except Exception as exc:
            raise bad_request("%r cannot be an index label here (%s)"
                              % (label, type(exc).__name__))
        levels = index.nlevels
        if levels > 1 and not (isinstance(label, tuple) and len(label) == levels):
            names = _index_name(index)
            raise bad_request(
                "label %r is a partial key of the value cached for %s, whose "
                "index has %d levels%s, so it selects %d row%s of it, not one "
                "element; pass the full label, a tuple of %d"
                % (label, key, levels, " (%s)" % names if names else "", rows,
                   "" if rows == 1 else "s", levels),
                rows=rows, levels=levels)
        if rows != 1:
            raise bad_request(
                "label %r selects %d rows of the value cached for %s, not one "
                "element" % (label, rows, key), rows=rows)
        out.append(value.iloc[row])
    return pd.Series(out, index=series.index, name=series.name), missing
