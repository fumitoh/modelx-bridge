"""run_python: the broad tool, off by default (--allow-python).

It is the Console, not a side channel: code runs in this server's interpreter
against the same live models, and afterwards the bridge's own post_execute hook
(`Bridge.on_execute`) decides what moved, exactly as it does after a Console
cell. That detector is conservative (protocol section 7), so the report never
says "nothing changed": it says what was detected, and cross-checks `dirty`.

Why it is off by default: it changes References and structure directly
(measured over stdio: `point_id = 2` after `pv_net_cf()` took BasicTerm_S from
1,832 computed nodes to 0, reported as `execute`), which is everything
formula.set and ref.set gate, and PLAN 3.6 makes propose_change the only way to
mutate a model. It is S6's configuration B; it is never in the demo.

The code gets no stdin and its output is captured at the file-descriptor
level (`_captured`). Over stdio, fds 0 and 1 are the MCP channel unless
__main__ has moved the transport off them, and the first cut swapped only
sys.stdout and sys.stderr. MEASURED over stdio with that cut: `exit()` and
`quit()` close sys.stdin, which was the protocol's input, so the server died
(exit code 1) without answering; `input()` swallowed the client's next request;
`os.system("printf 12345")` glued its bytes to the front of the response line,
the client could not parse it, and the call never completed.
"""
import ast
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import warnings

from . import render as R

MAX_OUT = 4000


class PythonTool(object):

    def __init__(self, session):
        self.session = session
        self.ns = None
        self.bound = {}

    def _namespace(self, models):
        """`models`: mx.get_models() now. Refreshed before every run, so a
        model opened by earlier code appears, and one it closed or renamed
        away goes: the first cut only added, and after `BasicTerm_S.close()`
        later code still computed the closed model (pv_net_cf() =
        910.92066093366), which no other tool could read. A name the code has
        since bound to something else is left alone."""
        if self.ns is None:
            import modelx as mx
            self.ns = {"__name__": "__run_python__", "mx": mx}
        for name, model in self.bound.items():
            if models.get(name) is not model and self.ns.get(name) is model:
                del self.ns[name]
        self.ns.update(models)
        self.bound = dict(models)
        return self.ns

    def _models(self):
        return dict((m["name"], m) for m in self.session.dispatch("session.info", {})["models"])

    def run(self, code):
        import modelx as mx
        bridge = self.session.bridge
        tools = self.session.tools
        tools.begin()
        # The bridge records a model's fingerprint when an evaluating dispatch
        # bumps it, and bridge 0.10.0 does that after the FIRST node a
        # value.get computes (one bump per dispatch) and, without B1 (deferred,
        # protocol 18.8), not at all when the evaluation fails. calculate sends
        # up to 32 nodes per dispatch, so the next post_execute reports what it
        # computed as `execute`. MEASURED on BasicTerm_S after pv_net_cf():
        # calculate of pols_if(t=200), premiums(t=130), claims(t=140), then
        # on_execute() emits rev 4 `execute` and session.info says dirty; the
        # same after claims(t=9999), which adds 4 nodes. Flushing here keeps
        # that out of this code's report. In the Console the flush happens
        # anyway, after every comm message.
        bridge.on_execute()
        earlier = bridge.drain_events()
        before = self._models()
        # The model objects, not their names: mx.read_model(path) of an open
        # model renames that model to <name>_BAK1 and gives <name> to the new
        # one, and comparing names reported "opened BasicTerm_S_BAK1" and
        # "BasicTerm_S ... computed nodes 1832 -> 0" (measured), both false.
        # Held for the run, so no id is reused by a model the code creates.
        held = dict(mx.get_models())
        ns = self._namespace(held)
        value, error = None, None
        t0 = time.perf_counter()
        with _captured() as cap:
            try:
                tree = ast.parse(code, "<run_python>", "exec")
                last = None
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    last = ast.Expression(tree.body.pop().value)
                exec(compile(tree, "<run_python>", "exec"), ns)
                if last is not None:
                    value = eval(compile(last, "<run_python>", "eval"), ns)
            except BaseException as exc:   # noqa: a SystemExit in the code must not stop the server
                te = traceback.TracebackException.from_exception(exc)
                # Only the code's own frames: the first cut kept this module's frame.
                frames = [f for f in te.stack if f.filename == "<run_python>"]
                te.stack = traceback.StackSummary.from_list(frames[-12:])
                error = "".join(te.format())
        dt = time.perf_counter() - t0
        bridge.on_execute()
        events = bridge.drain_events()
        after = self._models()
        now = dict(mx.get_models())
        was = dict((id(m), n) for n, m in held.items())

        def origin(name):
            """The name the model called `name` had before the code ran; None
            for a model the code opened."""
            if name in now:
                return was.get(id(now[name]))
            return name if name in held else None

        lines = ["ran in %.2f s; output is NOT a citation: re-read any number you will "
                 "state with get_value or calculate" % dt]
        if earlier:
            lines.append("before this code ran: %s -- earlier calculate calls computed more than "
                         "the bridge had recorded, so it reports that now; it is not this code's "
                         "doing, though the bridge counts it as unsaved changes"
                         % "; ".join("%s rev %s (%s)" % (e["params"]["model"], e["params"]["revision"],
                                                         e["params"]["reason"]) for e in earlier))
        report = []
        moved = []
        for ev in events:
            p = ev["params"]
            o = origin(p["model"])
            b, a = before.get(o, {}) if o else {}, after.get(p["model"], {})
            cb, ca = b.get("computed"), a.get("computed")
            label = p["model"]
            if o and o != p["model"]:
                label += " (was %s)" % o
            elif o is None and p["model"] in now:
                label += " (opened by this code)"
            moved.append("%s rev %s (%s)%s%s" % (
                label, p["revision"], p["reason"],
                ("; computed nodes %s -> %s" % (_n(cb), _n(ca))) if a and b and cb != ca else "",
                "; now marked as having unsaved changes" if a.get("dirty") and not b.get("dirty") else ""))
        if moved:
            report.append("model changed: " + "; ".join(moved) +
                          ". Values and refs read before this call may be stale.")
        else:
            dirty = [n for n, a in after.items()
                     if a.get("dirty") and not before.get(origin(n), {}).get("dirty")]
            report.append("no model change detected -- which is not proof of none: the detector "
                          "compares computed nodes, reference links and structure, and misses "
                          "e.g. a Reference assigned on a model with nothing computed (protocol "
                          "section 7)" + (". But %s now report%s unsaved changes, so something "
                                          "did change" % (", ".join(dirty), "s" if len(dirty) == 1 else "")
                                          if dirty else ""))
        live = set(id(m) for m in now.values())
        renamed = sorted((was[id(m)], n) for n, m in now.items() if id(m) in was and was[id(m)] != n)
        opened = sorted(n for n, m in now.items() if id(m) not in was)
        closed = sorted(n for n, m in held.items() if id(m) not in live)
        what = ["renamed %s to %s (the same model; computed nodes %s)"
                % (o, n, _n(after.get(n, {}).get("computed"))) for o, n in renamed]
        if closed:
            what.append("closed %s, which no other tool can read now" % ", ".join(closed))
        if opened:
            what.append("opened " + ", ".join(opened))
        if what:
            newname = dict(renamed)
            notes = ["%s now names a different model: a ref printed before this call that starts "
                     "%s. reads the new one%s. " % (n, n, ("; the model it read is now %s" % newname[n])
                                                   if n in newname else "")
                     for n in opened if n in held]
            unbound = [n for n in closed if n not in now]
            if unbound:
                notes.append("run_python no longer defines %s. " % ", ".join(unbound))
            report.append("open models changed: %s. %sThe model list in the server instructions "
                          "is now out of date." % ("; ".join(what), "".join(notes)))

        # --max-chars bounds this tool too. The first cut capped the output at
        # MAX_OUT whatever the bound and appended an exception whole: measured,
        # `raise ValueError('x' * 100000)` came back as 100,420 characters
        # under --max-chars 12000, and print('z' * 50000) as 4,373 under 2000.
        # The output and the value are cut to the room the change report
        # leaves. R.clip, the backstop, cuts at 200 under the bound; 100 more
        # is for the stdout cut line.
        room = tools.max_chars - 300 - sum(len(x) + 1 for x in lines + report)
        if error:
            second = "exception:\n" + error.rstrip("\n")
        elif value is not None:
            second = "\n".join(self._value(value))
        else:
            second = ""
        text = cap["text"]
        if text:
            cap_out = max(0, min(MAX_OUT, room - min(len(second), room // 2)))
            if len(text) > cap_out:
                text = text[:cap_out] + "\n... %d more characters of output not shown" % (len(text) - cap_out)
            text = text.rstrip("\n")
            lines.extend(["stdout:", text])
            room -= len("stdout:") + len(text) + 2
        if second:
            lines.append(_cut(second, room))
        result = R.clip(lines + report, tools.max_chars, "Print less, or a slice of it")
        return tools.end("run_python", {"code": code}, result)

    def _value(self, value):
        """The final expression's value, encoded by the bridge's own codec and
        rendered like any other: a Series prints its head and whole-column
        statistics from table.stats on a handle in the same store."""
        from .render import is_handle, scalar
        from .tools import Node
        tools = self.session.tools
        enc = json.loads(json.dumps(self.session.bridge.codec.encode(value)))
        if is_handle(enc):
            text, extra = tools._vector(Node("", "Cells", "", ""), "the value", enc)
            return ["value: " + text] + _unpaged(extra)
        return ["value: " + scalar(enc)]


#: tools._vector's three hint forms: "N more rows: get_value(...)",
#: "columns shown: 10 of 20; get_value(...) shows the rest" and
#: "... no labels). One by label: get_value(...)".
_HINT = re.compile(r"(: |; |\. One by label: )get_value\(.*$")


def _unpaged(extra):
    """_vector's extra lines, each paging hint kept without its get_value
    call: the call names a ref, and this value has none. The first cut
    dropped every line holding one. MEASURED: run_python on
    np.arange(200.0).reshape(10, 20) printed 5 rows, 10 columns and 12
    columns' statistics, and nothing said that 5 rows, 10 columns and 8
    columns' statistics were cut. A line in a form _HINT does not know is
    kept whole rather than dropped."""
    out, cut = [], False
    for line in extra:
        m = _HINT.search(line)
        if m is None:
            out.append(line)
        elif m.group(1) == ": ":
            out.append(line[:m.start()] + " not shown")
            cut = True
        else:
            out.append(line[:m.start()])
            cut = cut or m.group(1) == "; "
    if cut:
        out.append("    get_value cannot page this value (it has no ref): select what was not shown "
                   "in the code")
    return out


@contextlib.contextmanager
def _captured():
    """Run the body with no stdin and with everything it writes captured in
    the order written: sys.stdout, sys.stderr, warnings, and fds 1 and 2,
    where os.system, a subprocess or a C extension writes. -> a dict whose
    "text" is filled on exit.

    sys.stdin is an empty StringIO, so exit() and quit() close that (they call
    sys.stdin.close() before raising SystemExit) and input() raises EOFError.
    Warnings go to the output too: mcp's lowlevel Server wraps every request
    in warnings.catch_warnings(record=True) and logs what it records to the
    server's stderr, so `warnings.warn("x")` and numpy's "divide by zero
    encountered in log" never reached the result (measured over stdio)."""
    got = {"text": ""}
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    sink = tempfile.TemporaryFile()
    saved = (os.dup(1), os.dup(2))
    # Unbuffered and one open file description shared with fds 1 and 2, so
    # print("a", end=""); os.system("printf b") reads "ab", as on a terminal.
    writer = io.TextIOWrapper(io.FileIO(sink.fileno(), "w", closefd=False), encoding="utf-8",
                              errors="backslashreplace", write_through=True)
    streams = sys.stdin, sys.stdout, sys.stderr

    def show(message, category, filename, lineno, file=None, line=None):
        writer.write(warnings.formatwarning(message, category, filename, lineno, line))
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        sys.stdin, sys.stdout, sys.stderr = io.StringIO(), writer, writer
        # catch_warnings() also resets every warning registry, so a warning
        # repeated in a later call is shown again rather than once per server.
        with warnings.catch_warnings():
            warnings.showwarning = show
            # Python shows a DeprecationWarning raised by code in __main__; this is that code.
            warnings.filterwarnings("default", category=DeprecationWarning, module="__run_python__")
            yield got
    finally:
        try:
            writer.flush()
        except Exception:
            pass
        sys.stdin, sys.stdout, sys.stderr = streams
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        os.close(saved[0])
        os.close(saved[1])
        sink.seek(0)
        got["text"] = sink.read().decode("utf-8", "replace")
        sink.close()


def _cut(text, limit):
    """`text` within `limit` characters: its head and its tail, with how many
    characters between them were cut. An exception keeps its first frames and
    the start of its message, and its last line."""
    if len(text) <= limit:
        return text
    keep = max(limit - 60, 0)
    head = keep * 2 // 3
    tail = keep - head
    return "%s\n... %d characters not shown ...\n%s" % (text[:head], len(text) - head - tail,
                                                        text[len(text) - tail:] if tail else "")


def _n(v):
    return "unknown" if v is None else str(v)
