"""The in-process session: one Bridge, the models opened at launch, and the
adapter that gives tools.py a plain `dispatch` callable.

The only module that imports modelx_bridge (test_render holds that);
python_tool.py imports modelx itself.
modelx's model registry is process-wide, so a second Session in one process
sees the first one's models; the tests that need a fresh model run in a
process of their own.
"""
import contextlib
import sys

from .tools import Tools, WireError


class Session(object):

    def __init__(self, storage_root=None, samples=(), paths=(), max_chars=12000,
                 align="model_point", journal=None):
        from modelx_bridge import Bridge, set_storage_root
        if storage_root:
            set_storage_root(storage_root)
        self.bridge = Bridge()
        self.opened = []
        self.failed = []
        # Under __main__ fd 1 is already off the channel (isolate_stdio); this
        # keeps what a model prints while it loads out of a caller's stdout
        # when the Session runs in-process, as the suites run it.
        with contextlib.redirect_stdout(sys.stderr):
            for s in samples:
                self._open("model.open_sample", {"sample": s}, s)
            for p in paths:
                self._open("model.open", {"path": p}, p)
        self.bridge.drain_events()
        self.journal = journal
        self.tools = Tools(self.dispatch, max_chars=max_chars, align=align, journal=journal)

    def _open(self, method, params, what):
        from modelx_bridge.errors import BridgeError
        try:
            r = self.bridge.dispatch(method, params)
            self.opened.append(r["model"])
        except BridgeError as e:
            self.failed.append((what, e.message))
        except Exception as e:      # an import error inside a model, say
            self.failed.append((what, "%s: %s" % (type(e).__name__, e)))

    def dispatch(self, method, params):
        """Bridge.dispatch, with protocol section 3's guarantee: a BridgeError
        becomes a WireError with the wire's fields, and anything else becomes
        WireError('internal'). Nothing else escapes."""
        from modelx_bridge.errors import BridgeError
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return self.bridge.dispatch(method, params)
        except BridgeError as e:
            j = e.to_json()
            raise WireError(j["code"], j["message"], j.get("data"))
        except Exception as e:
            raise WireError("internal", "%s: %s" % (type(e).__name__, e))
        finally:
            # Nothing in an MCP server reads model.changed, and the queue would
            # otherwise grow by one per evaluating dispatch for the life of the
            # server.
            self.bridge.drain_events()

    def describe(self):
        """(models, items, names) for the server instructions, read once at launch.

        models: 'NAME (the shipped sample ID)' or 'NAME (PATH)', comma-separated,
        then each launch failure with its reason. items: one
        (space ref, parameters) per Space that takes parameters, so the
        instructions describe ItemSpaces only where an open model has them.
        names: the open models, whoever opened them."""
        info = self.dispatch("session.info", {})
        out, items = [], []
        names = [m["name"] for m in info["models"]]
        for m in info["models"]:
            src = (("the shipped sample %s" % m["sample"]) if m.get("sample")
                   else (m.get("path") or "no file"))
            out.append("%s (%s)" % (m["name"], src))
            try:
                root = self.dispatch("tree.get", {"model": m["name"]})["root"]
            except WireError:
                continue
            stack = [(m["name"], sp) for sp in root.get("spaces") or []]
            while stack:
                prefix, sp = stack.pop(0)
                ref = "%s.%s" % (prefix, sp["name"])
                if sp.get("parameters"):
                    refs = [r["name"] for r in sp.get("refs") or []]
                    items.append((ref, list(sp["parameters"]), sp["parameters"][0] in refs))
                stack.extend((ref, sub) for sub in sp.get("spaces") or [])
        text = ", ".join(out) or "none"
        if self.failed:
            text += "; failed to open: " + "; ".join("%s (%s)" % f for f in self.failed)
        return text, items, names
