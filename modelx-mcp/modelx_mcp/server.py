"""FastMCP wiring: the server instructions, the tool descriptions, and
run_python behind --allow-python.

Tools are plain `def`s, so FastMCP 1.28.1 runs them inline on its event loop,
one at a time, which is what the one kernel thread of Phase 3 does; the lock
keeps that true if a later FastMCP moves sync tools to threads (the Bridge and
modelx are single-threaded). MEASURED over stdio: CashValue_ME's 8.6 s
`pv_net_cf()` completed with no client timeout.

`structured_output=False` on every tool: with FastMCP's default a `-> str`
tool sends its text twice (`structuredContent: {"result": ...}`), and Claude
Code delivered results JSON-escaped in the audit pilot.
"""
import ast
import json
import logging
import os
import re
import threading
import warnings
from typing import List, Literal, Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
from mcp.server.fastmcp.tools.base import Tool
from mcp.types import ToolAnnotations

from . import __version__
from .refs import RefError
from .tools import ToolError, WireError

READ_ONLY = ("It is read-only: no tool edits a formula or a Reference.")
# "every other tool is read-only", the first cut, was false of calculate:
# measured, calculate of Projection[3].pv_net_cf() on a fresh BasicTerm_S
# created the ItemSpace Projection[3], which stays, computed 1,833 nodes and
# moved the revision 1 -> 3.
WITH_PYTHON = ("Only run_python can change a model's formulas or References, or overwrite its values; "
               "calculate computes values and can create ItemSpaces; the other tools only read.")

COMPUTES = ("calculate is the only tool that computes: use it for any number; it reads cached "
            "values without recomputing them.")
COMPUTES_PYTHON = ("calculate is the tool that computes: use it for any number; it reads cached "
                   "values without recomputing them. run_python can compute too, but cite only "
                   "what calculate or get_value prints.")

INSTRUCTIONS = """modelx-mcp reads a live modelx session (actuarial models built with modelx and lifelib) through the bridge the lifelib Studio UI uses. {access}

Open models: {models}.

REFS. Every tool names things with one string, written as Python, and prints the same strings{examples}:
  BasicTerm_S.Projection.claims(t=3)       a Cells node (keyword or positional arguments)
  BasicTerm_S.Projection[2].pv_net_cf()    inside the ItemSpace for point_id 2
  BasicTerm_S.Projection.disc_rate_ann     a Reference
  BasicTerm_S.Projection.claims            a Cells itself: its formula, or (get_value) its cached values
The model name may be left off while one model is open. Copy refs from tool output.

COMPUTING. Nothing is computed when a model opens. {computes} Every other tool prints NOT COMPUTED for a node with no value: unknown, never zero.

{items}

LINKS. trace reports what modelx recorded while calculating, so an uncomputed node's links are UNKNOWN. get_formulas and get_map report the names a formula's source contains. They can differ; each output says which it is.

CITING. Give every number with the ref printed left of its '=', e.g. BasicTerm_S.Projection.pv_claims() = 5501.19; for one element add .loc[label] or .iloc[i] ([i] on an ndarray only); for a statistic say which. For arithmetic on model values, pass the expression to calculate (A / B), which prints it with its operands; otherwise say the number is your own."""

D_TREE = """The structure of a model: its Spaces, each Space's Cells as name(parameters) with how many values that Space has cached after ':' (no count = none), its References with their types, and its ItemSpaces (e.g. Projection[2]). Never computes. filter= keeps the Cells and References whose names contain it (case-insensitive; '|' separates alternatives); path= narrows to one Space; offset= continues a long Cells list."""

D_FORMULAS = """What things are, read from their definitions; never computes. A Cells: its formula source with its docstring (docstrings=false drops docstrings), the Cells and References the formula names, and the formulas that name it. A Space or the model: its docstring, up to 8,000 characters per call (doc_offset= continues), and a summary of its formula map. A Reference: its value, the formulas that name it, and the paragraph of its Space's docstring that documents it. Up to 12 refs per call."""

D_MAP = """Which Cells each formula in a Space NAMES, read from the formulas' source: complete before anything is computed, and never computes. Without cells=: one line per Cells (offset= continues). With cells="name": that Cells' inputs and its formula-level dependents, level by level (depth=-1 for every level). This is the formula-level answer; trace gives what a calculation recorded."""

D_CALC = """The ONLY tool that computes. For each ref, computes the node if it has no value yet (with everything it needs) and prints its value, labelled [computed now] or [cached]; a cached node is read, not recomputed. Use it for any number. Up to 50 refs and 200 nodes per call. One argument may be range(a, b), e.g. Projection.claims(t=range(0, 121)): a table, plus the sum, min and max over those nodes and where they occur. A ref may be arithmetic over refs (+ - * /), e.g. X.pv_net_cf() / X.pv_premiums(). A ref inside an ItemSpace that does not exist yet (Projection[3].x) creates it. Can take seconds (CashValue_ME pv_net_cf(): about 8 s)."""

D_VALUE = """Reads values that are already computed; never computes. An uncomputed node prints NOT COMPUTED (unknown, not zero). Takes the same refs as calculate except arithmetic, and also: a Cells without arguments (Projection.claims) lists every value it has cached, with statistics over all of them; label= picks one element of each cached vector (one model point across t); .loc[label] or .iloc[i] read one element ([i] on an ndarray only); ["col"] one DataFrame column. offset=, rows= and col= page a long vector or table. Use it to page and to re-check a number without computing."""

D_TRACE = """The links modelx RECORDED when a node was computed, with their values; never computes. direction="preds": what the node read. direction="succs": the computed nodes that have read it so far, which is not every formula that names it. Neighbours of one Cells are grouped (claims(t=0..120) x121). Also prints what the formula names (read from source), the names it has that this computation did not read, and for preds the formula body. depth= 1-3; offset= pages a long list. An uncomputed node's links are UNKNOWN: calculate it first."""

D_PYTHON = """Runs Python in this server's own interpreter, against the same live models: mx (modelx) and each open model by name are defined. Prints stdout and the value of a final expression. NOT read-only: code can change formulas, References and cached values, and the output says what the bridge detected. Its numbers carry no checked ref: re-read anything you will cite with get_value or calculate. Use it only when no other tool can answer."""

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# calculate fills the cache, so it is not read-only; it never destroys anything.
CALC = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def items_text(items, one_model):
    """The ITEMS paragraph, from the Spaces the open models really have. A
    fixed BasicTerm_S paragraph would tell a model working on CashValue_ME,
    whose Projection takes no parameters, to write Projection[2]."""
    if not items:
        return ("ITEMS. No Space in the open models takes parameters, so there are no "
                "ItemSpaces (Space[k]).")

    def short(ref):
        return ref.split(".", 1)[1] if one_model else ref
    ref, params, has_ref = items[0]
    spaces = ", ".join("%s[%s]" % (short(r), ", ".join(p)) for r, p, _ in items)
    ex = short(ref)
    if len(params) == 1:
        text = ("ITEMS. %s hold%s one ItemSpace per argument: %s[2].x is item 2. %s.x, with no "
                "[k], is the base Space: " % (spaces, "s" if len(items) == 1 else " each", ex, ex))
    else:
        text = ("ITEMS. %s hold%s one ItemSpace per set of arguments: %s[%s].x. %s.x, with no "
                "[...], is the base Space: " % (spaces, "s" if len(items) == 1 else " each", ex,
                                                ", ".join(params), ex))
    if has_ref:
        text += "the item its Reference of the same name (%s) selects." % params[0]
    else:
        text += "its own values, which are no item's."
    return text


def instructions(models, items, allow_python, names=()):
    """The text sent in `initialize`: `models` and `items` from
    Session.describe(), `names` the models open."""
    return INSTRUCTIONS.format(
        access=WITH_PYTHON if allow_python else READ_ONLY,
        models=models,
        examples="" if "BasicTerm_S" in names else " (these examples are from the BasicTerm_S sample)",
        computes=COMPUTES_PYTHON if allow_python else COMPUTES,
        items=items_text(items, len(names) <= 1))


def build(session, allow_python=False):
    lock = threading.Lock()
    T = session.tools if session is not None else None
    tools = []

    def register(name, description, annotations):
        def deco(fn):
            tool = Tool.from_function(fn, name=name, description=description,
                                      annotations=annotations, structured_output=False)
            tool.parameters = _untitled(tool.parameters)
            tools.append(tool)
            return fn
        return deco

    def call(name, fn, *args, **kwargs):
        with lock:
            try:
                return fn(*args, **kwargs)
            except (ToolError, RefError) as e:
                message = str(e)
            except WireError as e:
                message = "%s: %s" % (e.code, e.message)
            except Exception as e:      # never a raw traceback to the model
                message = "internal error in modelx-mcp: %s: %s" % (type(e).__name__, e)
            # A whole-call error is journaled too: the journal is the record
            # of which bridge methods each call really made (spec 3.5).
            journal = getattr(session, "journal", None)
            if journal:
                journal({"tool": name, "args": [list(args), kwargs],
                         "calls": T.calls if T is not None else [], "error": message})
            raise MCPToolError(message)

    @register("get_tree", D_TREE, RO)
    def get_tree(model: Optional[str] = None, path: Optional[str] = None,
                 filter: Optional[str] = None, offset: int = 0) -> str:
        return call("get_tree", T.get_tree, model, path, filter, offset)

    @register("get_formulas", D_FORMULAS, RO)
    def get_formulas(refs: List[str], docstrings: bool = True, doc_offset: int = 0) -> str:
        return call("get_formulas", T.get_formulas, refs, docstrings, doc_offset)

    @register("get_map", D_MAP, RO)
    def get_map(model: Optional[str] = None, space: Optional[str] = None,
                cells: Optional[str] = None, depth: int = 1, offset: int = 0) -> str:
        return call("get_map", T.get_map, model, space, cells, depth, offset)

    @register("calculate", D_CALC, CALC)
    def calculate(refs: List[str]) -> str:
        return call("calculate", T.calculate, refs)

    @register("get_value", D_VALUE, RO)
    def get_value(refs: List[str], offset: int = 0, rows: Optional[int] = None, col: int = 0,
                  label: Optional[Union[int, str]] = None) -> str:
        return call("get_value", T.get_value, refs, offset, rows, col, label_literal(label))

    @register("trace", D_TRACE, RO)
    def trace(ref: str, direction: Literal["preds", "succs"] = "preds", depth: int = 1,
              offset: int = 0) -> str:
        return call("trace", T.trace, ref, direction, depth, offset)

    if allow_python:
        from .python_tool import PythonTool
        runner = PythonTool(session) if session is not None else None

        @register("run_python", D_PYTHON, PY)
        def run_python(code: str) -> str:
            return call("run_python", runner.run, code)

    if session is None:
        text = instructions("(none)", [], allow_python)
    else:
        models, items, names = session.describe()
        text = instructions(models, items, allow_python, names)
    # log_level WARNING: at FastMCP's default, INFO, mcp wrote "Processing
    # request of type ..." to stderr for every request (measured through
    # Claude Code 2.1.291 with mcp 1.30.0: one line per request).
    with warnings.catch_warnings():
        # MEASURED with mcp 1.10.0 and pydantic-settings 2.15.0: every launch
        # wrote "IncompleteFieldDefinitionWarning: Field 'lifespan' has an
        # incomplete definition ..." to stderr from FastMCP's own Settings.
        # mcp 1.28.1 (pydantic-settings 2.14.2) and 1.30.0 (2.15.0) did not.
        warnings.filterwarnings("ignore", message="Field 'lifespan' has an incomplete definition")
        server = FastMCP("modelx", instructions=text, tools=tools, log_level="WARNING")
    quiet_mcp_logging()
    # FastMCP 1.28.1 takes no version and fills serverInfo.version with the mcp
    # package's own: initialize said modelx 1.28.1 while --version said 0.1.0.
    server._mcp_server.version = __version__
    return server


class DiscoverProbeFilter(logging.Filter):
    """Drops one log record: mcp 1.x's warning that a request whose method is
    `server/discover` failed validation. Every other record passes, including
    a failed validation of any other request.

    MEASURED 2026-10-06: Claude Code 2.1.291 sends `server/discover` first on
    every connect. mcp 1.x does not know the method, answers it with error
    -32602, and Claude Code falls back and connects; but mcp also logs
    "Failed to validate request: 31 validation errors for ClientRequest", one
    pydantic error per request type, to stderr: 6,080 bytes of the server's
    6,115 with mcp 1.28.1 over raw stdio, and through Claude Code with mcp
    1.30.0 the server's stderr came to 6,996 to 7,039 bytes. Claude Code's
    debug log showed it under [ERROR], with the one line the server writes
    there itself, what it opened, at its head. mcp logs it with
    logging.warning() from mcp/shared/session.py, so the record is the root
    logger's.
    """

    #: The method's own literal error, which only a request whose method is
    #: server/discover produces: "PingRequest.method" then "  Input should be
    #: 'ping' [type=literal_error, input_value='server/discover', ...".
    METHOD = re.compile(r"^\w+\.method\n +Input should be [^\n]*"
                        r"\[type=literal_error, input_value='server/discover',", re.M)
    SOURCE = os.path.join("mcp", "shared", "session.py")

    def filter(self, record):
        if record.levelno != logging.WARNING or not record.pathname.endswith(self.SOURCE):
            return True
        message = record.getMessage()
        return not (message.startswith("Failed to validate request:") and self.METHOD.search(message))


DISCOVER_PROBE = DiscoverProbeFilter()


def quiet_mcp_logging():
    """The mcp package's INFO lines off, and DISCOVER_PROBE on.

    FastMCP's log_level reaches the root logger through logging.basicConfig,
    which does nothing once the root logger has a handler, and mcp 1.10.0's
    mcp/client/session.py calls basicConfig(level=INFO) when mcp is imported
    (the wheels of 1.11.0, 1.12.0, 1.14.1, 1.17.0, 1.20.0, 1.25.0, 1.28.1 and
    1.30.0 do not). MEASURED with 1.10.0: log_level="WARNING" still
    left "INFO:mcp.server.lowlevel.server:Processing request of type
    ListToolsRequest" on stderr. The "mcp" logger's own level removed it with
    1.10.0, 1.28.1 and 1.30.0.

    DISCOVER_PROBE goes on the root logger, where mcp's record starts, and on
    its handlers, which a record from a named logger also reaches. Adding it
    twice is a no-op (Filterer.addFilter)."""
    logging.getLogger("mcp").setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addFilter(DISCOVER_PROBE)
    for handler in root.handlers:
        handler.addFilter(DISCOVER_PROBE)


def run_stdio(server, stdin_fd, stdout_fd):
    """FastMCP.run("stdio") on the given fds rather than on sys.stdin and
    sys.stdout, which __main__.isolate_stdio has pointed away from the
    channel. The same calls as FastMCP.run_stdio_async, the streams wrapped
    as mcp's stdio_server wraps its defaults."""
    import io
    import os
    import anyio
    from mcp.server.stdio import stdio_server
    low = server._mcp_server

    async def main():
        stdin = anyio.wrap_file(io.TextIOWrapper(os.fdopen(stdin_fd, "rb"), encoding="utf-8",
                                                 errors="replace"))
        stdout = anyio.wrap_file(io.TextIOWrapper(os.fdopen(stdout_fd, "wb"), encoding="utf-8"))
        async with stdio_server(stdin=stdin, stdout=stdout) as (r, w):
            await low.run(r, w, low.create_initialization_options())
    anyio.run(main)


def print_tools(allow_python=False):
    """The tools/list JSON, exactly as a client receives it; opens no model."""
    import asyncio
    mcp = build(None, allow_python=allow_python)
    listed = asyncio.run(mcp.list_tools())
    return json.dumps([t.model_dump(exclude_none=True) for t in listed])


def _untitled(schema):
    """The input schema without pydantic's per-property "title" keys, which
    repeat each name ("refs" -> "Refs") in a prefix every request pays for."""
    if isinstance(schema, dict):
        return dict((k, _untitled(v)) for k, v in schema.items() if k != "title")
    if isinstance(schema, list):
        return [_untitled(v) for v in schema]
    return schema


def label_literal(text):
    """label= as a model sends it: 7342, "7342" or "'A'". A string that is a
    Python literal is read as one ("1" -> 1); anything else stays a string."""
    if text is None or isinstance(text, (int, float)):
        return text
    try:
        return ast.literal_eval(text)
    except Exception:
        return text
