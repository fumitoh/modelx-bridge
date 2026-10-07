"""JSON value codec (docs/bridge-protocol-v0.md section 5).

JSON natives pass through; everything else becomes a tagged object {"$t": ...}.
Encoding MUST NEVER raise: a value the codec does not understand degrades to
`opaque`, and a repr that itself raises degrades to a placeholder.

numpy and pandas are looked up in sys.modules rather than imported. A DataFrame
cannot exist unless pandas is already imported, so the lookup is both correct and
free -- and it keeps the codec importable in a kernel where neither is installed.
"""

import collections
import datetime
import json
import math
import sys

MAX_INLINE_BYTES = 8192
MAX_MESSAGE_BYTES = 1048576
MAX_HANDLES = 64
MAX_STR_CHARS = 4096
MAX_REPR_CHARS = 2000
PREVIEW_ROWS = 10
PREVIEW_COLS = 10
# A preview cell is one table cell in the UI. Budgeting it at MAX_STR_CHARS let a
# 10x10 block of wide strings make a "small" handle tag 41 KB -- five times
# max_inline_bytes -- and a batch of those could push value.get over
# max_message_bytes, which fails the WHOLE request. A handle tag has to be
# bounded by construction.
PREVIEW_STR_CHARS = 120
PREVIEW_CELL_BYTES = 256
MAX_DEPTH = 12


def limits(max_inline_bytes=MAX_INLINE_BYTES, max_handles=MAX_HANDLES):
    return {
        "max_inline_bytes": max_inline_bytes,
        "max_message_bytes": MAX_MESSAGE_BYTES,
        "max_handles": max_handles,
    }


def _safe_repr(value, limit=MAX_REPR_CHARS):
    try:
        text = repr(value)
    except Exception as exc:              # a broken __repr__ must not fail a response
        return "<unreprable %s: %s>" % (type(value).__name__, exc)
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _num(value):
    if value != value:
        return {"$t": "num", "v": "NaN"}
    return {"$t": "num", "v": "Infinity" if value > 0 else "-Infinity"}


def _scalar_float(value):
    return value if math.isfinite(value) else _num(value)


def _mx_ref(value):
    """Return an `mx` tag if `value` is a modelx object, else None."""
    mx = sys.modules.get("modelx")
    if mx is None:
        return None
    from modelx.core.base import Interface
    from modelx.core.reference import ReferenceProxy
    if isinstance(value, Interface):
        return {"$t": "mx", "kind": type(value).__name__,
                "obj": value._idstr, "display": value._get_repr()}
    if isinstance(value, ReferenceProxy):
        attrs = value._get_attrdict()
        return {"$t": "mx", "kind": attrs.get("type", "Reference"),
                "obj": attrs.get("namedid", ""), "display": attrs.get("repr", "")}
    return None


class Codec:
    """Encodes Python values for the wire and decodes request arguments.

    Holds the session's handle store: an LRU keyed by handle id. v0 has no way to
    read a handle beyond its preview, so the store exists to give `h` a defined
    lifetime (and for v1's table.get to page out of).
    """

    def __init__(self, max_inline_bytes=MAX_INLINE_BYTES, max_handles=MAX_HANDLES):
        self.max_inline_bytes = max_inline_bytes
        self.max_handles = max_handles
        self.handles = collections.OrderedDict()
        self._counter = 0

    def limits(self):
        return limits(self.max_inline_bytes, self.max_handles)

    # -- encoding ---------------------------------------------------------

    def encode(self, value):
        encoded = self._enc(value, 0)
        if isinstance(encoded, dict) and encoded.get("$t") == "handle":
            # _enc already allocated a handle (DataFrame, Series, Index,
            # ndarray). Promoting again would burn a SECOND id for the same
            # object, halving the store's effective capacity, and would return
            # an oversize tag anyway -- the promotion cannot shrink a handle.
            return encoded
        if isinstance(encoded, (list, dict)):
            if _json_size(encoded) > self.max_inline_bytes:
                return self.handle(value)
        return encoded

    def encode_inline(self, value):
        """``value`` encoded for a field that must NEVER mint a handle: the
        index labels of a stats block, ``argmin_label`` / ``argmax_label``
        (0.10.0, section 18.2).

        `encode` promotes a list or dict encoding over ``max_inline_bytes`` to
        a handle, and `_enc` makes one of any pandas or numpy container. Either
        is wrong for a label riding on `cells.page`, which mints no handle
        (section 13.2): MEASURED, a Cells keyed by two 4,500-character strings
        gave its ``argmax_label`` as ``{"$t":"handle","h":"h1",...}``, minting
        an LRU entry that can evict the handle a grid is paging, and a label
        that cannot be sent back -- ``decode`` refuses a `handle` tag. And an
        object Index holds an ndarray or a Series as a label, measured under
        pandas 3.0.6, which `table.stats` would have minted for.

        Both are an `opaque` tag with the repr instead: the codec's fallback,
        bounded by construction, and as unsendable as the handle was but
        without the LRU entry. Everything else encodes exactly as `encode`
        would encode it.
        """
        encoded = self._enc(value, 0, mint=False)
        if (isinstance(encoded, (list, dict))
                and _json_size(encoded) > self.max_inline_bytes):
            return _unminted(value)
        return encoded

    def _enc(self, value, depth, mint=True):
        if value is None or value is True or value is False:
            return value
        if depth > MAX_DEPTH:
            return {"$t": "opaque", "py": _pyname(value), "repr": _safe_repr(value)}

        # Exact types first, then the container and library branches, then
        # subclasses. Order matters: np.float64 IS a float subclass, so an
        # isinstance(value, float) test up here would silently drop its dtype.
        kind = type(value)
        if kind is int:
            return value
        if kind is float:
            return _scalar_float(value)
        if kind is str:
            return self._enc_str(value)

        np = sys.modules.get("numpy")
        if np is not None:
            if isinstance(value, np.ndarray):
                return self.handle(value) if mint else _unminted(value)
            if isinstance(value, np.generic):
                return self._enc_npscalar(value, np)

        pd = sys.modules.get("pandas")
        if pd is not None and isinstance(value, (pd.DataFrame, pd.Series, pd.Index)):
            return self.handle(value) if mint else _unminted(value)

        if isinstance(value, datetime.datetime):
            return {"$t": "datetime", "v": value.isoformat()}
        if isinstance(value, datetime.date):
            return {"$t": "date", "v": value.isoformat()}

        if isinstance(value, tuple):
            return {"$t": "tuple", "v": [self._enc(v, depth + 1, mint)
                                         for v in value]}
        if isinstance(value, list):
            return [self._enc(v, depth + 1, mint) for v in value]
        if isinstance(value, dict):
            return self._enc_dict(value, depth, mint)

        tagged = _mx_ref(value)
        if tagged is not None:
            return tagged

        # subclasses of the JSON natives (IntEnum, a str subclass, ...)
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return _scalar_float(float(value))
        if isinstance(value, str):
            return self._enc_str(str(value))

        return {"$t": "opaque", "py": _pyname(value), "repr": _safe_repr(value)}

    def _enc_str(self, value):
        if len(value) <= MAX_STR_CHARS:
            return value
        return {"$t": "str", "v": value[:MAX_STR_CHARS],
                "len": len(value), "truncated": True}

    def _enc_npscalar(self, value, np):
        try:
            item = value.item()
        except Exception:
            return {"$t": "opaque", "py": _pyname(value), "repr": _safe_repr(value)}
        dtype = str(value.dtype)
        if isinstance(item, float) and not math.isfinite(item):
            return {"$t": "np", "dtype": dtype, "v": _num(item)}
        if isinstance(item, (int, float, str, bool)) or item is None:
            return {"$t": "np", "dtype": dtype, "v": item}
        # complex, np.void, ... : keep the np tag out of it rather than lie
        return {"$t": "opaque", "py": _pyname(value), "repr": _safe_repr(value)}

    def _enc_dict(self, value, depth, mint=True):
        plain = "$t" not in value and all(isinstance(k, str) for k in value)
        if plain:
            return dict((k, self._enc(v, depth + 1, mint))
                        for k, v in value.items())
        return {"$t": "dict", "items": [
            [self._enc(k, depth + 1, mint), self._enc(v, depth + 1, mint)]
            for k, v in value.items()]}

    # -- handles ----------------------------------------------------------

    def handle(self, value):
        self._counter += 1
        h = "h%d" % self._counter
        self.handles[h] = value
        while len(self.handles) > self.max_handles:
            self.handles.popitem(last=False)

        tag = {"$t": "handle", "h": h, "kind": type(value).__name__,
               "dtype": None, "shape": None, "columns": None,
               "index_preview": None, "preview": None, "repr": _safe_repr(value)}
        try:
            self._describe(value, tag)
        except Exception:
            pass                       # a preview is a nicety; the handle still works
        return tag

    def _describe(self, value, tag):
        pd = sys.modules.get("pandas")
        np = sys.modules.get("numpy")

        if pd is not None and isinstance(value, pd.DataFrame):
            tag["shape"] = list(value.shape)
            tag["columns"] = [_clip(str(c)) for c in value.columns[:PREVIEW_COLS]]
            tag["index_preview"] = self._preview_row(value.index[:PREVIEW_ROWS])
            tag["index_name"] = _index_name(value.index)
            block = value.iloc[:PREVIEW_ROWS, :PREVIEW_COLS]
            tag["preview"] = [[self._preview_cell(v) for v in row]
                              for row in block.values.tolist()]
        elif pd is not None and isinstance(value, pd.Series):
            tag["dtype"] = str(value.dtype)
            tag["shape"] = [len(value)]
            tag["columns"] = [_clip(str(value.name))
                              if value.name is not None else "value"]
            tag["index_preview"] = self._preview_row(value.index[:PREVIEW_ROWS])
            tag["index_name"] = _index_name(value.index)
            tag["preview"] = [[v] for v in self._preview_row(
                value.values[:PREVIEW_ROWS])]
        elif pd is not None and isinstance(value, pd.Index):
            tag["dtype"] = str(value.dtype)
            tag["shape"] = [len(value)]
            tag["columns"] = [_clip(str(value.name))
                              if value.name is not None else "index"]
            tag["preview"] = [[v] for v in self._preview_row(value[:PREVIEW_ROWS])]
        elif np is not None and isinstance(value, np.ndarray):
            tag["dtype"] = str(value.dtype)
            tag["shape"] = list(value.shape)
            block = value[:PREVIEW_ROWS] if value.ndim else value
            if value.ndim >= 2:
                block = value[:PREVIEW_ROWS, :PREVIEW_COLS]
                tag["preview"] = [[self._preview_cell(v) for v in row]
                                  for row in block.tolist()]
            elif value.ndim == 1:
                tag["preview"] = [[self._preview_cell(v)] for v in block.tolist()]

    def _preview_row(self, values):
        """Encode a preview row, unwrapping numpy scalars to Python ones first:
        a preview is for display, and an `np` tag per cell would triple its size."""
        try:
            values = values.tolist()
        except AttributeError:
            values = list(values)
        return [self._preview_cell(v) for v in values]

    def _preview_cell(self, value):
        """One table cell of a handle preview, bounded by construction.

        The full encoder is right for a VALUE the user asked for; it is wrong for
        a thumbnail, where 100 cells each allowed 4096 characters (or an opaque
        repr of 2000) make the "small" tag that describes a big object bigger
        than the inline limit it was supposed to avoid.
        """
        if isinstance(value, str):
            text = str(value)
            if len(text) > PREVIEW_STR_CHARS:
                return {"$t": "str", "v": text[:PREVIEW_STR_CHARS],
                        "len": len(text), "truncated": True}
            return text
        encoded = self._enc(value, MAX_DEPTH)
        if (isinstance(encoded, (list, dict))
                and _json_size(encoded) > PREVIEW_CELL_BYTES):
            return {"$t": "opaque", "py": _pyname(value),
                    "repr": _safe_repr(value, PREVIEW_STR_CHARS)}
        return encoded

    # -- decoding ---------------------------------------------------------

    def decode(self, value):
        """Decode one codec-encoded request argument. Raises BridgeError on an
        unknown tag -- a request we cannot read exactly must not be guessed at."""
        from .errors import bad_request

        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if not isinstance(value, dict):
            return value
        tag = value.get("$t")
        if tag is None:
            return dict((k, self.decode(v)) for k, v in value.items())
        if tag == "tuple":
            return tuple(self.decode(v) for v in value.get("v", []))
        if tag == "num":
            return {"NaN": float("nan"), "Infinity": float("inf"),
                    "-Infinity": float("-inf")}.get(value.get("v"), float("nan"))
        if tag == "np":
            return self.decode(value.get("v"))
        if tag == "str":
            return value.get("v", "")
        if tag == "datetime":
            return datetime.datetime.fromisoformat(value["v"])
        if tag == "date":
            return datetime.date.fromisoformat(value["v"])
        if tag == "dict":
            return dict((_hashable(self.decode(k)), self.decode(v))
                        for k, v in value.get("items", []))
        raise bad_request("cannot decode argument tagged %r" % tag, tag=tag)


def _clip(text, limit=PREVIEW_STR_CHARS):
    return text if len(text) <= limit else text[:limit] + "..."


def _index_name(index):
    """`index_name` on a Series' or DataFrame's handle tag (0.10.0).

    What the labels in `index_preview` ARE -- `year` for BasicTerm_S's
    `disc_rate_ann`, `point_id` for its `model_point_table` -- so a reader can
    print "max at year=150" from the tag alone instead of paging the index to
    find a header. One string; null when the index has no name. A MultiIndex's
    level names are joined with ", " IN LEVEL ORDER (lifelib's BasicTerm_SE
    `premium_table` gives "age_at_entry, policy_term"), with an unnamed level
    written as None: dropping it would pair the remaining names with the wrong
    positions of a tuple label. Clipped like every other preview string.

    Only Series and DataFrame tags carry the key. An ndarray has no index at
    all, and an absent key says that; null would say "an unnamed index".
    """
    names = list(getattr(index, "names", None) or ())
    if not any(name is not None for name in names):
        return None
    return _clip(", ".join("None" if name is None else str(name)
                           for name in names))


def _hashable(key):
    return tuple(key) if isinstance(key, list) else key


def _unminted(value):
    """What `encode_inline` gives where `encode` would mint a handle."""
    return {"$t": "opaque", "py": _pyname(value), "repr": _safe_repr(value)}


def _pyname(value):
    cls = type(value)
    module = getattr(cls, "__module__", "")
    return (module + "." + cls.__name__) if module else cls.__name__


def _json_size(encoded):
    try:
        return len(json.dumps(encoded))
    except (TypeError, ValueError):
        return 0        # cannot happen for codec output; do not demote on a bug
