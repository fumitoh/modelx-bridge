"""modelx-mcp: an MCP server, over stdio, that reads a live modelx session.

    modelx-mcp --sample BasicTerm_S
    modelx-mcp --storage-root /path/to/models --open /MyModel

An MCP client such as Claude Code or Claude Desktop starts it; README.md (the
PyPI page, https://pypi.org/project/modelx-mcp/) says how. Models open at
launch and only at launch. `python -m modelx_mcp` is the same program.
"""
import argparse
import json
import os
import sys

from . import __version__

#: What --open without --storage-root says. MEASURED before this: an absolute
#: --open path to a model folder, with no --storage-root, was reported as
#: "<that path> is not a modelx model" and the server ran with nothing open,
#: while --storage-root set to the folder's parent and --open /<its name>
#: opened it.
NEEDS_ROOT = ("--open needs --storage-root: --storage-root is the folder that holds your saved "
              "models, and each --open PATH is a model folder or .zip under it. For example: "
              "modelx-mcp --storage-root /path/to/models --open /MyModel")


def main(argv=None):
    # A fixed model set keeps every ref the server prints naming one node for
    # the server's life. An earlier design had an open tool: replacing a
    # sample through it re-pointed refs already cited at the new model's nodes.
    ap = argparse.ArgumentParser(prog="modelx-mcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--storage-root", metavar="DIR",
                    help="the folder that holds your saved models; every --open path is under "
                         "it (needed with --open)")
    ap.add_argument("--sample", action="append", default=[], metavar="ID",
                    help="open a shipped sample, e.g. BasicTerm_S (repeatable; BasicTerm_S when "
                         "neither --sample nor --open is given)")
    ap.add_argument("--open", action="append", default=[], metavar="PATH",
                    help="open a model folder or zip by its path under --storage-root, e.g. "
                         "--storage-root /path/to/models --open /MyModel (repeatable)")
    ap.add_argument("--allow-python", action="store_true", help="add run_python, which is NOT read-only")
    ap.add_argument("--max-chars", type=int, default=12000, help="the bound on every tool output (default 12000)")
    ap.add_argument("--align-cells", default="model_point",
                    help="the Cells whose rows an ndarray's positions are aligned with (default "
                         "model_point; '' turns it off)")
    ap.add_argument("--log", metavar="FILE",
                    help="append one JSON line per tool call: its args, every bridge call with its "
                         "result, and the text")
    ap.add_argument("--print-tools", action="store_true",
                    help="print the tools/list JSON and exit, opening no model")
    ap.add_argument("--version", action="version", version="modelx-mcp " + __version__)
    args = ap.parse_args(argv)
    if args.print_tools:
        from .server import print_tools
        sys.stdout.write(print_tools(args.allow_python) + "\n")
        return 0
    if args.max_chars < 2000:
        ap.error("--max-chars must be at least 2000")
    if args.open and not args.storage_root:
        ap.error(NEEDS_ROOT)
    # Each model once, in the order given. MEASURED before this: `--sample
    # BasicTerm_S --sample BasicTerm_S` built the sample twice and stderr said
    # "open BasicTerm_S, BasicTerm_S".
    args.sample = list(dict.fromkeys(args.sample))
    args.open = list(dict.fromkeys(args.open))
    if not args.sample and not args.open:
        args.sample = ["BasicTerm_S"]
    # Before any model loads: from here on fds 0 and 1 are not the channel.
    proto_in, proto_out = isolate_stdio()
    journal = None
    if args.log:
        fh = open(args.log, "a", encoding="utf-8")

        def journal(record):
            fh.write(json.dumps(record, default=repr) + "\n")
            fh.flush()
    from .session import Session
    session = Session(args.storage_root, args.sample, args.open, max_chars=args.max_chars,
                      align=args.align_cells or None, journal=journal)
    for what, why in session.failed:
        sys.stderr.write("modelx-mcp: could not open %s: %s\n" % (what, why))
    sys.stderr.write("modelx-mcp %s: open %s%s\n" % (__version__, ", ".join(session.opened) or "nothing",
                                                     "; run_python ENABLED" if args.allow_python else ""))
    from .server import build, run_stdio
    run_stdio(build(session, allow_python=args.allow_python), proto_in, proto_out)
    return 0


def isolate_stdio():
    """Give the MCP transport private duplicates of fds 0 and 1 -> (in, out),
    then point fd 0 at the null device and fd 1 at stderr, and sys.stdin and
    sys.stdout with them.

    contextlib.redirect_stdout swaps sys.stdout only, and a model or
    run_python's code can reach the fds themselves. MEASURED before this, over
    stdio: a formula's os.write(1, b"FD1-NO-NEWLINE") during calculate, or
    os.system("printf 12345") in run_python, was glued to the front of the
    response line, the client could not parse it, and the call never
    completed; sys.__stdout__.write() and os.system("echo ...") put lines on
    the channel that the client logged as invalid JSON; and site's exit()
    closed sys.stdin, the protocol's input, so the server died with exit code
    1. Now a subprocess inherits the null device and stderr, and the
    duplicates are not inheritable (PEP 446), so no child can reach them."""
    for f in (sys.stdout, sys.stderr):
        f.flush()
    proto_in, proto_out = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    # closefd=False: exit() outside run_python closes this wrapper, never fd 0.
    sys.stdin = open(0, encoding="utf-8", closefd=False)
    sys.stdout = sys.stderr
    return proto_in, proto_out


if __name__ == "__main__":
    sys.exit(main())
