"""Typed errors for the wire protocol (docs/bridge-protocol-v0.md section 3).

The dispatcher must never let an exception escape, or the frontend's pending map
leaks a promise per lost request. Everything raised inside a method is therefore
either a BridgeError already or is wrapped as ``internal`` by Bridge.handle().
"""

import traceback

KERNEL_CODES = ("bad_request", "not_found", "no_model", "formula_error", "internal")


class BridgeError(Exception):

    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data or None

    def to_json(self):
        err = {"code": self.code, "message": self.message}
        if self.data:
            err["data"] = self.data
        return err


def bad_request(message, **data):
    return BridgeError("bad_request", message, data)


def not_found(message, **data):
    return BridgeError("not_found", message, data)


def no_model(message="no model is open; call model.open_sample first", **data):
    return BridgeError("no_model", message, data)


def formula_error(message, **data):
    return BridgeError("formula_error", message, data)


def internal(exc):
    # One UI-safe line in `message`; the full traceback only in `data`.
    return BridgeError(
        "internal",
        "%s: %s" % (type(exc).__name__, exc),
        {"traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))},
    )
