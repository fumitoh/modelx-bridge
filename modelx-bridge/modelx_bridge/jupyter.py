"""Jupyter comm adapter (docs/bridge-protocol-v0.md sections 1-2, 7).

Import-safe on plain CPython: `comm` and IPython are imported inside register(),
so the rest of the package can be exercised in-process without a kernel.

TWO THINGS HERE ARE LOAD-BEARING AND BOTH WERE BUGS:

1. In the JupyterLite Pyodide kernel, EVERY comm message fires IPython's
   post_execute -- a comm dispatch is not user code, but the kernel does not
   make that distinction. The event fan-out is therefore guarded twice: by the
   re-entrancy flag here (a post_execute raised while we are inside a comm
   dispatch is ours, never a user's), and, decisively, by the fingerprint check
   in Bridge.on_execute, which emits only for a model that actually moved. The
   flag alone would be unsafe -- on a runtime where comm messages do NOT fire
   post_execute it could swallow a real one -- so the fingerprint is the fix and
   the flag is the cheap optimisation.

2. Re-running the bootstrap cell re-execs a fresh copy of this module, so the
   module-global _ADAPTER is None again and a brand new CommAdapter is built.
   The previous adapter still owns the open channels. register_comm therefore
   adopts them from the previous adapter, or events stop reaching a panel that
   was connected before the re-run while its requests keep being answered --
   the worst of both, and invisible.
"""

import sys

from .methods import Bridge, split_buffers

TARGET = "modelx-bridge"

_ADAPTER = None


class CommAdapter:
    """Answers requests on one comm target and fans events out to open comms."""

    def __init__(self, bridge, target=TARGET):
        self.bridge = bridge
        self.target = target
        self.comms = []
        self._hook = None
        self._dispatching = 0
        self.stats = {"msgs": 0, "post_execute": 0, "post_execute_skipped": 0,
                      "events": 0, "buffers_in": 0, "buffers_out": 0}

    # -- target ------------------------------------------------------------

    def register(self):
        try:
            import comm
        except ModuleNotFoundError as exc:
            # Only `comm` itself missing. A comm that is installed but fails
            # to import (one of ITS dependencies missing) says so in its own
            # words, below.
            if exc.name != "comm":
                raise
            # MEASURED 2026-10-06, the 0.10.0 wheel in a clean 3.12 venv: this
            # used to surface as a bare "No module named 'comm'", naming
            # neither the extra nor the kernel it stands for.
            raise ImportError(
                'register_comm() needs the "comm" package, which a Jupyter kernel '
                'provides. Install it with: pip install "modelx-bridge[jupyter]". '
                "Inside an IPython kernel, comm comes with ipykernel>=6.19.1.",
                name="comm") from exc
        manager = comm.get_comm_manager()
        # Re-running the bootstrap must not double-register: drop the old target
        # first. unregister_target wants the callback it was given, and we do not
        # keep other people's, so reach for the mapping when that fails.
        try:
            manager.unregister_target(self.target, manager.targets[self.target])
        except Exception:
            manager.targets.pop(self.target, None)
        manager.register_target(self.target, self._on_open)
        return self

    def adopt(self, previous):
        """Take over the channels an earlier adapter opened.

        The callbacks those channels hold are bound to `previous`, so they are
        re-pointed here; otherwise the old adapter keeps answering on them with
        its own (now orphaned) Bridge.
        """
        if previous is None or previous is self:
            return self
        for channel in list(getattr(previous, "comms", ())):
            if channel in self.comms:
                continue
            self.comms.append(channel)
            self._bind(channel)
        try:
            previous.comms = []
        except Exception:
            pass
        return self

    def _bind(self, channel):
        channel.on_msg(lambda msg: self._on_msg(channel, msg))
        channel.on_close(lambda msg: self._on_close(channel))

    def _on_open(self, channel, open_msg):
        self.comms.append(channel)
        self._bind(channel)
        # hello is pushed unprompted so the frontend has versions and limits
        # without a round trip (section 1).
        self._send(channel, self.bridge.hello())

    def _on_msg(self, channel, msg):
        try:
            content = msg.get("content", {})
            data = content.get("data")
        except Exception:
            return
        buffers = _incoming_buffers(msg, content)
        self.stats["msgs"] += 1
        if buffers:
            self.stats["buffers_in"] += len(buffers)
        self._dispatching += 1
        try:
            response = self.bridge.handle(data, buffers)
            if response is not None:
                self._send(channel, response)
        finally:
            self._dispatching -= 1
        self.flush()

    def _on_close(self, channel):
        if channel in self.comms:
            self.comms.remove(channel)

    def _send(self, channel, message):
        # Binary parts ride on the comm message's own `buffers`, never inside
        # `data`: bytes are not JSON. split_buffers lifts them out (section 10).
        data, buffers = split_buffers(message)
        try:
            if buffers:
                self.stats["buffers_out"] += len(buffers)
                channel.send(data, buffers=buffers)
            else:
                channel.send(data)
        except Exception as exc:                 # a dead comm must not kill the kernel
            print("modelx-bridge: send failed: %r" % (exc,), file=sys.stderr)

    def flush(self):
        for event in self.bridge.drain_events():
            self.stats["events"] += 1
            for channel in list(self.comms):
                self._send(channel, event)

    # -- execution events ---------------------------------------------------

    def install_post_execute(self):
        """Hook IPython post_execute to push model.changed when a model moved.

        Drops any hook a previous adapter left behind -- including one owned by
        an earlier *copy* of this class, which is what re-running the bootstrap
        produces -- so an execution never emits two events.
        """
        shell = _get_ipython()
        if shell is None:
            return False
        for callback in list(shell.events.callbacks.get("post_execute", ())):
            if type(getattr(callback, "__self__", None)).__name__ == "CommAdapter":
                try:
                    shell.events.unregister("post_execute", callback)
                except Exception:
                    pass
        # Whatever is open right now was opened by the bootstrap, and the
        # frontend learns about it from `hello`. Adopt it silently.
        self.bridge.prime()
        self._hook = self._post_execute
        shell.events.register("post_execute", self._hook)
        return True

    def _post_execute(self):
        self.stats["post_execute"] += 1
        try:
            if self._dispatching:
                # Raised from inside our own comm dispatch: not user code.
                self.stats["post_execute_skipped"] += 1
            else:
                self.bridge.on_execute()
            self.flush()
        except Exception as exc:
            print("modelx-bridge: post_execute failed: %r" % (exc,), file=sys.stderr)


def _incoming_buffers(msg, content):
    """The binary parts of one comm message, wherever this kernel puts them.

    ipykernel hangs them off the message; some transports nest them under
    `content`. Checking both is two lines and avoids a class of "the upload
    silently arrived empty" bug that is very hard to see from the frontend.
    """
    for source in (msg, content):
        try:
            buffers = source.get("buffers")
        except Exception:
            continue
        if buffers:
            return list(buffers)
    return []


def _get_ipython():
    ipython = sys.modules.get("IPython")
    if ipython is None:
        return None
    try:
        return ipython.get_ipython()
    except Exception:
        return None


def register_comm(bridge=None, target=TARGET, previous=None):
    """Register the comm target and the post_execute hook. Idempotent.

    Returns the adapter. Called twice, it reuses the same Bridge so handles and
    revisions survive, and adopts the previous adapter's open channels so a
    panel connected before a bootstrap re-run keeps receiving events.
    """
    global _ADAPTER
    previous = previous or _ADAPTER
    if _ADAPTER is not None and _ADAPTER.target == target:
        adapter = _ADAPTER
        if bridge is not None:
            adapter.bridge = bridge
    else:
        adapter = CommAdapter(bridge or Bridge(), target)
    adapter.adopt(previous)
    adapter.register()
    adapter.install_post_execute()
    _ADAPTER = adapter
    return adapter
