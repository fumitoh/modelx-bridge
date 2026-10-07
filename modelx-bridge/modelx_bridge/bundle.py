"""Generate apps/lite/bridge_bootstrap.py -- the single file the lab extension
exec's inside the Pyodide kernel.

    python -m modelx_bridge.bundle [dest]

`dest` defaults to lifelib Studio's apps/lite/bridge_bootstrap.py, and is
required anywhere that folder does not exist.

The bootstrap vendors the package *as a string* rather than inlining the modules
as top-level code. Two reasons:

  * the kernel namespace is the user's Console namespace, and thirty helper names
    in it is a worse first impression than three;
  * exec'ing the string into a real module registered in sys.modules means
    `import modelx_bridge` works in the Console exactly as it does on the desktop,
    so panels, notebooks and tests share one import path.

The string is emitted one source line per list entry, each as a Python repr. That
is verbose, but it is the only encoding with no delimiter to collide with -- the
package uses \"\"\" docstrings and the sample formulas use ''' -- and it keeps the
generated file greppable and its diff aligned 1:1 with the package's.
"""

import os
import sys

# Import order. The bundle is ONE flat namespace, so a name defined twice is a
# silent override: files.py and samples.py both need a "is this a model
# directory" check and therefore each carry their own, under different names.
# tests/test_files.py asserts they agree and that no name collides.
MODULES = ["errors", "codec", "tables", "files", "samples", "methods", "jupyter"]

# Verified py3-none-any wheels (spikes S1 and S2), installed with deps=False.
#
# networkx comes from a direct URL, NOT by name: pyodide-lock carries a
# hand-written networkx -> matplotlib edge, and resolving it by name drags in
# the whole plotting stack for a graph library.
#
# The URL pin is necessary but NOT sufficient, and that cost a round. The kernel
# calls pyodide's loadPackagesFromImports() on the raw cell before running it, so
# a bare `import networkx` ANYWHERE in the bootstrap -- including inside a
# function body, which ast.walk still finds -- loads the lock's networkx and its
# lock dependencies behind micropip's back. See the long comment above
# _bridge_import in HEADER; that is the rule this WHEELS list depends on.
#
# deps=False is load-bearing and its correctness is not obvious. modelx 0.33.0
# declares Requires-Dist: networkx>=2.2, asttokens, libcst. Of those:
#   networkx  shipped here.
#   asttokens DELIBERATELY ABSENT. The kernel preloads asttokens for IPython,
#             and installing a second copy over the already-imported one yields
#             "ImportError: cannot import name AstNode from asttokens.util",
#             which is fatal and persists in the kernel's storage. Measured.
#   libcst    NOT shipped, and not needed today only because it is imported
#             solely by modelx/export/transformer.py, which is never on the
#             import path. A modelx that imports libcst eagerly would break the
#             demo with a bare ImportError -- which is what the smoke check in
#             the bootstrap exists to turn into a diagnosable message.
# et_xmlfile is openpyxl's only dependency; it is shipped rather than resolved.
WHEELS = [
    "https://files.pythonhosted.org/packages/9e/c9/"
    "b2622292ea83fbb4ec318f5b9ab867d0a28ab43c5717bb85b0a5f6b3b0a4/"
    "networkx-3.6.1-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/dd/6f/"
    "b0af7a380d134055427ee20c398ccb41c78d58783407ebe96429e5ce6675/"
    "modelx-0.33.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/c1/8b/"
    "5fe2cc11fee489817272089c4203e679c63b570a5aaeb18d852ae3cbba6a/"
    "et_xmlfile-2.0.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/c0/da/"
    "977ded879c29cbd04de313843e76868e6e13408a94ed6b987245dc7c8506/"
    "openpyxl-3.1.5-py2.py3-none-any.whl",
]

# Resolved from pyodide-lock by name, WITH dependencies (numpy, python-dateutil,
# pytz). This is the only entry that may resolve dependencies, and it is checked:
# pandas' closure in the lock contains no matplotlib. BasicTerm_S needs it --
# its three References are modelx pandas IO specs over .xlsx files.
PYODIDE_PACKAGES = ["pandas"]

HEADER = '''\
"""lifelib Studio -- modelx-bridge kernel bootstrap.

GENERATED FILE. Edit python/modelx_bridge/ and re-run:

    python -m modelx_bridge.bundle

This is ONE IPython cell, not an importable module: it uses top-level `await`
for micropip, so the extension must run it with kernel.requestExecute({code}),
and plain `import` of this file is a SyntaxError. Running it twice is safe --
the installs, the comm target and the demo model are all idempotent.

On success the cell prints one line:

    MODELX_BRIDGE_READY {"protocol": 0, ...}

which is the session.info payload, so the extension can confirm the boot from
the execute reply without waiting for the comm handshake. If the demo model
could not be opened the cell ALSO prints, before that line:

    MODELX_BRIDGE_WARNING {"stage": "sample", ...}

carrying every filesystem path it looked in. The cell still succeeds: a missing
model is a content problem, not a reason to leave the visitor with no kernel.
"""

import json
import linecache
import sys
import types

BRIDGE_TARGET = "modelx-bridge"
# The canonical sample id (docs/bridge-protocol-v0.md 6.2), equal to the model
# name. The frontend's Welcome cards and jupyter-lite.json's lifelibStudioSamples
# send the same string; "termlife" and "basicterm_s" remain registered aliases.
BRIDGE_SAMPLE = "BasicTerm_S"
BRIDGE_WHEELS = [
@@WHEELS@@
]
BRIDGE_PYODIDE_PACKAGES = @@PYODIDE@@

# find_spec, not import: importing pandas costs about a second and the model
# read that follows will do it anyway.
BRIDGE_REQUIRED = ["modelx", "networkx", "openpyxl", "pandas"]

BRIDGE_RESET_HINT = ("Run \\"lifelib Studio: Reset environment\\" from the command "
                     "palette, then reload the page.")


def _bridge_missing():
    import importlib.util
    missing = []
    for name in BRIDGE_REQUIRED:
        try:
            found = importlib.util.find_spec(name) is not None
        except Exception:
            found = False
        if not found:
            missing.append(name)
    return missing


def _bridge_needs_install():
    """Which packages are missing, or [] on a desktop run.

    Catches Exception, not ImportError: the S2 failure mode is a *corrupted
    persisted filesystem* that raises a fatal RangeError somewhere inside the
    import machinery. That is not an ImportError, it used to escape as an opaque
    traceback, and the one thing the visitor needs to be told is the name of the
    command that clears it.
    """
    if sys.platform != "emscripten":
        return []                # desktop / test runs use the installed packages
    try:
        return _bridge_missing()
    except BaseException as exc:
        raise RuntimeError(
            "the Python environment in this browser tab is damaged (%r). %s"
            % (exc, BRIDGE_RESET_HINT))


# NO `import` STATEMENT IN THIS CELL MAY NAME A NON-STDLIB MODULE.
#
# Not a style rule -- an import statement here is a download, and it happens
# before micropip is consulted at all. The JupyterLite Pyodide kernel runs
#
#     await _load_packages_from_imports(lite_cell)     # pyodide_kernel/kernel.py
#
# on the RAW cell before executing it. That is pyodide's loadPackagesFromImports,
# which calls find_imports() -> ast.walk() over the whole module, so an import
# nested inside a function body counts exactly as much as one at module level --
# and ast.parse accepts this cell's top-level `await`, because "await outside
# function" is raised when compiling to bytecode, not when parsing. Every name it
# finds that exists in pyodide-lock is then loaded by pyodide.loadPackage WITH
# that lock entry's declared dependencies. That path is not micropip: deps=False
# has no effect on it, and a direct wheel URL does not pre-empt it.
#
# MEASURED, pyodide v314.0.6: one `import networkx` inside _bridge_smoke was
# enough. pyodide-lock's networkx 3.6.1 declares
# depends = [decorator, setuptools, matplotlib, numpy], and matplotlib pulls
# contourpy, cycler, fonttools, kiwisolver, packaging, pillow, pyparsing,
# python-dateutil and pytz -- 8.88 MB of plotting stack on every cold boot,
# downloaded before the first line of this cell ran, plus a second copy of
# networkx that micropip then overwrote from the pinned URL.
#
# So: import the installed packages through _bridge_import, which is an
# ordinary function call and therefore invisible to find_imports. Enforced by
# modelx_bridge.tests.test_bootstrap, which re-implements find_imports and
# fails if this cell names anything outside sys.stdlib_module_names.
def _bridge_import(name):
    import importlib
    return importlib.import_module(name)


def _bridge_smoke():
    """Fail with a diagnosable message if deps=False stopped being correct."""
    try:
        _bridge_import("modelx")
        _bridge_import("networkx")
    except BaseException as exc:
        raise RuntimeError(
            "modelx does not import after installation (%r). Its declared "
            "dependencies may have drifted from what this bootstrap ships "
            "(networkx, openpyxl, et_xmlfile; asttokens is preloaded and must "
            "NOT be installed). %s" % (exc, BRIDGE_RESET_HINT))


# Top-level await: micropip has no synchronous install. Everything else in this
# file is ordinary Python, so the only thing that makes it cell-only is this.
_bridge_missing_names = _bridge_needs_install()
if _bridge_missing_names:
    micropip = _bridge_import("micropip")
    await micropip.install(BRIDGE_WHEELS, deps=False)            # noqa: F704
    await micropip.install(BRIDGE_PYODIDE_PACKAGES)              # noqa: F704
    _bridge_smoke()

'''

FOOTER = '''
# The package's own modules refer to each other as `files.x` / `tables.x`, and
# the bundle is ONE flat namespace, so those attribute lookups have to land back
# in it. A snapshot copy would not do: a host that sets `samples.MODEL_BASE_URL`
# expects the functions to see it, so the alias reads and writes the live
# globals rather than a copy of them.
class _BridgeFlatModule:
    """Stands in for a submodule inside the flat bundle."""

    def __init__(self, namespace, name):
        self.__dict__["__dict__ref"] = namespace
        self.__dict__["__name__"] = name

    def __getattr__(self, attr):
        try:
            return self.__dict__["__dict__ref"][attr]
        except KeyError:
            raise AttributeError("module %r has no attribute %r"
                                 % (self.__dict__["__name__"], attr))

    def __setattr__(self, attr, value):
        self.__dict__["__dict__ref"][attr] = value

    def __repr__(self):
        return "<bundled module %r>" % self.__dict__["__name__"]


_BRIDGE_SUBMODULES = @@SUBMODULES@@


def _bridge_install_module(name, lines):
    """exec the vendored source into a real module, and register its source with
    linecache so tracebacks out of the bridge still show code."""
    source = "\\n".join(lines)
    filename = "<%s bundled>" % name
    linecache.cache[filename] = (
        len(source), None, source.splitlines(True), filename)
    module = types.ModuleType(name)
    module.__file__ = filename
    exec(compile(source, filename, "exec"), module.__dict__)
    for _sub in _BRIDGE_SUBMODULES:
        alias = _BridgeFlatModule(module.__dict__, name + "." + _sub)
        module.__dict__.setdefault(_sub, alias)
        sys.modules[name + "." + _sub] = alias
    sys.modules[name] = module
    return module


# Re-running this cell reinstalls the module, so a code change takes effect, but
# carries the previous Bridge across so revisions and handles survive -- and the
# previous ADAPTER too, because it, not the Bridge, owns the open comm channels.
_bridge_previous = globals().get("_MODELX_BRIDGE")
modelx_bridge = _bridge_install_module("modelx_bridge", _BRIDGE_SOURCE)

_bridge_warning = None
try:
    # NOT build_sample: `boot` opens the sample only when this visitor has no
    # models of their own in storage (protocol 9.7). Opening it unconditionally
    # put a pristine BasicTerm_S on the one name every saved model of a demo
    # visitor also carries, which is what made "save, come back, re-open" hand
    # back the sample instead of the file. What it decided is in session.info's
    # `boot` block, so the frontend can greet a returning visitor with their own
    # work instead of an empty Explorer.
    modelx_bridge.boot(BRIDGE_SAMPLE)
except BaseException as _bridge_exc:
    _bridge_warning = {"stage": "sample", "sample": BRIDGE_SAMPLE,
                       "error": "%s: %s" % (type(_bridge_exc).__name__, _bridge_exc)}
    try:
        # Flat namespace: the bundled module has no submodules.
        _bridge_warning["paths"] = modelx_bridge.sample_paths()
    except BaseException:
        pass
    print("MODELX_BRIDGE_WARNING " + json.dumps(_bridge_warning, default=repr))

_MODELX_BRIDGE = modelx_bridge.register_comm(
    bridge=getattr(_bridge_previous, "bridge", None),
    target=BRIDGE_TARGET,
    previous=_bridge_previous)
print("MODELX_BRIDGE_READY " + json.dumps(
    _MODELX_BRIDGE.bridge.dispatch("session.info", {})))
'''


def package_source(package_dir):
    """Concatenate the package modules into one flat source, in import order."""
    lines = ['"""modelx-bridge, bundled from python/modelx_bridge/ by',
             'modelx_bridge.bundle. The package is flat here: the intra-package',
             'imports are stripped, so every name lives in one namespace."""']
    for name in MODULES:
        path = os.path.join(package_dir, name + ".py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        lines.append("")
        lines.append("# " + ("-" * 20) + " " + name + ".py " + ("-" * 20))
        lines.extend(_strip_local_imports(body).splitlines())
    return lines


def _strip_local_imports(source):
    out = []
    in_parens = False
    for line in source.splitlines():
        if in_parens:
            in_parens = ")" not in line
            continue
        stripped = line.lstrip()
        if stripped.startswith("from .") or stripped.startswith("from modelx_bridge"):
            # Replace rather than drop: several of these are the first statement
            # of a function body, which cannot be left empty.
            indent = line[:len(line) - len(stripped)]
            out.append(indent + "pass  # bundled: " + stripped.split("(")[0].rstrip())
            in_parens = stripped.count("(") > stripped.count(")")
            continue
        out.append(line)
    return "\n".join(out)


def render(package_dir):
    lines = package_source(package_dir)
    wheels = ",\n".join("    %r" % url for url in WHEELS)
    body = ["_BRIDGE_SOURCE = ["]
    body.extend("    %r," % line for line in lines)
    body.append("]")
    # Placeholder substitution, NOT %-formatting: the header is Python source
    # that contains its own %r/%s format strings, and a % template collides
    # with them (it did, and the collision is a TypeError at generation time).
    header = (HEADER.replace("@@WHEELS@@", wheels)
                    .replace("@@PYODIDE@@", repr(list(PYODIDE_PACKAGES))))
    footer = FOOTER.replace("@@SUBMODULES@@", repr(list(MODULES)))
    return header + "\n".join(body) + "\n" + footer


def main(argv):
    package_dir = os.path.dirname(os.path.abspath(__file__))
    # The default is lifelib Studio's: the bootstrap its site ships. Anywhere
    # without that site (the public modelx-bridge repo, an installed wheel) there
    # is no sensible place to put it, so the destination must be named.
    site = os.path.join(os.path.dirname(os.path.dirname(package_dir)), "apps", "lite")
    usage = "usage: python -m modelx_bridge.bundle [OUTPUT.py]"
    # An option is never an output path: `--help` used to be written to as a
    # file named "--help" (306 KB, exit 0; reported 2026-10-06).
    if len(argv) > 1 and argv[1].startswith("-"):
        if argv[1] in ("-h", "--help"):
            print(usage + "\nRenders the browser bootstrap (one Python file that "
                  "installs this package in a Pyodide kernel) to OUTPUT.py.")
            return 0
        print("%s\nunknown option %r" % (usage, argv[1]), file=sys.stderr)
        return 2
    from .samples import STUDIO_PYTHON_DIR
    in_studio = os.path.basename(os.path.dirname(package_dir)) == STUDIO_PYTHON_DIR
    if len(argv) > 1:
        dest = argv[1]
    elif in_studio and os.path.isdir(site):
        dest = os.path.join(site, "bridge_bootstrap.py")
    else:
        print("%s\nNo output path given, and there is no default here: the "
              "default is lifelib Studio's apps/lite/bridge_bootstrap.py, and "
              "this is not lifelib Studio's checkout." % usage, file=sys.stderr)
        return 2
    text = render(package_dir)
    with open(dest, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print("wrote %s (%d bytes, %d lines)"
          % (dest, len(text), text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
