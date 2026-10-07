"""The v0 methods and the transport-agnostic dispatcher.

Bridge.dispatch(method, params) -> result is the whole API; the Jupyter comm
adapter and the in-process tests are both thin wrappers over it.

This module leans on modelx private API (_idstr, _get_from_name, _get_object,
_impl.repr_parent, _named_itemspaces) on purpose -- PLAN 3.2 says that surface
is to be promoted into modelx itself. Most uses are in the helpers at the end
of this file; three calls are not (_get_object twice, _named_itemspaces once).
"""

import datetime
import json
import os
import ast
import platform
import re
import sys
import textwrap
import time

from . import files, samples, tables
from .samples import build_sample, sample_catalog
from .codec import Codec, MAX_MESSAGE_BYTES, _pyname
from .errors import (BridgeError, bad_request, formula_error, internal,
                     no_model, not_found)

PROTOCOL = 0
VERSION = "0.10.0"

#: Additive extensions to protocol 0, advertised in session.info so a frontend
#: can detect them instead of guessing from the version string. PROTOCOL stays 0
#: on purpose: section 1 says a differing `protocol` closes the comm, and
#: everything here is a NEW method or an OPTIONAL field, so a v0 client that
#: ignores this list keeps working unchanged.
#:
#: `open.conflict` is the one entry that announces a BEHAVIOUR change rather than
#: a new field: model.open reads the file the caller named instead of handing
#: back whatever model happened to hold that name (section 9.4). A client cannot
#: opt out of that -- it is the fix for a data-loss bug, not a feature -- but it
#: can tell which kernel it is talking to, and only a kernel listing it accepts
#: `on_conflict` / `force`.
#:
#: `save.backup` says this kernel decides modelx's backup-on-write for itself
#: (off in a browser), accepts `params.backup` and REPORTS what it did, so a UI
#: can stop showing a save that quietly stored the model twice as a plain save.
#: `kernel.identity` says session.info carries a `kernel` block whose `id` is
#: constant for the life of one interpreter -- the only reliable way for a
#: frontend to tell a real restart from a bootstrap re-run.
#:
#: `formula.set` is the FIRST entry that announces a method which CHANGES a
#: model. Everything before it reads, or moves whole model files around; this
#: one rewrites a Cells formula in place. A frontend that does not list it must
#: not offer an editor, because on an older kernel the request comes back
#: `bad_request: unknown method` after the user has typed a formula.
#:
#: `ref.set` is the second, and the two are advertised separately because a
#: kernel could plausibly have one without the other, and because their blast
#: radii differ by an order of magnitude: a formula edit clears what depended on
#: that formula, a reference edit clears EVERY cached value in the model
#: (section 12.1). A frontend must be able to tell the user which it is about to
#: do, and it can only do that if it knows which methods exist.
#:
#: `cells.page` and `table.stats` go back to reading, and they are advertised
#: because a client MUST detect them before drawing a grid frame: on an older
#: kernel the request comes back `bad_request: unknown method` after the panel
#: has already promised a table. They are listed separately because a kernel
#: could plausibly have one without the other, and because they answer different
#: questions -- `cells.page` reads one Cells' cached keys and values without
#: evaluating (section 13), `table.stats` reduces one whole column of a handle
#: the client is already paging (section 10.5).
#:
#: `trace.succs` and `map.get` (0.9.0) are the Trace tab's and the Model map's,
#: and both are reads that CANNOT evaluate: a dependent exists only once
#: something that reads the node has been computed, and the map is parsed from
#: formula source (sections 16 and 17). `trace.succs` also covers the optional
#: `evaluate` on `trace.preds`, which arrived in the same version.
#:
#: The seven 0.10.0 names are what modelx-mcp needs (PLAN 3.6, section 18), and
#: each is an OPTIONAL result field or an OPTIONAL param whose default is the
#: 0.9.0 behaviour. `session.computed`, `stats.extremes`, `error.display` and
#: `handle.index_name` are fields a client may ignore. `trace.values`,
#: `cells.page.element` and `table.get.label` are params, and a client MUST
#: detect them before sending one: `trace.*` and `table.get` DROP a param they
#: do not know, so on a 0.9.0 kernel `table.get {label}` answers row 0 as if it
#: were the label's row, and `values: false` mints every handle it was sent to
#: avoid. (`cells.page` refuses an unknown param, so `element` fails loudly.)
FEATURES = ["table.get", "buffers", "files", "storage", "model.close",
            "open.conflict", "save.backup", "kernel.identity",
            "formula.set", "ref.set", "cells.page", "table.stats",
            "doc.get", "trace.succs", "map.get",
            "session.computed", "stats.extremes", "trace.values",
            "cells.page.element", "table.get.label", "error.display",
            "handle.index_name"]

#: What model.open may do about a name that is already taken (section 9.4).
CONFLICT_MODES = ("open", "reuse", "replace")

#: How many of a Space's dynamic ItemSpaces  names inline.
#: Measured: 10,000 named ItemSpaces are 636,674 bytes against 148,894 for
#: bare internal names, on a payload re-read on every recalculation. Fifty is
#: more than any tree shows at once and costs about 3 KB. Paging past it is
#: Phase 2 (UI-DESIGN section 8).
MAX_TREE_ITEMSPACES = 50

#: ---- kernel identity (section 6.1, and what "Restart Python" has to prove) --
#:
#: These live in os.environ rather than in module globals ON PURPOSE. Re-running
#: the bootstrap cell re-execs this module into a brand new namespace, so a
#: module global would mint a fresh id with the interpreter unchanged -- telling
#: the frontend "you are talking to a new kernel" when it is the same one, which
#: is precisely the confusion this block exists to remove. os.environ survives a
#: module re-exec and dies with the interpreter: exactly the lifetime wanted.
#:
#: What it buys: `id` differs => a real restart happened (new interpreter).
#: `id` the same with `boots` higher => the bootstrap was re-run in place. A
#: "Restart Python" that appears to do nothing is then one field away from being
#: diagnosed instead of argued about.
KERNEL_ID_VAR = "MODELX_BRIDGE_KERNEL_ID"
KERNEL_STARTED_VAR = "MODELX_BRIDGE_KERNEL_STARTED"
KERNEL_BOOTS_VAR = "MODELX_BRIDGE_KERNEL_BOOTS"


def _new_kernel_id():
    try:
        return os.urandom(8).hex()
    except Exception:
        # Pyodide has os.urandom; a host that does not is still better served by
        # a weak id than by no id at all, because the comparison is equality.
        return "%016x" % (int(time.time() * 1000000) & ((1 << 64) - 1))


def _register_boot():
    """Record that the bridge was installed into this interpreter.

    Returns (id, started_epoch, boots). Called exactly once per exec of this
    module, which is once per bootstrap run.
    """
    if not os.environ.get(KERNEL_ID_VAR):
        os.environ[KERNEL_ID_VAR] = _new_kernel_id()
        os.environ[KERNEL_STARTED_VAR] = "%.6f" % time.time()
        os.environ[KERNEL_BOOTS_VAR] = "0"
    try:
        boots = int(os.environ[KERNEL_BOOTS_VAR]) + 1
    except (KeyError, ValueError):
        boots = 1
    os.environ[KERNEL_BOOTS_VAR] = str(boots)
    try:
        started = float(os.environ[KERNEL_STARTED_VAR])
    except (KeyError, ValueError):
        started = time.time()
    return os.environ[KERNEL_ID_VAR], started, boots


KERNEL_ID, KERNEL_STARTED, KERNEL_BOOTS = _register_boot()


def kernel_block():
    """Who is answering, and since when (section 6.1).

    Reads os.environ rather than the module constants above, because a Bridge is
    deliberately carried across a bootstrap re-run (jupyter.register_comm) and is
    therefore bound to the globals of the module copy that CREATED it. Reporting
    `boots` from those globals would freeze the count at the re-run that made the
    Bridge, and the one question this block answers is how many times the
    bootstrap has run in this interpreter.
    """
    try:
        started = float(os.environ.get(KERNEL_STARTED_VAR, KERNEL_STARTED))
    except ValueError:
        started = KERNEL_STARTED
    try:
        boots = int(os.environ.get(KERNEL_BOOTS_VAR, KERNEL_BOOTS))
    except ValueError:
        boots = KERNEL_BOOTS
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    except Exception:
        stamp = None
    return {"id": os.environ.get(KERNEL_ID_VAR) or KERNEL_ID, "started": stamp,
            "uptime": round(max(0.0, time.time() - started), 3),
            "boots": boots, "runtime": sys.platform}

#: How a Reference to a modelx object rebinds under inheritance. modelx does NOT
#: validate this -- `set_ref(name, value, refmode="sideways")` stores
#: "sideways", measured -- so ref.set checks it before passing it on.
REFMODES = ("auto", "absolute", "relative")

#: The longest formula source formula.set will accept. A formula is a screen of
#: Python, not a file: this is four times the longest in lifelib and exists so a
#: paste accident is refused with a sentence instead of arriving as a megabyte
#: the result then has to carry back.
MAX_SOURCE_CHARS = 20000

#: Stop walking a model's structure after this many Spaces and report the count
#: as negative. A fingerprint that costs O(model) on every post_execute would
#: re-introduce, in a smaller way, exactly the problem it exists to fix.
MAX_FINGERPRINT_SPACES = 2000

#: Everything `cells.page` accepts. It is a whitelist rather than a set of
#: getters because this method REJECTS an unknown key instead of dropping it the
#: way `table.get` does -- a frontend that sends `around_` must read as a client
#: error, not as a kernel that silently answered without a focus row.
_CELLS_PAGE_PARAMS = ("model", "obj", "row", "rows", "around", "format",
                      "element")


class Bridge:
    """One session. Holds the handle store, per-model revisions and the event queue."""

    def __init__(self, codec=None):
        self.codec = codec or Codec()
        self.revisions = {}
        self._events = []
        self._bumped = set()        # models already bumped during this dispatch
        self._states = {}           # model name -> fingerprint as last reported
        self._known = set()         # model names as last reported
        self._homes = {}            # model name -> virtual path it lives at
        self._dirty = set()         # models the USER's own code has moved
        self._baseline = {}         # model name -> edit key when last read/saved
        self._in = []               # buffers that arrived with this request
        self._out = []              # buffers this response will carry
        self._series_memo = None    # ((model, obj, revision), series) -- see _series

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, method, params=None, buffers=None):
        """Run one method. Raises BridgeError; anything else is a bug.

        ``buffers`` are the binary parts of the incoming comm message (section
        10). Any the method produced are collected in ``self._out`` and read back
        with ``take_buffers()`` -- keeping them off the result keeps the result
        JSON-serialisable, which ``_check_size`` and every test depend on.
        """
        handler = _METHODS.get(method)
        if handler is None:
            raise bad_request("unknown method %r" % (method,), method=method)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise bad_request("params must be an object")
        self._bumped = set()
        self._in = list(buffers or ())
        self._out = []
        return handler(self, params)

    def take_buffers(self):
        """The binary parts of the response just built, and clear them."""
        out, self._out = self._out, []
        return out

    def buffer_in(self, index=0):
        """One buffer that arrived with the request, as bytes."""
        if index >= len(self._in):
            raise bad_request(
                "this request carries %d binary buffer(s); buffer %d was "
                "expected" % (len(self._in), index), buffers=len(self._in))
        return _as_bytes(self._in[index])

    def handle(self, data, buffers=None):
        """Turn one wire message into exactly one `res` envelope, or None if the
        message is not a request we own. Never raises (protocol section 3).

        A response carrying binary parts puts them under the envelope's
        ``buffers`` key; ``split_buffers`` separates them for the transport.
        """
        if not isinstance(data, dict) or data.get("type") != "req":
            return None
        req_id = data.get("id")
        if not isinstance(req_id, str):
            return None             # nothing to correlate a response with
        try:
            result = self.dispatch(data.get("method"), data.get("params"), buffers)
            envelope = {"type": "res", "id": req_id, "result": result}
            out = self.take_buffers()
            if out:
                envelope["buffers"] = out
            return envelope
        except BridgeError as err:
            self.take_buffers()     # a failed request sends no binary parts
            return {"type": "res", "id": req_id, "error": err.to_json()}
        except Exception as exc:
            self.take_buffers()
            return {"type": "res", "id": req_id, "error": internal(exc).to_json()}

    def hello(self):
        return {"type": "hello", "result": self.dispatch("session.info", {})}

    # -- revisions and events ---------------------------------------------

    def revision(self, model):
        name = model if isinstance(model, str) else model.name
        return self.revisions.setdefault(name, 1)

    def bump(self, model, reason, state=None):
        name = model if isinstance(model, str) else model.name
        rev = self.revisions.get(name, 1) + 1
        self.revisions[name] = rev
        # `execute` means a post_execute in which THIS model moved, so the thing
        # that moved it is the user's own code in the Console; `edit` means a
        # mutating bridge method did. Both are work that exists nowhere else,
        # which is the whole definition of `dirty` (section 9.4.1). Evaluation
        # the bridge itself performed bumps with `evaluate` and is not work.
        if reason in ("execute", "edit"):
            self._dirty.add(name)
        # An event is a statement about what the frontend has now been told, so
        # the fingerprint we compare against next time is recorded here and
        # nowhere else. Without this, an evaluation we performed ourselves would
        # be re-reported by the next post_execute as if a user had caused it.
        self._remember(name, state=state)
        self._events.append({"type": "evt", "event": "model.changed",
                             "params": {"model": name, "revision": rev,
                                        "reason": reason}})
        return rev

    def prime(self):
        """Adopt the current state without emitting anything.

        Called when the post_execute hook is installed: the models open at that
        moment are the ones the bootstrap just opened, and the frontend learns
        about them from `hello`, not from an event.
        """
        models = _open_models()
        self._known = set(models)
        self._states = dict((n, _fingerprint(m)) for n, m in models.items())
        # setdefault, not assignment: re-running the bootstrap cell primes a
        # Bridge that is carried across (jupyter.register_comm), and re-baselining
        # a model the visitor has edited would call it untouched -- which is the
        # one thing that lets the kernel close it unasked.
        for name, model in models.items():
            self._baseline.setdefault(name, _touch_key(model))
        return self._known

    def on_execute(self, reason="execute"):
        """Called from IPython post_execute. Emits only for models that MOVED.

        MEASURED, and the reason this is not "bump everything": in the
        JupyterLite Pyodide kernel every comm_msg fires post_execute, so an
        unconditional bump makes each bridge request emit model.changed, which
        the panel answers with more requests. That loop was observed at ~935
        revisions/second on an idle tab. Comparing a cheap fingerprint breaks it
        at the source: a request that reads cached values changes nothing, so it
        emits nothing, so there is nothing for the panel to answer.

        The fingerprint is (computed nodes, reference edges, structure size).
        It is ~5 microseconds for BasicTerm_S and is a *conservative* change
        detector, not proof of equality: it can miss an edit that leaves all
        three counts identical (a formula rewritten with nothing computed, say).
        `revision` was always documented as "may have changed", and a missed
        bump costs a stale panel until the next real change, where a spurious
        bump costs the loop above.
        """
        models = _open_models()
        names = set(models)
        for name in sorted(self._known - names):
            # Closed in the Console, not through model.close. Drop the save
            # location, the edit baseline and the sample record with it: a stale
            # home would otherwise outlive the model it described, and a stale
            # sample record would tell model.close that the NEXT model to take
            # that name is rebuildable when it is not.
            self._forget_model(name)
            self.bump(name, "closed")
        for name in sorted(names):
            state = _fingerprint(models[name])
            if name not in self._known:
                self.bump(name, "open", state=state)
            elif self._states.get(name) != state:
                self.bump(name, reason, state=state)
        self._known = names

    def drain_events(self):
        events, self._events = self._events, []
        return events

    def _remember(self, name, state=None, model=None):
        if state is None:
            if model is None:
                model = _open_models().get(name)
            state = _fingerprint(model) if model is not None else None
        if state is None:
            self._states.pop(name, None)
            self._known.discard(name)
        else:
            self._states[name] = state
            self._known.add(name)

    def _evaluated(self, model):
        """Record that a method computed something. At most one bump per dispatch."""
        if model.name not in self._bumped:
            self._bumped.add(model.name)
            self.bump(model, "evaluate")

    # -- methods -----------------------------------------------------------

    def session_info(self, params):
        import modelx as mx
        return {
            "protocol": PROTOCOL,
            "bridge": VERSION,
            "modelx": mx.__version__,
            "python": platform.python_version(),
            "runtime": "pyodide" if sys.platform == "emscripten" else "cpython",
            # `path` is the model's ONE save location: what a model.save with no
            # path writes to, null when there is none (sections 9.5, 9.7). It is
            # NOT modelx's model.path -- see _home_of for why that fallback was
            # withdrawn. `dirty` and `sample` are what a UI needs to ask before
            # closing or replacing one (section 9.4.1). `computed` (0.10.0) is
            # the one cached-value total that counts inside ItemSpaces -- see
            # _computed.
            "models": [{"name": name, "revision": self.revision(name),
                        "path": self._homes.get(name),
                        "dirty": self._edited(name),
                        "sample": samples.sample_of(name),
                        "computed": _computed(model)}
                       for name, model in sorted(mx.get_models().items())],
            # Additive to the v0 envelope: the gallery a UI should offer, so the
            # sample list is not duplicated as a constant in the frontend.
            "samples": sample_catalog(),
            # Also additive, and the reason it is HERE rather than only on
            # storage.info: session.info is pushed as `hello`, so the frontend
            # learns at handshake time that this kernel has no persistent
            # filesystem -- which is the service-worker race, and the moment the
            # UI has to start telling the truth about it (UI-DESIGN 4.1).
            "storage": files.storage_info(),
            # What the bootstrap opened and why (section 9.7). A returning
            # visitor whose storage already holds their models gets NOTHING
            # opened, and this is how the frontend learns to greet them with
            # their own work instead of an empty Explorer.
            "boot": samples.boot_report(),
            # Which interpreter is answering. `id` is constant for the life of
            # one kernel and changes only when a new one starts, so a frontend
            # that kept the previous value can PROVE whether "Restart Python"
            # restarted anything -- and can drop every cached model name, handle
            # and revision the moment it sees a different id (section 6.1).
            "kernel": kernel_block(),
            "features": list(FEATURES),
            "limits": self._limits(),
        }

    def _limits(self):
        limits = self.codec.limits()
        limits["max_buffer_bytes"] = tables.MAX_BUFFER_BYTES
        limits["max_page_cells"] = tables.MAX_PAGE_CELLS
        # Checked against the FREE `len(cells)` before `.series` is touched, so
        # a client can say "1,400,000 cached values -- too many to page here"
        # from the reply instead of discovering the limit by freezing the one
        # kernel thread for the length of a materialisation (section 13).
        limits["max_series_rows"] = tables.MAX_SERIES_ROWS
        return limits

    def model_open_sample(self, params):
        sample = params.get("sample")
        if not isinstance(sample, str):
            raise bad_request("params.sample must be a string")
        model = build_sample(sample)
        # We opened it, so the frontend learns about it from this result. Adopt
        # the state here or the next post_execute reports it as a user change.
        self._remember(model.name, model=model)
        self._baseline_now(model)
        self._dirty.discard(model.name)
        return {"model": model.name, "revision": self.revision(model),
                "sample": samples.sample_of(model.name),
                "path": self._homes.get(model.name),
                "dirty": model.name in self._dirty}

    def tree_get(self, params):
        model = self._model(params)
        obj = params.get("obj", "")
        depth = params.get("depth", -1)
        if not isinstance(depth, int) or isinstance(depth, bool):
            raise bad_request("params.depth must be an integer")
        root = _resolve(model, _as_obj(obj))
        result = {"model": model.name, "revision": self.revision(model),
                  "root": self._tree_node(root, depth)}
        return _check_size(result, "tree.get", "narrow params.obj or params.depth")

    def formula_get(self, params):
        """One formula, and what the editor needs to know about it. Section 6.4.

        `derived`, `dynamic` and `parameters` are ADDITIVE (0.9.0), and they are
        here because the formula pane moved out of the Explorer into its own tab
        and lost the tree it used to read them from. Carrying them on the
        selection instead would have gone stale on exactly the event that
        changes them: editing an inherited Cells makes it defined here, and the
        selection that said "inherited" was published before the edit. This
        answer is re-read after every edit, so it cannot lag the model.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        target = _resolve(model, obj)
        formula = getattr(target, "formula", None)
        if formula is None or _is_null_formula(formula):
            raise not_found("%s has no formula" % _display(target, None), obj=obj)
        return {"source": formula.source, "doc": getattr(target, "doc", None),
                "kind": type(target).__name__,
                "parameters": _parameters(target),
                "derived": _derived(target),
                # A Cells modelx generated inside an ItemSpace: formula.set
                # refuses it, so an editor must not offer to try.
                "dynamic": _is_dynamic(target)}

    def doc_get(self, params):
        """One object's docstring. Section 14.

        WHY THIS IS ITS OWN METHOD AND NOT A FIELD ON `tree.get`. MEASURED on
        BasicTerm_S: the Projection space's docstring is 6,824 characters and
        all 40 of its Cells carry one, 6,552 more on Python 3.13+, which strips
        docstring indentation (7,372 on 3.12) -- about 13 KB added to a
        payload that is re-read on every `model.changed`, which fires on every
        recalculation. It fits inside `max_message_bytes` for this model and
        would not for a model with thousands of Cells, and paying it per
        recalculation to show one node's prose is the wrong trade at any size.
        So the doc travels for the node that is actually selected, one at a
        time, the way `formula.get` already does.

        WHY NOT JUST RELAX `formula.get`. It answers `not_found` for anything
        without a formula, and the formula pane depends on that -- returning a
        null source for a Space would put an empty formula box under a Space.
        A Space's docstring is the single richest piece of documentation the
        demo model ships, so it needs a route that does not distort the one
        method that already works.

        IT NEVER EVALUATES. `doc` is an attribute read on an object that is
        already resolved; nothing here can trigger a calculation, which is the
        standard section 7 holds every introspection call to.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        target = _resolve(model, obj)
        doc = getattr(target, "doc", None)
        return {
            "obj": obj,
            "kind": type(target).__name__,
            # Normalised to a string or null: modelx hands back whatever the
            # Python object carries, and a non-string `__doc__` is a thing that
            # exists in the wild.
            "doc": doc if isinstance(doc, str) and doc.strip() else None,
        }

    def formula_set(self, params):
        """Replace one Cells formula. Section 11 -- THE FIRST MUTATING METHOD.

        Everything the bridge did before this read a model or moved a whole
        model file. This rewrites a formula in place, so the four facts below
        are measured against modelx 0.33 rather than assumed, and each one is
        load-bearing:

        1. THE EDIT IS INVISIBLE TO BOTH DIRTY DETECTORS. `_fingerprint` is
           (computed nodes, reference edges, structure size) and `_touch_key` is
           (structure, Reference identities); a formula edit on a model with
           nothing computed yet moves NEITHER -- measured (0, 0) -> (0, 0), key
           unchanged. So this method marks the model dirty ITSELF, through a
           bump with reason `edit`. Leaving it to the detectors would let
           model.close discard a visitor's rewritten formula while calling the
           model untouched, which is exactly the class of bug section 9.4.1
           exists to prevent.

        2. MODELX VALIDATES BEFORE IT APPLIES, so a rejected edit changes
           nothing -- measured: after a SyntaxError the previous formula was
           still callable and still returned its old value. This method
           therefore needs no rollback; it must only avoid destroying anything
           of its own before handing the source over.

        3. THE SOURCE MUST BE EXACTLY ONE `def` OR ONE `lambda`. modelx answers
           anything else with `ValueError: invalid function or lambda
           definition` -- measured for `import os` plus a def, for two defs, for
           a bare expression and for the empty string. That is a real property
           worth stating rather than a quirk to work around: a formula cannot
           smuggle arbitrary statements into the kernel.

        4. RE-APPLYING THE IDENTICAL SOURCE IS NOT FREE. Setting a formula
           clears the cached values of that Cells and everything downstream --
           measured at 260 of BasicTerm_S's 1,832 computed nodes for an
           unchanged `claims`. An editor whose Apply is reachable when nothing
           was typed would silently throw a model's computation away, so an
           unchanged source is reported (`changed: false`) and NOT applied.

        `expect` is optimistic concurrency and the reason the pane can follow
        `model.changed` safely: the editor sends the source it started from, and
        if the live formula is no longer that -- because the Console rewrote it,
        or another panel did -- the edit is refused with the current source in
        `data.current` rather than overwriting a change nobody has seen.

        What comes back is everything the UI has to say out loud: the new source
        as modelx normalised it, the docstring modelx parsed out of it, the new
        parameter list (a formula MAY change it -- measured, `t` -> `t, kind`),
        how many computed values the edit invalidated, and whether the edit
        turned an inherited Cells into an override.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        source = params.get("source")
        if not isinstance(source, str):
            raise bad_request("params.source must be a string")
        if len(source) > MAX_SOURCE_CHARS:
            raise bad_request(
                "the formula is %d characters, over the %d character limit"
                % (len(source), MAX_SOURCE_CHARS), chars=len(source),
                max_chars=MAX_SOURCE_CHARS)
        expect = params.get("expect")
        if expect is not None and not isinstance(expect, str):
            raise bad_request("params.expect must be a string or null")

        target = _resolve(model, obj)
        self._require_editable(target, obj)

        current = (None if _is_null_formula(target.formula)
                   else target.formula.source)
        if expect is not None and expect != current:
            raise bad_request(
                "%s has changed since the editor read it; nothing was written"
                % _display(target, None),
                conflict=True, obj=obj, current=current, expected=expect)

        was_derived = _derived(target)
        if current is not None and source == current:
            # Applying it would clear this Cells and everything downstream for
            # no change (fact 4 above). Report, do not destroy.
            return self._formula_result(model, target, obj, changed=False,
                                        cleared=0, was_derived=was_derived,
                                        submitted=source, source=current)

        before = _traced(model)
        try:
            target.formula = source
        except SyntaxError as exc:
            raise _syntax_refusal(exc, obj)
        except BridgeError:
            raise
        except Exception as exc:
            # ValueError("invalid function or lambda definition") lands here,
            # and so does modelx's own refusal of a dynamic Cells. Neither is an
            # internal fault -- both are the caller being told what a formula
            # may be -- so they must not come back as `internal` carrying a
            # traceback the UI then shows to someone who mistyped a colon.
            raise _shape_refusal(exc, obj)

        cleared = max(0, before - _traced(model))
        # THE DIRTY MARK (fact 1). bump(reason="edit") records the new
        # fingerprint, adds the model to _dirty and emits model.changed, so the
        # panel re-reads and model.close now refuses without `force`.
        self.bump(model, "edit")
        return self._formula_result(model, target, obj, changed=True,
                                    cleared=cleared, was_derived=was_derived,
                                    submitted=source)

    def _require_editable(self, target, obj):
        """Refuse everything that is not a static Cells, by name.

        A Space formula IS its parameter list -- rewriting one rebuilds every
        ItemSpace beneath it -- and a Reference holds a value, not a formula.
        Both are selectable in the Explorer, and a dynamic Cells is selectable
        from the Precedents list (a node inside an ItemSpace), so each gets a
        sentence saying what it is rather than modelx's bare ValueError.
        """
        kind = type(target).__name__
        if _is_model(target):
            raise bad_request(
                "a Model has no formula; select a Cells to edit one", obj=obj)
        if _is_reference(target):
            raise bad_request(
                "%s is a Reference: it holds a value, not a formula" % obj,
                obj=obj, kind="Reference")
        if kind != "Cells":
            raise bad_request(
                "%s is a %s; this build edits Cells formulas only, because a "
                "Space formula is its parameter list and rewriting one rebuilds "
                "every ItemSpace beneath it" % (obj, kind), obj=obj, kind=kind)
        if _is_dynamic(target):
            raise bad_request(
                "%s lives in an ItemSpace, which modelx builds from its Space's "
                "formula; edit the Cells it was derived from instead"
                % _display(target, None), obj=obj, dynamic=True)

    def _formula_result(self, model, target, obj, changed, cleared,
                        was_derived, submitted, source=None):
        if source is None:
            source = target.formula.source
        result = {
            "model": model.name,
            "revision": self.revision(model),
            "obj": obj,
            "display": _display(target, None),
            "source": source,
            "doc": getattr(target, "doc", None),
            "parameters": _parameters(target),
            "changed": changed,
            "cleared": cleared,
            "dirty": self._edited(model.name),
            # An inherited Cells that is written to becomes an override: the
            # base keeps its own formula (measured) and this Space now has one
            # of its own. modelx says nothing about it, and a visitor who edits
            # a derived Cells has just changed the shape of the model.
            "overrode": bool(was_derived) and not _derived(target),
            "derived": _derived(target),
            "name": target.name,
            # modelx REWRITES the name in the `def` to the Cells name --
            # measured: `def something_else(t): return 3.0` set on `claims` is
            # stored as `def claims(t):\n    return 3.0\n`. Renaming a formula
            # therefore looks, in an editor that re-reads the stored source,
            # like the edit silently undoing itself. It is not an error and must
            # not be refused -- renaming a Cells is `rename`, not a formula edit
            # -- but it is the one thing here a person cannot work out from what
            # they see, so the result says it happened.
            "renamed_from": _renamed_from(submitted, target.name),
        }
        return _check_size(result, "formula.set", "shorten the formula")

    def ref_set(self, params):
        """Assign a new value to an existing Reference. Section 12.

        The sibling of formula_set, and deliberately the same shape -- `expect`,
        `changed`, `cleared`, `overrode`, a bump with reason `edit` -- because it
        is the same kind of act and a UI should not have to learn two idioms for
        it. Four things differ, all measured against modelx 0.33:

        1. THE BLAST RADIUS IS THE WHOLE MODEL. A formula edit clears what
           depended on that formula -- 260 of BasicTerm_S's 1,832 computed
           nodes. A reference edit clears ALL 1,832. Measured twice, and the
           second time is the one that settles it: in a two-branch toy model
           where `uses_k` reads `k` and `uses_j` reads `j`, changing `k` cleared
           `uses_j` as well. modelx invalidates globally on a reference change,
           so `cleared` here is not "what depended on this" -- it is the model's
           entire computation, and a UI that lets someone nudge a reference
           after a long projection must say so BEFORE the click, not after.

        2. ONLY VALUES THAT CAN CROSS THE WIRE. A Reference holds any Python
           object: BasicTerm_S's own are a DataFrame, a DataFrame, a Series, and
           the `numpy` and `pandas` modules. Those reach a frontend as a handle
           or an opaque tag (section 5), which means there is nothing to edit and
           nothing to send back -- and assigning over one is not merely useless,
           it breaks the model: setting `model_point_table = 3` made every
           formula that reads it raise. So both the CURRENT and the NEW value
           must be of a settable kind, and a refusal names the type it found.

        3. AN EXISTING REFERENCE ONLY. Assigning an unknown name creates one --
           modelx is happy to -- and that is a change to what the model IS, not
           to what it holds. Creating, renaming and deleting are a Phase 2
           surface with their own hazards; this method refuses a name it does not
           already find, and says so.

        4. `refmode` IS NOT VALIDATED BY MODELX. `set_ref(name, value,
           refmode="sideways")` stores "sideways" as the mode, silently --
           measured. So it is validated here, and preserved rather than reset:
           an edit that quietly changed a reference's mode would be a change
           nobody asked for. In practice it never matters for a value this
           method can set -- refmode governs how a reference to a modelx OBJECT
           rebinds under inheritance, and those are refused by rule 2 -- which is
           exactly why it must not be disturbed.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        if "value" not in params:
            raise bad_request("params.value is required")
        target = _resolve(model, obj)
        name, parent = self._require_reference(target, obj)

        current = target.value
        self._require_settable(current, obj, "is")
        value = self.codec.decode(params.get("value"))
        self._require_settable(value, obj, "would be set to")

        if "expect" in params:
            self._check_expected(current, params.get("expect"), target, obj)

        refmode = self._refmode_for(params, target, parent, obj)
        was_derived = _derived(target)

        if _same_value(current, value):
            # A no-op set costs the model's ENTIRE computation (fact 1). This
            # guard is worth far more here than the same guard in formula_set.
            return self._ref_result(model, obj, name, parent, changed=False,
                                    cleared=0, was_derived=was_derived,
                                    refmode=refmode)

        before = _traced(model)
        try:
            if refmode is not None and hasattr(parent, "set_ref"):
                parent.set_ref(name, value, refmode)
            else:
                # Model has no set_ref, and a Model-level reference has no mode.
                setattr(parent, name, value)
        except BridgeError:
            raise
        except Exception as exc:
            # AttributeError("Cells 'claims' is not a scalar.") lands here when a
            # name is taken by something else. The caller is being told what the
            # model is, not that the bridge broke.
            raise bad_request("%s: %s" % (type(exc).__name__, exc), obj=obj)

        cleared = max(0, before - _traced(model))
        self.bump(model, "edit")
        return self._ref_result(model, obj, name, parent, changed=True,
                                cleared=cleared, was_derived=was_derived,
                                refmode=refmode)

    def _require_reference(self, target, obj):
        """(name, parent) for an existing Reference, or a refusal that says why."""
        if not _is_reference(target):
            kind = "Model" if _is_model(target) else type(target).__name__
            raise bad_request(
                "%s is a %s, not a Reference; ref.set assigns to a name a Space "
                "or Model already holds" % (obj or "the model", kind),
                obj=obj, kind=kind)
        name = target.name
        parent = target.parent
        try:
            exists = name in parent.refs
        except Exception:
            exists = False
        if not exists:
            raise bad_request(
                "%s has no Reference named %r; this build changes the value of "
                "an existing Reference and does not create one"
                % (getattr(parent, "name", "the model"), name),
                obj=obj, name=name)
        return name, parent

    def _require_settable(self, value, obj, phrase):
        """Refuse a value that cannot honestly travel as JSON (fact 2)."""
        if not _is_settable(value):
            raise bad_request(
                "%s %s a %s, which this build cannot edit: a Reference is "
                "editable here only when its value is a number, string, "
                "boolean, date, null, or a list, tuple or dict of those"
                % (obj, phrase, _pyname(value)),
                obj=obj, value_type=_pyname(value))

    def _check_expected(self, current, expect, target, obj):
        """Refuse when the value moved under the editor (section 12.2).

        Compared as ENCODED json, not as Python objects: `expect` arrives as the
        codec-encoded value the frontend was last shown, and round-tripping the
        live value through the same encoder is the only comparison that means
        "what you were shown is still what is there".
        """
        encoded = self.codec.encode(current)
        if _canonical(encoded) == _canonical(expect):
            return
        raise bad_request(
            "%s has changed since it was read; nothing was written"
            % _display(target, None),
            conflict=True, obj=obj, current=encoded, expected=expect)

    def _refmode_for(self, params, target, parent, obj):
        """The mode to write, validated -- modelx will not do it (fact 4)."""
        supported = hasattr(parent, "set_ref")
        raw = params.get("refmode")
        if raw is not None:
            if not supported:
                raise bad_request(
                    "a Reference on a %s has no refmode" % type(parent).__name__,
                    obj=obj, refmode=raw)
            if raw not in REFMODES:
                raise bad_request(
                    "params.refmode must be one of %s" % ", ".join(REFMODES),
                    obj=obj, refmode=raw)
            return raw
        if not supported:
            return None
        # Preserve what is there, unless it is something modelx let through that
        # is not a mode at all -- in which case "auto" is the honest repair.
        held = getattr(target, "refmode", None)
        return held if held in REFMODES else "auto"

    def _ref_result(self, model, obj, name, parent, changed, cleared,
                    was_derived, refmode):
        proxy = parent._get_object(name, as_proxy=True)
        attrs = proxy._get_attrdict()
        result = {
            "model": model.name,
            "revision": self.revision(model),
            "obj": obj,
            "display": _display(proxy, None),
            "name": name,
            "parent": _objid(parent) if not _is_model(parent) else "",
            "value": self.codec.encode(proxy.value),
            "value_type": attrs.get("value_type"),
            "refmode": refmode,
            "changed": changed,
            # NOT "what depended on this": a reference change clears the whole
            # model (fact 1), and calling it anything narrower would understate
            # what the click cost.
            "cleared": cleared,
            "dirty": self._edited(model.name),
            "overrode": bool(was_derived) and not _derived(proxy),
            "derived": _derived(proxy),
        }
        return _check_size(result, "ref.set", "assign a smaller value")

    def value_get(self, params):
        model = self._model(params)
        nodes = params.get("nodes")
        if not isinstance(nodes, list):
            raise bad_request("params.nodes must be an array of {obj, args}")
        evaluate = params.get("evaluate", True)
        if not isinstance(evaluate, bool):
            raise bad_request("params.evaluate must be a boolean")

        values = []
        for spec in nodes:
            # A per-node failure is reported inside the entry: one bad node must
            # not fail the batch (protocol section 6.5).
            try:
                values.append(self._one_value(model, spec, evaluate))
            except BridgeError as err:
                values.append({"ok": False, "error": err.to_json()})
            except Exception as exc:
                values.append({"ok": False, "error": internal(exc).to_json()})
        result = {"revision": self.revision(model), "values": values}
        return _check_size(result, "value.get", "request fewer nodes")

    def trace_preds(self, params):
        """A node's precedents. Section 6.6, and 16 for `evaluate: false`.

        `evaluate` defaults to True, which is what the node bar's Precedents
        button has always meant: compute the node if it has no value, then
        answer. The Trace tab sends False, because a panel that follows the
        selection must never start a calculation the visitor did not ask for;
        an uncomputed node then answers `cached: false` and NO `preds` key -- an
        empty list would read as "this node is an input", which is a claim
        about the model, and a false one.

        `values` (0.10.0, default True) set to False sends the neighbours
        WITHOUT their values -- see _node_payload for what that saves and why
        the key is never encoded at all. `values: null` is refused like
        `evaluate: null` -- see _values.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        args = self._args(params.get("args", []))
        evaluate = params.get("evaluate", True)
        if not isinstance(evaluate, bool):
            raise bad_request("params.evaluate must be a boolean")
        values = _values(params)
        target = _resolve(model, obj)
        if not evaluate:
            return self._trace_neighbours(model, target, obj, args, "preds",
                                          values)
        node = self._node(model, target, args, evaluate=True)
        # A Cells with is_cached=False keeps no value and therefore no trace.
        if node is None or not node.has_value():
            raise not_found("%s keeps no cached value, so it has no trace"
                            % _display(target, args), obj=obj)
        result = {
            "revision": self.revision(model),
            "node": self._node_payload(node, values),
            "cached": True,
            "preds": [self._node_payload(p, values) for p in node.preds],
        }
        return _check_size(result, "trace.preds", "narrow the node")

    def trace_succs(self, params):
        """A node's dependents. Section 16. NEVER evaluates.

        There is no evaluating variant, because computing a node does not give
        it dependents: a dependent exists only once something that READS this
        node has been computed. So the honest answer for an uncomputed node is
        `cached: false`, and for a computed one it is exactly the nodes modelx
        has recorded as reading it -- which is a statement about what has been
        calculated so far, not about the formulas. The Model map answers the
        formula-level question (section 17). `values` as in trace_preds.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        args = self._args(params.get("args", []))
        values = _values(params)
        target = _resolve(model, obj)
        return self._trace_neighbours(model, target, obj, args, "succs",
                                      values)

    def _trace_neighbours(self, model, target, obj, args, side, values=True):
        method = "trace." + side
        node = _make_node(target, args)
        if not node.has_value():
            stub = {"obj": _objid(target),
                    "args": [self.codec.encode(a) for a in args],
                    "display": _display(target, args),
                    "value": None, "predslen": 0, "succslen": 0}
            if not values:
                # A null mints nothing; it goes so that `values: false` means
                # no `value` key anywhere in the reply.
                del stub["value"]
            result = {"revision": self.revision(model), "cached": False,
                      "node": stub}
            return _check_size(result, method, "narrow the node")
        result = {
            "revision": self.revision(model),
            "cached": True,
            "node": self._node_payload(node, values),
            side: [self._node_payload(n, values) for n in getattr(node, side)],
        }
        return _check_size(result, method, "narrow the node")

    def map_get(self, params):
        """One Space's Cells, and which Cells each formula NAMES. Section 17.

        READ FROM THE FORMULAS' SOURCE, NEVER FROM A CALCULATION. The mockup's
        map was built from modelx's tracegraph after `result_pv()` and
        `result_cf()`, and in the product that is not available: nothing in
        the sample is computed on a fresh load, and a picture of the model must
        not cost a projection. MEASURED on the shipped BasicTerm_S: parsing the
        40 formulas gives 40 Cells, 82 links and one cycle group {pols_death,
        pols_if, pols_lapse, pols_maturity} in ~2.4 ms, and the tracegraph
        after result_pv() + result_cf() holds 77 of those 82 and none that are
        not in them -- the other five are `check_pv_net_cf`'s four reads and
        `model_point -> sex`, which that calculation never reaches. So the
        source is a superset of any one calculation, and it is complete before
        anything is computed.

        WHAT IT CANNOT SEE, stated so a client can say it: a name built at run
        time (`getattr(space, name)`), and anything reached through another
        Space. A formula parameter or a local that shadows a Cells' name is
        excluded, because it is not a read of that Cells.
        """
        model = self._model(params)
        obj = _require(params, "obj")
        space = _resolve(model, obj)
        if _is_model(space):
            # The Model maps its Space when it has exactly one - BasicTerm_S's
            # Projection - so the map has something to draw before anything is
            # selected. With several, which one is the caller's choice.
            spaces = list(space.spaces.values())
            if len(spaces) != 1:
                raise bad_request(
                    "%s has %d Spaces; the map is drawn per Space, so name one "
                    "(%s)" % (model.name, len(spaces),
                              ", ".join(s.name for s in spaces) or "none"),
                    obj=obj, kind="Model")
            space = spaces[0]
            obj = _objid(space)
        cells_map = getattr(space, "cells", None)
        if _is_model(space) or _is_reference(space) or cells_map is None:
            raise bad_request("%s is not a Space; the map is drawn per Space"
                              % (_display(space, None) or "the model"),
                              obj=obj, kind=type(space).__name__)
        cells = list(cells_map.values())
        names = set(c.name for c in cells)
        refs = sorted(n for n in _ref_names(space) if not n.startswith("_"))
        ref_set = set(refs)
        edges, ref_edges, recursive, unread = [], [], [], []
        for cell in cells:
            source = _formula_source(cell)
            if source is None:
                unread.append(cell.name)
                continue
            used = _names_read(source, set(_parameters(cell)))
            if used is None:
                unread.append(cell.name)
                continue
            for name in sorted(used):
                if name == cell.name:
                    recursive.append(cell.name)
                elif name in names:
                    edges.append([name, cell.name])
                elif name in ref_set:
                    ref_edges.append([name, cell.name])
        result = {
            "model": model.name,
            "revision": self.revision(model),
            "obj": obj,
            "display": _display(space, None),
            "cells": [{"name": c.name, "obj": _objid(c),
                       "parameters": _parameters(c),
                       "derived": _derived(c)} for c in cells],
            "refs": [{"name": n, "obj": (obj + "." + n) if obj else n}
                     for n in refs],
            # [from, to]: `to`'s formula names `from`, so values flow from->to.
            "edges": edges,
            "ref_edges": ref_edges,
            # Cells whose formula names itself - a recursion through time such
            # as pols_if(t - 1). A self-loop, so it is not an edge.
            "recursive": sorted(set(recursive)),
            # Cells whose source could not be parsed; listed rather than
            # silently drawn with no links, which would read as an input.
            "unread": unread,
        }
        return _check_size(result, "map.get", "select a smaller Space")

    # -- tables (section 10) -----------------------------------------------

    def table_get(self, params):
        """Page a handle. The only way to read one beyond its 10x10 preview.

        `label` (0.10.0, a codec-encoded index label, exclusive with `row`)
        starts the page at the row carrying that label and adds `focus_row`.
        It exists so ONE element of a long Series can be read by its label
        without paging to it. MEASURED on lifelib's BasicTerm_ME (modelx-mcp
        spike S6, not shipped): `premiums(t=0)` is 10,000 rows over
        `policy_id` 1..10000, so label 7342 is row 7341 -- one request with
        `label`, where without it a client guesses a row from the label, which
        is off by one here and wrong outright on an index with gaps. On the
        shipped BasicTerm_S, `disc_rate_ann` label 10 is row 10 (0.01188).
        `tables.locate` is an index lookup, never a scan.

        `label_matches` beside `focus_row` is how many rows carry the label.
        A repeated label lands on its first row, and the others are on this
        page only when they are adjacent and fit: measured, label 7 at rows 0
        and 1001 of a 1,002-row Series pages rows 0..99 with ONE 7 in them,
        and `label_matches: 2` is what says the page is not all of them.
        """
        value = tables.handle_value(self.codec, params.get("h"))
        focus = matches = None
        if params.get("label") is not None:
            if params.get("row") is not None:
                raise bad_request(
                    "pass row or label, not both: label decides the first row",
                    row=params.get("row"))
            focus, matches = tables.locate(value,
                                           self.codec.decode(params["label"]))
            params = dict(params, row=focus)
        result = tables.page(self.codec, value, params, sink=self._out,
                             max_buffer_bytes=tables.MAX_BUFFER_BYTES)
        result["h"] = params.get("h")
        if focus is not None:
            result["focus_row"] = focus
            result["label_matches"] = matches
        # Only the JSON half is size-checked; the binary parts ride outside the
        # JSON and are bounded by max_buffer_bytes inside tables.page.
        return _check_size(result, "table.get", "ask for fewer rows or columns")

    def table_stats(self, params):
        """Reduce ONE WHOLE COLUMN of a handle. Section 10.5.

        The statistic is always over the whole column and NEVER over the page the
        client happens to be showing. MEASURED on BasicTerm_S `claims`: the whole
        column is sum=5814.680788 min=0.0 max=64.478472, the first 100 rows give
        sum=4562.909698 min=31.015247 max=61.562898 -- a Sum 22% low and both
        extremes wrong. A number whose correctness depends on a scope label
        surviving a narrow panel is not a number this bridge will help print.

        Doing it here is also the cheap way round: 1.2 ms to reduce 200,000
        float64 with a ~90 byte reply, against 1,600,000 bytes of buffers to ship
        that column to JS and do it wrong there.

        It resolves through `tables.handle_value`, so it inherits the `not_found`
        sentence for an evicted handle and the LRU touch for free, and it never
        reaches the model: the object is already behind the handle.

        The extremes' LABELS (0.10.0) come from the container's own index, so
        only a Series or a DataFrame has them; `tables.column` wraps an Index
        in a Series whose RangeIndex would hand back the position again,
        dressed as a label.
        """
        h = params.get("h")
        value = tables.handle_value(self.codec, h)
        kind, total_rows, _total_cols = tables.describe(value)
        col = tables._int(params, "col", 0)
        name, values = tables.column(value, kind, col)
        dtype = getattr(values, "dtype", None)
        block = tables.stats(values, dtype)
        tables.label_extremes(
            block, value.index if kind in ("Series", "DataFrame") else None,
            self.codec)
        result = {"h": h, "kind": kind, "col": col, "name": name,
                  "dtype": str(dtype) if dtype is not None else None,
                  "total_rows": total_rows,
                  "stats": block}
        return _check_size(result, "table.stats", "read fewer columns")

    # -- one Cells' cached values (section 13) ------------------------------

    def cells_page(self, params):
        """One page of a Cells' cached keys and values, and what the WHOLE column
        adds up to. Section 13. THIS METHOD MUST NOT EVALUATE.

        It is handle-free on purpose. `tables.page` on a hand-built frame returns
        no `h` and mints 0 handles -- measured; `h` is bolted on one line later by
        `table_get`. So there is no LRU on this path, no `not_found` to recover
        from and no stale snapshot: every page is a fresh read of the live cache,
        which is the property that lets a panel following every tree selection
        stay honest without holding any kernel state at all.

        PERMITTED modelx calls, each MEASURED at 0 tracegraph nodes against a
        populated BasicTerm_S (tracegraph 1832): ``len(cells)``,
        ``cells.parameters``, ``cells.is_cached``, ``cells.series``,
        ``series.index.get_indexer([key])``. FORBIDDEN, and neither looks like an
        evaluator: ``cells[k]`` -- measured, ``claims[130]`` adds 8 tracegraph
        nodes and grows ``len(cells)`` 121 -> 122 -- and ``cells.to_frame(...)``,
        where ``to_frame()`` and ``to_frame(*args)`` are one name with opposite
        semantics and the argument form evaluates. This method therefore
        standardises on the ``.series`` PROPERTY, which takes no arguments and so
        cannot be turned into an evaluator by a later edit.

        The enforcement is not this docstring. `tests/test_cells_page.py` asserts
        ``len(model._impl.tracegraph)`` is identical before and after every call,
        on a populated cache, a partial cache, an empty cache, a zero-parameter
        Cells, a multi-parameter Cells and an ``is_cached=False`` Cells. That
        assertion IS the evaluation policy and it must never be deleted.

        Two replies carry no page at all, and both stop before touching
        ``.series``:

        - A ZERO-PARAMETER CELLS. Measured on `pv_net_cf`: ``parameters ()``,
          ``len 1``, and a ``.series`` indexed ``[nan]`` -- a degenerate one-row
          Series that is never worth a round trip and would page as a NaN key.
        - MORE CACHED VALUES THAN ``limits.max_series_rows``. Checked against
          ``len(cells)``, which is 0.005 ms, so the refusal costs nothing.

        Unknown params are REJECTED rather than dropped, unlike `table.get`: a
        frontend typo must read as a client error, not as a kernel that quietly
        answered a different question.

        `element` (0.10.0, a codec-encoded label) pages ONE ELEMENT of each
        cached value instead of the values, for a Cells whose values are
        Series -- every lifelib _ME model, where a value is one number per
        model point. The column is then one model point across t, with the
        whole-column `stats` and where its extremes are: MEASURED on lifelib's
        CashValue_ME (spike S6, not shipped), `margin_mortality` element 1 is
        121 values with its maximum, 23.19979903678737, at t=87. It is
        `value.loc[label]` on values already cached, a pandas read and never a
        modelx call, so it inherits this method's evaluation policy unchanged.
        `element_missing` counts the cached values that do not carry the
        label; it is null on a reply that picked nothing (no series was read).
        """
        unknown = sorted(set(params) - set(_CELLS_PAGE_PARAMS))
        if unknown:
            raise bad_request(
                "cells.page does not take %s; it takes %s"
                % (", ".join(unknown), ", ".join(_CELLS_PAGE_PARAMS)),
                unknown=unknown)
        model = self._model(params)
        obj = _require(params, "obj")
        target = _resolve(model, obj)
        kind = "Model" if _is_model(target) else type(target).__name__
        if kind != "Cells":
            raise bad_request(
                "%s is a %s; cells.page reads one Cells' cached values. A "
                "Reference or a Space holds no series" % (obj or model.name, kind),
                obj=obj, kind=kind)

        names = list(target.parameters or ())
        n = int(len(target))
        result = {
            "revision": self.revision(model),
            "obj": obj,
            "display": _display(target, None),
            "params": names,
            "value_name": target.name,
            "value_dtype": None,
            "scalar_values": True,
            # `retains` is "this Cells KEEPS its values", NOT "this Cells HAS
            # values" -- measured True on a claims with zero cached. A client
            # that conflates them shows "does not keep its values" for a model
            # nobody has run yet, which is the wrong sentence and the wrong fix.
            "n_cached": n,
            "retains": bool(target.is_cached),
            "too_large": None,
            "focus_row": None,
            "keys": None,
            "stats": None,
            # Nested, not flattened: a null page is then unambiguous, and a client
            # can hand `result.page` straight to its table decoder.
            "page": None,
        }
        element = params.get("element")
        if element is not None:
            # Echoed on EVERY reply to a request that carried it, so a client
            # can always tell which question was answered.
            result["element"] = element
            result["element_missing"] = None

        if not names:
            result["note"] = ("this cells takes no arguments, so it has no "
                              "series; its value is on the node bar")
            return _check_size(result, "cells.page", "ask for fewer rows")

        if n > tables.MAX_SERIES_ROWS:
            result["too_large"] = {"cached": n, "limit": tables.MAX_SERIES_ROWS}
            return _check_size(result, "cells.page", "ask for fewer rows")

        series = self._series(model, obj, target, n)
        if element is not None:
            # A new Series; the memo keeps the values themselves.
            series, missing = tables.pick_element(series,
                                                  self.codec.decode(element))
            result["element_missing"] = missing
        # Everything below is sourced from the series the page is actually built
        # from, never from the separately-read `len(cells)` above. They agree --
        # the memo key now carries the count, so a cache that moved cannot be
        # served from it -- and sourcing them separately is what made a disagreement
        # printable: a stale 121-value series against a fresh `n` of 122 produced
        # `stats` over 121 under `n_cached: 122`, a page claiming `total_rows: 122`
        # while carrying 121, and `keys.gaps: -1`, a number that cannot mean anything.
        n_rows = int(len(series))
        result["value_dtype"] = str(series.dtype)
        result["stats"] = tables.stats(series, series.dtype)
        tables.label_extremes(result["stats"], series.index, self.codec)
        result["keys"] = _key_range(series, names)

        rows = tables._int(params, "rows", tables.DEFAULT_SERIES_ROWS)
        pos = self._locate(series, names, params.get("around"))
        result["focus_row"] = pos if pos >= 0 else None
        # FIXED BLOCKS, not a window centred on the focus. Centring opens a
        # 121-value column at row 51 with Previous already live, which reads as
        # if the page lost the beginning -- and, more usefully, fixed blocks let
        # a client tell LOCALLY that a new argument is inside the page it already
        # holds, so stepping t across a projection costs no requests at all.
        row = tables._int(params, "row", None)
        if row is None:
            row = (pos // rows) * rows if pos >= 0 and rows else 0
        elif rows and n_rows and row >= n_rows:
            # A client holding `row: 200` from before an edit CLEARED the cache --
            # which is exactly the Re-read path this method exists to make honest --
            # would otherwise be handed an empty window reported as `complete` at a
            # row that does not exist: "rows 201-200 of 50". Land on the last block
            # instead; `page.row` says where it went, so the pager re-syncs itself.
            row = ((n_rows - 1) // rows) * rows

        frame, scalar_values = tables.key_frame(series.iloc[row:row + rows])
        result["scalar_values"] = scalar_values
        page = tables.page(self.codec, frame,
                           {"row": 0, "rows": len(frame), "col": 0,
                            "cols": tables.DEFAULT_COLS,
                            "format": params.get("format", "json")},
                           sink=self._out)
        # The frame IS the window, so `tables.page` describes the window and
        # would report total_rows 200 for a 50,000-value column -- a pager reading
        # it would print "rows 1-200 of 200" and be wrong by two orders of
        # magnitude. Restate the geometry over the whole series instead; `rows`
        # and the columns are already right.
        page["row"] = row
        page["total_rows"] = n_rows
        page["shape"] = [n_rows, page["total_cols"]]
        page["complete"] = (row + page["rows"] >= n_rows
                            and page["cols"] >= page["total_cols"])
        result["page"] = page
        return _check_size(result, "cells.page", "ask for fewer rows")

    def _locate(self, series, names, around):
        """The position of ``around`` in the series index, or -1.

        ``get_indexer`` is a lookup, not a call: measured 0 tracegraph nodes, and
        -1 for a key that was never computed -- including ``[(9, 9)]`` on a
        MultiIndex. That -1 is what the reply's null ``focus_row`` means, and it
        is the only honest answer for "t = 130 has not been computed".
        """
        if around is None or len(series) == 0:
            return -1
        args = self._args(around)
        key = args[0] if len(names) == 1 and len(args) == 1 else tuple(args)
        try:
            return int(series.index.get_indexer([key])[0])
        except Exception:
            # A non-unique or non-comparable index. Not knowing where the focus
            # is costs a highlight; guessing would cost a wrong current value.
            return -1

    def _series(self, model, obj, cells, n):
        """``cells.series``, memoised on (model, obj, revision, n_cached). ONE entry.

        THIS IS WHAT MAKES STATELESSNESS AFFORDABLE. MEASURED: `cells.series` is
        0.06 ms at 121 cached values but 2.6 ms at 10,000 and 15 ms at 50,000,
        while the window slice out of it is 0.05 ms and the page build 0.2 ms. So
        without a memo every page turn pays the whole materialisation again.

        THE REVISION IS IN THE KEY, and so is ``n``, the free ``len(cells)``.
        The revision alone is NOT enough, and that was measured rather than
        argued: `on_execute`'s fingerprint is a conservative change detector, not
        proof of equality -- its own docstring says it can miss a change that
        leaves all three counts identical -- and any evaluation that happens
        without a bump leaves this memo holding the column as it was. Driven
        straight into it, a 121-value memo served a Cells with 122 cached values
        and produced `stats` over 121 labelled `n_cached: 122`, a page claiming
        `total_rows: 122` while carrying 121 rows, and `keys.gaps: -1`.

        ``n`` closes it for nothing: it is already in hand, it is the one number
        the page's geometry and the statistic are both about, and any change to
        the cache that the revision missed changes it. What the pair still cannot
        catch is a cache whose VALUES moved while its count and the whole model's
        fingerprint both stood still; that is the fingerprint's documented hole,
        not this memo's.

        Unlike a handle the memo has no lifetime a client can observe, no id on
        the wire and no `not_found` to recover from.

        One entry, not an LRU: the panel this serves reads one column at a time,
        so a second entry would buy a visitor alternating between two columns
        exactly one avoided materialisation and would pin a second large Series.
        """
        key = (model.name, obj, self.revision(model), n)
        memo = self._series_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        series = cells.series
        self._series_memo = (key, series)
        return series

    # -- storage and files (section 9) --------------------------------------

    def storage_info(self, params):
        """What the filesystem looks like right now.

        ``refresh`` forces a re-probe. The frontend calls this after retrying the
        service-worker registration: a worker that wins on the retry flips the
        mode here, with no kernel restart.
        """
        refresh = params.get("refresh", False)
        if not isinstance(refresh, bool):
            raise bad_request("params.refresh must be a boolean")
        return {"storage": files.storage_info(refresh=refresh)}

    def files_list(self, params):
        return files.list_dir(params.get("path", "/"))

    # -- model IO (section 9) ------------------------------------------------

    def model_open(self, params):
        """Open the model at ``path``. Section 9.4.

        OPENING A FILE OPENS THAT FILE. modelx keys open models by name, and
        0.2.0 turned that into the answer: a model already open under the target
        name came back untouched, the file was never read, and -- the part that
        lost work -- the requested path was adopted as that model's save
        location. On a fresh kernel the model holding the name is the pristine
        demo sample, so double-clicking a visitor's own file aimed the sample at
        it and the next plain Save wrote the sample over their work.

        So the default reads the file, and a name that is already taken is
        resolved WITHOUT destroying anything:

          same file already open   return it (reused). Its save location already
                                   IS this path, so nothing is adopted and
                                   re-reading would only discard live changes.
          a reconstructible sample the one model we may close on the user's
                                   behalf -- model.open_sample rebuilds it byte
                                   for byte. Closed, and reported as `replaced`.
          anything else            the file is opened under a DISTINCT name and
                                   both models stay open (`opened_as`).

        `on_conflict: "reuse"` is the explicit "give me what is already open",
        and it reports that model's own save location, never the caller's path.
        `on_conflict: "replace"` (and the 0.2.0 spelling `reload: true`) is
        Revert, and is refused unless the model is safe to close (9.4.1) or
        `force` is passed.
        """
        import modelx as mx

        info = files.storage_info()
        requested = _require(params, "path")
        virtual, real = files.resolve(requested, info)
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            raise bad_request("params.name must be a string or null")
        on_conflict = _conflict_mode(params)
        force = _flag(params, "force")

        fmt = files.model_format(real)
        if fmt is None:
            raise not_found(
                "%s is not a modelx model; a model is a folder or a .zip with "
                "%s at its root" % (virtual, files.MODEL_MARKER),
                path=virtual, real_path=real, exists=os.path.exists(real),
                storage=info)

        saved_name = files.model_name(real, fmt)
        wanted = name or saved_name or _stem(real)
        existing = mx.get_models().get(wanted)
        read_as, extra = name, {}

        if existing is not None:
            home = self._homes.get(wanted)
            if on_conflict == "reuse" or (on_conflict == "open"
                                          and home == virtual):
                return self._reused(existing, virtual, info, saved_name)
            if on_conflict == "replace":
                blocked = self._close_reason(wanted)
                if blocked and not force:
                    raise self._close_refusal(wanted, blocked, "re-read")
                extra["replaced"] = {"model": wanted,
                                     "sample": samples.sample_of(wanted),
                                     "path": home, "forced": bool(blocked)}
                extra["warning"] = ("%s was closed and re-read from %s"
                                    % (wanted, virtual))
                self._close_model(existing)
            elif self._discardable(wanted):
                # The ONE model the kernel may close unasked: a sample this
                # session built, never saved, never touched. model.open_sample
                # rebuilds it exactly, so freeing the name costs nothing -- and
                # it is what lets a visitor's own BasicTerm_S open under its own
                # name on a kernel the demo sample booted into.
                #
                # Closing BEFORE the read means a corrupt file leaves the sample
                # gone as well as the open failed. Accepted, and only because
                # "discardable" means "one request rebuilds it": the Welcome
                # gallery is the undo. Nothing else is ever closed this way.
                extra["replaced"] = {"model": wanted,
                                     "sample": samples.sample_of(wanted),
                                     "path": None, "forced": False}
                extra["warning"] = (
                    "the %s sample was open and unchanged, so it was closed to "
                    "free the name for %s" % (wanted, virtual))
                self._close_model(existing)
            else:
                # Not ours to close. Open the file beside it rather than
                # refusing: the caller asked for this file, and both models can
                # be addressed by name (section 6).
                read_as = _free_model_name(wanted)
                extra["opened_as"] = read_as
                extra["warning"] = (
                    "a model named %s is already open, so %s was opened as %s; "
                    "both are in the Explorer" % (wanted, virtual, read_as))

        before = set(_open_models())
        model = self._read_model(virtual, real, read_as)
        result = self._opened(model, virtual, real, fmt, info, reused=False)
        result["requested"] = virtual
        result["saved_name"] = saved_name
        result.update(extra)
        # mx.read_model does not refuse a name clash, it RENAMES the model that
        # was already open to <name>_BAK1 and warns on stderr. Predicting the
        # name above makes that rare, but two different files can carry the same
        # saved name, so when it happens the frontend is told rather than left
        # with a mystery model in its list.
        renamed = sorted(n for n in set(_open_models()) - before
                         if n != model.name and _BAK_RE.match(n))
        if renamed:
            result["renamed"] = renamed
            result["warning"] = (
                "a model named %r was already open; modelx renamed it to %s"
                % (model.name, ", ".join(renamed)))
        return result

    def model_close(self, params):
        """Close one model. Section 9.4.1.

        Destructive and unundoable, so it refuses unless the session can point at
        something that reconstructs the model -- the file it lives at, or the
        sample id it was built from -- and nothing has moved it since. `force`
        closes anyway and is reported back, so a UI can tell a confirmed discard
        from an ordinary close.
        """
        model = self._model(params)
        force = _flag(params, "force")
        name = model.name
        blocked = self._close_reason(name)
        if blocked and not force:
            raise self._close_refusal(name, blocked, "close")
        was_dirty = self._edited(name)
        self._close_model(model)
        return {"model": name, "closed": True, "was_dirty": was_dirty,
                "forced": bool(blocked), "models": sorted(_open_models())}

    def model_save(self, params):
        """Write the model back to storage. ``path`` makes it a save-as.

        The reply carries the storage block and a ``verified`` level, because a
        UI that shows "Saved" over a write into MEMFS is the worst bug this
        product could ship (UI-DESIGN 4.1). ``persistent: false`` in that block
        means: the bytes are written, and they die with this tab.

        It also carries a ``backup`` block (section 9.8). modelx's ``write_model``
        keeps a rotating backup by default, and in a browser that means an
        ordinary Save silently storing the model up to four times against one
        finite quota and growing ``_BAK1`` folders in the Files panel. The
        default is therefore per-runtime -- off in the browser, modelx's own
        behaviour on a local install -- ``params.backup`` overrides it, and the
        reply says which happened and what is on disk because of it.
        """
        model = self._model(params)
        info = files.require_writable()
        path = params.get("path")
        if path is None:
            path = self._home_of(model)
        virtual, real = files.resolve(path, info)
        fmt = self._format_for(params, real)
        verify = params.get("verify", "exists")
        if verify not in ("none", "exists", "read"):
            raise bad_request(
                "params.verify must be one of none, exists, read", verify=verify)

        written = self._write(model, virtual, real, fmt, info, verify,
                              self._backup_policy(params, info))
        # One of the two events that may create a save location (section 9.7):
        # these bytes and this model are now the same thing.
        self._set_home(model.name, virtual, "saved")
        self._dirty.discard(model.name)
        self._baseline_now(model)
        written["dirty"] = False
        return written

    def model_export_zip(self, params):
        """Zip the model into storage, and optionally hand the bytes back.

        ``download: true`` returns the archive as a binary buffer. That is not a
        convenience: in ``temporary`` mode the file lands in MEMFS, where the
        frontend cannot reach it through the contents API at all, so the buffer
        is the ONLY way an export leaves the tab. Which is exactly when a user
        most needs it -- UI-DESIGN 4.1's rule that export is always offered.
        """
        model = self._model(params)
        info = files.require_writable()
        path = params.get("path") or _default_export(model.name)
        virtual, real = files.resolve(path, info)
        if not real.lower().endswith(".zip"):
            virtual, real = virtual + ".zip", real + ".zip"
        download = params.get("download", False)
        if not isinstance(download, bool):
            raise bad_request("params.download must be a boolean")

        # Deliberately does NOT touch self._homes: mx.zip_model sets model.path
        # to the archive, so an export that also moved the save target would
        # make the next plain Save overwrite the export.
        result = self._write(model, virtual, real, "zip", info,
                             params.get("verify", "exists"),
                             self._backup_policy(params, info))
        if download:
            with open(real, "rb") as archive:
                data = archive.read()
            if len(data) > tables.MAX_BUFFER_BYTES:
                raise bad_request(
                    "the archive is %d bytes, over the %d byte buffer limit; it "
                    "is written at %s and can be fetched from there"
                    % (len(data), tables.MAX_BUFFER_BYTES, virtual),
                    bytes=len(data), path=virtual)
            self._out.append(data)
            result["download"] = {
                "$t": "bin", "buffer": len(self._out) - 1, "bytes": len(data),
                "media_type": "application/zip",
                "filename": os.path.basename(real)}
        return result

    def model_import_zip(self, params):
        """Open a zip that is already in storage, or one sent as a buffer.

        Both directions matter. With a drive, the frontend writes the user's file
        through the contents API and passes its ``path``. Without one there is no
        contents API to write through, so the bytes come in on the request's
        binary channel and the kernel stages them itself.
        """
        info = files.storage_info()
        path = params.get("path")
        wrote = None
        if self._in:
            name = params.get("filename") or (
                os.path.basename(path) if path else "imported.zip")
            name = _safe_name(name)
            if not name.lower().endswith(".zip"):
                name += ".zip"
            info = files.require_writable()
            virtual, real = files.resolve(path or ("/imports/" + name), info)
            if os.path.isdir(real):
                virtual, real = files.join_virtual(virtual, name), \
                    os.path.join(real, name)
            data = self.buffer_in(0)
            files.make_dirs(real)
            with open(real, "wb") as out:
                out.write(data)
            wrote = {"path": virtual, "real_path": real, "bytes": len(data)}
            if not files.is_model_zip(real):
                raise bad_request(
                    "the uploaded file is not a modelx model archive; a model "
                    "zip has %s at its root" % files.MODEL_MARKER,
                    path=virtual, bytes=len(data))
        elif path is None:
            raise bad_request(
                "model.import_zip needs params.path, or the archive itself as "
                "the request's first binary buffer")

        opened = self.model_open({"path": wrote["path"] if wrote else path,
                                  "name": params.get("name"),
                                  "on_conflict": params.get("on_conflict"),
                                  "force": params.get("force", False),
                                  "reload": params.get("reload", False)})
        if wrote is not None:
            opened["wrote"] = wrote
        return opened

    # -- IO internals --------------------------------------------------------

    def _opened(self, model, virtual, real, fmt, info, reused):
        return {"model": model.name, "revision": self.revision(model),
                "path": virtual, "real_path": real, "model_format": fmt,
                "reused": reused, "storage": info,
                "dirty": self._edited(model.name),
                "sample": samples.sample_of(model.name),
                "models": sorted(_open_models())}

    def _reused(self, model, requested, info, saved_name):
        """The reply for an open that read nothing (section 9.4).

        `path` is the model's OWN save location, and null when it has none. It is
        never the path that was asked for: reporting the request back is how the
        0.2.0 bug looked from the frontend -- the panel showed the model moving
        to the user's file, because the kernel had told it so.
        """
        self._remember(model.name, model=model)
        home = self._homes.get(model.name)
        real = files.to_real(home, info) if home else None
        result = self._opened(
            model, home, real,
            files.model_format(real) if real else None, info, reused=True)
        result["requested"] = requested
        result["saved_name"] = saved_name
        return result

    def _set_home(self, model_name, virtual, how):
        """Record where a model lives. THE ONLY WRITER of self._homes.

        `how` is "read" or "saved", and those are the only two events that may
        create a save location (section 9.7): reading THOSE bytes into THIS
        model, or writing THIS model to those bytes. Opening something else must
        never assign one -- that is the data-loss path, a pristine sample aimed
        at a user's file and then written over it by the next plain Save -- and
        it is prevented by there being no third caller rather than by a check.
        A branch that wanted to adopt a path would have to come here and lie
        about `how`, which is the point: it cannot happen by omission.
        """
        if how not in ("read", "saved"):
            raise internal(ValueError(
                "a save location may only be created by reading or writing the "
                "file (section 9.7), not by %r" % (how,)))
        self._homes[model_name] = virtual
        return virtual

    def _read_model(self, virtual, real, name):
        """mx.read_model, plus everything the session must record about it."""
        import modelx as mx

        try:
            model = mx.read_model(real, name=name)
        except BridgeError:
            raise
        except Exception as exc:
            raise internal(exc)
        self._set_home(model.name, virtual, "read")
        # It came out of a file, so it is neither unsaved work nor a sample any
        # more, whatever held that name before.
        self._dirty.discard(model.name)
        samples.forget_sample(model.name)
        self._remember(model.name, model=model)
        self._baseline_now(model)
        return model

    def _baseline_now(self, model):
        """Record this model as it is: read, written or built, but not edited."""
        self._baseline[model.name] = _touch_key(model)

    def _edited(self, name):
        """Has anything happened to this model that only the user could have done?

        TWO detectors, because one of them is not enough and the gap between them
        is where work gets lost:

          the event    a model.changed with reason `execute` (section 7) -- a
                       post_execute in which this model's fingerprint moved. It
                       sees structural edits and anything that computes.
          the key      _touch_key compared against what was recorded when the
                       model was last read, written or built. It sees the edit
                       the fingerprint CANNOT: assigning a Reference on a model
                       with nothing computed yet leaves all three of the
                       fingerprint's counts identical, and `Projection.point_id
                       = 3` in a Console is the most likely edit a visitor makes.

        Either one says "edited". A model with no baseline is treated as edited:
        not knowing is not the same as knowing it is clean, and this answer
        decides whether something may be destroyed.
        """
        if name in self._dirty:
            return True
        model = _open_models().get(name)
        if model is None:
            return False
        recorded = self._baseline.get(name)
        return recorded is None or recorded != _touch_key(model)

    def _discardable(self, name):
        """May the kernel close this model unasked, losing nothing? (9.4)

        True only for a model that (a) this session built from a sample id, so
        one request rebuilds it byte for byte, (b) has never been read from or
        written to a file, so there is no save location to lose, and (c) has not
        been edited. Anything else is stepped around, not closed.
        """
        return (samples.sample_of(name) is not None
                and not self._homes.get(name)
                and not self._edited(name))

    def _close_reason(self, name):
        """Why closing ``name`` would lose something, or None when it cannot.

        Closing has no undo, so the answer is "unsafe" unless the session can
        name something that reconstructs the model. Being unedited is still only
        a MAY-have-changed signal (section 9.4.1) -- _touch_key does not compare
        formula bodies, for one -- so `force` exists and a UI still confirms.
        """
        home = self._homes.get(name)
        if self._edited(name):
            if home:
                return ("%s has changed since it was last saved to %s" % (name, home))
            return "%s has changed and has no save location" % name
        if home or samples.sample_of(name):
            return None
        return ("%s has no save location and was not opened from a sample, so "
                "nothing can bring it back" % name)

    def _close_refusal(self, name, reason, what):
        edited = self._edited(name)
        return bad_request(
            "%s; save it first, or pass force: true to %s it anyway"
            % (reason, "discard those changes and " + what if edited else what),
            model=name, dirty=edited, path=self._homes.get(name),
            sample=samples.sample_of(name), reason=reason)

    def _close_model(self, model):
        """Close it, and forget everything this session recorded about it."""
        name = model.name
        model.close()
        self._forget_model(name)
        # Emit `closed` now rather than leaving it to the next post_execute: the
        # frontend that asked for this is waiting on the reply, and bump() drops
        # the model from _known so on_execute will not report it twice.
        self.bump(name, "closed")

    def _home_of(self, model):
        home = self._homes.get(model.name)
        if home:
            return home
        # NO fallback to modelx's model.path. It looks harmless and is not: a
        # sample read out of the site's shipped content carries that content's
        # directory in model.path, so a plain Save would have silently written
        # the visitor's edits over the shipped model -- at a location the UI was
        # showing as "no save location yet", because session.info reports _homes.
        # One save location, one source for it (sections 9.5, 9.7).
        raise bad_request(
            "%s has no save location yet; pass params.path to save it somewhere"
            % model.name, model=model.name, dirty=self._edited(model.name),
            sample=samples.sample_of(model.name))

    def _format_for(self, params, real):
        fmt = params.get("format")
        if fmt is None:
            return "zip" if real.lower().endswith(".zip") else "folder"
        if fmt not in ("folder", "zip"):
            raise bad_request("params.format must be 'folder' or 'zip'", format=fmt)
        return fmt

    def _backup_policy(self, params, info):
        """What this write does about modelx's rotating backup (section 9.8).

        ``params.backup`` is ``true``, ``false``, or ``"auto"``/absent for the
        default, which is per-runtime and comes from files.backup_default: OFF in
        a browser, ON on a local install. Returns the block that will be reported
        back, minus the fields only the write itself can fill in.
        """
        raw = params.get("backup")
        default, why = files.backup_default(info["mode"])
        if raw is None or raw == "auto":
            policy, enabled, reason = "auto", default, why
        elif isinstance(raw, bool):
            policy, enabled = ("on", True) if raw else ("off", False)
            reason = ("the caller asked for a backup" if raw
                      else "the caller asked for no backup")
        else:
            raise bad_request(
                "params.backup must be true, false or 'auto'", backup=raw)
        return {"policy": policy, "enabled": enabled, "default": default,
                "reason": reason, "max": files.backup_limit()}

    def _write(self, model, virtual, real, fmt, info, verify, backup=None):
        import modelx as mx

        backup = backup or self._backup_policy({}, info)
        # What is at the target BEFORE the write decides what the reply may
        # claim: modelx only rotates a backup when it had something to rotate,
        # so "no backup was made" means two different things depending on this.
        try:
            existed = os.path.exists(real)
        except Exception:
            existed = False

        files.make_dirs(real)
        try:
            if fmt == "zip":
                mx.zip_model(model, real, backup=backup["enabled"])
            else:
                mx.write_model(model, real, backup=backup["enabled"])
        except BridgeError:
            raise
        except Exception as exc:
            raise internal(exc)

        result = {"model": model.name, "revision": self.revision(model),
                  "path": virtual, "real_path": real, "format": fmt,
                  "bytes": files.tree_bytes(real), "storage": info,
                  "dirty": self._edited(model.name),
                  "verified": "none", "persistent": bool(info["persistent"]),
                  "backup": self._backup_report(virtual, real, backup, existed)}
        if verify == "none":
            return result
        if files.model_format(real) is None:
            raise internal(OSError(
                "wrote %s but %s is not there afterwards; nothing was saved"
                % (virtual, files.MODEL_MARKER)))
        result["verified"] = "exists"
        if verify == "read":
            # The load-bearing check: read the bytes back as a model. Costs a
            # full parse, so it is opt-in -- but it is the only level that
            # proves the file is a model and not just a directory of bytes.
            name = _verify_name(model.name)
            copy = mx.read_model(real, name=name)
            try:
                result["verified"] = "read"
                result["verified_spaces"] = len(copy.spaces)
            finally:
                copy.close()
        return result

    def _backup_report(self, virtual, real, backup, existed):
        """What the write actually did to storage, in a shape a UI can say aloud.

        THE BUG THIS EXISTS FOR: a plain Save over ``/models/throwaway`` left
        ``/models/throwaway_BAK1`` beside it, the same size and the same
        timestamp, and nothing anywhere said so. In a browser that is the model
        stored twice against one finite quota, and a folder in the Files panel
        the visitor is certain they never made. The fix is two halves -- the
        default is now off in the browser (files.backup_default), and whatever
        happens is REPORTED, including the backups earlier saves already left
        behind, so the UI can offer to reclaim them instead of pretending they
        are not there.
        """
        slots = files.backup_slots(real)
        paths = ["%s_BAK%d" % (virtual, slot) for slot in slots]
        kept = None
        if backup["enabled"] and existed and 1 in slots:
            kept = "%s_BAK1" % virtual
        size = None
        if slots:
            size = 0
            for slot in slots:
                part = files.tree_bytes(real + "_BAK%d" % slot)
                if part is None:
                    size = None
                    break
                size += part

        report = dict(backup)
        report.update({"overwrote": bool(existed), "kept": kept,
                       "paths": paths, "bytes": size})
        report["note"] = self._backup_note(virtual, report)
        return report

    @staticmethod
    def _backup_note(virtual, report):
        """One sentence a UI may show verbatim. Never claims more than happened."""
        if report["kept"]:
            note = ("the previous version of %s was kept at %s"
                    % (virtual, report["kept"]))
        elif report["enabled"] and not report["overwrote"]:
            note = "nothing was there before, so there was nothing to back up"
        elif report["overwrote"]:
            note = ("the previous version of %s was replaced; no backup was kept"
                    % virtual)
        else:
            note = "a new model was written; no backup was needed"
        count = len(report["paths"])
        if count:
            size = ("" if report["bytes"] is None
                    else ", %d bytes" % report["bytes"])
            note += ("; %d backup%s of %s %s in storage%s"
                     % (count, "" if count == 1 else "s", virtual,
                        "is" if count == 1 else "are", size))
        return note

    def _forget_model(self, name):
        # Drop the memoised series too, or a closed model's column stays pinned
        # in this session for as long as nothing else is read.
        if self._series_memo is not None and self._series_memo[0][0] == name:
            self._series_memo = None
        self._homes.pop(name, None)
        self._states.pop(name, None)
        self._baseline.pop(name, None)
        self._dirty.discard(name)
        self._known.discard(name)
        # A stale entry here would tell model.close that a user's model is
        # rebuildable from a sample id when it is not.
        samples.forget_sample(name)

    # -- internals ---------------------------------------------------------

    def _model(self, params):
        """Resolve params.model, or the single open model.

        `params.model` is explicitly nullable: the frontend holds the name it
        got from a previous result and passes it back, and `null` there must
        mean "I have no name yet", not "look up a model called None". Sending
        the key with a null is the natural shape for that and used to be a
        bad_request, which made the panel's first call fail.
        """
        models = _open_models()
        name = params.get("model")
        if name is not None:
            if not isinstance(name, str):
                raise bad_request("params.model must be a string or null")
            if name not in models:
                raise not_found(
                    "no open model named %r; open models: %s"
                    % (name, ", ".join(sorted(models)) or "(none)"),
                    model=name, models=sorted(models))
            return models[name]
        if not models:
            raise no_model()
        if len(models) > 1:
            raise bad_request(
                "several models are open (%s); params.model is required"
                % ", ".join(sorted(models)), models=sorted(models))
        return list(models.values())[0]

    def _args(self, raw):
        if not isinstance(raw, list):
            raise bad_request("args must be an array, positional in parameters order")
        return tuple(self.codec.decode(a) for a in raw)

    def _tree_node(self, obj, depth):
        kind = type(obj).__name__
        if _is_model(obj):
            node = {"kind": "Model", "obj": "", "name": obj.name,
                    "display": obj.name, "refs": [], "spaces": []}
            if depth != 0:
                node["refs"] = [self._ref_node(obj, n) for n in _ref_names(obj)]
                node["spaces"] = [self._tree_node(s, depth - 1)
                                  for s in obj.spaces.values()]
            return node

        node = {
            "kind": kind,
            "obj": _objid(obj),
            "name": obj.name,
            "display": _space_display(obj),
            "parameters": _parameters(obj),
            "bases": [_objid(b) for b in getattr(obj, "bases", ())],
            "derived": _derived(obj),
            "itemspaces": self._itemspaces(obj),
            "spaces": [], "cells": [], "refs": [],
        }
        if depth != 0:
            node["spaces"] = [self._tree_node(s, depth - 1)
                              for s in obj.named_spaces.values()]
            node["cells"] = [self._cells_node(c) for c in obj.cells.values()]
            node["refs"] = [self._ref_node(obj, n) for n in _ref_names(obj)]
        return node

    def _itemspaces(self, space):
        """A Space's dynamic ItemSpaces: a bounded list, and the true total.

        THE BOUND IS NOT THE FEATURE - paging and searching them is Phase 2 -
        IT IS WHAT KEEPS NAMING THEM FROM BEING A REGRESSION. Measured on a
        synthetic model of 10,000 ItemSpaces: bare internal names are 148,894
        bytes on the wire and the named form is 636,674, a 4.3x rise on a
        payload `tree.get` re-reads on every `model.changed` - which fires on
        every recalculation. Sending fifty and saying how many there are costs
        about 3 KB and is less than the tree sent before, while the frontend
        gains the names it could not previously show at all.

        `total` is the honest count whatever the bound, so the panel can say
        "50 of 10,000" rather than implying it has them all.
        """
        view = getattr(space, "_named_itemspaces", None)
        if not view:
            return {"total": 0, "shown": []}
        names = list(view)
        return {
            "total": len(names),
            "shown": [self._itemspace_node(view[n], n)
                      for n in names[:MAX_TREE_ITEMSPACES]],
        }

    def _itemspace_node(self, item, name):
        """One dynamic ItemSpace, named the way a reader needs it.

        `_get_repr()` IS THE NAME. modelx formats it already - `Projection[1]`
        - and the tree used to send `name`, which is modelx's internal
        `__Space1`. A visitor who computed `Projection[1]` was shown
        `__Space1`, and it was not selectable either, so there was no way to
        find out what it meant. Measured: naming all 10,000 ItemSpaces of a
        synthetic model costs 6 ms and reading their `argvalues` 2 ms, so the
        honest name is free.

        `obj` round-trips: `_objid` gives `Projection.__Space1`, `_resolve`
        hands back the same object, and `value.get` inside it answers with
        `BasicTerm_S.Projection[1].claims(t=3)` - all measured before this was
        written, because a selectable row whose obj the kernel cannot resolve
        would be worse than the dead text it replaces.
        """
        return {
            "kind": "ItemSpace",
            "obj": _objid(item),
            "name": name,
            "display": item._get_repr(),
            "args": [self.codec.encode(a) for a in item.argvalues],
        }

    def _cells_node(self, cells):
        return {"kind": "Cells", "obj": _objid(cells), "name": cells.name,
                "display": cells._get_repr(),
                "parameters": _parameters(cells),
                "has_formula": not _is_null_formula(cells.formula),
                "derived": _derived(cells),
                "cached": len(cells)}

    def _ref_node(self, parent, name):
        proxy = parent._get_object(name, as_proxy=True)
        attrs = proxy._get_attrdict()
        return {"kind": "Reference", "obj": attrs.get("namedid", name), "name": name,
                "display": name, "value_type": attrs.get("value_type"),
                "derived": _derived(proxy)}

    def _one_value(self, model, spec, evaluate):
        """One entry of a value.get reply.

        `args` is the arguments this entry was ACTUALLY read at, codec-encoded --
        the same tuple `display` is already built from, so nothing new is
        computed. It is additive and a client may ignore it.

        IT IS HERE BECAUSE AN ENTRY THAT CANNOT SAY WHICH NODE IT IS FOR CAN LIE
        BY SITTING STILL. A frontend that publishes values on a bus keeps the
        previous entry while a new read is in flight -- the shipped node bar does
        exactly that, deliberately, so the panel does not blink -- so stepping an
        argument from t=0 to t=1 leaves t=0's number, and its handle id, on the
        bus labelled t=1 for at least one frame. Plausibly, and silently. The
        client cannot reconstruct `display` (it does not know the kernel's model
        prefix) and a local guess races the bus's own emit order, so this is the
        one honest way for any consumer to ask "is this entry for the node I am
        drawing".
        """
        if not isinstance(spec, dict):
            raise bad_request("each node must be an object {obj, args}")
        obj = _require(spec, "obj")
        args = self._args(spec.get("args", []))
        target = _resolve(model, obj)
        encoded = [self.codec.encode(a) for a in args]

        if _is_reference(target):
            # A Reference holds a value and takes no arguments, so its entry is
            # for the node addressed by `obj` alone -- `args` is [] whatever the
            # caller sent, which is the answer the comparison needs.
            return {"ok": True, "display": _display(target, None), "cached": True,
                    "args": [], "value": self.codec.encode(target.value)}

        node = self._node(model, target, args, evaluate)
        if node is None or not node.has_value():
            return {"ok": True, "display": _display(target, args),
                    "args": encoded, "cached": False, "value": None}
        # `predslen`/`succslen` ride along with a value that was going to be
        # sent anyway. They were already computed by `_node_payload` for
        # `trace.preds`, which meant the Inspector could only show "this node's
        # precedent/dependent counts" -- a Phase 1 promise -- after the visitor
        # clicked Precedents, and the dependent count was on the wire with no
        # line of frontend code reading it. Both are graph lookups on a node
        # that is already resolved and already has a value, so nothing is
        # evaluated and no extra request is made: the node bar's existing read
        # carries them.
        return {"ok": True, "display": _display(target, args), "args": encoded,
                "cached": True, "value": self.codec.encode(node.value),
                "predslen": len(node.preds), "succslen": len(node.succs)}

    def _node(self, model, target, args, evaluate):
        """Return a modelx node with a value, or None if uncached and evaluate=False."""
        node = _make_node(target, args)
        if node.has_value():
            return node
        if not evaluate:
            return None
        self._evaluate(target, args)
        self._evaluated(model)
        return _make_node(target, args)

    def _evaluate(self, target, args):
        """Call a formula, turning modelx's FormulaError into the wire error.

        error_obj/error_args address the node that actually failed, so the UI can
        jump to it -- which means error_args has to be codec-encoded like every
        other node address (protocol section 4). It used to be [repr(a)], which
        the frontend cannot turn back into an argument.

        `error_display` (0.10.0) is the same node NAMED, for a reader rather
        than for a resolver. `error_obj` is opaque (section 4) and inside an
        ItemSpace it is modelx's internal name -- measured,
        `Projection.__Space1.model_point` for a failure in `Projection[99999]`
        -- which names nothing a person or a model can cite, where the display
        is `BasicTerm_S.Projection[99999].model_point()`. Null when modelx kept
        no traceback, because an empty name would read as a node.
        """
        import modelx as mx
        from modelx.core.errors import FormulaError
        try:
            target(*args)
        except FormulaError as exc:
            traceback = mx.get_traceback()
            error_obj, error_args, error_display = "", [], None
            if traceback:
                node = traceback[-1][0]
                error_obj = _objid(node.obj)
                error_args = list(node.args or ())
                error_display = _display(node.obj, error_args)
            error = mx.get_error()
            raise formula_error(
                "%s: %s" % (type(error).__name__, error) if error
                else str(exc).split("\n")[0],
                formula_traceback=str(exc),
                error_obj=error_obj,
                error_args=[self.codec.encode(a) for a in error_args],
                error_display=error_display)
        except TypeError as exc:
            raise bad_request("%s: %s" % (_display(target, None), exc),
                              obj=_objid(target))

    def _node_payload(self, node, values=True):
        """One trace entry. With ``values`` False (0.10.0) there is no `value`
        key, and -- the half that matters -- the value is NEVER ENCODED.

        Encoding a vector value mints a handle (section 5), so a trace of a
        node with many vector-valued neighbours fills the 64-entry LRU with
        handles nobody asked for and evicts the ones a grid is paging.
        Encoding and then deleting the key would mint them all the same; that
        was the first cut, caught by counting the codec's handles rather than
        the reply's bytes. MEASURED on BasicTerm_S after `pv_net_cf()`:
        `pv_claims()`'s 123 precedents are 25,578 bytes and one handle (for
        `disc_factors()`) with values, 15,454 bytes and none without. On
        lifelib's CashValue_ME (modelx-mcp spike S6, not shipped):
        `pv_premiums()`'s 1,143 precedents are 543,881 bytes, 1,143 handles and
        300 ms with values against 149,977 bytes, none and 13 ms without; the
        4,575 dependents of `model_point()` are REFUSED with values, at
        2,073,349 bytes and after minting 4,576 handles, and are 634,002 bytes
        and no handle without. The tracegraph is read either way: `predslen`
        and `succslen` stay.
        """
        target = node.obj
        args = node.args
        has = node.has_value()
        payload = {
            "obj": _objid(target),
            "args": [self.codec.encode(a) for a in (args or ())],
            "display": _display(target, args),
        }
        if values:
            payload["value"] = self.codec.encode(node.value) if has else None
        payload["predslen"] = len(node.preds) if has else 0
        payload["succslen"] = len(node.succs) if has else 0
        return payload


_METHODS = {
    "session.info": Bridge.session_info,
    "model.open_sample": Bridge.model_open_sample,
    "tree.get": Bridge.tree_get,
    "formula.get": Bridge.formula_get,
    "formula.set": Bridge.formula_set,
    "ref.set": Bridge.ref_set,
    "value.get": Bridge.value_get,
    "trace.preds": Bridge.trace_preds,
    # Additive to protocol 0 (FEATURES above).
    "trace.succs": Bridge.trace_succs,
    "map.get": Bridge.map_get,
    "table.get": Bridge.table_get,
    "table.stats": Bridge.table_stats,
    "doc.get": Bridge.doc_get,
    "cells.page": Bridge.cells_page,
    "storage.info": Bridge.storage_info,
    "files.list": Bridge.files_list,
    "model.open": Bridge.model_open,
    "model.close": Bridge.model_close,
    "model.save": Bridge.model_save,
    "model.export_zip": Bridge.model_export_zip,
    "model.import_zip": Bridge.model_import_zip,
}


# --- buffers ------------------------------------------------------------

def split_buffers(envelope):
    """(data, buffers) for one outgoing envelope.

    Binary parts travel as the comm message's own `buffers`, never inside
    `data` -- so they must be lifted out before the envelope is serialised.
    Returns a copy; the caller's envelope is not mutated.
    """
    if not isinstance(envelope, dict) or "buffers" not in envelope:
        return envelope, None
    data = dict(envelope)
    buffers = data.pop("buffers") or None
    return data, buffers


def _as_bytes(buffer):
    """Whatever the kernel handed us as one incoming buffer, as bytes.

    ipykernel gives memoryview; the Pyodide kernel can give bytes, a memoryview
    or a JS-backed buffer proxy. Every one of them supports the buffer protocol
    or `.to_py()`, and guessing wrong here fails a model import with a type
    error thrown from deep inside zipfile.
    """
    if isinstance(buffer, (bytes, bytearray)):
        return bytes(buffer)
    to_py = getattr(buffer, "to_py", None)
    if to_py is not None:
        try:
            return _as_bytes(to_py())
        except Exception:
            pass
    try:
        return bytes(memoryview(buffer))
    except Exception as exc:
        raise bad_request("cannot read the request's binary buffer (%s: %s)"
                          % (type(buffer).__name__, exc))


# --- param helpers ------------------------------------------------------

_BAK_RE = re.compile(r".+_BAK\d*$")

#: Characters a name coming off the wire may not carry into a real path.
_UNSAFE = set('/\\:*?"<>|\x00')


def _stem(real):
    """The basename of a path without a .zip suffix -- the fallback model name."""
    base = os.path.basename(os.path.normpath(real))
    return base[:-4] if base.lower().endswith(".zip") else base


def _flag(params, key, default=False):
    value = params.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise bad_request("params.%s must be a boolean" % key)
    return value


def _values(params):
    """`values` on trace.* (0.10.0, section 18.3): absent is True, and any
    non-boolean is refused, null INCLUDED.

    Not `_flag`, which reads null as the default. The first cut used it, and
    measured: `trace.succs {values: null}` answered WITH values while
    `evaluate: null` in the same call is refused. For `force` the default is
    the cautious reading of a null; here it is every value and, for a vector,
    the handle section 18.3 exists to avoid, so a null that meant "no values"
    would get the expensive answer silently. A client drops the key instead.
    """
    value = params.get("values", True)
    if not isinstance(value, bool):
        raise bad_request("params.values must be a boolean")
    return value


def _conflict_mode(params):
    """params.on_conflict, with `reload: true` as the 0.2.0 spelling (9.4).

    `reload` does NOT imply `force`: the old call closed whatever was open
    unconditionally, and a reload that would discard changes is now refused
    rather than performed. No shipped caller sends it.
    """
    mode = params.get("on_conflict")
    if mode is None:
        return "replace" if _flag(params, "reload") else "open"
    if mode not in CONFLICT_MODES:
        raise bad_request(
            "params.on_conflict must be one of %s" % ", ".join(CONFLICT_MODES),
            on_conflict=mode)
    return mode


def _free_model_name(base):
    """A model name nothing holds: `BasicTerm_S_2`, `_3`, ...

    modelx validates a model name as an identifier, and `base` can come from a
    file's stem when the model's own name could not be read -- so a base that is
    not an identifier falls back rather than raising from inside the serializer.
    """
    models = _open_models()
    stem = base if (base or "").isidentifier() else "model"
    for suffix in range(2, 1000):
        candidate = "%s_%d" % (stem, suffix)
        if candidate not in models:
            return candidate
    raise bad_request(
        "cannot open a second model named %s: 998 of them are already open"
        % stem, model=stem)


def _safe_name(name):
    cleaned = "".join("_" if c in _UNSAFE else c for c in str(name)).strip()
    cleaned = cleaned.lstrip(".") or "imported"
    return cleaned[:120]


def _default_export(model_name):
    return "/exports/" + _safe_name(model_name) + ".zip"


def _verify_name(model_name):
    """A name no open model holds, for the read-back check.

    MUST NOT start with an underscore: modelx validates a model name as an
    identifier that does not, and ``rename`` raises ValueError deep inside the
    deserializer, which surfaces as a failed save of a model that saved fine.
    """
    models = _open_models()
    stem = model_name if re.match(r"^[A-Za-z]", model_name or "") else "model"
    for suffix in range(1, 1000):
        candidate = "%s_verify%d" % (stem, suffix)
        if candidate not in models:
            return candidate
    return "%s_verify%d" % (stem, id(models) % 1000000)


def _require(params, key):
    value = params.get(key)
    if not isinstance(value, str):
        raise bad_request("params.%s must be a string" % key)
    return value


def _as_obj(obj):
    if not isinstance(obj, str):
        raise bad_request("params.obj must be a string")
    return obj


def _check_size(result, method, remedy):
    # allow_nan=False on purpose: the codec tags every non-finite float, so a
    # bare NaN here is a codec bug, and failing the request beats putting
    # JSON that no browser will parse on the wire.
    size = len(json.dumps(result, allow_nan=False))
    if size > MAX_MESSAGE_BYTES:
        raise bad_request(
            "%s result is %d bytes, over the %d byte message limit; %s"
            % (method, size, MAX_MESSAGE_BYTES, remedy), bytes=size)
    return result


# --- the modelx private-API seam ----------------------------------------

def _open_models():
    import modelx as mx
    return mx.get_models()


def _fingerprint(model):
    """A cheap, total change detector for one model.

    Three counts, all O(1) or O(#Spaces):

      len(tracegraph)   computed nodes. Every evaluation adds nodes; clearing
                        values or editing a formula removes them.
      len(refgraph)     reference dependency edges.
      _structure_size   1 + len(cells) + len(refs) per Space, recursively.

    The third is not redundant: a structural edit CLEARS the tracegraph, so on a
    model with nothing computed yet `new_cells` moves (0, 0, n) to (0, 0, n+1)
    and the first two counts alone would have reported no change.

    Measured on BasicTerm_S: ~5 microseconds. It must stay that cheap -- it runs
    on every post_execute, which in the Pyodide kernel means every comm message.
    """
    try:
        impl = model._impl
        traced, refs = len(impl.tracegraph), len(impl.refgraph)
    except Exception:
        traced, refs = -1, -1
    return (traced, refs, _structure_size(model))


def _touch_key(model):
    """A key that moves when a model is EDITED and stands still when it is read.

    (structure, one id() per Reference value), over the same ItemSpace-free walk
    as _structure_size. Both halves are invariant under evaluation -- ItemSpaces
    are a computation result and are not walked, and no evaluation assigns a
    Reference -- and both move on an edit.

    IT EXISTS FOR THE EDIT THE EVENT FINGERPRINT CANNOT SEE. `_fingerprint` is
    (computed nodes, reference edges, structure size); on a model with nothing
    computed yet, `Projection.point_id = 3` in a Console leaves all three
    identical, so no model.changed is emitted and nothing marks the model dirty.
    That assignment is the most likely edit a demo visitor ever makes, and
    mistaking it for "untouched" is how a model gets closed with work in it.

    id(), not the value: identity changes on assignment -- even to an equal
    object, which is the safe direction -- and not otherwise, it costs nothing,
    and it never has to compare a DataFrame for equality. Measured at 11 us for
    BasicTerm_S, and unlike _fingerprint it runs only when something destructive
    is being decided, not on every post_execute.
    """
    try:
        key = [_ref_ids(model)]
        stack, seen = list(model.spaces.values()), 0
        while stack:
            space = stack.pop()
            seen += 1
            if seen > MAX_FINGERPRINT_SPACES:
                key.append(("capped", seen))
                break
            key.append((space.name, len(space.cells), _ref_ids(space)))
            stack.extend(space.named_spaces.values())
        return tuple(key)
    except Exception:
        # Unreadable means unknown, and unknown must not read as "unchanged":
        # None is what _edited treats as edited.
        return None


def _ref_ids(parent):
    return tuple(sorted((name, id(parent.refs[name])) for name in parent.refs))


def _structure_size(model):
    """Count Spaces, Cells and References. ItemSpaces are deliberately excluded:
    they are a *computation* result, and the tracegraph already counts them."""
    total, seen = 0, 0
    try:
        stack = list(model.spaces.values())
        while stack:
            space = stack.pop()
            seen += 1
            if seen > MAX_FINGERPRINT_SPACES:
                return -seen        # still a fingerprint: it moves with `seen`
            total += 1 + len(space.cells) + len(space.refs)
            stack.extend(space.named_spaces.values())
    except Exception:
        return -1
    return total


#: Types ref.set will read from the wire and write into a model. Everything else
#: -- DataFrame, Series, ndarray, module, modelx object, function, anything
#: custom -- reaches a frontend as a handle or an opaque tag (section 5), so
#: there is nothing to edit and nothing to send back.
_SETTABLE = (bool, int, float, str, list, tuple, dict)


def _is_settable(value, depth=0):
    """May this value be written into a Reference from the wire? (section 12.1)

    Recursive, because a list of DataFrames is not a settable list. Depth-capped
    for the same reason the codec is: a self-referential container must fail the
    check rather than the interpreter.
    """
    if value is None:
        return True
    if depth > 6:
        return False
    if isinstance(value, (datetime.date, datetime.datetime)):
        return True
    np = sys.modules.get("numpy")
    if np is not None and isinstance(value, np.generic):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_settable(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(_is_settable(k, depth + 1) and _is_settable(v, depth + 1)
                   for k, v in value.items())
    # After the containers, so an ndarray (not in _SETTABLE) is already out.
    return isinstance(value, _SETTABLE)


def _same_value(current, value):
    """Is assigning `value` over `current` provably a no-op?

    Type-first, because `1 == True` and `1 == 1.0` in Python and neither is the
    same reference value. Anything whose __eq__ misbehaves answers False, which
    is the safe direction: the set goes ahead.
    """
    if type(current) is not type(value):
        return False
    try:
        return bool(current == value)
    except Exception:
        return False


def _canonical(encoded):
    """One comparable string for a codec-encoded value."""
    try:
        return json.dumps(encoded, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return repr(encoded)


def _traced(model):
    """How many nodes the model has computed. The first third of _fingerprint.

    formula.set reads it either side of the write to report how many cached
    values the edit invalidated -- the number that tells a visitor an edit to
    one formula threw away a quarter of the model's computation.
    """
    try:
        return len(model._impl.tracegraph)
    except Exception:
        return 0


def _computed(model):
    """`session.info.models[].computed` (0.10.0): len(tracegraph), or None.

    THE ONLY CACHED-VALUE TOTAL THAT COUNTS INSIDE ITEMSPACES. `tree.get`'s
    per-Cells `cached` covers the named Spaces only (section 15 does not recurse
    into ItemSpaces), so summing it misses everything an ItemSpace computed.
    MEASURED on BasicTerm_S: 0 fresh; 1,832 after `pv_net_cf()`, equal to the
    tree's sum; then 5,465 after `Projection[2].pv_net_cf()`, while the tree
    still sums to 1,832. That is 5,464 cached Cells values (3,632 of them in
    `Projection[2]`) plus one node for the ItemSpace the Space formula created.
    A Cells with `is_cached = False` adds nothing. 0.45 microseconds.

    None, not 0, when the graph cannot be read: 0 is the claim "nothing is
    computed", and an unreadable graph has not established it. (`_traced` keeps
    its 0, because its callers subtract one reading from another.)
    """
    try:
        return len(model._impl.tracegraph)
    except Exception:
        return None


def _is_dynamic(obj):
    """Is this Cells one modelx generated inside an ItemSpace?

    Its Interface class is still `Cells`; only the impl differs, and modelx
    answers an assignment to `.formula` with ValueError("'x' is dynamic"). The
    check is here so the refusal can say WHY instead of relaying that.
    """
    try:
        return type(obj._impl).__name__.startswith("Dynamic")
    except Exception:
        return False


def _renamed_from(source, name):
    """The name the caller typed in the `def`, when modelx replaced it.

    None for a lambda, for a source that does not start with a def, and -- the
    usual case -- when the typed name already matched.
    """
    match = re.match(r"\s*def\s+([A-Za-z_]\w*)", source or "")
    typed = match.group(1) if match else None
    return typed if typed and typed != name else None


#: modelx's word for "that parsed, but it is not a single function".
_NOT_A_FORMULA = "invalid function or lambda definition"


def _shape_refusal(exc, obj):
    """modelx's own refusal of a formula, said in the UI's words.

    `invalid function or lambda definition` is accurate and tells a person
    nothing about what to do. It is what modelx answers for an `import` above
    the def, for two defs, for a bare expression and for an empty box -- fact 3
    in formula_set -- so the message names the rule that was broken instead of
    relaying the phrase.
    """
    if _NOT_A_FORMULA in str(exc):
        return bad_request(
            "a formula must be exactly one `def` or one `lambda`, and nothing "
            "else -- no imports, no second function, no statements around it",
            obj=obj, shape=True, modelx_message=str(exc))
    return bad_request("%s: %s" % (type(exc).__name__, exc), obj=obj)


def _syntax_refusal(exc, obj):
    """A SyntaxError from modelx's compile, addressed to the editor.

    lineno/offset are relative to the source that was sent, which is exactly
    what an editor needs to put the caret on the bad line; `internal` would have
    thrown the position away and shown a traceback instead.
    """
    where = ""
    if getattr(exc, "lineno", None):
        where = " (line %d" % exc.lineno
        if getattr(exc, "offset", None):
            where += ", column %d" % exc.offset
        where += ")"
    return bad_request(
        "the formula does not parse: %s%s" % (exc.msg, where), obj=obj,
        syntax=True, lineno=getattr(exc, "lineno", None),
        offset=getattr(exc, "offset", None), text=getattr(exc, "text", None))


def _is_model(obj):
    return type(obj).__name__ == "Model"


def _is_reference(obj):
    return type(obj).__name__ == "ReferenceProxy"


def _is_null_formula(formula):
    return formula is None or type(formula).__name__ == "NullFormula"


def _formula_source(cells):
    formula = getattr(cells, "formula", None)
    if _is_null_formula(formula):
        return None
    try:
        return formula.source
    except Exception:
        return None


def _names_read(source, parameters):
    """The bare names a formula READS, or None if its source does not parse.

    A name the formula binds itself - a parameter, an assignment target, a
    comprehension variable, a nested lambda's argument - is not a read of a
    Cells that happens to share it, so every bound name is removed. That is
    conservative in one direction only: a local that shadows a Cells' name
    for part of the body hides a real read later in it. None of the shipped
    model's 40 formulas does that.
    """
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return None
    read, bound = set(), set(parameters)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (read if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return read - bound


def _derived(obj):
    # Cells expose _is_derived(), ReferenceProxy is_derived(); UserSpace exposes
    # neither in modelx 0.33, so a space reports False until PLAN 3.2's
    # introspection API lands.
    for name in ("_is_derived", "is_derived"):
        check = getattr(obj, name, None)
        if check is None:
            continue
        try:
            return bool(check() if callable(check) else check)
        except Exception:
            return False
    return False


def _ref_names(parent):
    return [n for n in parent.refs if not n.startswith("_")]


def _objid(obj):
    idstr = getattr(obj, "_idstr", None)
    if idstr is not None:
        return idstr
    return obj._get_attrdict().get("namedid", "")        # ReferenceProxy


def _resolve(model, obj):
    """Resolve a wire `obj` (protocol section 4) to a modelx object.

    A reference is resolved through its parent with as_proxy=True, because
    Model._get_from_name on a reference returns the reference's *value*.
    """
    if obj == "":
        return model                # _get_from_name("") raises KeyError
    parent_name, _, last = obj.rpartition(".")
    parent = model
    if parent_name:
        parent = _lookup(model, parent_name, obj)
    try:
        is_ref = last in parent.refs
    except Exception:
        is_ref = False
    if is_ref:
        return parent._get_object(last, as_proxy=True)
    return _lookup(model, obj, obj)


def _lookup(model, name, reported):
    try:
        return model._get_from_name(name)
    except Exception:
        raise not_found("no object named %r in model %r" % (reported, model.name),
                        obj=reported)


def _key_range(series, names):
    """`keys` for a cells.page reply, or None when the question is undefined.

    THE ONE GENUINELY NEW AUDIT FACT in a cached-values page: it answers "did
    this run to term, and are there holes". MEASURED on BasicTerm_S, and the
    reason it is worth a block of its own: `claims` is 121 values over t = 0..120
    while `pols_maturity` is 120 over t = 1..120 -- a difference nothing else in
    the product surfaces.

    `gaps` is (max - min + 1) - len(index), so it is 0 for a contiguous run.
    COUNTED OFF THE INDEX IT JUST MEASURED, not off a count read from anywhere
    else: an index carries every key exactly once, so a gap count sourced this
    way cannot be negative. Sourced from a separately-read `len(cells)` it could,
    and did -- `gaps: -1` came back from a 121-key index counted against 122. A
    number that cannot mean anything is worse than no block at all.

    Bounds are DECIMAL STRINGS for the same reason integer statistics are: an
    integer key past 2^53 is silently wrong once a browser has parsed it as a
    JSON number.

    Only a SINGLE INTEGER parameter gets a block. A float or string key has no
    "gap" to count, and a multi-parameter Cells has a defensible cross-product
    answer that nobody has specified -- returning None says so, where returning
    a half-filled block would invite a client to print a number for it.
    """
    if len(names) != 1:
        return None
    index = series.index
    if (len(index) == 0 or index.nlevels != 1
            or getattr(index.dtype, "kind", "O") not in "iu"):
        return None
    low, high = int(index.min()), int(index.max())
    return {"integer": True, "min": str(low), "max": str(high),
            "gaps": (high - low + 1) - len(index)}


def _parameters(obj):
    try:
        return list(obj.parameters or ())
    except Exception:
        return []


def _space_display(obj):
    params = _parameters(obj)
    return obj.name + ("[" + ", ".join(params) + "]" if params else "")


def _display(obj, args):
    """Build the UI string from modelx's repr_parent + repr; never from `obj`."""
    try:
        # repr_parent() is "" for a Model, so joining with a bare "." produced a
        # leading dot (".BasicTerm_S") in messages the UI is told it can show.
        base = ".".join(part for part in (obj._impl.repr_parent(),
                                          obj._get_repr(add_params=False))
                        if part)
    except Exception:
        try:
            base = obj._get_attrdict().get("fullname") or _objid(obj)
        except Exception:
            base = _objid(obj)
    if args is None:
        return base
    names = _parameters(obj)
    parts = []
    for i, arg in enumerate(args):
        parts.append("%s=%r" % (names[i], arg) if i < len(names) else repr(arg))
    return base + "(" + ", ".join(parts) + ")"


def _make_node(target, args):
    node = getattr(target, "node", None)
    if node is None:
        # _objid of a Model is "", which used to put " does not take arguments"
        # -- a message opening with a space -- on the wire.
        raise bad_request("%s does not take arguments" % _display(target, None),
                          obj=_objid(target))
    try:
        return node(*args)
    except TypeError as exc:
        raise bad_request("%s: %s" % (_display(target, None), exc),
                          obj=_objid(target))
