"""The six tools, over one `dispatch(method, params)` callable.

This module never imports modelx or modelx_bridge (tests/test_render.py reads
its imports from the syntax tree). Every bridge result is round-tripped through JSON before
anything reads it, so no tool can depend on an in-process object the Phase 3
comm would not carry, and a golden replay through a fake `dispatch` gives the
same text byte for byte (tests/test_golden.py).

EXACTLY ONE TOOL, `calculate`, EVER PASSES `evaluate: true`. Every other tool
reads with `evaluate: false`, `trace.succs` (which never evaluates) or methods
that cannot evaluate; tests/test_basicterm_s.py holds that by measuring
(computed nodes, revision, ItemSpaces) around every reader call.

Written against bridge 0.10.0 WITHOUT its deferred B1 and B2 (protocol 18.8):

- a failed evaluation does not move the revision, although modelx keeps what
  it computed before the failure, so `calculate` reports the change from
  `session.info.computed` and says a failed calculation keeps it;
- `value.get`'s `display` echoes the arguments as requested, so one node can
  print as `pv_claims()` here and `pv_claims(kind=None)` in a trace. Both forms
  resolve to the same node, because the bridge fills trailing defaults when it
  looks a node up; tests/test_synthetic.py pins that.
"""
import ast
import difflib
import json
import math
import re
import time

from . import render as R
from .refs import EXAMPLES, MAX_NODES, Range, RefError, clean, fmt_arg, not_literal, parse

MAX_CHARS = 12000          # the task judge measured 6,000 cutting a 121-node slice at t=46
MAX_REFS = 50
MAX_FORMULA_REFS = 12
CHUNK = 32                 # value.get nodes per dispatch: every handle a chunk mints
                           # stays alive in the 64-entry LRU until this call reads it
DOC_PAGE = 8000            # BasicTerm_S's 6,824-char Projection docstring is one call
TRACE_GROUPS = 40
GROUP_MIN = 4              # a Cells with fewer neighbours prints each one: the first cut
                           # showed the first and last of CashValue_ME's three claims
                           # kinds and hid LAPSE
FANOUT = 200               # depth > 1 never expands a node with more neighbours
MAX_NEIGHBOURS = 5000      # CashValue_ME model_point() has 4,575 dependents: 634,002
                           # bytes with values:false; above this the list is not asked for
BODY_CHARS = 1500          # formula body printed by trace, after the answer
PAGE_ROWS = 20
MAX_ROWS = 200
STAT_COLS = 12
RANGE_ROWS = 130
MAX_OPERANDS = 20

NO_MODEL = ("no model is open. The server opens models only at launch (--sample, --open); "
            "its instructions name any that failed to open, and why")

#: What can read part of a value table.get cannot page: a list, dict or tuple
#: too large to send inline, a 0-d ndarray, an ndarray of 3 or more
#: dimensions. Only run_python, and this module cannot see whether the server
#: enabled it. MEASURED: [3] on a 2,000-item list was refused "write
#: .loc[label] or .iloc[position]", and .iloc[3] then failed "bad_request:
#: table.get cannot page a list".
NO_READER = ("no tool here reads part of it (table.get pages values of 1 and 2 dimensions only); "
             "run_python can, and only when the server was started with --allow-python")


#: calculate's cut hint when a ref gave no value. MEASURED: its cut line was
#: 219 chars at the 12,000 bound, more than the 200 render.clip reserved
#: (test_render.py).
NOT_EVERYTHING = ("NOT everything was computed: the lines at the top count what was refused or FAILED; "
                  "get_value(refs) re-reads what was computed, without computing")


def _unpageable(tag):
    """True for a handle whose parts table.get cannot read (see NO_READER)."""
    shape = tag.get("shape")
    return shape is None or len(shape) == 0 or len(shape) > 2


class WireError(Exception):
    """A bridge error as the wire carries it (protocol section 3)."""

    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code, self.message, self.data = code, message, data or {}


class ToolError(Exception):
    """A whole-call failure, or a per-ref one inside a batch. One sentence that
    names the fix; the server sets isError for a whole-call one."""


class Node(object):
    def __init__(self, model, kind, obj, name, params=None, entry=None):
        self.model, self.kind, self.obj, self.name = model, kind, obj, name
        self.params = params or []
        self.entry = entry
        self.args = None          # positional args, None when no call was given
        self.expand = None        # (position, Range) for a range or slice
        self.accessors = []       # [('loc', x) | ('iloc', i) | ('pos', i) | ('col', name)]
        self.notes = []
        self.missing_item = None  # printed name of an ItemSpace that does not exist
        self.created = []         # ItemSpaces this resolution created
        self.space_obj = ""
        self.space_entry = None
        self.prefix = model       # printed name of the containing Space (of a Space: its own)
        self.text = None
        # The resolution's facts, never re-read from printed text: the base-Space
        # note once tested '[' in the prefix's last dotted part, and
        # Projection[2.0] or Rate[0.03] split inside the key (finding 7).
        self.item = False         # its own Space was given [k] or (k)
        self.inside_item = False  # its obj lies inside an existing ItemSpace, so the
                                  # base Space's tree entry does not hold its counts

    def argsets(self):
        if self.expand is None:
            return [list(self.args or [])]
        pos, rng = self.expand
        out = []
        for x in rng.values():
            a = list(self.args)
            a[pos] = x
            out.append(a)
        return out


def enc(a):
    """A Python literal as a codec value for a request."""
    if isinstance(a, tuple):
        return {"$t": "tuple", "v": [enc(x) for x in a]}
    if isinstance(a, float) and not math.isfinite(a):
        return {"$t": "num", "v": "NaN" if a != a else ("Infinity" if a > 0 else "-Infinity")}
    return a


def dec(v):
    """A codec value back to a comparable Python literal (labels, args)."""
    if isinstance(v, dict):
        t = v.get("$t")
        if t == "np":
            return dec(v.get("v"))
        if t == "tuple":
            return tuple(dec(x) for x in v.get("v", []))
        if t == "num":
            return float("nan")
        if R.literal_opaque(v):
            # A MultiIndex label's elements (see render.literal_opaque): as
            # dicts they made the label unhashable, and a range over such a
            # Series failed with "TypeError: unhashable type: 'dict'".
            return ast.literal_eval(v["repr"])
    return v


def _q(text):
    return json.dumps(text)


def _computed(m):
    """session.info's `computed`, or None. None is UNKNOWN (the bridge could
    not read modelx's graph), and is never printed as zero."""
    c = (m or {}).get("computed")
    return c if isinstance(c, int) and not isinstance(c, bool) else None


UNKNOWN_COMPUTED = "computed nodes unknown (the bridge could not read modelx's graph)"


class Tools(object):

    def __init__(self, dispatch, max_chars=MAX_CHARS, align="model_point", journal=None,
                 clock=time.perf_counter):
        self._dispatch = dispatch
        self.clock = clock
        self.max_chars = max_chars
        self.align = align
        self.journal = journal       # callable(record) or None
        self.calls = []
        self._memo = {}
        self._created_log = []

    # -- bridge -------------------------------------------------------------

    def d(self, method, params):
        t0 = self.clock()
        try:
            result = self._dispatch(method, params)
        except WireError as e:
            self.calls.append({"method": method, "params": params,
                               "ms": (self.clock() - t0) * 1000,
                               "error": {"code": e.code, "message": e.message, "data": e.data}})
            # A handle id means nothing to a reader (spec 6.1), and the
            # bridge's eviction message names one.
            e.message = _HANDLE_ID.sub("a handle", e.message)
            raise
        text = json.dumps(result)
        record = {"method": method, "params": params,
                  "ms": (self.clock() - t0) * 1000, "chars": len(text)}
        if self.journal:
            record["result"] = json.loads(text)
        self.calls.append(record)
        return json.loads(text)

    def begin(self):
        """Start a tool call. Nothing is memoised across calls, so no tool can
        print a tree, a map or an ItemSpace lookup another call left behind."""
        self.calls = []
        self._memo = {}
        self._created_log = []      # (model, label) of every ItemSpace created this call

    def end(self, tool, args, text):
        if self.journal:
            self.journal({"tool": tool, "args": args, "calls": self.calls, "text": text})
        return text

    def info(self):
        if "info" not in self._memo:
            self._memo["info"] = self.d("session.info", {})
        return self._memo["info"]

    def model_names(self):
        return [m["name"] for m in self.info()["models"]]

    def model_info(self, name):
        for m in self.info()["models"]:
            if m["name"] == name:
                return m
        return None

    def tree(self, model):
        key = ("tree", model)
        if key not in self._memo:
            self._memo[key] = self.d("tree.get", {"model": model})
        return self._memo[key]

    def item_tree(self, model, obj):
        """tree.get of a Space inside an ItemSpace, by the obj its `mx` tag
        named: ITS OWN Cells' cached counts. The model's tree.get does not
        recurse into ItemSpaces, so a ref's base-Space entry holds the BASE's
        counts. MEASURED after calculate(Projection[3].pv_net_cf()):
        get_formulas printed Projection[3].claims "nothing cached" (it held
        121), and get_tree(path="Projection[3]") "none computed"."""
        key = ("itree", model, obj)
        if key not in self._memo:
            self._memo[key] = self.d("tree.get", {"model": model, "obj": obj})["root"]
        return self._memo[key]

    def several_spaces(self, model):
        """True when a formula in another Space could name this Space's Cells.
        map.get reads ONE Space's formulas, so "none (an output)" was printed
        for Assumptions.rate while Results.total() = 100 * Assumptions.rate()
        read it, and trace listed that reader one line above."""
        return sum(1 for _, e in self._all_entries(self.tree(model)["root"])
                   if e.get("kind", "").endswith("Space")) > 1

    def map_of(self, model, space_obj):
        key = ("map", model, space_obj)
        if key not in self._memo:
            try:
                self._memo[key] = self.d("map.get", {"model": model, "obj": space_obj})
            except WireError:
                self._memo[key] = None
        return self._memo[key]

    # -- resolution ---------------------------------------------------------

    def resolve(self, text, create=False):
        """A ref -> Node, through tree.get and (for ItemSpaces) value.get only.
        `obj` is never built from text: it is a tree entry's, or the one the
        bridge's `mx` tag names. Every non-exact match leaves a note."""
        try:
            segs = parse(text)
        except RefError as e:
            if EXAMPLES not in str(e):
                raise
            raise RefError(str(e).replace(EXAMPLES, self._examples()))
        names = self.model_names()
        if not names:
            raise ToolError(NO_MODEL)
        if segs[0][0] == "name" and segs[0][1] in names:
            return self._resolve_in(text, segs[0][1], segs[1:], create)
        if len(names) == 1:
            return self._resolve_in(text, names[0], segs, create)
        hits = []
        for m in names:
            try:
                hits.append(self._resolve_in(text, m, list(segs), False, probe=True))
            except (ToolError, RefError):
                pass
        if len(hits) == 1:
            node = self._resolve_in(text, hits[0].model, segs, create)
            node.notes.insert(0, "model %s inferred: the only open model where %r "
                                 "resolves" % (node.model, text))
            return node
        if hits:
            raise ToolError("%r resolves in %d open models; start it with one: %s"
                            % (text, len(hits), ", ".join("%s.%s" % (h.model, text.strip())
                                                          for h in hits)))
        raise ToolError("%r resolves in none of the open models (%s)"
                        % (text, ", ".join(names)))

    def _examples(self):
        """The "Write refs like ..." of a refusal, from an open model. MEASURED
        on CashValue_ME: the fixed BasicTerm_S text offered
        BasicTerm_S.Projection[2].pv_net_cf(), and Projection[2] there was an
        internal AttributeError (its Projection takes no parameters). Only forms
        that need no invented value: a Cells over t, a Cells of no parameters,
        a Reference."""
        names = self.model_names()
        if not names or "BasicTerm_S" in names:
            return EXAMPLES
        m = names[0]
        found = {}
        for _, e in self._all_entries(self.tree(m)["root"]):
            kind, params = e.get("kind", ""), e.get("parameters") or []
            if kind == "Cells" and params == ["t"]:
                found.setdefault("t", "%s.%s(t=3)" % (m, e["obj"]))
            elif kind == "Cells" and not params:
                found.setdefault("call", "%s.%s()" % (m, e["obj"]))
            elif kind == "Reference":
                found.setdefault("ref", "%s.%s" % (m, e["obj"]))
        got = [found[k] for k in ("t", "call", "ref") if k in found]
        if not got:
            return EXAMPLES + " (from the BasicTerm_S sample, which is not open)"
        return ", ".join(got[:-1]) + (" or " if len(got) > 1 else "") + got[-1]

    def _children(self, entry):
        out = []
        for key in ("spaces", "cells", "refs"):
            out.extend(entry.get(key) or [])
        return out

    def _all_entries(self, entry, path=()):
        for e in self._children(entry):
            yield path, e
            if e.get("kind", "").endswith("Space"):
                for x in self._all_entries(e, path + (e,)):
                    yield x

    def _lookup(self, model, cur, name, first, notes, text):
        for e in self._children(cur):
            if e["name"] == name:
                return cur, e
        cands = [e for e in self._children(cur) if e["name"].lower() == name.lower()]
        if len(cands) == 1:
            notes.append("%r matched %s (case-insensitive)" % (name, cands[0]["name"]))
            return cur, cands[0]
        if first:
            root = self.tree(model)["root"]
            hits = [(p, e) for p, e in self._all_entries(root) if e["name"] == name]
            if len(hits) == 1:
                path, e = hits[0]
                where = path[-1] if path else root
                notes.append("%r resolved to %s.%s" % (name, model, e["obj"]))
                return where, e
            if not hits:
                hits = [(p, e) for p, e in self._all_entries(root)
                        if e["name"].lower() == name.lower()]
                if len(hits) == 1:
                    path, e = hits[0]
                    notes.append("%r resolved to %s.%s (case-insensitive)" % (name, model, e["obj"]))
                    return (path[-1] if path else root), e
            if len(hits) > 1:
                raise ToolError("%r is ambiguous in %s: %s; write the Space too"
                                % (name, model, ", ".join("%s.%s" % (model, e["obj"])
                                                          for _, e in hits)))
        pool = [e["name"] for _, e in self._all_entries(self.tree(model)["root"])]
        close = difflib.get_close_matches(name, pool, n=3, cutoff=0.6)
        raise ToolError("no %r in %s%s get_tree(model=%s, filter=%s) lists names"
                        % (name, model if not cur.get("obj") else "%s.%s" % (model, cur["obj"]),
                           ("; did you mean " + ", ".join(close) + "?") if close else ".",
                           _q(model), _q(name[:4].lower())))

    def _positional(self, name, params, call, text):
        args, kws = call
        out = list(args)
        for k, v in kws.items():
            if k not in params:
                raise ToolError("%s takes (%s); there is no parameter %r"
                                % (name, ", ".join(params), k))
            i = params.index(k)
            if i < len(args):
                raise ToolError("%s: %r is given twice" % (name, k))
            while len(out) <= i:
                out.append(_MISSING)
            out[i] = v
        if any(a is _MISSING for a in out):
            gap = [params[j] for j, a in enumerate(out) if a is _MISSING]
            raise ToolError("%s(%s): give %s too; arguments are positional on the "
                            "bridge, so an earlier one cannot be skipped"
                            % (name, ", ".join(params), ", ".join(gap)))
        if len(out) > len(params):
            raise ToolError("%s takes (%s) but %d arguments were given"
                            % (name, ", ".join(params), len(out)))
        return out

    def _resolve_in(self, text, model, segs, create, probe=False):
        notes = []
        root = self.tree(model)["root"]
        if not segs:
            node = Node(model, "Model", "", model, entry=root)
            node.text = model
            return node
        cur = root
        prefix = model
        item_obj = None          # obj of the ItemSpace we are inside, if any
        missing = None
        created = []
        i, first = 0, True
        is_item = False
        node = None
        while i < len(segs):
            kind, val = segs[i]
            if kind != "name":
                raise ToolError("%r: %s cannot come here" % (text, kind))
            where, e = self._lookup(model, cur, val, first, notes, text)
            if first and where is not cur:
                cur = where
                prefix = model + ("." + where["obj"] if where.get("obj") else "")
            first = False
            i += 1
            nxt = segs[i] if i < len(segs) else None
            obj = ((item_obj + "." + e["name"]) if item_obj else e["obj"])
            if e["kind"].endswith("Space"):
                space_disp = prefix + "." + e["name"]
                sparams = e.get("parameters") or []
                is_item = False
                if nxt and nxt[0] == "params":
                    # Projection[point_id], as get_tree prints the Space: the
                    # Space itself, never an item.
                    if nxt[1] != sparams:
                        raise not_literal(nxt[1][0], text)
                    i += 1
                    nxt = segs[i] if i < len(segs) else None
                    prefix = space_disp
                    if item_obj:
                        item_obj = obj
                elif nxt and nxt[0] in ("sub", "call"):
                    # [k] is checked as (k) is. MEASURED on CashValue_ME, whose
                    # Projection takes no parameters: Projection[2].pv_net_cf()
                    # went to the bridge unchecked and came back from every
                    # tool as "internal: AttributeError: 'NoneType' object has
                    # no attribute 'signature'".
                    if not sparams:
                        raise ToolError("%s takes no parameters, so it has no ItemSpaces: write "
                                        "%s.<name>, with no [k]" % (space_disp, space_disp))
                    sargs = self._positional(e["name"], sparams,
                                             (list(nxt[1]), {}) if nxt[0] == "sub" else nxt[1], text)
                    if any(isinstance(a, Range) for a in sargs):
                        raise ToolError("an ItemSpace takes plain arguments, not range()")
                    i += 1
                    nxt = segs[i] if i < len(segs) else None
                    label = "%s[%s]" % (space_disp, ", ".join(fmt_arg(a) for a in sargs))
                    is_item = True
                    if missing is None and not probe:
                        found = self._item(model, obj, sargs, create)
                        if found is None:
                            missing = label
                            item_obj = None
                        else:
                            item_obj = found
                            if create and found.startswith("+"):
                                item_obj = found[1:]
                                created.append(label)
                                self._created_log.append((model, label))
                    prefix = label
                    obj = item_obj or obj
                else:
                    prefix = space_disp
                    if item_obj:
                        item_obj = obj
                cur = e
                if nxt is None:
                    node = Node(model, "Space", obj, e["name"], e.get("parameters"), entry=e)
                    node.prefix = prefix
                    node.item = is_item
                    node.inside_item = bool(item_obj)
                continue
            # Cells or Reference
            node = Node(model, "Reference" if e["kind"] == "Reference" else "Cells",
                        obj, e["name"], e.get("parameters"), entry=e)
            node.space_obj = item_obj or cur.get("obj", "")
            node.space_entry = cur
            node.prefix = prefix
            node.item = is_item
            node.inside_item = bool(item_obj)
            if node.kind == "Reference":
                node.args = []
            if nxt is not None and nxt[0] == "params":
                # claims(t), as get_tree prints the Cells: the Cells itself.
                if node.kind != "Cells" or nxt[1] != node.params:
                    raise not_literal(nxt[1][0], text)
                i += 1
                nxt = segs[i] if i < len(segs) else None
            if nxt is not None and node.kind == "Cells":
                if nxt[0] == "call":
                    node.args = self._positional(e["name"], node.params, nxt[1], text)
                    i += 1
                elif nxt[0] == "sub":
                    node.args = self._positional(e["name"], node.params, (nxt[1], {}), text)
                    i += 1
                elif nxt[0] == "slice":
                    a, b, s = nxt[1]
                    if len(node.params) != 1:
                        raise ToolError("a slice [a:b] needs a one-parameter Cells; %s takes "
                                        "(%s): write %s(%s=range(a, b), ...)"
                                        % (e["name"], ", ".join(node.params), e["name"],
                                           node.params[0] if node.params else "t"))
                    if b is None:
                        raise ToolError("a slice needs an end, e.g. %s[0:121]" % e["name"])
                    node.args = [Range(a or 0, b, s or 1)]
                    i += 1
            while i < len(segs):
                k2, v2 = segs[i]
                if k2 in ("loc", "iloc"):
                    node.accessors.append((k2, v2))
                elif k2 == "sub" and len(v2) == 1 and isinstance(v2[0], str):
                    node.accessors.append(("col", v2[0]))
                elif (k2 == "sub" and len(v2) == 1 and isinstance(v2[0], int)
                      and not isinstance(v2[0], bool)):
                    node.accessors.append(("pos", v2[0]))
                else:
                    raise ToolError("%r: nothing but .loc[...], .iloc[...], [i] or "
                                    "[\"column\"] can follow %s" % (text, e["name"]))
                i += 1
            break
        if node is None:
            raise ToolError("%r names nothing" % text)
        if node.kind == "Cells" and node.args is not None:
            ranges = [j for j, a in enumerate(node.args) if isinstance(a, Range)]
            if len(ranges) > 1:
                raise ToolError("only one argument may be a range() in %r" % text)
            if ranges:
                rng = node.args[ranges[0]]
                node.expand = (ranges[0], rng)
                n = len(rng)
                if not n:
                    # MEASURED: claims(t=range(5, 5)), claims[3:3] and
                    # claims[120:0] became an item with no nodes, and calculate
                    # failed whole on it ("internal error: KeyError") after
                    # computing every other ref of the call.
                    raise ToolError("%r is empty: %r holds no values%s" % (
                        text, rng, "; a descending range needs a negative step, e.g. range(%d, %d, -1)"
                        % (rng.start, rng.stop) if rng.step > 0 and rng.start > rng.stop else ""))
                if n > MAX_NODES:
                    raise ToolError("%r is %d nodes; at most %d per call. Split it: "
                                    "%s first" % (text, n, MAX_NODES, fmt_arg(Range(
                                        rng.start, rng.start + MAX_NODES * rng.step, rng.step))))
                if node.accessors:
                    # Refused here, so no tool drops it. MEASURED:
                    # premiums(t=range(0, 3))[99999] and .loc[...] on a range
                    # printed the range's three values with the accessor
                    # silently dropped, in calculate and get_value, and
                    # calculate's NO VALUE line did not name the ref.
                    raise ToolError("%r: %s picks from one value, and this ref names %d nodes; nothing "
                                    "was read. Write one node per ref, with no range() or [a:b]"
                                    % (text, "".join(_acc_text(k, v) for k, v in node.accessors), n))
        node.notes = notes
        node.missing_item = missing
        node.created = created
        node.text = text
        return node

    def _item(self, model, space_obj, args, create):
        """obj of an existing ItemSpace, '+obj' for one created now, or None.

        MEASURED: value.get{evaluate:false} on a Space node answers cached:false
        and creates nothing when the ItemSpace does not exist, and an `mx` tag
        whose obj is modelx's internal __SpaceN name when it does. That name is
        used for this call only and never printed (spec 5.4)."""
        spec = {"obj": space_obj, "args": [enc(a) for a in args]}
        key = ("item", model, space_obj, json.dumps(spec["args"]))
        # A "does not exist" remembered by a create=False read is no answer for
        # create=True: calculate checks every ref without creating first.
        if key in self._memo and (self._memo[key] is not None or not create):
            return self._memo[key]
        found = self._item_read(model, spec, create)
        self._memo[key] = found.lstrip("+") if found else None
        return found

    def _item_read(self, model, spec, create):
        r = self.d("value.get", {"model": model, "evaluate": False, "nodes": [spec]})
        v = r["values"][0]
        if not v.get("ok"):
            raise ToolError(self._error_text(v["error"]))
        if v.get("cached"):
            return v["value"]["obj"]
        if not create:
            return None
        # calculate only: running the Space formula creates the ItemSpace
        # (bridge reason `evaluate`, so the model is not marked dirty).
        r = self.d("value.get", {"model": model, "evaluate": True, "nodes": [spec]})
        v = r["values"][0]
        if not v.get("ok"):
            raise ToolError(self._error_text(v["error"]))
        return "+" + v["value"]["obj"]

    # -- shared pieces ------------------------------------------------------

    def _error_text(self, err):
        code, msg, data = err.get("code"), err.get("message"), err.get("data") or {}
        if code == "formula_error":
            return formula_error_text(msg, data)
        return "%s: %s" % (code, msg)

    def _example(self, node, page=None):
        """(arguments text, range text or None, [parameters left unfilled])
        for a ready-to-copy call of a Cells given without arguments. A value
        is never invented: the Cells' first cached key when it has one,
        else t=0 for t (lifelib's time) and a <name> placeholder for every
        other parameter. MEASURED on CashValue_ME: the old 0-for-every-
        parameter hint claims(t=0, kind=0) raised "invalid kind",
        av_pp_at(t=0, timing=0) "invalid timing", and pv_claims(kind=range(0,
        12)) put a range on a string parameter. range() goes on t, or on a
        parameter whose cached key is an int."""
        key = None
        if page is None:
            try:
                page = self.d("cells.page", {"model": node.model, "obj": node.obj, "row": 0, "rows": 1})
            except WireError:
                page = {}
        pg = page.get("page") or {}
        if page.get("n_cached") and pg.get("rows"):
            vals = [c["values"][0] for c in pg["columns"][:len(node.params)]]
            # cells.page turns a None key into NaN (bridge gap G4): unwritable.
            if not any(isinstance(v, dict) and v.get("$t") == "num" for v in vals):
                key = [dec(v) for v in vals]
        given = [fmt_arg(key[j]) if key else ("0" if p == "t" else None) for j, p in enumerate(node.params)]
        open_ = [p for p, g in zip(node.params, given) if g is None]
        one = ", ".join("%s=%s" % (p, g if g is not None else "<%s>" % p) for p, g in zip(node.params, given))
        at = [j for j, p in enumerate(node.params)
              if p == "t" or (key and isinstance(key[j], int) and not isinstance(key[j], bool))]
        rng = None
        if at:
            rng = ", ".join("%s=%s" % (p, "range(0, 12)" if j == at[0] else
                                       (given[j] if given[j] is not None else "<%s>" % p))
                            for j, p in enumerate(node.params))
        return one, rng, open_

    def _args_hint(self, node, call_prefix, page=None):
        """'name takes (a, b): give arguments, e.g. ..., or a range: ...'."""
        one, rng, open_ = self._example(node, page)
        return "%s takes (%s): give arguments, e.g. %s(%s)%s%s" % (
            node.name, ", ".join(node.params), call_prefix, one,
            (", or a range: %s(%s)" % (call_prefix, rng)) if rng else "",
            ("; get_formulas([%s]) shows what %s takes" % (_q("%s.%s" % (node.prefix, node.name)),
                                                           " and ".join(open_))) if open_ else "")

    def _base_note(self, node):
        """Once per output: a value read in the base Space of a parameterised
        Space is ONE item, the one a Reference of the parameter's name selects.
        The audit pilot showed a separate NOTE paraphrased away ("the policy"),
        and faithful's live S6 b1 answered for model point 1 instead of 2."""
        sp = node.space_entry
        if node.kind != "Cells" or not sp or node.missing_item or node.item:
            return None
        params = sp.get("parameters") or []
        if not params:
            return None
        refs = [r["name"] for r in sp.get("refs") or []]
        if params[0] in refs:
            v = self.d("value.get", {"model": node.model, "evaluate": False,
                                     "nodes": [{"obj": (sp.get("obj") + "." if sp.get("obj") else "")
                                                + params[0], "args": []}]})["values"][0]
            val = R.scalar(v.get("value")) if v.get("ok") else "?"
            return ("# %s takes (%s): its values are for the item that the Reference %s = %s "
                    "selects; %s[k] is item k" % (node.prefix, ", ".join(params), params[0], val,
                                                  node.prefix))
        return ("# %s takes (%s): these are its base Space's values; %s[k] is item k"
                % (node.prefix, ", ".join(params), node.prefix))

    def _aligned_labels(self, node, n, positions, retry=False):
        """({position: 'policy_id 7342'}, model_point ref) for an ndarray as long
        as the Space's model_point() table, or None.

        An inference, printed as one (spec 6.6): lifelib's vectorised models
        order every array by model_point()'s rows. MEASURED on BasicTerm_ME:
        pv_net_cf()[7341] is policy 7342, the task truth; position 7342 is the
        number S6 s8's live failure cited. model_point() is read, never computed."""
        if not self.align or not node.space_entry:
            return None
        names = [c["name"] for c in node.space_entry.get("cells") or []]
        if self.align not in names:
            return None
        obj = (node.space_obj + "." if node.space_obj else "") + self.align
        key = ("align", node.model, obj)
        if key not in self._memo:
            v = self.d("value.get", {"model": node.model, "evaluate": False,
                                     "nodes": [{"obj": obj, "args": []}]})["values"][0]
            self._memo[key] = v if v.get("ok") and v.get("cached") and R.is_handle(v["value"]) else None
        v = self._memo[key]
        if v is None or (v["value"].get("shape") or [0])[0] != n:
            return None
        h = v["value"]["h"]
        out = {}
        try:
            for start, stop in _runs(sorted(set(positions))):
                pg = self.d("table.get", {"h": h, "row": start, "rows": stop - start, "cols": 0})
                idx = pg["index"]
                for j, lab in enumerate(idx["values"]):
                    out[start + j] = "%s %s" % (idx.get("name") or "label", R.cell(lab))
        except WireError as e:
            # The memoised model_point() handle can leave the LRU while a
            # big call reads other values: read it again, once.
            if not _evicted(e) or retry:
                raise
            del self._memo[key]
            return self._aligned_labels(node, n, positions, retry=True)
        return out, "%s.%s()" % (node.prefix, self.align)

    def _vector(self, node, display, tag, indent="    ", stats=True):
        """(inline text, extra lines) for a handle value."""
        kind = tag.get("kind")
        if tag.get("shape") is None:
            return R.unshaped(tag), []
        shape = tag.get("shape") or []
        if not shape:
            return R.zero_d(tag), []
        prev = tag.get("preview") or []
        idx = tag.get("index_preview")
        iname = tag.get("index_name")
        n = shape[0] if shape else 0
        extra = []
        # Any 1-D value the codec sent a preview of. MEASURED: pd.RangeIndex(5)
        # and a two-label MultiIndex arrived as kinds "RangeIndex" and
        # "MultiIndex", fell through, and printed "RangeIndex int64 [5]" and
        # "MultiIndex object [2]" with no value; table.get pages both.
        if len(shape) == 1 and (kind in ("Series", "ndarray", "Index") or tag.get("preview") is not None):
            vals = [R.cell(r[0]) for r in prev]
            aligned = None
            if kind == "ndarray":
                want = list(range(min(n, len(prev) if n <= len(prev) else R.HEAD)))
                al = self._aligned_labels(node, n, want)
                if al:
                    aligned, mp_ref = al
            text = R.head(tag)
            if n <= len(prev) and n <= R.SHORT:
                if idx is not None:
                    body = "{" + ", ".join("%s: %s" % (R.cell(k), v) for k, v in zip(idx, vals)) + "}"
                    text += (" %s " % iname if iname else " ") + body
                else:
                    text += " [" + ", ".join(vals) + "]"
                if stats and n > 1 and (tag.get("dtype") or "")[:1] in "fiu":
                    st = self.d("table.stats", {"h": tag["h"], "col": 0})["stats"]
                    text += "  sum %s" % R.stat(st, "sum")
                if aligned:
                    labs = [aligned[p] for p in sorted(aligned)]
                    extra.append(indent + "positions 0..%d are the rows of %s, %s %s (by position: an ndarray "
                                 "carries no labels)" % (n - 1, mp_ref, labs[0].split(" ")[0],
                                                         ", ".join(x.split(" ", 1)[1] for x in labs)))
                return text, extra
            shown = list(range(min(R.HEAD, len(prev))))
            if idx is not None:
                body = ", ".join("%s: %s" % (R.cell(idx[p]), vals[p]) for p in shown)
                text += (" %s " % iname if iname else " ") + "{" + body + ", ...}"
            elif aligned:
                body = ", ".join("[%d] %s: %s" % (p, aligned[p], vals[p]) for p in shown)
                text += " {" + body + ", ...}"
            else:
                text += " [" + ", ".join(vals[p] for p in shown) + ", ...]"
            if stats:
                st = self.d("table.stats", {"h": tag["h"], "col": 0})["stats"]
                al_ext = None
                if kind == "ndarray" and st.get("argmax") is not None:
                    got = self._aligned_labels(node, n, [p for p in (st.get("argmin"), st["argmax"])
                                                         if p is not None])
                    al_ext = got[0] if got else None
                extra.append(indent + R.stats_line(st, n, "whole column (all %d)" % n,
                                                   iname, al_ext))
            if aligned:
                extra.append(indent + "positions are %s rows (same length %d; an ndarray carries "
                             "no labels). One by label: get_value([%s])"
                             % (mp_ref, n, _q(display + ".loc[<label>]")))
            extra.append(indent + "more rows: get_value([%s], offset=%d)" % (_q(display), R.HEAD))
            return text, extra
        if kind == "DataFrame" or len(shape) == 2:
            cols = tag.get("columns") or []
            if not cols and prev:
                # A 2-D ndarray has no column names, only positions. MEASURED:
                # with none, np.arange(12.0).reshape(3, 4) printed its index
                # column and no value, and "col=0 shows the rest" printed the
                # same again.
                cols = [str(j) for j in range(len(prev[0]))]
            ncols = shape[1] if len(shape) > 1 else len(cols)
            text = R.head(tag) + ((" (index %s)" % iname) if iname else "")
            rows = prev[:R.HEAD]
            hdr = [iname or "index"] + cols
            body = [[R.cell(k)] + [R.cell(c) for c in r] for k, r in zip(idx or range(len(rows)), rows)]
            if rows:
                extra.extend(R.grid(hdr, body, indent))
            if n > len(rows):
                extra.append(indent + "%d more rows: get_value([%s], offset=%d)"
                             % (n - len(rows), _q(display), len(rows)))
            if stats:
                k = min(ncols, STAT_COLS)
                for c in range(k):
                    st = self.d("table.stats", {"h": tag["h"], "col": c})
                    extra.append(indent + R.stats_line(st["stats"], n, "%s, whole column (all %d)"
                                                       % (st.get("name"), n), iname))
                if ncols > k:
                    extra.append(indent + "statistics for %d more columns: get_value([%s], col=%d)"
                                 % (ncols - k, _q(display), k))
            if ncols > len(cols):
                extra.append(indent + "columns shown: %d of %d; get_value([%s], col=%d) shows the rest"
                             % (len(cols), ncols, _q(display), len(cols)))
            return text, extra
        # Never the bare head: it reads as a value with nothing in it.
        # MEASURED: np.zeros((2, 3, 4)) printed "ndarray float64 [2x3x4]" and
        # nothing else, and np.array(5.0) "ndarray float64 []" (zero_d above).
        size = 1
        for k in shape:
            size *= k
        if _unpageable(tag):
            return "%s: %d values, none shown, and %s" % (R.head(tag), size, NO_READER), extra
        # A 1-D value with no preview (the codec's description of it failed).
        extra.append(indent + "%d values, not shown; get_value([%s], offset=0) shows them" % (size, _q(display)))
        return R.head(tag), extra

    def _value_lines(self, node, entry, status, refused=None):
        """A value's lines. When the ref's accessor gives no value (refused,
        or the bridge's error), True is appended to `refused`, a list.

        A handle read earlier in a big call can be evicted before it is
        rendered (the store is an LRU of 64): MEASURED on BasicTerm_ME,
        calculate([premiums(t=0), claims[0:121]]) minted 122 handles and
        premiums(t=0)'s table.stats answered not_found, with the handle id in
        the message. So the node is re-read, never computed, and rendered from
        the fresh handle."""
        try:
            return self._value_lines_once(node, entry, status, refused)
        except WireError as e:
            if not _evicted(e) or not entry.get("ok") or not entry.get("cached"):
                raise
        fresh = self.d("value.get", {"model": node.model, "evaluate": False, "nodes": [
            {"obj": node.obj, "args": entry.get("args") or []}]})["values"][0]
        return self._value_lines_once(node, fresh, status, refused)

    def _value_lines_once(self, node, entry, status, refused=None):
        disp = entry.get("display") or node.text
        if not entry.get("ok"):
            return ["%s -> %s" % (disp, self._error_text(entry["error"]))]
        if not entry.get("cached"):
            return ["%s = NOT COMPUTED" % disp]
        value = entry.get("value")
        # A Reference is never computed and modelx does not trace it, so it
        # carries neither a [computed now]/[cached] label nor counts.
        tagbits = [status] if status and node.kind == "Cells" else []
        if node.kind == "Cells" and "predslen" in entry:
            tagbits.append("reads %d; read by %d computed" % (entry["predslen"], entry["succslen"]))
        tagtxt = ("  [%s]" % "; ".join(tagbits)) if tagbits else ""
        if node.accessors:
            lines, gave = self._accessor_lines(node, disp, value, tagtxt)
            if not gave and refused is not None:
                refused.append(True)
            return lines
        if R.is_handle(value):
            inline, extra = self._vector(node, disp, value)
            return ["%s = %s%s" % (disp, inline, tagtxt)] + extra
        return ["%s = %s%s" % (disp, R.scalar(value), tagtxt)]

    def _element(self, node, disp, value):
        """Apply a ref's accessors to a value. -> dict: {"refused": text} or
        {"shown", "cell", "note"} for one element, or {"shown", "col"} for a
        whole DataFrame column."""
        if not R.is_handle(value):
            return {"refused": "%s is %s, not a Series, DataFrame or ndarray" % (disp, R.scalar(value))}
        if _unpageable(value):
            return {"refused": "%s is %s, and %s" % (disp, R.head(value), NO_READER)}
        h, kind = value["h"], value.get("kind")
        shown, col = disp, None
        acc = list(node.accessors)
        # df.loc[k]["c"] is df["c"].loc[k], and pandas reads both: the column is
        # chosen first. MEASURED: the row was returned at .loc[k] and ["c"]
        # dropped, so get_value printed the whole row (a misspelt column too)
        # and calculate refused "pick one column with [...]" for a ref that did.
        if (kind == "DataFrame" and len(acc) == 2 and acc[1][0] == "col"
                and acc[0][0] in ("loc", "iloc", "pos")):
            acc = [acc[1], acc[0]]
        for j, (k, v) in enumerate(acc):
            if j < len(acc) - 1 and k != "col":
                # Every accessor is applied or refused, never dropped.
                return {"refused": "%s selects one %s, so %s cannot follow it"
                                   % (_acc_text(k, v), "row" if kind == "DataFrame" and col is None
                                      else "element", "".join(_acc_text(*x) for x in acc[j + 1:]))}
            if k == "col":
                if kind != "DataFrame":
                    return {"refused": "[%r] selects a DataFrame column; this is a %s" % (v, kind)}
                if col is not None:
                    return {"refused": "one column per ref: %s then [%r]" % (shown, v)}
                names = [c["name"] for c in self.d("table.get", {"h": h, "rows": 0, "cols": 10000})["columns"]]
                if v not in names:
                    return {"refused": "no column %r; columns: %s" % (v, ", ".join(names))}
                col = names.index(v)
                shown += "[%s]" % _q(v)
                continue
            if k == "loc" and kind == "ndarray":
                got = self._locate_aligned(node, value, v)
                if isinstance(got, str):
                    return {"refused": got}
                row, why = got
                page = self.d("table.get", {"h": h, "row": row, "rows": 1})
                return {"shown": shown + "[%d]" % row, "cell": page["columns"][0]["values"][0],
                        "note": why}
            # A DataFrame row with no column chosen is the whole row: printing
            # its first column alone under `.loc[k]` would cite one cell as the row.
            whole_row = kind == "DataFrame" and col is None
            width = {"col": 0, "cols": 10} if whole_row else {"col": col or 0, "cols": 1}
            if k == "loc":
                page = self.d("table.get", dict({"h": h, "label": enc(v), "rows": 1}, **width))
                shown += ".loc[%s]" % fmt_arg(v)
                label = fmt_arg(v)
            else:
                # pandas 3 reads s[k] by LABEL on an integer index, and a model
                # will mean either, so [k] is read by position on an ndarray only.
                if k == "pos" and kind != "ndarray":
                    return {"refused": "[%d] on a %s is ambiguous; write .loc[label] or "
                                       ".iloc[position]" % (v, kind)}
                page = self.d("table.get", dict({"h": h, "row": v, "rows": 1}, **width))
                if not page["rows"]:
                    return {"refused": "position %d is past the end (%d rows)" % (v, page["total_rows"])}
                shown += (".iloc[%d]" % v) if k == "iloc" else ("[%d]" % v)
                label = R.cell(page["index"]["values"][0])
            note = None if kind == "ndarray" else "%s %s, position %d" % (
                value.get("index_name") or page["index"].get("name") or "label", label, page["row"])
            if whole_row:
                more = page["total_cols"] - len(page["columns"])
                return {"shown": shown, "note": note, "row": "{%s%s}" % (
                    ", ".join("%r: %s" % (c["name"], R.cell(c["values"][0])) for c in page["columns"]),
                    (", ... %d more columns; one: %s[\"<column>\"]" % (more, shown)) if more else "")}
            return {"shown": shown, "cell": page["columns"][0]["values"][0], "note": note}
        return {"shown": shown, "col": col}

    def _accessor_lines(self, node, disp, value, tagtxt):
        """-> (lines, gave): gave is False when the accessor gave no value.
        Said structurally, for calculate's NO VALUE line. MEASURED: with 30
        refs before it, premiums(t=29).loc[999999] (not_found) and
        premiums(t=29)[99999] (refused) were computed, their ' -> ' line fell
        under the 12,000-char cut, and the cut line said "Everything was
        computed"."""
        try:
            el = self._element(node, disp, value)
        except WireError as e:
            if _evicted(e):
                raise           # _value_lines re-reads the node
            return ["%s -> %s: %s" % (node.text, e.code, e.message)], False
        if "refused" in el:
            return ["%s -> refused: %s" % (node.text, el["refused"])], False
        if "cell" in el:
            return ["%s = %s%s%s" % (el["shown"], R.cell(el["cell"]),
                                     ("  (%s)" % el["note"]) if el.get("note") else "", tagtxt)], True
        if "row" in el:
            return ["%s = row %s  (%s)%s" % (el["shown"], el["row"], el["note"], tagtxt)], True
        h, col, shown = value["h"], el["col"], el["shown"]
        st = self.d("table.stats", {"h": h, "col": col})
        page = self.d("table.get", {"h": h, "row": 0, "rows": R.HEAD, "col": col, "cols": 1})
        vals = page["columns"][0]["values"]
        iname = value.get("index_name") or page["index"].get("name")
        n = page["total_rows"]
        body = ", ".join("%s: %s" % (R.cell(k), R.cell(x)) for k, x in zip(page["index"]["values"], vals))
        return ["%s = column %s of %d rows {%s%s}%s" % (shown, _q(st["name"]), n, body,
                                                        ", ..." if n > len(vals) else "", tagtxt),
                "    " + R.stats_line(st["stats"], n, "whole column (all %d)" % n, iname)], True

    def _locate_aligned(self, node, value, label):
        n = (value.get("shape") or [0])[0]
        names = [c["name"] for c in (node.space_entry or {}).get("cells") or []]
        if not self.align or self.align not in names:
            return "an ndarray has no labels; read it by position: [i]"
        obj = (node.space_obj + "." if node.space_obj else "") + self.align
        v = self.d("value.get", {"model": node.model, "evaluate": False,
                                 "nodes": [{"obj": obj, "args": []}]})["values"][0]
        if not (v.get("ok") and v.get("cached") and R.is_handle(v["value"])):
            return ("an ndarray has no labels, and %s.%s() is not computed, so its "
                    "positions cannot be related to model point ids" % (node.prefix, self.align))
        if (v["value"].get("shape") or [0])[0] != n:
            return ("an ndarray has no labels, and its length %d differs from %s.%s()'s %d "
                    "rows, so positions cannot be aligned" % (n, node.prefix, self.align,
                                                             v["value"]["shape"][0]))
        try:
            pg = self.d("table.get", {"h": v["value"]["h"], "label": enc(label), "rows": 1, "cols": 0})
        except WireError as e:
            return e.message + " of %s.%s()" % (node.prefix, self.align)
        row = pg["focus_row"]
        return row, ("%s %s: position %d of %s.%s(), the same length; an ndarray carries no labels"
                     % (pg["index"].get("name") or "label", fmt_arg(label), row,
                        node.prefix, self.align))

    def _read(self, model, specs, evaluate):
        """value.get in chunks; -> entries in order, and the ms spent.

        The ms is the sum of what d() recorded for each dispatch, not a second
        clock reading around it, so a golden replay (whose clock advances by
        the recorded ms) prints the same "(0.02 s)" byte for byte."""
        out, ms = [], 0.0
        for k in range(0, len(specs), CHUNK):
            r = self.d("value.get", {"model": model, "evaluate": evaluate,
                                     "nodes": specs[k:k + CHUNK]})
            ms += self.calls[-1]["ms"]
            out.extend(r["values"])
        return out, ms

    # =====================================================================
    # get_tree
    # =====================================================================

    def get_tree(self, model=None, path=None, filter=None, offset=0):
        self.begin()
        args = {"model": model, "path": path, "filter": filter, "offset": offset}
        names = self.model_names()
        if not names:
            raise ToolError(NO_MODEL)
        if model is None:
            if len(names) != 1:
                lines = ["%d models are open; pass model= to get_tree:" % len(names)]
                for m in self.info()["models"]:
                    c = _computed(m)
                    lines.append("  %s  rev %d  %s  %s" % (
                        m["name"], m["revision"],
                        ("sample %s" % m["sample"]) if m.get("sample") else (m.get("path") or "no file"),
                        UNKNOWN_COMPUTED if c is None else
                        ("%d nodes computed" % c) if c else "nothing computed"))
                return self.end("get_tree", args, "\n".join(lines))
            model = names[0]
        if model not in names:
            raise ToolError("no open model %r; open: %s" % (model, ", ".join(names)))
        t = self.tree(model)
        c = _computed(self.model_info(model))
        root = t["root"]
        pats = [p.strip().lower() for p in (filter or "").split("|") if p.strip()]

        def keep(name):
            return not pats or any(p in name.lower() for p in pats)

        start = root
        title = None
        if path:
            node = self.resolve(path if path.startswith(model + ".") else model + "." + path)
            if node.kind != "Space":
                raise ToolError("%s is a %s; path= names a Space" % (path, node.kind))
            start = node.entry
            # An ItemSpace's counts are its own, from its own tree.get; the
            # base Space's entry printed the base's ("none computed" for a
            # Projection[3] holding 121 claims, and 121 for a Projection[42]
            # that did not exist).
            if node.missing_item:
                return self.end("get_tree", args, (
                    "ItemSpace %s does not exist yet, so nothing is computed in it. It would have its "
                    "base Space's Cells and References: get_tree(model=%s, path=%s)"
                    % (node.missing_item, _q(model), _q(node.entry["obj"]))))
            if node.inside_item:
                start = self.item_tree(model, node.obj)
                # Never the item tree's own display, which is modelx's __SpaceN.
                title = node.prefix[len(model) + 1:]
        # The header's count is the model's (ItemSpaces included); the counts
        # after ':' are tree.get's, which covers the named Spaces only.
        lines = ["%s rev %d, %s%s" % (model, t["revision"],
                                      UNKNOWN_COMPUTED if c is None else
                                      ("%d nodes computed in the model, ItemSpaces included" % c)
                                      if c else "nothing computed",
                                      ("; filter %s matches Cells and Reference names only" % _q(filter))
                                      if pats else "")]
        spaces = [start] if start is not root else (root.get("spaces") or [])
        if start is root and root.get("refs"):
            lines.append("model References: " + ", ".join(
                "%s: %s" % (r["name"], r.get("value_type")) for r in root["refs"]))

        def emit(sp, depth):
            ind = "  " * depth
            its = sp.get("itemspaces") or {}
            itxt = ""
            if its.get("total"):
                shown = [x["display"] for x in its.get("shown", [])]
                itxt = "  ItemSpaces (%d%s): %s" % (its["total"], "" if len(shown) == its["total"]
                                                     else ", %d listed" % len(shown), ", ".join(shown))
            lines.append("%sSpace %s%s%s" % (ind, title if sp is start and title else sp["display"],
                                             " (inherited)" if sp.get("derived") else "", itxt))
            # The filter matches Cells and Reference names only, never the model
            # or Space name: the Explorer's filter matched the model name and
            # returned the whole tree for 44 substrings of BasicTerm_S (CLAUDE.md).
            cells = [c for c in sp.get("cells") or [] if keep(c["name"])]
            total = len(sp.get("cells") or [])
            ncomp = sum(1 for c in sp.get("cells") or [] if c.get("cached"))
            items = []
            for c in cells:
                s = c["display"] if c["display"].endswith(")") else c["display"] + "()"
                if c.get("cached"):
                    s += ":%d" % c["cached"]
                items.append(s)
            off = offset if sp is spaces[0] else 0
            more = 0
            if off:
                items = items[off:]
            # The bound less 2,500 leaves room for the References and the
            # Spaces after this one; at the 2,000 minimum it would be negative,
            # and a page of no Cells whose continuation is offset=0 never ends.
            budget = max(self.max_chars - 2500, self.max_chars // 2)
            used = sum(len(x) for x in lines)
            if used + sum(len(x) + 2 for x in items) > budget:
                kept, acc = [], used
                for x in items:
                    if kept and acc + len(x) + 2 > budget:
                        break
                    kept.append(x)
                    acc += len(x) + 2
                more = len(items) - len(kept)
                items = kept
            lines.append("%s  Cells (%s%s; %s; values cached in this Space after ':'):" % (
                ind, ("%d of %d" % (len(cells), total)) if pats else str(total),
                (", from #%d" % off) if off else "",
                ("%d computed" % ncomp) if ncomp else "none computed"))
            lines.extend(R.wrap(items, ind + "    "))
            if more:
                lines.append("%s    [%d more Cells not shown: get_tree(model=%s%s%s, offset=%d)]"
                             % (ind, more, _q(model), (", path=%s" % _q(path)) if path else "",
                                (", filter=%s" % _q(filter)) if filter else "", off + len(items)))
            refs = [r for r in sp.get("refs") or [] if keep(r["name"])]
            if refs or not pats:
                lines.append("%s  References (%s):" % (ind, ("%d of %d" % (len(refs), len(sp.get("refs") or [])))
                                                     if pats else str(len(refs))))
                lines.extend(R.wrap(["%s: %s" % (r["name"], r.get("value_type")) for r in refs], ind + "    "))
            for sub in sp.get("spaces") or []:
                emit(sub, depth + 1)
        for sp in spaces:
            emit(sp, 0)
        return self.end("get_tree", args,
                        R.clip(lines, self.max_chars, "Narrow it: get_tree(model=%s, filter=...)" % _q(model)))

    # =====================================================================
    # get_formulas
    # =====================================================================

    def get_formulas(self, refs, docstrings=True, doc_offset=0):
        self.begin()
        refs = list(refs or [])
        args = {"refs": refs, "docstrings": docstrings, "doc_offset": doc_offset}
        if not refs:
            raise ToolError("refs is empty; pass e.g. [\"BasicTerm_S.Projection.claims\"]")
        over = refs[MAX_FORMULA_REFS:]
        asked = refs[:MAX_FORMULA_REFS]
        blocks = []
        for ref in asked:
            try:
                node = self.resolve(ref)
                blocks.append(self._formula_block(node, docstrings, doc_offset))
            except (ToolError, RefError) as e:
                blocks.append(["%s -> %s" % (ref, e)])
            except WireError as e:
                blocks.append(["%s -> %s: %s" % (ref, e.code, e.message)])
        lines, used, dropped = [], 0, []
        for ref, b in zip(asked, blocks):
            size = sum(len(x) + 1 for x in b) + 1
            if lines and used + size > self.max_chars - 300:
                dropped.append(ref)
                continue
            lines.extend(b + [""])
            used += size
        dropped += over
        if dropped:
            lines.append("[%d refs not shown, to stay under %d chars and %d refs: get_formulas(%s)]"
                         % (len(dropped), self.max_chars, MAX_FORMULA_REFS, json.dumps(dropped)))
        return self.end("get_formulas", args, R.clip(
            lines, self.max_chars, "Ask for fewer refs, or docstrings=false"))

    def _doc_page(self):
        """8,000 characters of docstring per call, less at a --max-chars that
        could not hold that much: a page the output bound cut would be
        continued by a hint that names no doc_offset."""
        return max(1000, min(DOC_PAGE, self.max_chars - 2000))

    def _formula_block(self, node, docstrings, doc_offset):
        b = ["# " + n for n in node.notes]
        m = node.model
        if node.missing_item:
            b.append("# ItemSpace %s does not exist yet; its formulas are its base Space's, shown "
                     "below" % node.missing_item)
        if node.kind == "Cells":
            f = self.d("formula.get", {"model": m, "obj": node.obj})
            e = node.entry
            sig = "%s.%s(%s)" % (node.prefix, node.name, ", ".join(node.params))
            if node.missing_item:
                cached = "nothing cached (the ItemSpace does not exist)"
            else:
                own = e
                if node.inside_item:
                    own = dict((c["name"], c) for c in self.item_tree(m, node.space_obj).get("cells") or []
                               ).get(node.name, {})
                cached = (_count(own["cached"], "value") + " cached") if own.get("cached") else "nothing cached"
            b.append("## %s - Cells%s; %s" % (sig, " (inherited)" if e.get("derived") else "", cached))
            mp = self.map_of(m, node.space_obj)
            if mp:
                b.extend(self._links(mp, node.name, self.several_spaces(m)))
            src = f["source"] if docstrings else strip_doc(f["source"])
            b.extend(src.rstrip("\n").splitlines())
            return b
        if node.kind == "Reference":
            v = self.d("value.get", {"model": m, "evaluate": False,
                                     "nodes": [{"obj": node.obj, "args": []}]})["values"][0]
            vt = node.entry.get("value_type")
            b.append("## %s - Reference (%s)" % (v.get("display") or node.text, vt))
            if v.get("ok"):
                b.extend(self._value_lines(node, v, "")[0:1])
            mp = self.map_of(m, node.space_obj)
            if mp:
                users = sorted(to for fr, to in mp["ref_edges"] if fr == node.name)
                b.append("named by the formulas of: %s" % (", ".join(users) or "none in this Space"))
            dd = self.d("doc.get", {"model": m, "obj": node.obj})
            if dd.get("doc"):
                b.append("its own docstring:")
                b.extend("  " + x for x in dd["doc"].splitlines())
            sd = self.d("doc.get", {"model": m, "obj": node.space_obj}) if node.space_obj else {}
            # lifelib documents a Reference only in its Space's docstring.
            para = doc_paragraph(sd.get("doc") or "", node.name)
            if para:
                b.append("documented in %s's docstring:" % node.prefix)
                b.extend(para)
            elif not dd.get("doc"):
                b.append("no docstring documents it (its own is empty, and %s's does not "
                         "name it)" % node.prefix)
            return b
        # Space or Model
        dd = self.d("doc.get", {"model": m, "obj": node.obj})
        doc = dd.get("doc") or ""
        disp = node.prefix if node.kind == "Space" else m
        if doc_offset:
            # A continuation page repeats no header or summary (spec C11).
            end = min(len(doc), doc_offset + self._doc_page())
            b.append("## %s docstring, chars %d-%d of %d" % (disp, doc_offset, end, len(doc)))
            b.extend(doc[doc_offset:end].splitlines())
            if end < len(doc):
                b.append("[%d more chars: get_formulas([%s], doc_offset=%d)]" % (len(doc) - end, _q(disp), end))
            return b
        if node.kind == "Model":
            sp = [s["display"] for s in node.entry.get("spaces") or []]
            b.append("## %s - Model; Spaces: %s" % (m, ", ".join(sp)))
        else:
            # Every count from the ItemSpace's own tree entry, never its base
            # Space's. The base's entry said "1 ItemSpaces" for Projection[2],
            # which held none; and with only that count redirected, a Space
            # Outer(i) printed "Nest.Outer[1] - ItemSpace; 1 Cells, 0
            # References" (and Outer[1].Kid the same) while get_tree and
            # modelx gave Outer[1] one Reference, its parameter i.
            own = node.entry
            if node.inside_item and not node.missing_item:
                own = self.item_tree(m, node.obj)
            its = 0 if node.missing_item else (own.get("itemspaces") or {}).get("total", 0)
            b.append("## %s - %s; %d Cells, %d References%s" % (
                disp, dd.get("kind"), len(own.get("cells") or []), len(own.get("refs") or []),
                (", %d ItemSpaces" % its) if its else ""))
            mp = self.map_of(m, node.obj)
            if mp:
                names = [c["name"] for c in mp["cells"]]
                read = set(fr for fr, to in mp["edges"])
                reading = set(to for fr, to in mp["edges"])
                several = self.several_spaces(m)
                b.append("formulas name each other in %d links (read from source; get_map has them)"
                         % len(mp["edges"]))
                b.append(("outputs (no formula in this Space names them; formulas in other Spaces are "
                          "not read here): " if several else "outputs (no formula names them): ")
                         + ", ".join(n for n in names if n not in read))
                b.append("inputs (name no other Cells%s): " % (" of this Space" if several else "") + ", ".join(
                    n for n in names if n not in reading))
                if mp["recursive"]:
                    b.append("recursive: " + ", ".join(mp["recursive"]))
        if not doc:
            b.append("no docstring")
            return b
        end = min(len(doc), self._doc_page())
        b.append("docstring (%d chars%s):" % (len(doc), "" if end == len(doc) else ", chars 0-%d" % end))
        b.extend(doc[:end].splitlines())
        if end < len(doc):
            b.append("[%d more chars: get_formulas([%s], doc_offset=%d)]" % (len(doc) - end, _q(disp), end))
        return b

    def _links(self, mp, name, several=False):
        """The two formula-level lines. With several Spaces in the model they
        say their scope, one Space's formulas (see several_spaces)."""
        reads = sorted(fr for fr, to in mp["edges"] if to == name)
        rreads = sorted(fr for fr, to in mp["ref_edges"] if to == name)
        users = sorted(to for fr, to in mp["edges"] if fr == name)
        rec = name in mp["recursive"]
        if several:
            named = ("%s (in this Space; formulas in other Spaces are not read here)" % ", ".join(users)
                     if users else "none in this Space (formulas in other Spaces are not read here; "
                                   "trace(direction=\"succs\") shows what has read it)")
        else:
            named = ", ".join(users) or "none (an output)"
        return ["formula names: %s%s%s" % (", ".join(reads) or ("no other Cells of this Space" if several
                                                                else "no other Cells"),
                                           "; itself (recursive)" if rec else "",
                                           ("; References " + ", ".join(rreads)) if rreads else ""),
                "named by the formulas of: %s" % named]

    # =====================================================================
    # get_map
    # =====================================================================

    def get_map(self, model=None, space=None, cells=None, depth=1, offset=0):
        self.begin()
        args = {"model": model, "space": space, "cells": cells, "depth": depth, "offset": offset}
        names = self.model_names()
        if not names:
            raise ToolError(NO_MODEL)
        if depth == 0 or depth < -1:
            raise ToolError("depth is 1 or more, or -1 for every level; got %d" % depth)
        if model is None:
            if len(names) != 1:
                raise ToolError("%d models are open (%s); pass model=" % (len(names), ", ".join(names)))
            model = names[0]
        obj, shown, lines = "", None, []
        if space:
            node = self.resolve(space if space.startswith(model + ".") else model + "." + space)
            if node.kind != "Space":
                raise ToolError("%s is a %s; space= names a Space" % (space, node.kind))
            obj = node.obj
            # An ItemSpace by the ref that names it: map.get's display is the
            # item's internal name, MEASURED "BasicTerm_S.__Space1 rev 3: ...".
            if node.inside_item or node.missing_item:
                shown = node.prefix
            if node.missing_item:
                lines.append("# ItemSpace %s does not exist yet; its formulas are its base Space's, "
                             "mapped below" % node.missing_item)
        try:
            mp = self.d("map.get", {"model": model, "obj": obj})
        except WireError as e:
            raise ToolError("%s: %s" % (e.code, e.message))
        shown = shown or mp["display"]
        edges = mp["edges"]
        # Protocol section 17 asks every client to say the map is read from source.
        head = ("%s rev %d: %d Cells, %d links between Cells, %d reads of References. Read from "
                "formula SOURCE, not from a calculation: a link means a formula names it. "
                "Blind to names built at run time and to reads through another Space."
                % (shown, mp["revision"], len(mp["cells"]), len(edges), len(mp["ref_edges"])))
        lines.append(head)
        if mp["recursive"]:
            lines.append("names itself (recursion): " + ", ".join(mp["recursive"]))
        if mp.get("unread"):
            lines.append("source not parsed, links unknown: " + ", ".join(mp["unread"]))
        if cells is None:
            body = []
            for c in mp["cells"]:
                n = c["name"]
                reads = sorted(fr for fr, to in edges if to == n)
                rr = sorted(fr for fr, to in mp["ref_edges"] if to == n)
                body.append("%s <- %s%s" % (n, ", ".join(reads) or "(no Cells)", (" [%s]" % ", ".join(rr)) if rr else ""))
            body = body[offset:]
            lines.append("each line: Cells <- the Cells its formula names [References]")
            used = sum(len(x) + 1 for x in lines)
            for k, x in enumerate(body):
                if used + len(x) + 1 > self.max_chars - 200:
                    lines.append("[%d more lines: get_map(model=%s%s, offset=%d)]"
                                 % (len(body) - k, _q(model), (", space=%s" % _q(space)) if space else "",
                                    offset + k))
                    break
                lines.append(x)
                used += len(x) + 1
            return self.end("get_map", args, "\n".join(lines))
        if cells not in [c["name"] for c in mp["cells"]]:
            close = difflib.get_close_matches(cells, [c["name"] for c in mp["cells"]], n=3)
            raise ToolError("no Cells %r in %s%s" % (cells, shown,
                                                     ("; did you mean " + ", ".join(close)) if close else ""))
        for direction, word in (("in", "names (its inputs)"), ("out", "is named by (its formula-level dependents)")):
            seen, frontier, level = {cells}, {cells}, 0
            lines.append("%s %s, %s:" % (cells, word, "all levels" if depth < 0 else "to depth %d" % depth))
            while frontier and (depth < 0 or level < depth):
                level += 1
                if direction == "in":
                    nxt = set(fr for fr, to in edges if to in frontier) - seen
                else:
                    nxt = set(to for fr, to in edges if fr in frontier) - seen
                if not nxt:
                    break
                lines.append("  level %d (%d): %s" % (level, len(nxt), ", ".join(sorted(nxt))))
                seen |= nxt
                frontier = nxt
            if lines[-1].endswith(":"):
                lines.append("  none")
            if depth >= 0 and frontier and level == depth:
                more = (set(fr for fr, to in edges if to in frontier) if direction == "in"
                        else set(to for fr, to in edges if fr in frontier)) - seen
                if more:
                    lines.append("  [deeper levels not shown: depth=-1 lists all]")
        return self.end("get_map", args, R.clip(lines, self.max_chars, "Ask for a smaller depth"))

    # =====================================================================
    # get_value
    # =====================================================================

    def get_value(self, refs, offset=0, rows=None, col=0, label=None):
        self.begin()
        refs = list(refs or [])
        args = {"refs": refs, "offset": offset, "rows": rows, "col": col, "label": label}
        if not refs:
            raise ToolError("refs is empty; pass e.g. [\"BasicTerm_S.Projection.pv_net_cf()\"]")
        if len(refs) > MAX_REFS:
            raise ToolError("%d refs; at most %d per call" % (len(refs), MAX_REFS))
        ctx = {"single": len(refs) == 1, "paging": bool(offset or rows or col), "offset": offset,
               "rows": rows, "col": col, "label": label, "notes": [], "uncomputed": [],
               "missing": [], "total": 0}
        lines = []
        for ref in refs:
            # One bad ref never fails the call (protocol 6.5's rule), whatever
            # step it fails at -- resolving it, reading it or rendering it.
            try:
                lines.extend(self._value_ref(ref, ctx))
            except (ToolError, RefError) as e:
                lines.append("%s -> %s" % (ref, e))
            except WireError as e:
                lines.append("%s -> %s: %s" % (ref, e.code, e.message))
        out = ctx["notes"] + lines
        todo = ctx["uncomputed"] + ctx["missing"]
        if todo:
            out.append("NOT COMPUTED means no value exists yet: unknown, not zero. "
                       "calculate(%s) computes %s.%s" % (
                           json.dumps(todo[:8]), "them" if len(todo) > 1 else "it",
                           (" (%d more refs above are NOT COMPUTED too)" % (len(todo) - 8))
                           if len(todo) > 8 else ""))
        return self.end("get_value", args, R.clip(out, self.max_chars, "Ask for fewer refs"))

    def _value_ref(self, ref, ctx):
        node = self.resolve(ref)
        lines = ["# " + n for n in node.notes]
        if node.missing_item:
            # The footer offers calculate(refs) only for refs calculate takes:
            # it refuses a Space and a Cells given without arguments. MEASURED:
            # it offered calculate(["Projection[42].claims"]).
            if node.kind == "Space":
                return lines + ["%s -> ItemSpace %s does not exist yet; a Space has no value, and "
                                "calculate on one of its nodes creates it" % (ref, node.missing_item)]
            if node.kind == "Cells" and node.args is None and node.params:
                return lines + ["%s -> NOT COMPUTED: ItemSpace %s does not exist yet. Calculating one node "
                                "creates it: %s" % (ref, node.missing_item,
                                                    self._args_hint(node, "%s.%s" % (node.prefix, node.name)))]
            ctx["missing"].append(ref)
            return lines + ["%s -> NOT COMPUTED: ItemSpace %s does not exist yet"
                            % (ref, node.missing_item)]
        if node.kind in ("Space", "Model"):
            return lines + ["%s -> a %s has no value; get_tree or get_formulas([%s])"
                            % (ref, node.kind, _q(ref))]
        if node.kind == "Cells" and node.args is None:
            if node.params:
                return lines + self._cells_page(node, ctx["offset"], ctx["rows"], ctx["label"])
            node.args = []
        if ctx["label"] is not None:
            lines.append("# label= picks from a Cells' cached column (a Cells without "
                         "arguments); it was not applied to %s. One element of one value: "
                         ".loc[label]" % ref)
        sets = node.argsets()
        if ctx["total"] + len(sets) > MAX_NODES:
            return lines + ["%s -> refused: more than %d nodes in one call" % (ref, MAX_NODES)]
        ctx["total"] += len(sets)
        note = self._base_note(node)
        if note and note not in ctx["notes"]:
            ctx["notes"].append(note)
        specs = [{"obj": node.obj, "args": [enc(a) for a in s]} for s in sets]
        entries, _ = self._read(node.model, specs, False)
        if node.expand:
            if any(e.get("ok") and not e.get("cached") for e in entries):
                ctx["uncomputed"].append(ref)
            return lines + self._range_lines(node, entries, ["cached"] * len(entries))
        entry = entries[0]
        if ctx["paging"] and R.is_handle(entry.get("value")) and not node.accessors:
            if ctx["single"]:
                return lines + self._page_lines(node, entry, ctx["offset"], ctx["rows"] or PAGE_ROWS,
                                                ctx["col"])
            return lines + self._value_lines(node, entry, "") + [
                "    (offset=, rows= and col= page a vector only when it is the call's one ref: "
                "get_value([%s], offset=%d))" % (_q(entry.get("display") or ref), ctx["offset"])]
        if entry.get("ok") and not entry.get("cached"):
            ctx["uncomputed"].append(entry.get("display") or ref)
        return lines + self._value_lines(node, entry, "")

    def _page_lines(self, node, entry, offset, rows, col):
        rows = min(rows, MAX_ROWS)
        tag = entry["value"]
        disp = entry["display"]
        pg = self.d("table.get", {"h": tag["h"], "row": offset, "rows": rows, "col": col, "cols": 10})
        n = pg["total_rows"]
        iname = pg["index"].get("name") or ("position" if tag.get("kind") == "ndarray" else "index")
        hdr = [iname] + [c["name"] for c in pg["columns"]]
        idxv = pg["index"]["values"]
        al = None
        # No rows, no labels to align: MEASURED, offset=10000 on BasicTerm_ME's
        # pv_net_cf() (10,000 rows) failed the whole call with KeyError 10000
        # instead of the "no rows at offset" line below.
        if tag.get("kind") == "ndarray" and pg["rows"]:
            got = self._aligned_labels(node, n, list(range(pg["row"], pg["row"] + pg["rows"])))
            if got:
                al = got[0]
                hdr = ["position", got[0][pg["row"]].split(" ")[0]] + [c["name"] for c in pg["columns"]]
        body = []
        for j in range(pg["rows"]):
            row = [R.cell(idxv[j])]
            if al:
                row.append(al[pg["row"] + j].split(" ", 1)[1])
            row += [R.cell(c["values"][j]) for c in pg["columns"]]
            body.append(row)
        lines = []
        for c in range(pg["col"], pg["col"] + min(pg["cols"], STAT_COLS)):
            st = self.d("table.stats", {"h": tag["h"], "col": c})
            ext = None
            if al is not None and st["stats"].get("argmax") is not None:
                got = self._aligned_labels(node, n, [p for p in (st["stats"].get("argmin"),
                                                                 st["stats"]["argmax"]) if p is not None])
                ext = got[0] if got else None
            lines.append("  " + R.stats_line(st["stats"], n, "%s, whole column (all %d)" % (st["name"], n),
                                             pg["index"].get("name"), ext))
        table = self._fit(lines, R.grid(hdr, body))
        shown = len(table) - 1
        lines.insert(0, "%s = %s, %s, columns %d-%d of %d"
                     % (disp, R.head(tag), ("rows %d-%d of %d" % (pg["row"], pg["row"] + shown - 1, n))
                        if shown else "no rows at offset %d: there are %d" % (pg["row"], n),
                        pg["col"], pg["col"] + pg["cols"] - 1, pg["total_cols"]))
        lines.extend(table)
        if pg["row"] + shown < n:
            lines.append("next rows: get_value([%s], offset=%d%s)" % (
                _q(disp), pg["row"] + shown, (", col=%d" % col) if col else ""))
        if pg["col"] + pg["cols"] < pg["total_cols"]:
            lines.append("more columns: get_value([%s], offset=%d, col=%d)" % (
                _q(disp), offset, pg["col"] + pg["cols"]))
        return lines

    def _cells_page(self, node, offset, rows, label):
        rows = min(rows or PAGE_ROWS, MAX_ROWS)
        params = {"model": node.model, "obj": node.obj, "row": offset, "rows": rows}
        if label is not None:
            params["element"] = enc(label)
        disp = "%s.%s" % (node.prefix, node.name)
        try:
            p = self.d("cells.page", params)
        except WireError as e:
            return ["%s -> %s: %s" % (disp, e.code, e.message)]
        names = p["params"]
        head = "%s: %d cached values" % (disp, p["n_cached"])
        if not p["n_cached"]:
            one, _, open_ = self._example(node, p)
            return [head + ": NOTHING COMPUTED yet (unknown, not zero). calculate computes "
                    "values, e.g. calculate([%s])%s" % (
                        _q("%s(%s)" % (disp, one)),
                        ("; get_formulas([%s]) shows what %s takes" % (_q(disp), " and ".join(open_)))
                        if open_ else "")]
        if p.get("too_large"):
            return [head + ": too many to page here (limit %d)" % p["too_large"]["limit"]]
        keys = p.get("keys")
        if keys:
            head += ", %s = %s..%s%s" % (names[0], keys["min"], keys["max"],
                                          " contiguous" if keys["gaps"] == 0 else ", %d gaps" % keys["gaps"])
        if label is not None:
            head += "; element %s of each value%s" % (
                fmt_arg(label), (" (%d values lack it)" % p["element_missing"]) if p.get("element_missing") else "")
        head += "; read from the cache, nothing computed"
        lines = [head]
        if p["scalar_values"]:
            st = p["stats"]
            lines.append("  " + R.stats_line(st, p["n_cached"], "whole column (all %d)" % p["n_cached"],
                                             ", ".join(names) if len(names) > 1 else names[0]))
        else:
            lines.append("  each value is a vector (dtype object): shown as a clipped repr, no statistics. "
                         "One model point across %s: get_value([%s], label=<id>)" % (names[0], _q(disp)))
        page = p["page"]
        cols = page["columns"]
        hdr = names + [p["value_name"]]
        body = []
        nan_key = False
        for j in range(page["rows"]):
            row = []
            for c in cols[:-1]:
                x = c["values"][j]
                if isinstance(x, dict) and x.get("$t") == "num":
                    nan_key = True
                row.append(R.cell(x))
            row.append(R.cell(cols[-1]["values"][j]))
            body.append(row)
        table = self._fit(lines, R.grid(hdr, body))
        shown = len(table) - 1
        lines.append(("rows %d-%d of %d:" % (page["row"], page["row"] + shown - 1, page["total_rows"]))
                     if shown else "no rows at offset %d: there are %d" % (page["row"], page["total_rows"]))
        lines.extend(table)
        if nan_key:
            # cells.page turns a None key into NaN (bridge gap G4, PLAN 5a).
            lines.append("a key shown as nan may be None: pandas stores a None argument as NaN in a "
                         "multi-parameter key")
        if page["row"] + shown < page["total_rows"]:
            lines.append("next rows: get_value([%s]%s, offset=%d)" % (
                _q(disp), (", label=%s" % fmt_arg(label)) if label is not None else "",
                page["row"] + shown))
        return lines

    def _fit(self, lines, table, reserve=600):
        """The header and as many rows of a page grid as fit under the bound
        after `lines`, so the "next rows" offset names the first row NOT
        printed rather than leaving the cut to the last-resort bound."""
        used = sum(len(x) + 1 for x in lines) + len(table[0]) + 1
        out = table[:1]
        for row in table[1:]:
            if len(out) > 1 and used + len(row) + 1 > self.max_chars - reserve:
                break
            out.append(row)
            used += len(row) + 1
        return out

    def _range_lines(self, node, entries, status):
        pos, rng = node.expand
        pname = node.params[pos]
        xs = rng.values()
        rtext = "%s.%s(%s)" % (node.prefix, node.name, ", ".join(
            ("%s=%s" % (node.params[j], fmt_arg(a))) for j, a in enumerate(node.args)))
        okv = [(x, e) for x, e in zip(xs, entries) if e.get("ok") and e.get("cached")]
        bad = [(x, e) for x, e in zip(xs, entries) if not e.get("ok")]
        unc = [x for x, e in zip(xs, entries) if e.get("ok") and not e.get("cached")]
        counts = {}
        for s, e in zip(status, entries):
            if e.get("ok") and e.get("cached"):
                counts[s] = counts.get(s, 0) + 1
        lines = ["%s: %d nodes (%s)" % (rtext, len(xs), ", ".join(
            ["%d %s" % (v, k) for k, v in counts.items()]
            + (["%d NOT COMPUTED" % len(unc)] if unc else [])
            + (["%d failed" % len(bad)] if bad else [])))]
        for x, e in bad[:3]:
            lines.append("  %s=%s -> %s" % (pname, fmt_arg(x), self._error_text(e["error"])))
        if len(bad) > 3:
            lines.append("  [%d more failed; calculate one of them to see its error]" % (len(bad) - 3))
        if not okv:
            return lines
        vals = [e["value"] for _, e in okv]
        nvec = sum(1 for v in vals if R.is_handle(v))
        if nvec and nvec < len(vals):
            # Scalars and vectors in one range: every row says which it is.
            # MEASURED: a scalar first failed the whole call (AttributeError in
            # R.head after computing), and a scalar later was covered by "each
            # value is Series float64 [2]".
            heads = sorted(set(R.head(v) for v in vals if R.is_handle(v)))
            nums = [(x, dec(v)) for (x, _), v in zip(okv, vals) if not R.is_handle(v)]
            numeric = [(x, v) for x, v in nums if isinstance(v, (int, float)) and not isinstance(v, bool)
                       and not (isinstance(v, float) and v != v)]
            lines.append("  mixed values: %d scalars, %d vectors (%s); read a vector with get_value([%s])"
                         % (len(vals) - nvec, nvec, ", ".join(heads),
                            _q([e for _, e in okv if R.is_handle(e["value"])][0]["display"])))
            if numeric:
                lines.append("  scalars: " + _range_stats(numeric, len(okv), pname))
            body = [[R.cell(x), R.head(v) if R.is_handle(v) else R.scalar(v)] for (x, _), v in zip(okv, vals)]
            hdr = [pname, node.name]
        elif not nvec:
            nums = [(x, dec(v)) for (x, _), v in zip(okv, vals)]
            numeric = [(x, v) for x, v in nums if isinstance(v, (int, float)) and not isinstance(v, bool)
                       and not (isinstance(v, float) and v != v)]
            if numeric:
                lines.append("  " + _range_stats(numeric, len(okv), pname))
            body = [[R.cell(x), R.scalar(v)] for (x, _), v in zip(okv, vals)]
            hdr = [pname, node.name]
        else:
            small = all(R.is_handle(v) and v.get("index_preview") is not None and len(v.get("shape") or []) == 1
                        and v["shape"][0] <= R.SHORT and len(v.get("preview") or []) >= v["shape"][0]
                        for v in vals)
            if not small:
                heads = {}
                for v in vals:
                    heads[R.head(v)] = heads.get(R.head(v), 0) + 1
                # label= picks from Series values only. MEASURED: it was offered
                # for a range of 2,000-item lists, 0-d and 3-D ndarrays, and
                # get_value(label=) answered "bad_request: element picks by
                # label from Series values" for each.
                lines.append("  %s; read one with get_value([%s])%s"
                             % (("each value is %s" % R.head(vals[0])) if len(heads) == 1 else
                                "the values differ: " + ", ".join("%s (%d nodes)" % kv for kv in heads.items()),
                                _q(okv[0][1]["display"]),
                                (", or one model point across %s with get_value([%s], label=<id>)"
                                 % (pname, _q("%s.%s" % (node.prefix, node.name))))
                                if all(v.get("kind") == "Series" for v in vals) else ""))
                return lines
            # Matched by decoded label, printed from the wire's own label: a
            # MultiIndex label decodes to a tuple, which R.cell cannot print.
            shown_lab = dict((dec(k), k) for k in vals[0]["index_preview"])
            labels = list(shown_lab)
            iname = vals[0].get("index_name") or "label"
            per = {lab: [] for lab in labels}
            for (x, _), v in zip(okv, vals):
                for lab, row in zip([dec(k) for k in v["index_preview"]], v["preview"]):
                    if lab in per and isinstance(row[0], (int, float)) and not isinstance(row[0], bool):
                        per[lab].append((x, row[0]))
            for lab in labels:
                lines.append("  %s %s: %s" % (iname, R.cell(shown_lab[lab]), _range_stats(per[lab], len(okv), pname)))
            hdr = [pname] + ["%s %s" % (iname, R.cell(shown_lab[lab])) for lab in labels]
            body = []
            for (x, _), v in zip(okv, vals):
                m = dict(zip([dec(k) for k in v["index_preview"]], [r[0] for r in v["preview"]]))
                body.append([R.cell(x)] + [R.cell(m.get(lab)) for lab in labels])
        # The statistics come first, so a cut never takes them; rows stop at
        # half the bound (the token judge's must_avoid) and at RANGE_ROWS.
        table = R.grid(hdr, body)
        used = sum(len(x) + 1 for x in lines) + len(table[0]) + 1
        shown = 0
        for row in table[1:]:
            if used + len(row) + 1 > self.max_chars // 2 or shown >= RANGE_ROWS:
                break
            shown += 1
            used += len(row) + 1
        lines.extend(table[:1 + shown])
        if len(body) > shown:
            # The rest with the range's own step. MEASURED: dropping it named
            # range(260, 399) for the 70 rows t=260..398 step 2 (139 nodes, half
            # never asked for), and range(69, 2) -- empty -- for claims[199:0:-1].
            rest = okv[shown:]
            lines.append("  [%d more rows: get_value([%s])]" % (len(rest), _q(
                "%s.%s(%s)" % (node.prefix, node.name, ", ".join(
                    "%s=%s" % (node.params[j], fmt_arg(Range(rest[0][0], rest[-1][0] + rng.step, rng.step))
                               if j == pos else fmt_arg(a)) for j, a in enumerate(node.args))))))
        return lines

    # =====================================================================
    # calculate -- THE ONLY TOOL THAT PASSES evaluate: true
    # =====================================================================

    def calculate(self, refs):
        self.begin()
        refs = list(refs or [])
        args = {"refs": refs}
        if not refs:
            raise ToolError("refs is empty; pass e.g. [\"BasicTerm_S.Projection.pv_net_cf()\"]")
        if len(refs) > MAX_REFS:
            raise ToolError("%d refs; at most %d per call" % (len(refs), MAX_REFS))
        before = dict((m["name"], m) for m in self.info()["models"])
        items, total, derived = [], 0, {}
        for ref in refs:
            try:
                expr = derived_expr(ref)
            except RefError as e:
                items.append((ref, str(e)))
                continue
            # EVERY ref is checked with create=False first; an ItemSpace is
            # created only for a ref that passed every check (_made). MEASURED:
            # resolving with create=True up front ran the Space formula for
            # Projection[5].pv_netcf() (a typo), Projection[8].claims (no
            # arguments) and a 300-node range, and each call printed only its
            # refusal while the ItemSpace, the revision and the computed count
            # all moved.
            if expr is not None:
                tree, leaves = expr
                k0 = len(items)
                leaf_nodes = []
                for leaf in leaves:
                    n = self._checked(leaf, operand=True)
                    if isinstance(n, str):
                        leaf_nodes = "%s: %s" % (leaf, n)
                        break
                    leaf_nodes.append(n)
                if isinstance(leaf_nodes, str):
                    items.append((ref, "refused: " + leaf_nodes))
                    continue
                if total + len(leaf_nodes) > MAX_NODES:
                    items.append((ref, "refused: this call would read more than %d nodes" % MAX_NODES))
                    continue
                made = [self._made(leaf, n) for leaf, n in zip(leaves, leaf_nodes)]
                bad = [(leaf, n) for leaf, n in zip(leaves, made) if isinstance(n, str)]
                if bad:
                    items.append((ref, "refused: %s: %s" % bad[0]))
                    continue
                total += len(made)
                for n in made:
                    items.append((None, n))
                derived[len(items)] = (ref, tree, leaves, list(range(k0, len(items))))
                items.append((ref, "derived"))
                continue
            node = self._checked(ref)
            if isinstance(node, str):
                items.append((ref, node))
                continue
            if total + len(node.argsets()) > MAX_NODES:
                items.append((ref, "refused: this call would read more than %d nodes" % MAX_NODES))
                continue
            node = self._made(ref, node)
            if isinstance(node, str):
                items.append((ref, node))
                continue
            total += len(node.argsets())
            items.append((ref, node))
        # Phase 1 reads what is cached; phase 2 computes the misses only. That
        # is how each line is labelled [computed now] or [cached] from fact.
        by_model = {}
        for k, (ref, node) in enumerate(items):
            if isinstance(node, Node):
                by_model.setdefault(node.model, []).append(k)
        results, timing, distinct = {}, {}, {}
        for m, ks in by_model.items():
            # One read per DISTINCT node. MEASURED: claims(t=range(0, 10)),
            # claims(t=range(5, 15)), claims(t=7) and claims(7) printed "22
            # nodes: 22 computed now" for the 15 nodes modelx computed.
            specs, slot, owner = [], {}, []
            for k in ks:
                for s in items[k][1].argsets():
                    spec = {"obj": items[k][1].obj, "args": [enc(a) for a in s]}
                    key = json.dumps(spec, sort_keys=True)
                    if key not in slot:
                        slot[key] = len(specs)
                        specs.append(spec)
                    owner.append((k, slot[key]))
            first, _ = self._read(m, specs, False)
            status = ["cached" if e.get("ok") and e.get("cached") else None for e in first]
            todo = [j for j, e in enumerate(first) if e.get("ok") and not e.get("cached")]
            ms = 0.0
            if todo:
                second, ms = self._read(m, [specs[j] for j in todo], True)
                for j, e in zip(todo, second):
                    first[j] = e
                    status[j] = "computed now" if e.get("ok") and e.get("cached") else None
                if len(specs) > 1:
                    # "read by N computed" as of the END of the call: a node
                    # read before another was computed has gained readers since.
                    # MEASURED: calculate([pv_claims(), pv_net_cf()]) printed
                    # pv_claims() "read by 0 computed" beside pv_net_cf()
                    # [computed now], which reads it; trace said 2. A count
                    # read without evaluating; a failed entry is kept.
                    final, _ = self._read(m, specs, False)
                    for j, e in enumerate(final):
                        if first[j].get("ok") and e.get("ok") and e.get("cached"):
                            first[j] = e
            timing[m] = (ms, len(todo))
            distinct[m] = (status, first, len(owner) - len(specs))
            for k, j in owner:
                results.setdefault(k, []).append((first[j], status[j]))
        self._memo.pop("info", None)
        after = dict((m["name"], m) for m in self.info()["models"])
        # An ItemSpace created for a ref that then failed to create the next
        # one (a derived value's later operand) is printed and headed too.
        shown_made = set(c for _, n in items if isinstance(n, Node) for c in n.created)
        leftover = [(m, c) for m, c in self._created_log if c not in shown_made]
        lines, notfine = [], False
        for m in list(by_model) + [x for x, _ in leftover if x not in by_model]:
            b, a = before[m], after.get(m, {})
            sts, ents, rep = distinct.get(m, ([], [], 0))
            nnow = sts.count("computed now")
            ncached = sts.count("cached")
            nfail = sum(1 for e in ents if not e.get("ok"))
            nunc = sum(1 for e in ents if e.get("ok") and not e.get("cached"))
            notfine = notfine or bool(nfail or nunc)
            ms, ntodo = timing.get(m, (0.0, 0))
            parts = []
            if nnow:
                parts.append("%d computed now (%.2f s)" % (nnow, ms / 1000.0))
            if ncached:
                parts.append("%d already cached" % ncached)
            if nfail:
                parts.append("%d FAILED" % nfail)
            if nunc:
                parts.append("%d still not computed" % nunc)
            ra = a.get("revision", b["revision"])
            line = "%s rev %d%s: %d node%s%s: %s." % (
                m, b["revision"], ("->%d" % ra) if ra != b["revision"] else "",
                len(sts), "" if len(sts) == 1 else "s",
                (" (%d more named again in the refs, counted once)" % rep) if rep else "",
                ", ".join(parts) or "nothing")
            cb, ca = _computed(b), _computed(a)
            if cb is None or ca is None:
                line += " Computed nodes in the model: unknown (the bridge could not read modelx's graph)."
            elif ca != cb:
                line += " Computed nodes in the model: %d -> %d (%+d)." % (cb, ca, ca - cb)
            else:
                line += " Nothing was computed."
            # Without bridge B1 (deferred, protocol 18.8) a failure moves
            # `computed` but not the revision: measured, claims(t=9999) after
            # pv_net_cf() leaves 4 more nodes at the same revision.
            if nfail and cb is not None and ca is not None and ca != cb:
                line += " A failed calculation keeps what it computed before it failed."
            lines.append(line)
        notes = ["# created ItemSpace %s (ran the Space formula; it stays in the model)" % c
                 for _, c in leftover]
        body, novalue = [], []
        for k, (ref, node) in enumerate(items):
            try:
                body.extend(self._calculated(k, ref, node, notes, items, derived, results, novalue))
            except WireError as e:
                # Rendering reads (table.stats, table.get) can fail; the value
                # was computed all the same, and the header above says so.
                body.append("%s -> %s: %s" % (ref or node.text, e.code, e.message))
                novalue.append(ref or node.text)
        # The refs that gave no value are named up here, where no cut reaches.
        # MEASURED at the default bound: 30 refs and a 180-node range, refused,
        # printed "30 nodes: 30 computed now" and a cut line saying "Everything
        # was computed", with the refusal among the lines cut.
        if novalue:
            lines.append("NO VALUE for %d of the %d refs; each one's reason is its ' -> ' line below: %s%s"
                         % (len(novalue), len(refs), json.dumps(novalue[:8]),
                            (" and %d more" % (len(novalue) - 8)) if len(novalue) > 8 else ""))
        hint = ("Everything was computed; get_value(refs) re-reads it without computing"
                if not novalue and not notfine else NOT_EVERYTHING)
        return self.end("calculate", args, R.clip(notes + lines + body, self.max_chars, hint))

    def _checked(self, text, operand=False):
        """A ref calculate will evaluate, resolved WITHOUT creating anything,
        or why it is refused (a str). Every check runs here, before _made."""
        try:
            n = self.resolve(text)
        except (ToolError, RefError) as e:
            return str(e)
        except WireError as e:
            return "%s: %s" % (e.code, e.message)
        if operand:
            if n.kind not in ("Cells", "Reference") or n.expand or (n.kind == "Cells" and n.args is None
                                                                    and n.params):
                return "an operand is one node with its arguments, or a Reference"
        elif n.kind in ("Space", "Model"):
            return "a %s has no value to calculate" % n.kind
        elif n.kind == "Cells" and n.args is None and n.params:
            return self._args_hint(n, n.name)
        if n.args is None:
            n.args = []
        return n

    def _made(self, text, node):
        """node, resolved again with create=True when its ItemSpace does not
        exist yet: the one place calculate creates one. -> Node, or a str."""
        if not node.missing_item:
            return node
        try:
            n = self.resolve(text, create=True)
        except (ToolError, RefError) as e:
            return str(e)
        except WireError as e:
            return "%s: %s" % (e.code, e.message)
        if n.args is None:
            n.args = []
        return n

    def _calculated(self, k, ref, node, notes, items, derived, results, novalue):
        """The lines of one item of a calculate call; a ref that gives no
        value is added to `novalue`."""
        if ref is None:
            # An operand of a derived value: its value is printed with the
            # expression, but what resolving it did is said here.
            note = self._base_note(node)
            if note and note not in notes:
                notes.append(note)
            return (["# " + n for n in node.notes] +
                    ["# created ItemSpace %s (ran the Space formula; it stays in the model)" % c
                     for c in node.created])
        if node == "derived":
            out, gave = self._derived_lines(derived[k], items, results)
            if not gave:
                novalue.append(ref)
            return out
        if not isinstance(node, Node):
            novalue.append(ref)
            return ["%s -> %s" % (ref, node)]
        lines = ["# " + n for n in node.notes]
        lines.extend("# created ItemSpace %s (ran the Space formula; it stays in the model)" % c
                     for c in node.created)
        note = self._base_note(node)
        if note and note not in notes:
            notes.append(note)
        got = results.get(k, [])
        if node.expand:
            return lines + self._range_lines(node, [e for e, _ in got], [s for _, s in got])
        refused = []
        for e, s in got:
            lines.extend(self._value_lines(node, e, s, refused))
        if refused:
            novalue.append(ref)
        return lines

    def _derived_lines(self, spec, items, results):
        """-> (lines, gave): gave is False when the expression gave no value,
        said structurally rather than read back from the line's text."""
        ref, tree, leaves, ks = spec
        values, parts, shown = {}, [], {}
        for leaf, k in zip(leaves, ks):
            node = items[k][1]
            e, s = results.get(k, [({}, None)])[0]
            disp = e.get("display") or node.text
            if not e.get("ok"):
                return ["%s -> %s: %s" % (ref, disp, self._error_text(e["error"]))], False
            if not e.get("cached"):
                return ["%s -> %s is not computed" % (ref, disp)], False
            v = e.get("value")
            if node.accessors:
                try:
                    el = self._element(node, disp, v)
                except WireError as err:
                    if not _evicted(err):
                        raise
                    e = self.d("value.get", {"model": node.model, "evaluate": False, "nodes": [
                        {"obj": node.obj, "args": e.get("args") or []}]})["values"][0]
                    el = self._element(node, disp, e.get("value"))
                if "cell" not in el:
                    return ["%s -> refused: %s" % (ref, el.get("refused") or (
                        "%s is a DataFrame %s, not a number; pick one column with [\"<column>\"]"
                        % (el["shown"], "row" if "row" in el else "column")))], False
                disp, v = el["shown"], el["cell"]
            x = dec(v)
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                # "[i] or .loc[label]" only where one can work. MEASURED: it was
                # advised for a 2,000-item list, where [3] is refused and
                # .iloc[3] fails "table.get cannot page a list", and for a
                # 0-d ndarray and an inline list, where both are refused.
                if R.is_handle(v) and not _unpageable(v):
                    why = "%s, not a number; pick one element with [i] or .loc[label]" % R.head(v)
                elif R.is_handle(v):
                    why = "%s, not a plain number, and %s" % (R.head(v), NO_READER)
                elif isinstance(x, (list, tuple, dict)):
                    why = "%s, not a number, and %s" % (R.scalar(v), NO_READER)
                else:
                    why = "%s, not a number" % R.scalar(v)
                return ["%s -> refused: %s is %s" % (ref, disp, why)], False
            values[leaf] = x
            shown[leaf] = disp
            parts.append("%s = %s [%s]" % (disp, R.num(x), s if node.kind == "Cells" else "Reference"))
        try:
            result = eval_derived(tree, values)
        except ZeroDivisionError:
            return ["%s -> division by zero" % ref], False
        return ["%s = %s  [derived here from %s; cite this expression]"
                % (_substitute(clean(ref), leaves, shown), R.num(result),
                   "the value below" if len(parts) == 1 else "the %d values below" % len(parts))
                ] + ["    " + p for p in parts], True

    # =====================================================================
    # trace
    # =====================================================================

    def trace(self, ref, direction="preds", depth=1, offset=0):
        self.begin()
        args = {"ref": ref, "direction": direction, "depth": depth, "offset": offset}
        if direction not in ("preds", "succs"):
            raise ToolError("direction is \"preds\" (what it reads) or \"succs\" (what reads it)")
        lines = []
        asked = int(depth)
        depth = max(1, min(asked, 3))
        if depth != asked:
            lines.append("# depth=%d read as %d: trace expands 1 to 3 levels" % (asked, depth))
        node = self.resolve(ref)
        if node.kind == "Reference":
            # MEASURED: the Space wording offered Projection.point_id.<cells>(...),
            # which cannot exist.
            raise ToolError("%s is a Reference, and modelx does not trace References; "
                            "get_formulas([%s]) shows which formulas name it" % (ref, _q(ref)))
        if node.kind != "Cells":
            raise ToolError("%s is a %s; trace follows a Cells node, e.g. %s.%s<cells>(...)"
                            % (ref, node.kind, ref, "<Space>." if node.kind == "Model" else ""))
        if node.args is None:
            if node.params:
                one, _, open_ = self._example(node)
                cells = "%s.%s" % (node.prefix, node.name)
                raise ToolError("%s takes (%s); trace one node, e.g. %s(%s)%s" % (
                    node.name, ", ".join(node.params), cells, one,
                    ("; get_formulas([%s]) shows what %s takes" % (_q(cells), " and ".join(open_)))
                    if open_ else ""))
            node.args = []
        if node.expand:
            raise ToolError("trace follows one node; range() is for calculate and get_value")
        lines.extend("# " + n for n in node.notes)
        mp = self.map_of(node.model, node.space_obj)
        several = self.several_spaces(node.model)
        if node.missing_item:
            lines.append("%s -> NOT COMPUTED: ItemSpace %s does not exist yet, so nothing has been "
                         "recorded. calculate([%s]) creates and computes it."
                         % (ref, node.missing_item, _q(ref)))
            if mp:
                lines.extend("formula level: " + x for x in self._links(mp, node.name, several))
            return self.end("trace", args, "\n".join(lines))
        spec = {"obj": node.obj, "args": [enc(a) for a in node.args]}
        root = self.d("value.get", {"model": node.model, "evaluate": False, "nodes": [spec]})["values"][0]
        disp = root.get("display") or ref
        if not root.get("ok"):
            raise ToolError(self._error_text(root["error"]))
        word = "precedents" if direction == "preds" else "dependents"
        if not root.get("cached"):
            # Never 0 and never an empty list: an uncomputed node's links are
            # unknown, and the wire's predslen 0 on it is not read (spec 8.6).
            lines.append("%s = NOT COMPUTED, so its recorded %s are UNKNOWN, not none. "
                         "calculate([%s]) computes it." % (disp, word, _q(disp)))
            if mp:
                lines.append("formula level (read from source, not a calculation): "
                             + "; ".join(self._links(mp, node.name, several)))
            if direction == "preds":
                lines.extend(self._body(node))
            return self.end("trace", args, "\n".join(lines))
        count = root["predslen"] if direction == "preds" else root["succslen"]
        lines.append("%s = %s  [reads %d; read by %d computed]" % (disp, self._brief(root),
                                                                   root["predslen"], root["succslen"]))
        note = self._base_note(node)
        if note:
            lines.insert(0, note)
        if count > MAX_NEIGHBOURS:
            lines.append("%d recorded %s: too many to list (over %d); get_map(cells=%s) gives the "
                         "formula-level answer" % (count, word, MAX_NEIGHBOURS, _q(node.name)))
            return self.end("trace", args, "\n".join(lines))
        if direction == "preds":
            lines.append("A. recorded precedents - the %s modelx recorded this value READING:"
                         % _count(count, "node"))
        else:
            lines.append("A. recorded dependents - the %s recorded READING this value so far "
                         "(this grows as more is calculated):" % _count(count, "computed node"))
        recorded = set()
        self._tree_level(node, spec, direction, depth, 1, lines, set([disp]), offset, recorded)
        if mp:
            name = node.name
            if direction == "preds":
                named = sorted(fr for fr, to in mp["edges"] if to == name)
                refs_named = sorted(fr for fr, to in mp["ref_edges"] if to == name)
                gap = [n for n in named if n not in recorded]
                lines.append("B. formula level (read from source): %s names %s%s" % (
                    name, ", ".join(named) or ("no other Cells of this Space" if several else "no other Cells"),
                    "; itself" if name in mp["recursive"] else ""))
                if gap:
                    lines.append("   named in the formula but NOT read by this computation: %s "
                                 "(a branch not taken, or a name not called for these arguments)" % ", ".join(gap))
                lines.append("   References it names (modelx does not trace References): %s"
                             % (", ".join(refs_named) or "none"))
            else:
                named = sorted(to for fr, to in mp["edges"] if fr == name)
                gap = [n for n in named if n not in recorded]
                lines.append("B. formula level (read from source): named by the formulas of %s%s%s"
                             % (", ".join(named) or "no other Cells",
                                "; and itself" if name in mp["recursive"] else "",
                                " in this Space (formulas in other Spaces are not read here; A is what "
                                "has read it)" if several else ""))
                if gap:
                    lines.append("   named in a formula but not in A: %s - nothing computed from them has read %s"
                                 % (", ".join(gap), disp))
        if direction == "preds":
            lines.extend(self._body(node))
        return self.end("trace", args, R.clip(lines, self.max_chars,
                        "A smaller depth= prints less"))

    def _body(self, node):
        try:
            f = self.d("formula.get", {"model": node.model, "obj": node.obj})
        except WireError:
            return []
        body = "\n".join(x for x in strip_doc(f["source"]).splitlines() if x.strip())
        out = ["formula (docstring omitted; get_formulas shows it):"]
        if len(body) > BODY_CHARS:
            cut = body[:BODY_CHARS].rsplit("\n", 1)[0]
            out.extend("    " + x for x in cut.splitlines())
            out.append("    [%d more chars: get_formulas([%s])]" % (len(body) - len(cut), _q(node.text)))
        else:
            out.extend("    " + x for x in body.splitlines())
        return out

    def _tree_level(self, node, spec, direction, depth, level, lines, seen, offset, recorded):
        # values: false (bridge 0.10.0, B5): the trace reply mints no handle.
        # MEASURED on CashValue_ME pv_premiums(): 1,143 handles and 543,881
        # bytes with values, none and 149,977 bytes without. The values this
        # output prints come from one batched value.get below.
        params = {"model": node.model, "obj": spec["obj"], "args": spec["args"], "values": False}
        if direction == "preds":
            params["evaluate"] = False
        r = self.d("trace." + direction, params)
        neigh = r.get(direction) or []
        # Grouped by obj in first-seen order, not by consecutive runs:
        # CashValue_ME model_point()'s 4,575 dependents interleave their Cells.
        groups, order = {}, []
        for x in neigh:
            if x["obj"] not in groups:
                groups[x["obj"]] = []
                order.append(x["obj"])
            groups[x["obj"]].append(x)
        # Every recorded name, printed on this page or not: part B's "named but
        # NOT read" must never list a name that sits on another page.
        recorded.update(o.rsplit(".", 1)[-1] for o in order)
        ind = "  " * level
        start = offset if level == 1 else 0
        page = order[start:start + TRACE_GROUPS]
        want = []
        for o in page:
            g = groups[o]
            want.extend(g if len(g) < GROUP_MIN else [g[0], g[-1]])
        specs = [{"obj": x["obj"], "args": x["args"]} for x in want]
        ents, _ = self._read(node.model, specs, False) if specs else ([], 0)
        val = dict((x["display"], e) for x, e in zip(want, ents))
        # At the top level the page also stops where the output would pass
        # the bound, keeping room for part B and the formula body, so the
        # continuation's offset names the first group NOT printed.
        budget = self.max_chars - BODY_CHARS - 1500
        printed = 0
        for o in page:
            if level == 1 and printed and sum(len(x) + 1 for x in lines) > budget:
                break
            printed += 1
            g = groups[o]
            if len(g) < GROUP_MIN:
                for x in g:
                    self._neighbour_line(node, x, val, direction, depth, level, lines, seen, ind)
                continue
            f, l = g[0], g[-1]
            lines.append("%s%s  x%d nodes; first %s = %s, last %s = %s  (one: trace(%s))" % (
                ind, _arg_range(g), len(g), _argtext(f), self._brief(val.get(f["display"], {})),
                _argtext(l), self._brief(val.get(l["display"], {})), _q(f["display"])))
        if len(order) > start + printed:
            lines.append("%s[%d more groups (%d nodes) not shown: trace(%s%s, offset=%d)]" % (
                ind, len(order) - start - printed,
                sum(len(groups[o]) for o in order[start + printed:]),
                _q(node.text), "" if direction == "preds" else ", direction=\"succs\"",
                start + printed))

    def _neighbour_line(self, node, x, val, direction, depth, level, lines, seen, ind):
        e = val.get(x["display"], {})
        if x["display"] in seen:
            lines.append("%s%s (shown above)" % (ind, x["display"]))
            return
        seen.add(x["display"])
        cnt = x["predslen" if direction == "preds" else "succslen"]
        lines.append("%s%s = %s  (%s)" % (ind, x["display"], self._brief(e),
                                          ("reads %d" % cnt) if direction == "preds"
                                          else ("read by %d computed" % cnt)))
        if level < depth and cnt:
            if cnt > FANOUT:
                lines.append("%s  [%d %s not expanded (over %d): trace(%s%s)]" % (
                    ind, cnt, "precedents" if direction == "preds" else "dependents", FANOUT,
                    _q(x["display"]), "" if direction == "preds" else ", direction=\"succs\""))
            else:
                self._tree_level(node, {"obj": x["obj"], "args": x["args"]}, direction, depth,
                                 level + 1, lines, seen, 0, set())

    def _brief(self, e):
        if not e or not e.get("ok"):
            return "?"
        if not e.get("cached"):
            return "NOT COMPUTED"
        v = e.get("value")
        if not R.is_handle(v):
            return R.scalar(v)
        if v.get("shape") == []:
            return R.zero_d(v)
        shape = v.get("shape") or []
        prev = v.get("preview") or []
        if len(shape) == 1 and shape[0] <= R.SHORT and len(prev) >= shape[0]:
            idx = v.get("index_preview")
            if idx is not None:
                return (R.head(v) + (" %s" % v["index_name"] if v.get("index_name") else "") + " {"
                        + ", ".join("%s: %s" % (R.cell(k), R.cell(r[0])) for k, r in zip(idx, prev)) + "}")
            return R.head(v) + " [" + ", ".join(R.cell(r[0]) for r in prev) + "]"
        if len(shape) == 1:
            return R.head(v) + " [" + ", ".join(R.cell(r[0]) for r in prev[:3]) + ", ...]"
        return R.head(v)


_MISSING = object()


_HANDLE_ID = re.compile(r"\bhandle '?h\d+'?")


def _evicted(e):
    """A table read refused because its handle left the 64-entry LRU."""
    return e.code == "not_found" and "h" in (e.data or {})


def _runs(sorted_positions):
    """[(start, stop)] contiguous runs: one table.get each."""
    runs = []
    for p in sorted_positions:
        if runs and runs[-1][1] == p:
            runs[-1][1] = p + 1
        else:
            runs.append([p, p + 1])
    return [tuple(r) for r in runs]


def _argtext(x):
    return x["display"][x["display"].find("("):] if "(" in x["display"] else ""


def _acc_text(k, v):
    """An accessor as a ref writes it."""
    return {"loc": ".loc[%s]", "iloc": ".iloc[%s]", "pos": "[%s]", "col": "[%s]"}[k] % (
        _q(v) if k == "col" else fmt_arg(v))


def _arg_range(group):
    """'BasicTerm_S.Projection.claims(t=0..120)' when the first argument runs
    contiguously and the rest are equal; else '<base>(...)'."""
    f = group[0]
    base = f["display"][:f["display"].find("(")] if "(" in f["display"] else f["display"]
    firsts = [dec(x["args"][0]) if x["args"] else None for x in group]
    rests = set(json.dumps(x["args"][1:]) for x in group)
    if (all(isinstance(a, int) and not isinstance(a, bool) for a in firsts) and len(rests) == 1
            and sorted(firsts) == list(range(min(firsts), max(firsts) + 1))):
        inner = f["display"][f["display"].find("(") + 1:-1]
        pname = inner.split("=", 1)[0] if "=" in inner else "arg"
        tail = inner.split(",", 1)[1] if "," in inner else ""
        return "%s(%s=%d..%d%s)" % (base, pname, min(firsts), max(firsts), ("," + tail) if tail else "")
    return "%s(...)" % base


def _range_stats(pairs, n, pname):
    """Statistics over exactly the values one call received: [(x, v)]. Sums of
    floats use math.fsum; of ints, exact Python ints."""
    if not pairs:
        return "no numbers"
    vals = [v for _, v in pairs]
    lo = min(pairs, key=lambda p: p[1])
    hi = max(pairs, key=lambda p: p[1])
    tot = sum(vals) if all(isinstance(v, int) for v in vals) else math.fsum(vals)
    return ("over these %d nodes: sum %s, min %s at %s=%s, max %s at %s=%s"
            % (len(pairs), R.num(tot), R.num(lo[1]), pname, R.cell(lo[0]),
               R.num(hi[1]), pname, R.cell(hi[0])))


def strip_doc(src):
    """A formula's source without its docstring (the first statement, when it
    is a string). Text in, text out."""
    try:
        mod = ast.parse(src)
        fn = mod.body[0]
        first = fn.body[0]
        if (isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant)
                and isinstance(first.value.value, str)):
            lines = src.splitlines(True)
            return "".join(lines[:first.lineno - 1] + lines[first.end_lineno:])
    except Exception:
        pass
    return src


def doc_paragraph(doc, name):
    """The block of a Space docstring that documents `name`: a line
    '<indent>name: ...' and every following line indented deeper."""
    lines = doc.splitlines()
    pat = re.compile(r"^(\s*)%s\s*:" % re.escape(name))
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        ind = len(m.group(1))
        out = [line.rstrip()]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= ind:
                break
            out.append(nxt.rstrip())
        while out and not out[-1].strip():
            out.pop()
        return ["  " + x[ind:] if len(x) >= ind else "  " + x.strip() for x in out]
    return []


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def formula_error_text(msg, data):
    """modelx's formula error as a reader needs it: the message, the call
    chain, and the line that raised, parsed from the wire's formula_traceback.

    The failing node is named by `error_display` (bridge 0.10.0, B8), never by
    `error_obj`, which inside an ItemSpace is modelx's internal __SpaceN name
    (measured: Projection.__Space2.model_point for Projection[99999])."""
    tb = data.get("formula_traceback") or ""
    where = data.get("error_display")
    if not where:
        obj = data.get("error_obj") or ""
        where = obj if obj and "__Space" not in obj else None
    frames, src = [], []
    part = None
    for line in tb.splitlines():
        if line.startswith("Formula traceback:"):
            part = "tb"
            continue
        if line.startswith("Formula source:"):
            part = "src"
            continue
        if part == "tb" and re.match(r"^\d+: ", line):
            idx, text = line.split(": ", 1)
            frames.append((int(idx), text))
        elif part == "src":
            src.append(line)
    out = ["formula error: %s" % msg]
    if frames:
        out.append("    call chain: " + _chain(frames))
        m = re.search(r", line (\d+)$", frames[-1][1])
        if m and src and where:
            k = int(m.group(1))
            if 0 < k <= len(src):
                out.append("    raised at line %d of %s: %s" % (k, where, src[k - 1].strip()))
    if where:
        out.append("    get_formulas([%s]) shows that formula" % _q(where.split("(")[0]))
    else:
        out.append("    the bridge did not name the failing node (modelx kept no traceback)")
    return "\n".join(out)


def _chain(frames):
    """[(frame number, text)] -> 'a -> b -> ... N more ... -> z': the first 6
    and last 5 frames, and every gap counted from modelx's own frame numbers.
    MEASURED: modelx itself prints a 65,001-frame DeepReferenceError chain as
    frames 0-7, '...', 64991-65000; counting the lines it printed said "7
    more" where 64,990 were hidden."""
    keep = frames if len(frames) <= 12 else frames[:6] + frames[-5:]
    out, prev = [], -1
    for idx, text in keep:
        if idx > prev + 1:
            out.append("... %d more ..." % (idx - prev - 1))
        out.append(re.sub(r", line \d+$", "", text))
        prev = idx
    return " -> ".join(out)


_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}


def derived_expr(text):
    """None for a plain ref; (tree, [leaf ref texts]) for arithmetic over refs:
    + - * / and parentheses over refs and numbers (spec C8). The task judge
    measured ratios left to the model wrong in 3 of 4 live S6 b7 runs."""
    s = clean(text)
    try:
        tree = ast.parse(s, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(tree, (ast.BinOp, ast.UnaryOp)):
        return None
    leaves = []

    def walk(n):
        if isinstance(n, ast.BinOp):
            if type(n.op) not in _OPS:
                raise RefError("a derived value uses + - * / only: %r" % text)
            walk(n.left)
            walk(n.right)
        elif isinstance(n, ast.UnaryOp):
            if not isinstance(n.op, (ast.USub, ast.UAdd)):
                raise RefError("a derived value uses + - * / only: %r" % text)
            walk(n.operand)
        elif isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
                raise RefError("a constant in a derived value must be a number: %r" % text)
        else:
            seg = ast.get_source_segment(s, n)
            n._leaf = seg
            leaves.append(seg)
    walk(tree)
    if not leaves:
        raise RefError("%r names no node" % text)
    if len(leaves) > MAX_OPERANDS:
        raise RefError("a derived value has at most %d operands; %r has %d"
                       % (MAX_OPERANDS, text, len(leaves)))
    return tree, leaves


def eval_derived(n, values):
    if isinstance(n, ast.BinOp):
        return _OPS[type(n.op)](eval_derived(n.left, values), eval_derived(n.right, values))
    if isinstance(n, ast.UnaryOp):
        x = eval_derived(n.operand, values)
        return -x if isinstance(n.op, ast.USub) else x
    if isinstance(n, ast.Constant):
        return n.value
    return values[n._leaf]


def _substitute(text, leaves, shown):
    """The expression with each operand replaced by the ref its value was read
    at. Longest operand first, through placeholders, so an operand that is a
    substring of another is never replaced inside it."""
    order = sorted(set(leaves), key=len, reverse=True)
    marks = dict((leaf, "\x01%d\x01" % k) for k, leaf in enumerate(order))
    for leaf in order:
        text = text.replace(leaf, marks[leaf])
    for leaf in order:
        text = text.replace(marks[leaf], shown[leaf])
    return text


def verify_citations(tools, claims):
    """Re-check stated numbers against the cache. NEVER evaluates (spec 8.7).

    claims: [{"ref": str, "value": str | number}]. A string value is compared
    to the decimal places written ("910.92"); a number to 1e-9 relative.
    Returns [{"ref", "verdict", "model_value", "display"}], verdict one of
    MATCH, MISMATCH, NOT COMPUTED, CANNOT CHECK. Not an MCP tool: a model-facing
    MATCH verdict returned MATCH for a misaddressed pv_net_cf()[7342] in S6 s8.
    """
    out = []
    tools.begin()
    for c in claims:
        ref, stated = c.get("ref"), c.get("value")
        row = {"ref": ref, "verdict": "CANNOT CHECK", "model_value": None, "display": None}
        out.append(row)
        try:
            node = tools.resolve(ref)
        except (ToolError, RefError, WireError) as e:
            row["why"] = str(e)
            continue
        if node.missing_item or node.kind not in ("Cells", "Reference") or node.expand:
            row["verdict"] = "NOT COMPUTED" if node.missing_item else "CANNOT CHECK"
            continue
        if node.args is None:
            if node.params:
                continue
            node.args = []
        e = tools._read(node.model, [{"obj": node.obj, "args": [enc(a) for a in node.args]}], False)[0][0]
        row["display"] = e.get("display")
        if not e.get("ok"):
            continue
        if not e.get("cached"):
            row["verdict"] = "NOT COMPUTED"
            continue
        v = e.get("value")
        if node.accessors:
            el = tools._element(node, e.get("display"), v)
            if "cell" not in el:
                continue
            row["display"], v = el["shown"], el["cell"]
        x = dec(v)
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            continue
        row["model_value"] = x
        if isinstance(stated, str):
            text = stated.strip().replace(",", "")
            try:
                s = float(text)
            except ValueError:
                continue
            places = len(text.split(".", 1)[1]) if "." in text else 0
            ok = round(x, places) == round(s, places)
        else:
            ok = math.isclose(x, float(stated), rel_tol=1e-9, abs_tol=0.0 if x else 1e-12)
        row["verdict"] = "MATCH" if ok else "MISMATCH"
    return out
