"""The ref grammar: one string names a node, written the way the bridge displays it.

Pure: no modelx, no bridge (tests/test_render.py reads its imports from the
syntax tree).
`parse` turns text into segments; resolving them against a model happens in
tools.py, through `dispatch` only.

    ref      := [MODEL "."] path [call | slice] accessor*
    path     := NAME ( "." NAME | "[" args "]" | "(" args ")" )*
    args     := literal-or-range ("," ...)*, positional and/or NAME=...
              | NAME ("," NAME)*  -- a signature as get_tree prints it:
                                     Projection[point_id], claims(t)
    accessor := ".loc[" literal "]" | ".iloc[" int "]" | "[" int "]" | '["' col '"]'
    range    := range(a, b[, step]) -- in ONE argument position; one node per value
    slice    := NAME "[" a ":" b "]" -- a one-parameter Cells over range(a, b)
"""
import ast

#: Nodes one call may expand to, over every ref, range and derived operand.
MAX_NODES = 200

#: numpy 2 reprs a scalar as np.int64(3), and the bridge builds a display from
#: an argument's repr, so a display pasted back must parse.
_NP_SCALARS = {"int8", "int16", "int32", "int64", "uint8", "uint16", "uint32",
               "uint64", "float16", "float32", "float64", "bool_", "str_"}

EXAMPLES = ("BasicTerm_S.Projection.claims(t=3), BasicTerm_S.Projection[2].pv_net_cf() "
            "or BasicTerm_S.Projection.disc_rate_ann")


class RefError(Exception):
    """A ref that cannot be parsed. The message names the fix."""


class Range(object):
    """range(a, b[, step]) in an argument position."""

    def __init__(self, start, stop, step=1):
        if step == 0:
            raise RefError("range step must not be 0")
        self.start, self.stop, self.step = start, stop, step

    def __len__(self):
        """How many values, without building them. MEASURED: counting with
        len(values()) built the whole list before the 200-node refusal --
        range(0, 10**8) took 19 s and 3.9 GB, and 10**9 was a MemoryError that
        failed the whole call."""
        return len(range(self.start, self.stop, self.step))

    def values(self):
        return list(range(self.start, self.stop, self.step))

    def __repr__(self):
        if self.step == 1:
            return "range(%d, %d)" % (self.start, self.stop)
        return "range(%d, %d, %d)" % (self.start, self.stop, self.step)


def _lit(node, text):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("np", "numpy")
            and node.func.attr in _NP_SCALARS and len(node.args) == 1
            and not node.keywords):
        node = node.args[0]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "range" and not node.keywords
            and 1 <= len(node.args) <= 3):
        vals = [_lit(a, text) for a in node.args]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            raise RefError("range() takes integers in %r" % text)
        if len(vals) == 1:
            vals = [0] + vals
        return Range(*vals)
    try:
        return ast.literal_eval(node)
    except Exception:
        seg = ast.get_source_segment(text, node) or "?"
        raise RefError("cannot read %r in %r as an argument: arguments are "
                       "Python literals such as 3, 'DEATH', None, or "
                       "range(0, 121)" % (seg, text))


def clean(text):
    """Strip what a model pastes around a ref: surrounding backticks, and a
    citation's ' = value' tail.

    The text is used whole when it already parses, so `claims(t = 3)` keeps its
    keyword argument: the first cut split at every ' = ' and broke both that
    and `X.point_id = 1` (found by running the grammar table, spec 5.1).
    """
    s = (text or "").strip()
    whole = s.strip("`").strip()
    try:
        ast.parse(whole, mode="eval")
        return whole
    except SyntaxError:
        pass
    if " = " in s:
        return s.split(" = ", 1)[0].strip().strip("`").strip()
    return whole


def _signature(nodes):
    """[names] when every argument is a bare name -- the signature get_tree
    prints (`Projection[point_id]`, `pols_if_at(t, timing)`) -- else None.
    Resolution reads it as the Space or Cells itself when the names are its
    parameters; any other bare name is refused as before."""
    if nodes and all(isinstance(x, ast.Name) for x in nodes):
        return [x.id for x in nodes]
    return None


def parse(text, examples=EXAMPLES):
    """-> list of (kind, value) segments.

    ('name', str) | ('sub', [args]) | ('call', ([args], {kw: v}))
    | ('slice', (a, b, step)) | ('loc', label) | ('iloc', int)
    | ('params', [names])

    `examples` is the "Write refs like ..." text of a refusal; tools.py passes
    refs from the open model, so a model working on CashValue_ME is not shown
    BasicTerm_S.Projection[2] (CashValue_ME's Projection takes no parameters).
    """
    s = clean(text)
    if not s:
        raise RefError("empty ref")
    try:
        tree = ast.parse(s, mode="eval").body
    except SyntaxError:
        raise RefError("%r is not a ref. Write refs like %s" % (text, examples))
    segs = []

    def walk(node):
        if isinstance(node, ast.Name):
            segs.append(("name", node.id))
        elif isinstance(node, ast.Attribute):
            walk(node.value)
            segs.append(("name", node.attr))
        elif isinstance(node, ast.Subscript):
            base = node.value
            sl = node.slice
            if isinstance(base, ast.Attribute) and base.attr in ("loc", "iloc"):
                walk(base.value)
                val = _lit(sl, s)
                if base.attr == "iloc":
                    if not isinstance(val, int) or isinstance(val, bool):
                        raise RefError(".iloc[...] takes one integer position "
                                       "in %r" % text)
                    segs.append(("iloc", val))
                else:
                    segs.append(("loc", val))
                return
            walk(base)
            elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
            if isinstance(sl, ast.Slice):
                vals = [(_lit(x, s) if x is not None else None)
                        for x in (sl.lower, sl.upper, sl.step)]
                segs.append(("slice", tuple(vals)))
            elif _signature(elts):
                segs.append(("params", _signature(elts)))
            else:
                segs.append(("sub", [_lit(x, s) for x in elts]))
        elif isinstance(node, ast.Call):
            walk(node.func)
            if not node.keywords and _signature(node.args):
                segs.append(("params", _signature(node.args)))
                return
            args = [_lit(a, s) for a in node.args]
            if any(k.arg is None for k in node.keywords):
                raise RefError("**kwargs are not refs: %r" % text)
            kws = dict((k.arg, _lit(k.value, s)) for k in node.keywords)
            segs.append(("call", (args, kws)))
        elif isinstance(node, (ast.BinOp, ast.UnaryOp)):
            raise RefError("%r is arithmetic, which only calculate evaluates: "
                           "calculate([%r])" % (text, s))
        else:
            raise RefError("%r is not a ref (it contains %s). Write refs like %s"
                           % (text, type(node).__name__, examples))
    walk(tree)
    if segs[0][0] != "name":
        raise RefError("%r must start with a name" % text)
    return segs


def fmt_arg(value):
    """An argument as a ref prints it: its repr, and range(a, b) for a Range."""
    return repr(value)


def not_literal(name, text):
    """The refusal of a bare name where a value belongs, worded as _lit's."""
    return RefError("cannot read %r in %r as an argument: arguments are "
                    "Python literals such as 3, 'DEATH', None, or "
                    "range(0, 121)" % (name, text))
