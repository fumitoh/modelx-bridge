"""Codec values (protocol section 5) to text. Pure: JSON in, str out.

Nothing here imports modelx or the bridge, and nothing reads anything but what
the wire carries, so these functions can be ported to TypeScript for Phase 3
and held equal by golden replays (tests/test_golden.py).
"""
import ast
import json

#: A vector this short prints whole. It is the codec's preview size (protocol
#: section 5), so printing it costs no `table.get`.
SHORT = 10
#: A longer vector prints this many elements, then whole-column statistics.
HEAD = 5


def num(v):
    """A JSON number or `num` tag -> text: Python's shortest round-trip repr,
    so a cited 910.92066093366 compares with == (rounding saved 2-13 % of the
    characters and was rejected, spec 6.1)."""
    if isinstance(v, dict) and v.get("$t") == "num":
        return {"NaN": "nan", "Infinity": "inf", "-Infinity": "-inf"}.get(v.get("v"), "nan")
    return repr(v)


def scalar(v):
    """One codec-encoded value -> text. A handle prints its head only."""
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, (int, float)):
        return num(v)
    if isinstance(v, str):
        return repr(v)
    if isinstance(v, list):
        body = ", ".join(scalar(x) for x in v[:20])
        return "[" + body + (", ... %d more" % (len(v) - 20) if len(v) > 20 else "") + "]"
    t = v.get("$t") if isinstance(v, dict) else None
    if t is None:
        items = list(v.items())
        body = ", ".join("%r: %s" % (k, scalar(x)) for k, x in items[:20])
        return "{" + body + (", ... %d more" % (len(items) - 20) if len(items) > 20 else "") + "}"
    if t == "num":
        return num(v)
    if t == "np":
        inner = v.get("v")
        text = scalar(inner) if not isinstance(inner, str) else repr(inner)
        if v.get("dtype") not in ("float64", "int64", "bool"):
            text += " (%s)" % v.get("dtype")
        return text
    if t == "tuple":
        inner = [scalar(x) for x in v.get("v", [])]
        return "(" + ", ".join(inner) + ("," if len(inner) == 1 else "") + ")"
    if t == "str":
        return "%r... [a string of %d chars, first %d shown]" % (
            v.get("v", ""), v.get("len", 0), len(v.get("v", "")))
    if t in ("datetime", "date"):
        return v.get("v")
    if t == "dict":
        items = v.get("items", [])
        return "{" + ", ".join("%s: %s" % (scalar(k), scalar(x)) for k, x in items[:20]) + (
            ", ... %d more" % (len(items) - 20) if len(items) > 20 else "") + "}"
    if t == "mx":
        return "<%s %s>" % (v.get("kind"), v.get("display"))
    if literal_opaque(v):
        return v["repr"]
    if t == "opaque":
        return "<%s %s>" % (v.get("py"), v.get("repr"))
    if t == "handle":
        return head(v)
    # Protocol section 5's decoder rule: an unknown tag never throws.
    return v.get("repr") or json.dumps(v)


#: An `opaque` tag of one of these types is a plain literal. MEASURED: the
#: codec sends each element of a MultiIndex label as one (its preview encodes
#: a tuple at the depth limit), and the label printed as
#: "(<builtins.int 53>, <builtins.int 10>)", which no tool accepts back;
#: premium_table.loc[(53, 10)] reads the same row.
LITERAL_TYPES = ("builtins.int", "builtins.float", "builtins.str", "builtins.bool", "builtins.NoneType")


def literal_opaque(v):
    """True for an opaque tag whose repr is a Python literal of a plain type."""
    if not (isinstance(v, dict) and v.get("$t") == "opaque" and v.get("py") in LITERAL_TYPES):
        return False
    try:
        ast.literal_eval(v.get("repr"))
        return True
    except Exception:
        return False


def cell(v):
    """A preview or page cell (the bridge has already unwrapped numpy there)."""
    if isinstance(v, float):
        return num(v)
    return scalar(v)


def is_handle(v):
    return isinstance(v, dict) and v.get("$t") == "handle"


def head(tag):
    """'Series float64 [4]' / 'DataFrame 10000x6' / 'ndarray float64 [10000]'.
    Never the handle id: an id means nothing to a reader and is evicted from
    the 64-entry LRU within a few calls."""
    if tag.get("shape") is None:
        return "%s (too large to send inline; its size is not sent)" % tag.get("kind")
    shape = tag.get("shape") or []
    kind = tag.get("kind")
    if not shape:
        # MEASURED: np.array(5.0) arrived with shape [] and printed as
        # "ndarray float64 []", an empty-looking head for a value holding one.
        return "%s %s 0-d (one value)" % (kind, tag.get("dtype"))
    if kind == "DataFrame" or len(shape) == 2:
        return "%s %s" % (kind, "x".join(str(s) for s in shape))
    return "%s %s [%s]" % (kind, tag.get("dtype"), "x".join(str(s) for s in shape))


#: Characters of an undescribed handle's repr that a value line prints.
REPR_CHARS = 300


def repr_text(r):
    """A repr whole, or its first REPR_CHARS characters, said to be cut there.

    A fixed-length cut, never one at a guessed element boundary. MEASURED: a
    cut at the last ", " within 300 chars fell inside a string element of
    ['a, b, c' * 40, ...] and printed "['a, b, ca, b, ..., b, ..." -- an
    unclosed quote and an element boundary that is not one. A cut that says
    nothing printed a last element "7" for "76", so this one says it may fall
    inside an element."""
    if len(r) <= REPR_CHARS:
        return "its repr: %s" % r
    return "its repr's first %d chars, cut at that length even inside an element: %s" % (
        REPR_CHARS, r[:REPR_CHARS])


def unshaped(tag):
    """A handle the codec could not describe -- a list, dict or tuple too
    large to send inline: its kind and the start of its repr. MEASURED:
    list(range(5000)) arrived as kind 'list', shape and preview null, and
    printed as "list None []", an empty list, in calculate, get_value and
    run_python. The repr is the bridge's own, itself cut at 2,000 chars."""
    return "%s; %s" % (head(tag), repr_text(tag.get("repr") or ""))


def repr_scalar(r):
    """The plain value a 0-d ndarray's repr holds, as text, or None:
    'array(5.)' -> '5.0', "array('a, dtype=b', dtype='<U10')" -> "'a, dtype=b'".
    The whole inside is tried before the part before its last ', dtype=', so a
    string holding ', dtype=' is not split inside."""
    if not (r.startswith("array(") and r.endswith(")")):
        return None
    inner = r[len("array("):-1]
    cut = inner.rfind(", dtype=")
    for text in [inner] + ([inner[:cut]] if cut >= 0 else []):
        text = text.strip()
        if text in ("nan", "inf", "-inf"):
            return text
        try:
            x = ast.literal_eval(text)
        except Exception:
            continue
        if x is None or isinstance(x, (bool, int, float, complex, str, bytes)):
            return num(x) if isinstance(x, float) else repr(x)
    return None


def zero_d(tag):
    """A 0-d ndarray: the one value it holds, read from the bridge's repr and
    said to be (the codec sends a 0-d array with no preview, and table.get
    pages 1- and 2-D values only). MEASURED: np.array(5.0) arrived as shape
    [], preview null, repr "array(5.)", and printed as "ndarray float64 []" --
    an empty-looking value for a non-empty one -- in calculate, get_value and
    run_python."""
    r = tag.get("repr") or ""
    v = repr_scalar(r)
    if v is None:
        return "%s; %s" % (head(tag), repr_text(r))
    return "%s: %s, read from its repr %s" % (head(tag), v, r)


def stat(st, key):
    v = st.get(key)
    if v is None:
        return "-"
    if isinstance(v, str):          # integer statistics arrive as decimal strings
        return v
    return num(v)


def at_text(st, key, index_name, aligned):
    """' at t=87' / ' at policy_id=1370' / ' at [1369] (policy_id 1370)'.
    Empty when the bridge gave no position: a position is never guessed."""
    pos = st.get("arg" + key)
    if pos is None:
        return ""
    label = st.get("arg%s_label" % key)
    if label is not None:
        return " at %s=%s" % (index_name or "label", scalar(label))
    if aligned is not None and pos in aligned:
        return " at [%d] (%s)" % (pos, aligned[pos])
    return " at [%d]" % pos


def stats_line(st, n, scope, index_name=None, aligned=None):
    """One statistics line from a bridge stats block (protocol 10.5, 18.2)."""
    kind = st.get("kind")
    if kind in ("numeric", "integer"):
        if not st.get("count"):
            return "%s: no numbers (all %d values are null)" % (scope, n)
        return ("%s: sum %s, mean %s, min %s%s, max %s%s%s"
                % (scope, stat(st, "sum"), stat(st, "mean"),
                   stat(st, "min"), at_text(st, "min", index_name, aligned),
                   stat(st, "max"), at_text(st, "max", index_name, aligned),
                   (", %d null" % st["nulls"]) if st.get("nulls") else ""))
    if kind == "text":
        return "%s: %s text values, %s distinct, %s null" % (
            scope, st.get("count"), st.get("unique"), st.get("nulls"))
    return "%s: no statistics (%s)" % (scope, st.get("note") or "not numbers")


def grid(header, rows, indent="  "):
    widths = [max([len(str(h))] + [len(r[i]) for r in rows]) for i, h in enumerate(header)]
    out = [indent + "  ".join(str(h).rjust(w) for h, w in zip(header, widths))]
    for r in rows:
        out.append(indent + "  ".join(c.rjust(w) for c, w in zip(r, widths)))
    return out


def clip(lines, max_chars, cut_hint):
    """Join lines under a character bound. The cut line counts what it dropped
    and names the call that returns it; the result never exceeds max_chars."""
    out, used = [], 0
    # Room for the cut line itself: at least 200, more for a long hint.
    # MEASURED: with calculate's 144-char "NOT everything was computed" hint
    # the cut line is 219 chars, and a fixed 200 let the output reach 12,021
    # chars at the 12,000 bound.
    worst = "[output bound %d chars reached: %d more lines (%d chars) not shown. %s]" % (
        max_chars, len(lines), sum(len(x) + 1 for x in lines), cut_hint)
    reserve = max(200, len(worst) + 1)
    for k, line in enumerate(lines):
        if used + len(line) + 1 > max_chars - reserve:
            rest = lines[k:]
            out.append("[output bound %d chars reached: %d more line%s (%d chars) "
                       "not shown. %s]" % (max_chars, len(rest),
                                           "" if len(rest) == 1 else "s",
                                           sum(len(x) + 1 for x in rest), cut_hint))
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def wrap(items, indent="    ", width=100):
    lines, cur = [], indent
    for i, item in enumerate(items):
        piece = item + ("," if i < len(items) - 1 else "")
        if len(cur) + len(piece) + 1 > width and cur.strip():
            lines.append(cur.rstrip())
            cur = indent
        cur += piece + " "
    if cur.strip():
        lines.append(cur.rstrip())
    return lines
