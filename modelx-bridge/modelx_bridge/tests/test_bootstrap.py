"""Run apps/lite/bridge_bootstrap.py the way the kernel will, on CPython.

The bootstrap is one IPython cell, not an importable module, so this compiles it
with PyCF_ALLOW_TOP_LEVEL_AWAIT and drives the resulting coroutine -- which is
exactly what IPython does. sys.platform is not "emscripten" here, so the micropip
branch is skipped and everything else runs for real, against fakes for `comm` and
`IPython`.

    python -m modelx_bridge.tests.test_bootstrap

The cell runs with the working directory set to apps/lite/files, because that
directory IS the site's contents root: what lives there is what the kernel sees
at the root of its drive. That makes the shipped BasicTerm_S reachable here by
the same relative path the kernel resolves, instead of by a repo path the kernel
does not have. The browser-side check that the absolute mount point is /drive is
a separate matter -- `modelx_bridge.sample_paths()` answers it from the Console,
and the bootstrap prints it in MODELX_BRIDGE_WARNING when the model is missing.

WHICH BOOTSTRAP, AND WHICH CONTENTS ROOT. In lifelib Studio, after a site build,
this runs apps/lite/bridge_bootstrap.py -- the file the site ships -- from
apps/lite/files. Where there is no such file (a fresh clone before its first
build, or the public modelx-bridge repo, which has no site) it runs a bootstrap
rendered by bundle.render() into a temporary folder, the same function the
build calls. Where there is no apps/lite/files (the public repo, an installed
wheel) the contents root is the package folder itself, whose models/ subfolder
holds the same BasicTerm_S. The first lines of output say which was used.
"""

import ast
import asyncio
import atexit
import io
import json
import linecache
import os
import re
import shutil
import sys
import tempfile
import textwrap

from .fakes import install_fakes
from .shipped import repo_root

REPO = repo_root()
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_BOOTSTRAP = os.path.join(REPO, "apps", "lite", "bridge_bootstrap.py")
SITE_CONTENTS = os.path.join(REPO, "apps", "lite", "files")
#: Set by prepare(): the site's files where they exist, else stand-ins.
BOOTSTRAP = SITE_BOOTSTRAP
CONTENTS_ROOT = SITE_CONTENTS

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-46s %s" % ("ok  " if condition else "FAIL", label, detail))


def _alias_writes_through(module):
    """A host pins the site root with `samples.MODEL_BASE_URL = ...`. In the flat
    bundle that goes through the alias, so it has to reach the real globals or
    the setting is silently lost."""
    before = module.MODEL_BASE_URL
    try:
        module.samples.MODEL_BASE_URL = "https://example.invalid/"
        return module.MODEL_BASE_URL == "https://example.invalid/"
    finally:
        module.samples.MODEL_BASE_URL = before


def run_cell(source, namespace):
    """Compile and run one cell, awaiting it if it came out as a coroutine."""
    code = compile(source, BOOTSTRAP, "exec", ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    captured = io.StringIO()
    stdout, sys.stdout = sys.stdout, captured
    cwd = os.getcwd()
    try:
        os.chdir(CONTENTS_ROOT)             # stand in for the kernel's drive root
        result = eval(code, namespace)      # noqa: S307 -- this is the cell
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    finally:
        os.chdir(cwd)
        sys.stdout = stdout
    return captured.getvalue()


def find_imports(source):
    """pyodide's find_imports, re-implemented (_pyodide/_base.py, v314.0.6).

    This is the function the kernel runs against the RAW cell, through
    pyodide_kernel/kernel.py's `await _load_packages_from_imports(lite_cell)`,
    BEFORE the cell executes. Every name it returns that exists in pyodide-lock
    is loaded by pyodide.loadPackage together with that entry's lock
    dependencies -- which is not micropip, so `deps=False` and a pinned wheel URL
    do not apply. Two details make it bite harder than it looks:

      * it is ast.walk over the whole module, so an import inside a function body
        counts exactly as much as a top-level one;
      * ast.parse accepts the bootstrap's top-level `await` (the "await outside
        function" error is raised when compiling to bytecode, not when parsing),
        so the cell is NOT skipped as unparseable.

    Kept here, rather than asserting on a substring, so this test fails for the
    real reason: what the kernel would download.
    """
    try:
        module = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return []
    found = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return sorted(found)


def prepare():
    """Choose the bootstrap and the contents root (module docstring), and say so."""
    global BOOTSTRAP, CONTENTS_ROOT
    from modelx_bridge import bundle, samples

    rendered = bundle.render(PACKAGE)
    if os.path.isfile(SITE_BOOTSTRAP):
        BOOTSTRAP = SITE_BOOTSTRAP
        with open(BOOTSTRAP, encoding="utf-8") as handle:
            current = handle.read() == rendered
        print("bootstrap: %s, the site build's (%s)"
              % (BOOTSTRAP, "the same as a fresh render of %s" % PACKAGE if current
                 else "STALE: it differs from a fresh render of %s; the site "
                      "build regenerates it" % PACKAGE))
    else:
        folder = tempfile.mkdtemp(prefix="bridge-bootstrap-")
        atexit.register(shutil.rmtree, folder, True)
        BOOTSTRAP = os.path.join(folder, "bridge_bootstrap.py")
        with open(BOOTSTRAP, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        print("bootstrap: %s, rendered by bundle.render() because %s does not exist"
              % (BOOTSTRAP, SITE_BOOTSTRAP))
    CONTENTS_ROOT = SITE_CONTENTS if os.path.isdir(SITE_CONTENTS) else PACKAGE
    print("contents root: %s%s" % (CONTENTS_ROOT, "" if CONTENTS_ROOT == SITE_CONTENTS
                                   else " (the package's own models/ folder is in it)"))
    # The kernel never has the override, and this suite runs the kernel's cell
    # in the environment the kernel has. The bundled lookup ignores it anyway;
    # a check below sets it to prove that.
    override = os.environ.pop(samples.MODELS_ENV, None)
    if override:
        print("$%s=%s is ignored here: the kernel never has it"
              % (samples.MODELS_ENV, override))


def ready_payload(output):
    for line in reversed(output.strip().splitlines()):
        if line.startswith("MODELX_BRIDGE_READY "):
            return json.loads(line[len("MODELX_BRIDGE_READY "):])
    return None


def main():
    prepare()
    manager, shell = install_fakes()
    with open(BOOTSTRAP, encoding="utf-8") as handle:
        source = handle.read()
    check("bootstrap uses top-level await", "await micropip.install" in source,
          "%d bytes" % len(source))
    # -- asttokens: the kernel's copy is the kernel's ----------------------
    #
    # The Pyodide kernel preloads asttokens for its own IPython at EVERY boot.
    # That copy is expected, correct and none of our business. The standing rule
    # is only that WE must never put a second one next to it: modelx declares
    # asttokens as a dependency, and installing it over the already-imported
    # module yields "ImportError: cannot import name AstNode from
    # asttokens.util", which is fatal AND persists in the kernel's storage.
    # Measured. Four checks, so this stops being re-raised as a suspected
    # regression every time someone sees asttokens load in the network panel.
    check("bootstrap never installs asttokens", "asttokens-" not in source,
          "a second asttokens is fatal in the kernel")
    # Searched over the WHOLE file, not just the cell's own statements: the
    # vendored package is carried as repr'd source lines, so an `import
    # asttokens` added to python/modelx_bridge/ would land in this file quoted
    # and would be invisible to an ast walk of the cell.
    check("nothing in the cell or the vendored bridge imports asttokens",
          re.search(r"(import|from)\s+asttokens\b", source) is None
          and "asttokens" not in find_imports(source),
          "the only mention left is the prose warning on line %d"
          % (source[:source.index("asttokens")].count("\n") + 1))
    header = source.split("_BRIDGE_SOURCE")[0]
    check("bootstrap installs networkx by URL, not by name",
          "networkx-3.6.1-py3-none-any.whl" in header
          and "BRIDGE_PYODIDE_PACKAGES = ['pandas']" in header,
          "pyodide-lock's networkx pulls matplotlib; only pandas resolves by name")
    # THE REGRESSION THIS PINS: a single `import networkx` inside a helper cost
    # 8.88 MB of matplotlib, Pillow, contourpy, cycler, fonttools and kiwisolver
    # on every cold boot, because the kernel loads a cell's imports from
    # pyodide-lock before the cell runs. A substring check would not have caught
    # it; this asks the question the kernel asks.
    cell_imports = find_imports(source)
    outside = [name for name in cell_imports
               if name not in sys.stdlib_module_names]
    check("the cell imports nothing outside the standard library",
          outside == [],
          "would be loadPackage'd from pyodide-lock: %s"
          % (", ".join(outside) if outside else "none")
          + " | cell imports: " + ", ".join(cell_imports))
    check("networkx is never named by an import statement",
          "networkx" not in cell_imports and "matplotlib" not in cell_imports,
          "lock networkx 3.6.1 depends on matplotlib")
    check("the installed packages are reached through _bridge_import",
          "_bridge_import(\"modelx\")" in header
          and "_bridge_import(\"networkx\")" in header
          and "micropip = _bridge_import(\"micropip\")" in header)
    check("only one micropip call may resolve dependencies",
          header.count("micropip.install") == 2
          and "micropip.install(BRIDGE_WHEELS, deps=False)" in header,
          "wheels deps=False, pyodide packages by name")
    check("bootstrap ships openpyxl for the Excel-backed model",
          "openpyxl-3.1.5" in source and "et_xmlfile-2.0.0" in source)
    try:
        compile(source, BOOTSTRAP, "exec")
        check("bootstrap is cell-only, not importable", False)
    except SyntaxError as exc:
        check("bootstrap is cell-only, not importable", True, exc.msg)

    # The kernel's own preloaded asttokens, as it is at this moment. This
    # interpreter has one installed too, which is the same shape as the kernel:
    # already importable, already imported. Running the cell must leave it
    # exactly as it found it.
    import asttokens                                    # noqa: E402
    asttokens_before = (asttokens, getattr(asttokens, "__file__", None))

    namespace = {"__name__": "__main__"}
    output = run_cell(source, namespace)
    check("the wheel list names no asttokens",
          not [w for w in namespace["BRIDGE_WHEELS"] if "asttokens" in w]
          and "asttokens" not in namespace["BRIDGE_PYODIDE_PACKAGES"],
          "%d wheels + %s" % (len(namespace["BRIDGE_WHEELS"]),
                              namespace["BRIDGE_PYODIDE_PACKAGES"]))
    check("the kernel's own preloaded asttokens is left untouched",
          sys.modules.get("asttokens") is asttokens_before[0]
          and getattr(sys.modules.get("asttokens"), "__file__", None)
          == asttokens_before[1],
          "same module object, same file: %s"
          % os.path.basename(asttokens_before[1] or "?"))
    check("no warning line: the model content was found",
          "MODELX_BRIDGE_WARNING" not in output,
          output.split("MODELX_BRIDGE_WARNING", 1)[-1][:160]
          if "MODELX_BRIDGE_WARNING" in output else "")
    info = ready_payload(output)
    check("prints MODELX_BRIDGE_READY", info is not None,
          output.strip().splitlines()[-1][:80] if output.strip() else "(no output)")
    check("ready line is the session.info payload",
          info["protocol"] == 0 and info["models"] == [
              # `path` is null: the demo model was opened by sample id, not from
              # a path in storage, so it has no save location until Save As --
              # and from 0.3.0 nothing but a read or a save can give it one
              # (protocol 9.7). `sample` is what makes it the one model the
              # kernel may close unasked; `dirty` is false until user code moves
              # it. `computed` (0.10.0) is 0: booting opens the model and
              # computes nothing.
              {"name": "BasicTerm_S", "revision": 1, "path": None,
               "dirty": False, "sample": "BasicTerm_S", "computed": 0}],
          json.dumps(info["models"]))
    check("the boot block says the sample was opened, and why",
          info["boot"]["reason"] == "sample"
          and info["boot"]["model"] == "BasicTerm_S"
          and info["boot"]["saved"] == [],
          json.dumps(info["boot"]))
    check("the bootstrap calls boot(), not build_sample()",
          "modelx_bridge.boot(BRIDGE_SAMPLE)" in source
          and "modelx_bridge.build_sample(BRIDGE_SAMPLE)" not in source,
          "an unconditional sample squats the name every saved model carries")
    check("hello carries the storage block",
          isinstance(info.get("storage"), dict)
          and info["storage"]["mode"] in ("drive", "temporary", "local"),
          json.dumps({k: info.get("storage", {}).get(k)
                      for k in ("mode", "persistent", "writable")}))
    check("hello advertises the additive features",
          set(info.get("features") or [])
          >= {"table.get", "buffers", "files", "save.backup", "kernel.identity"},
          json.dumps(info.get("features")))
    # WHAT "RESTART PYTHON DOES NOTHING" NEEDED AND DID NOT HAVE: a way to tell,
    # from the frontend, whether a restart produced a new interpreter. The id is
    # minted once per interpreter and survives a bootstrap re-run; see the
    # os.environ comment in methods.py for why a module global cannot do this.
    kernel = info.get("kernel") or {}
    check("the ready line identifies the interpreter answering",
          isinstance(kernel.get("id"), str) and len(kernel["id"]) >= 12
          and kernel.get("boots") >= 1 and isinstance(kernel.get("started"), str)
          and kernel.get("uptime") >= 0,
          json.dumps(kernel))
    check("the storage block says what a plain save will do about backups",
          info["storage"].get("backup_default") in (True, False)
          and isinstance(info["storage"].get("backup_reason"), str)
          and info["storage"].get("max_backups") >= 1,
          json.dumps({k: info["storage"].get(k)
                      for k in ("backup_default", "max_backups")}))
    check("the demo model is the real lifelib model",
          [s["id"] for s in info["samples"]] == ["BasicTerm_S"],
          json.dumps([s["id"] for s in info["samples"]]))

    bridge_module = sys.modules["modelx_bridge"]
    check("vendored module is registered in sys.modules",
          bridge_module.__file__ == "<modelx_bridge bundled>",
          bridge_module.__file__)
    check("vendored source is in linecache for tracebacks",
          "<modelx_bridge bundled>" in linecache.cache,
          "%d lines" % len(linecache.cache["<modelx_bridge bundled>"][2]))
    # The kernel's lookup, in the module the kernel runs. __file__ is not a path
    # in the bundle, so the two desktop candidates (apps/lite/files and the
    # package's own models/) drop out and the order is MODEL_ROOTS alone.
    kernel_order = [os.path.join(root, "models", "BasicTerm_S") if root
                    else os.path.join("models", "BasicTerm_S")
                    for root in bridge_module.MODEL_ROOTS]
    candidates = bridge_module._candidate_dirs("BasicTerm_S")
    check("the bundled lookup is the kernel's: MODEL_ROOTS and nothing else",
          candidates == kernel_order, " > ".join(candidates))
    # $MODELX_BRIDGE_MODELS is the desktop's override. The kernel's list is
    # MODEL_ROOTS exactly, with or without it in os.environ.
    env = bridge_module.samples.MODELS_ENV
    os.environ[env] = CONTENTS_ROOT
    try:
        overridden = bridge_module._candidate_dirs("BasicTerm_S")
    finally:
        del os.environ[env]
    check("...even with $%s set" % env, overridden == kernel_order,
          " > ".join(overridden))
    cwd = os.getcwd()
    try:
        os.chdir(CONTENTS_ROOT)
        try:
            found = bridge_module.find_model_dir("BasicTerm_S")
        except Exception as exc:
            found = "%s: %s" % (type(exc).__name__, exc)
    finally:
        os.chdir(cwd)
    check("from the contents root it finds models/BasicTerm_S, as the kernel does",
          found == os.path.join(CONTENTS_ROOT, "models", "BasicTerm_S"), found)
    # The package's modules address each other as `files.x` / `tables.x`, and
    # the bundle flattens all of them into one namespace -- so those lookups
    # only work because _bridge_install_module binds an alias per submodule.
    # Without it session.info raises NameError on its very first field.
    check("submodule aliases resolve inside the flat bundle",
          bridge_module.files.storage_info()["mode"] in
          ("drive", "temporary", "local")
          and bridge_module.tables.MAX_BUFFER_BYTES > 0,
          "%s / %d" % (bridge_module.files.storage_info()["mode"],
                       bridge_module.tables.MAX_BUFFER_BYTES))
    check("an alias writes through to the flat globals",
          _alias_writes_through(bridge_module), "samples.MODEL_BASE_URL")
    check("only the bridge target is registered",
          list(manager.targets) == ["modelx-bridge"], list(manager.targets))
    check("one post_execute hook",
          len(shell.events.callbacks["post_execute"]) == 1,
          str(len(shell.events.callbacks["post_execute"])))

    # -- the comm handshake and one round trip ----------------------------
    channel = manager.open("modelx-bridge")
    check("comm open pushes hello unprompted",
          len(channel.sent) == 1 and channel.sent[0]["type"] == "hello",
          json.dumps(channel.sent[0])[:70])

    sent = channel.request({"type": "req", "id": "c1", "method": "tree.get",
                            "params": {}})
    check("req over the comm gets exactly one res",
          len(sent) == 1 and sent[0]["type"] == "res" and sent[0]["id"] == "c1",
          "%d cells" % len(sent[0]["result"]["root"]["spaces"][0]["cells"]))

    sent = channel.request({"type": "req", "id": "c2", "method": "value.get",
                            "params": {"nodes": [{"obj": "Projection.pv_net_cf",
                                                  "args": []}]}})
    responses = [m for m in sent if m["type"] == "res"]
    events = [m for m in sent if m["type"] == "evt"]
    value = responses[0]["result"]["values"][0]["value"]
    value = value.get("v") if isinstance(value, dict) else value
    check("an evaluating req answers once and then emits model.changed",
          len(responses) == 1 and len(events) == 1
          and events[0]["params"]["reason"] == "evaluate",
          "pv_net_cf = %r" % value)
    check("pv_net_cf is the native ground truth",
          abs(value - 910.92066093366) <= 1e-12 * 910.92066093366, repr(value))

    channel.sent = []
    shell.run_post_execute()
    check("post_execute after a bridge evaluation emits nothing",
          channel.sent == [], json.dumps(channel.sent))

    import modelx as mx
    mx.get_models()["BasicTerm_S"].Projection[7].pv_net_cf()
    channel.sent = []
    shell.run_post_execute()
    check("post_execute after real user work emits exactly one model.changed",
          len(channel.sent) == 1
          and channel.sent[0]["params"]["reason"] == "execute",
          json.dumps(channel.sent))
    revision_before = channel.sent[0]["params"]["revision"]

    # -- the backup policy, in the ARTEFACT the kernel actually runs -------
    #
    # Everything above about backups is checked against python/modelx_bridge/.
    # What the browser executes is THIS file, and the two are only the same
    # because `python -m modelx_bridge.bundle` was re-run. So save twice through
    # the bundled module and look at the filesystem.
    store = tempfile.mkdtemp(prefix="bootstrap-backup-")
    bridge_module.set_storage_root(store, bridge_module.files.MODE_DRIVE)
    channel.request({"type": "req", "id": "b1", "method": "model.save",
                     "params": {"model": "BasicTerm_S", "path": "/BT"}})
    channel.request({"type": "req", "id": "b2", "method": "model.save",
                     "params": {"model": "BasicTerm_S", "path": "/BT"}})
    again_save = [m for m in channel.request(
        {"type": "req", "id": "b3", "method": "model.save",
         "params": {"model": "BasicTerm_S", "path": "/BT"}})
        if m["type"] == "res"][0]["result"]
    check("the SHIPPED bootstrap saves without leaving a _BAK copy",
          not os.path.isdir(os.path.join(store, "BT_BAK1"))
          and again_save["backup"]["enabled"] is False
          and again_save["backup"]["overwrote"] is True,
          again_save["backup"]["note"])
    check("...and the reply carries the block a UI needs to tell the truth",
          set(again_save["backup"]) >= {"policy", "enabled", "default", "reason",
                                        "overwrote", "kept", "paths", "bytes",
                                        "note", "max"},
          json.dumps(sorted(again_save["backup"])))
    kept_save = [m for m in channel.request(
        {"type": "req", "id": "b4", "method": "model.save",
         "params": {"model": "BasicTerm_S", "path": "/BT", "backup": True}})
        if m["type"] == "res"][0]["result"]
    check("...and backup: true still works through the shipped bundle",
          kept_save["backup"]["kept"] == "/BT_BAK1"
          and os.path.isdir(os.path.join(store, "BT_BAK1")),
          kept_save["backup"]["note"])
    bridge_module.set_storage_root(None)
    shutil.rmtree(store, ignore_errors=True)
    channel.sent = []

    # -- re-run the same cell ---------------------------------------------
    output = run_cell(source, namespace)
    info2 = ready_payload(output)
    check("re-run does not double-register the target",
          list(manager.targets) == ["modelx-bridge"], list(manager.targets))
    check("re-run leaves one post_execute hook",
          len(shell.events.callbacks["post_execute"]) == 1,
          str(len(shell.events.callbacks["post_execute"])))
    check("re-run does not duplicate the demo model",
          list(mx.get_models()) == ["BasicTerm_S"], list(mx.get_models()))
    check("re-run carries the Bridge across",
          info2["models"][0]["revision"] == revision_before,
          "revision %d" % info2["models"][0]["revision"])
    # The distinction the frontend's restart logic has to be able to draw: a
    # bootstrap re-run is NOT a restart. Same interpreter, same id, one more
    # boot. A frontend seeing a DIFFERENT id knows a new kernel is answering and
    # that every model name, handle and revision it cached is stale.
    check("a bootstrap re-run keeps the same kernel id and counts the boot",
          info2["kernel"]["id"] == kernel["id"]
          and info2["kernel"]["boots"] == kernel["boots"] + 1,
          "id %s, boots %d -> %d" % (kernel["id"][:8], kernel["boots"],
                                     info2["kernel"]["boots"]))
    check("the re-run did not import a second asttokens either",
          sys.modules.get("asttokens") is asttokens_before[0],
          "still the kernel's own copy")

    # The finding this is here for: the footer used to hand register_comm only
    # the previous BRIDGE, but the previous ADAPTER owns the open channels. The
    # new adapter took the post_execute hook with an empty comms list, so events
    # stopped reaching a panel connected before the re-run while its requests
    # went on being answered -- silent, and the README invites the re-run.
    mx.get_models()["BasicTerm_S"].Projection[8].pv_net_cf()
    channel.sent = []
    shell.run_post_execute()
    check("events still reach a comm opened BEFORE the re-run",
          len(channel.sent) == 1
          and channel.sent[0]["event"] == "model.changed",
          json.dumps(channel.sent))

    channel2 = manager.open("modelx-bridge")
    sent = channel2.request({"type": "req", "id": "c3", "method": "trace.preds",
                             "params": {"obj": "Projection.net_cf", "args": [12]}})
    result = [m for m in sent if m["type"] == "res"][0]["result"]
    preds = [p["obj"] for p in result["preds"]]
    check("trace.preds still answers after a re-run",
          "Projection.premiums" in preds and "Projection.claims" in preds,
          result["node"]["display"] + " <- " + ", ".join(preds))

    # Separate lists, deliberately: `a.sent = b.sent = []` binds ONE list to
    # both and every fan-out then looks like a duplicate.
    channel.sent = []
    channel2.sent = []
    mx.get_models()["BasicTerm_S"].Projection[9].pv_net_cf()
    shell.run_post_execute()
    check("an event fans out to every open comm",
          len(channel.sent) == 1 and len(channel2.sent) == 1,
          "%d / %d" % (len(channel.sent), len(channel2.sent)))

    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
