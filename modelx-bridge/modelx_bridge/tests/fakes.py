"""Stand-ins for `comm` and `IPython`, shared by the kernel-side tests.

These are deliberately small and deliberately WRONG in one specific way that
matches the real target: `PyodideKernel.run_message` fires post_execute after
every comm message, not only after user code. That behaviour is measured (a
post_execute counter in the JupyterLite Pyodide kernel went 4 -> 5 -> 6 for
three comm messages, and did not move over six seconds of idle), and it is the
whole reason the event fan-out needs a change detector. A fake that only fires
post_execute for "executions" would let the bug back in.
"""

import sys
import types


class FakeComm:
    def __init__(self, manager):
        self.manager = manager
        self.sent = []
        self._on_msg = None
        self._on_close = None

    def on_msg(self, callback):
        self._on_msg = callback

    def on_close(self, callback):
        self._on_close = callback

    def send(self, data, metadata=None, buffers=None):
        self.sent.append(data)

    def deliver(self, message):
        """One comm message, WITHOUT the kernel's post_execute."""
        self.sent = []
        self._on_msg({"content": {"data": message}})
        return self.sent

    # test_bootstrap's original name
    request = deliver


class FakeCommManager:
    def __init__(self):
        self.targets = {}

    def register_target(self, name, callback):
        self.targets[name] = callback

    def unregister_target(self, name, callback):
        if self.targets.get(name) is not callback:
            raise KeyError(name)
        del self.targets[name]

    def open(self, name):
        channel = FakeComm(self)
        self.targets[name](channel, {"content": {"data": {"protocol": 0}}})
        return channel


class FakeEvents:
    def __init__(self):
        self.callbacks = {"post_execute": []}

    def register(self, event, callback):
        self.callbacks[event].append(callback)

    def unregister(self, event, callback):
        self.callbacks[event].remove(callback)


class FakeShell:
    def __init__(self):
        self.events = FakeEvents()
        self.post_executes = 0

    def run_post_execute(self):
        self.post_executes += 1
        for callback in list(self.events.callbacks["post_execute"]):
            callback()


def install_fakes():
    """Put the fakes in sys.modules and return (manager, shell)."""
    manager = FakeCommManager()
    comm = types.ModuleType("comm")
    comm.get_comm_manager = lambda: manager
    sys.modules["comm"] = comm

    shell = FakeShell()
    ipython = types.ModuleType("IPython")
    ipython.get_ipython = lambda: shell
    sys.modules["IPython"] = ipython
    return manager, shell
