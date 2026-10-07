"""Curated sample models (docs/bridge-protocol-v0.md section 6.2).

v0 has no filesystem browsing, so a sample id is the only way to get a model.

The demo model is the REAL lifelib model, not a hand-built imitation. It is
shipped as JupyterLite *contents* -- a directory of modelx serializer output
plus its three .xlsx inputs -- rather than by installing the lifelib wheel:

    BasicTerm_S model directory          324,563 bytes
    lifelib-0.17.1-py3-none-any.whl   27,862,746 bytes  (27.9 MB)

an 86x reduction for the same model, which is the content strategy PLAN 3.7
calls for. Reading it needs `openpyxl` (pure Python, micropip) and `pandas`
(a Pyodide built-in), because the model's three References are modelx pandas
IO specs over those .xlsx files.

The model directory is NOT importable Python and is not on sys.path; it is data
read through ``mx.read_model``. Where it lands in the kernel's filesystem is a
property of the host and turned out NOT to be what the S1 spike recorded, so
``find_model_dir`` searches an explicit, ordered list of roots, falls back to
downloading the files as static assets, and reports every path it tried when it
fails -- see MODEL_ROOTS and ``stage_model``. That list is the browser's alone:
run from a real file (a checkout or an installed wheel) the bridge looks where
$MODELX_BRIDGE_MODELS says and beside its own code, never in the working
directory or under /tmp (``_candidate_dirs``).

The hand-built ``termlife_synthetic`` model stays registered but out of the
gallery: it is the fixture the codec and dispatcher tests run against, and it is
NOT a fallback. Its mortality basis is invented, so it must never be what a
visitor is shown -- a demo that silently degrades to a made-up model is worse
than one that says the content is missing, which is what the bootstrap does.

Formulas for the synthetic model are passed to modelx as *source strings*, not
as Python functions, because this module is exec'd from a string in the Pyodide
kernel and inspect.getsource() cannot recover a body that never came from a file.
"""

import os
import sys

from . import files
from .errors import not_found

_BUILDERS = {}
_ALIASES = {}

#: Open model name -> the sample id it was built from, for every sample this
#: session opened (section 6.2). A model that one request rebuilds byte for byte
#: is the ONLY model the kernel may close on the user's behalf, which is what
#: model.open needs when the demo sample is squatting on the name a file wants
#: (section 9.4) and what model.close needs to know it can skip `force`
#: (section 9.4.1). Kept here rather than on the Bridge because the BOOTSTRAP
#: opens the first one, before any Bridge exists.
_OPENED_SAMPLES = {}

#: What stage_model downloaded this session, for sample_paths() to report.
STAGED = {}

#: Directory name, under each search root, that holds the shipped models.
MODELS_DIRNAME = "models"

#: Where staged content is written when nothing is mounted. Under /tmp, which is
#: MEMFS: it is thrown away with the kernel and never persisted, so a corrupt
#: download cannot brick a later boot the way a bad install in IndexedDB does.
STAGE_ROOT = "/tmp/lifelib-studio"

#: Ordered search roots for shipped model content. The first entry that holds
#: ``<root>/<dirname>/_system.json`` wins.
#:
#:   /drive          JupyterLite's DriveFS mount, when there IS one.
#:   ""              the kernel's cwd, whatever JupyterLite set it to.
#:   /, /files       a drive mounted somewhere other than /drive.
#:   STAGE_ROOT      what ``stage_model`` downloaded on an earlier call.
#:
#: MEASURED, 2026-09-22, JupyterLite 0.8.3 + jupyterlite-pyodide-kernel, both the
#: `lab` and `repl` apps, from a Console in the running site:
#:
#:     sys.platform  'emscripten'
#:     os.getcwd()   '/home/pyodide'
#:     os.listdir('/')  ['tmp', 'home', 'dev', 'proc', 'lib', 'share']
#:     os.path.isdir('/drive')  False
#:
#: There was NO /drive. The service worker failed to register ("An unknown error
#: occurred when fetching the script"), and DriveFS is mounted by the service
#: worker, so the kernel could not see the site's contents as files at all. That
#: is why ``find_model_dir`` falls through to ``stage_model``: the same bytes are
#: served as ordinary static files under <site>/files/, which needs no service
#: worker, no cross-origin isolation and no contents API.
#:
#: That failure was JupyterLite losing its own registration race, and
#: serviceworker.ts has handled it since later the same day (PLAN 3.7); a
#: cross-origin-isolated page reaches /drive without the service worker at all.
#: MEASURED AGAIN, 2026-09-27, the built site in headless Chromium, Pyodide
#: 314.0.6 / Python 3.14.2, with and without ``serve_site.py --isolate``:
#:
#:     os.getcwd()                    '/drive'
#:     find_model_dir('BasicTerm_S')  '/drive/models/BasicTerm_S'
#:     STAGED                         {}     (stage_model never ran)
#:
#: So /drive is the path that runs, and stage_model is the fallback for a page
#: where /drive is absent -- the lost race, which is still possible.
#:
#: The BROWSER's roots only. They are consulted when the package runs bundled,
#: and never when it runs from a real file, where "" is whatever folder the
#: process happened to start in (see ``_candidate_dirs`` for what that let
#: through). A host may append to this list before the first ``build_sample``
#: call in the browser kernel.
MODEL_ROOTS = ["/drive", "", "/", "/files", "/drive/files", STAGE_ROOT]

#: Path under the site root where the model content is served as static files.
MODEL_URL_PREFIX = "files/" + MODELS_DIRNAME

#: Set to pin the site root instead of deriving it from the kernel worker's URL.
MODEL_BASE_URL = ""

#: Every file of each shipped model, relative to its directory. Explicit because
#: an HTTP fetch cannot list a directory, and checked against the filesystem by
#: tests/test_model.py so it cannot drift from what is shipped.
MODEL_MANIFEST = {
    "BasicTerm_S": [
        "_system.json",
        "__init__.py",
        "Projection/__init__.py",
        "_data/data.pickle",
        "disc_rate_ann.xlsx",
        "mort_table.xlsx",
        "model_point_table.xlsx",
    ],
}


def register_sample(sample_id, model_name, builder, title=None,
                    summary=None, gallery=True):
    """Register a sample. ``builder(model_name)`` must return an open Model."""
    _BUILDERS[sample_id] = {
        "id": sample_id,
        "model": model_name,
        "builder": builder,
        "title": title or model_name,
        "summary": summary or "",
        "gallery": bool(gallery),
    }


def register_alias(alias, sample_id):
    """Point an old sample id at a current one. Aliases are never in the gallery."""
    _ALIASES[alias] = sample_id


def sample_ids():
    """Every id that ``build_sample`` accepts, aliases included."""
    return sorted(set(_BUILDERS) | set(_ALIASES))


def sample_catalog():
    """The gallery: what a UI should offer, in display order."""
    return [{"id": e["id"], "model": e["model"], "title": e["title"],
             "summary": e["summary"]}
            for e in sorted(_BUILDERS.values(), key=lambda e: e["id"])
            if e["gallery"]]


def build_sample(sample_id):
    """Open a sample, or return it if already open. Idempotent (section 6.2)."""
    import modelx as mx

    entry = _BUILDERS.get(_ALIASES.get(sample_id, sample_id))
    if entry is None:
        raise not_found(
            "no sample named %r; have %s" % (sample_id, ", ".join(sample_ids())),
            sample=sample_id, available=sample_ids())
    existing = mx.get_models().get(entry["model"])
    if existing is not None:
        # Recorded on this branch too, and deliberately: re-running the bootstrap
        # cell re-execs this module, so the registry is empty again while the
        # model it built is still open. Claiming it back is self-healing, and it
        # cannot over-claim -- "was built from a sample" only ever *weakens* the
        # protection around a model that has neither been saved nor touched
        # (section 9.4.1), and a model the user opened from a file has a save
        # location, which outranks this (methods.Bridge._discardable).
        _OPENED_SAMPLES[existing.name] = entry["id"]
        return existing
    model = entry["builder"](entry["model"])
    _OPENED_SAMPLES[model.name] = entry["id"]
    return model


def sample_of(model_name):
    """The sample id this model was built from, or None (section 6.2)."""
    return _OPENED_SAMPLES.get(model_name)


def note_sample(model_name, sample_id):
    """Record that ``model_name`` was built from ``sample_id``."""
    _OPENED_SAMPLES[model_name] = sample_id


def forget_sample(model_name):
    """Drop the sample record for a model that was closed, or replaced by a file.

    Called whenever a model of that name stops being the sample -- it was closed,
    or `model.open` read a file into that name. A stale entry here would tell
    `model.close` a user's model is rebuildable when it is not.
    """
    _OPENED_SAMPLES.pop(model_name, None)


def opened_samples():
    """{model name: sample id} for every sample this session opened."""
    return dict(_OPENED_SAMPLES)


# --- what boots (section 9.7) -------------------------------------------
#
# The bootstrap used to call build_sample() unconditionally, so every kernel --
# including a returning visitor's -- came up with a pristine BasicTerm_S holding
# the name BasicTerm_S. Since the only shipped sample is also what everything a
# visitor saves is derived from, that name is exactly the one their own files
# carry, and the squat is what made "save, come back, re-open" unreachable.
#
# Opening nothing when the visitor has their own models is the honest default:
# the Explorer's empty state is designed, the Files panel lists what they saved,
# and `boot` in session.info says why so the UI can greet them with it.

#: What ``boot`` decided this session; reported as session.info.boot (9.7).
_BOOT = {"reason": "not-run", "sample": None, "model": None, "saved": []}

#: Bounds on the boot scan. It runs before the visitor sees anything, on the one
#: kernel thread, so it is depth-1 and capped rather than a walk.
MAX_BOOT_DIRS = 40
MAX_BOOT_MODELS = 24


def boot_report():
    """What the bootstrap opened, and why (section 6.1's `boot` block)."""
    report = dict(_BOOT)
    report["saved"] = list(report.get("saved") or ())
    return report


def shipped_paths():
    """Virtual paths the SITE's own model content occupies.

    The demo's model directory is JupyterLite *contents*: when the drive mounts,
    ``/models/BasicTerm_S`` is already there on a first visit, having been put
    there by the site rather than by the visitor. The boot scan must not read it
    as "this visitor has saved work" -- that would mean the sample never opens
    for anyone -- so these paths are excluded by name.

    Verified against the built site: dist/api/contents/all.json lists ``models``
    at the contents ROOT, so ``/models/<dirname>`` is where it lands. The
    ``/files`` spelling is here because MODEL_ROOTS hedges on ``/drive/files``
    for hosts that mount contents one level down; the scan is depth-1 and would
    not reach it today, and this keeps that a layout question rather than a
    behaviour change if the depth ever moves.

    A visitor who saves OVER one of these paths is the one case this excludes
    something of theirs -- and harmlessly: read_shipped_model reads that same
    path, so the sample they get IS their version.
    """
    return set(prefix + "/" + MODELS_DIRNAME + "/" + dirname
               for dirname in MODEL_MANIFEST for prefix in ("", "/files"))


def saved_models(limit=MAX_BOOT_MODELS):
    """The models THIS VISITOR saved into storage, shipped content excluded.

    Scans the storage root and one level below it -- where Save As, import and
    export put things -- and stops at MAX_BOOT_DIRS directories or ``limit``
    models. Never raises: a storage that cannot be listed is reported as no
    saved models, which is the same answer as an empty one and keeps a
    filesystem problem from stopping the kernel booting.
    """
    try:
        info = files.storage_info()
        if not info.get("root"):
            return []
        shipped = shipped_paths()
        found, scanned, seen = [], 0, set()
        queue = ["/"]
        while queue and len(found) < limit and scanned < MAX_BOOT_DIRS:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            scanned += 1
            try:
                listing = files.list_dir(path, info)
            except Exception:
                continue
            for entry in listing["entries"]:
                if entry.get("is_model"):
                    if entry["path"] in shipped:
                        continue
                    # modelx's own backup rotation (section 9.8). It is a model
                    # directory and it is the visitor's data, but it is not work
                    # they chose to save under that name, and offering
                    # "BasicTerm_S_BAK2" on the Welcome screen beside
                    # "BasicTerm_S" is noise at exactly the moment a returning
                    # visitor needs to recognise their own file.
                    if entry.get("is_backup"):
                        continue
                    found.append({
                        "path": entry["path"],
                        "name": entry["name"],
                        "model_name": entry.get("model_name"),
                        "model_format": entry.get("model_format"),
                        "modified": entry.get("modified"),
                    })
                    if len(found) >= limit:
                        break
                elif entry.get("kind") == "directory" and path == "/":
                    queue.append(entry["path"])       # depth 1, deliberately
        return found
    except BaseException:
        return []


def boot(sample_id, scan=True):
    """What a kernel opens at startup (section 9.7). Returns the boot report.

    Three answers, in order:

      models already open   the bootstrap cell was re-run. Touch nothing.
      saved models found    this visitor has their own work in storage. Open
                            NOTHING: a pristine sample would take the very name
                            their files carry, and reuse-by-name is what used to
                            hand them the sample instead of the file (9.4).
      otherwise             open the sample. A first visit, and the demo.

    Raises whatever build_sample raises, after recording the failure, so the
    bootstrap keeps its own warning path: a missing sample is a content problem,
    not a reason to leave the visitor with no kernel.
    """
    import modelx as mx

    global _BOOT
    already = sorted(mx.get_models())
    if already:
        _BOOT = {"reason": "models-open", "sample": None, "model": None,
                 "saved": [], "models": already}
        return boot_report()

    saved = saved_models() if scan else []
    if saved:
        _BOOT = {"reason": "saved-models", "sample": None, "model": None,
                 "saved": saved}
        return boot_report()

    try:
        model = build_sample(sample_id)
    except BaseException as exc:
        _BOOT = {"reason": "failed", "sample": sample_id, "model": None,
                 "saved": [], "error": "%s: %s" % (type(exc).__name__, exc)}
        raise
    _BOOT = {"reason": "sample", "sample": sample_id, "model": model.name,
             "saved": []}
    return boot_report()


# --- shipped model content ----------------------------------------------

#: Environment variable naming a folder that holds ``<dirname>/`` for each
#: shipped model. Read on every lookup, not at import, so a test can set it.
MODELS_ENV = "MODELX_BRIDGE_MODELS"

#: The folder that holds the package in lifelib Studio's checkout
#: (<repo>/python/modelx_bridge). Only there does the lookup read the copy the
#: site ships; see _candidate_dirs.
STUDIO_PYTHON_DIR = "python"


def _candidate_dirs(dirname):
    """Every path ``find_model_dir`` will try, in order, with no filesystem access.

    Two lists that never mix, chosen by whether this module was loaded from a
    real file. The first existing model directory wins.

    BUNDLED, in the browser kernel: the bootstrap exec's the package from a
    string with ``__file__ == "<modelx_bridge bundled>"``. The candidates are
    MODEL_ROOTS, in order, and nothing else -- not even $MODELX_BRIDGE_MODELS.
    MEASURED 2026-10-06, the built site on the --pages rehearsal in headless
    Chromium: os.environ has no MODELX_BRIDGE_MODELS, this returns the six
    MODEL_ROOTS paths, and find_model_dir('BasicTerm_S') is
    '/drive/models/BasicTerm_S' with STAGED {}, as MODEL_ROOTS' 2026-09-27
    measurement found it. tests/test_bootstrap.py pins this list.

    FROM A REAL FILE, anywhere else (CPython, a checkout or an installed wheel):

      1. $MODELX_BRIDGE_MODELS/<dirname>
             An explicit override, so a desktop user or a test can point the
             bridge at a copy of their choosing. First because it is the only
             entry a person sets on purpose. It is a candidate like the others,
             not a pin: if it holds no model the lookup goes on, and the
             not_found message lists it first among the paths tried.
      2. <repo>/apps/lite/files/models/<dirname>
             lifelib Studio's checkout, and ONLY when the package sits in that
             checkout's layout, <repo>/python/modelx_bridge (the folder above
             the package is named "python"; a name check, so this function
             still touches no file). There it is the bytes the site ships, read
             straight out of the repo, so the suites exercise the real model
             with no site build. Before 3 so that, in lifelib Studio, the copy
             the site ships is the copy that is tested. Without the name check
             <repo> was simply two folders above the package, which for
             `pip install --target DIR` is DIR's parent: a folder outside the
             install that anyone could populate (reported 2026-10-06 by the
             re-verification of the 0.10.0 wheel). An installed wheel
             (site-packages/), a --target folder and the public repo
             (modelx-bridge/modelx_bridge) never take this entry.
      3. <package>/models/<dirname>
             The package's own data folder. It exists only where lifelib
             Studio's scripts/sync_modelx_bridge.py copied BasicTerm_S into it:
             the public modelx-bridge repo, and the wheel built from that.
             lifelib Studio's python/modelx_bridge has no models/ folder, so in
             its checkout this never shadows 2.

    and NEVER MODEL_ROOTS or STAGE_ROOT. Those are the browser's: its cwd and
    its /tmp are the kernel's own MEMFS. On a desktop the cwd is wherever the
    process was started and /tmp/lifelib-studio is a predictable path in a
    world-writable folder, so either one let a stranger's folder answer for
    "the shipped sample" -- and reading a model runs its pickle, which modelx's
    unpickler does not restrict. MEASURED 2026-10-06, the 0.10.0 wheel in a
    fresh CPython 3.12 venv, when MODEL_ROOTS still came first here: started
    from a folder holding models/BasicTerm_S with pv_net_cf's body changed to
    ``return 12345.0``, modelx-mcp announced "the shipped sample BasicTerm_S"
    and calculated pv_net_cf() = 12345.0 (910.92066093366 from any other
    folder), and a _data/data.pickle planted there ran at launch and touched a
    marker file. tests/test_model.py opens the sample from such a folder.
    """
    seen, out = set(), []

    def add(path):
        if path and path not in seen:
            seen.add(path)
            out.append(path)

    here = globals().get("__file__") or ""
    if not (os.path.isabs(here) or os.sep in here):
        for root in MODEL_ROOTS:
            add(os.path.join(root, MODELS_DIRNAME, dirname) if root
                else os.path.join(MODELS_DIRNAME, dirname))
        return out
    override = os.environ.get(MODELS_ENV, "")
    if override:
        add(os.path.join(override, dirname))
    package = os.path.dirname(os.path.abspath(here))
    if os.path.basename(os.path.dirname(package)) == STUDIO_PYTHON_DIR:
        repo = os.path.dirname(os.path.dirname(package))
        add(os.path.join(repo, "apps", "lite", "files", MODELS_DIRNAME, dirname))
    add(os.path.join(package, MODELS_DIRNAME, dirname))
    return out


def shipped_model_dir(dirname):
    """The first local copy of a shipped model, as an absolute path, or None.

    The same search as ``find_model_dir`` without its fallback: it never stages,
    never downloads and never raises. It is what the test suites use to find the
    sample, in lifelib Studio's layout and in the public modelx-bridge repo's.
    """
    for path in _candidate_dirs(dirname):
        if _is_model_dir(path):
            return os.path.abspath(path)
    return None


def _is_model_dir(path):
    try:
        return os.path.isfile(os.path.join(path, "_system.json"))
    except Exception:
        return False


def site_base_url():
    """The site root as the kernel worker sees it, with a trailing slash.

    `js.location.href` inside the worker is the kernel worker script, measured as

        <site>/extensions/@jupyterlite/pyodide-kernel-extension/static/
        comlink.worker.<hash>.js

    so cutting at the first path segment that is part of the app's own layout
    gives the site root. Set ``MODEL_BASE_URL`` before the first call to override.
    """
    if MODEL_BASE_URL:
        return MODEL_BASE_URL if MODEL_BASE_URL.endswith("/") else MODEL_BASE_URL + "/"
    import js
    href = str(js.location.href)
    for marker in ("/extensions/", "/build/", "/static/"):
        if marker in href:
            return href.split(marker, 1)[0] + "/"
    return href.rsplit("/", 1)[0] + "/"


def _http_get(url):
    """One synchronous binary GET, from inside the kernel worker.

    Synchronous XMLHttpRequest with responseType='arraybuffer' is allowed in a
    Worker (it is not on the main thread), and it is what lets a plain function
    like ``build_sample`` fetch content without the whole call chain becoming a
    coroutine. VERIFIED in the running kernel: 200, 284,765 bytes for
    model_point_table.xlsx.
    """
    import js

    xhr = js.XMLHttpRequest.new()
    xhr.open("GET", url, False)
    try:
        xhr.responseType = "arraybuffer"
    except Exception:
        pass                       # refused (main thread): fall back to text
    xhr.send(None)
    status = int(xhr.status or 0)
    if status not in (0, 200):
        raise OSError("HTTP %d for %s" % (status, url))
    response = xhr.response
    if response is None:
        return str(xhr.responseText).encode("utf-8")
    return response.to_py().tobytes()


def stage_model(dirname, base=None):
    """Download a shipped model into the kernel's own filesystem, and return the path.

    The fallback for a kernel with no /drive (see MODEL_ROOTS: when the drive
    mounts, the model is read from it and this never runs). It needs no service
    worker, no contents API and no cross-origin isolation -- only that the site
    serves its own files, which is the one thing a static site is guaranteed to do.
    """
    dest = os.path.join(STAGE_ROOT, MODELS_DIRNAME, dirname)
    if _is_model_dir(dest):
        return dest
    files = MODEL_MANIFEST.get(dirname)
    if not files:
        raise not_found("no manifest for model %r; have %s"
                        % (dirname, ", ".join(sorted(MODEL_MANIFEST))),
                        dirname=dirname)
    base = base or site_base_url()
    prefix = base + MODEL_URL_PREFIX + "/" + dirname + "/"
    total = 0
    for rel in files:
        data = _http_get(prefix + rel)
        total += len(data)
        path = os.path.join(dest, *rel.split("/"))
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
    if not _is_model_dir(dest):
        raise not_found("staged %s from %s but it is not a model directory"
                        % (dirname, prefix), dirname=dirname, url=prefix)
    STAGED[dirname] = {"path": dest, "url": prefix, "bytes": total,
                       "files": len(files)}
    return dest


def find_model_dir(dirname):
    """Locate a shipped model directory, downloading it if nothing is mounted.

    Raises not_found naming every path tried AND the URL it fell back to, so the
    failure says where to look instead of "no such file".
    """
    found = shipped_model_dir(dirname)
    if found:
        return found
    tried = _candidate_dirs(dirname)
    if sys.platform == "emscripten":
        try:
            return stage_model(dirname)
        except Exception as exc:
            raise not_found(
                "the %s model content is not mounted in this kernel (looked in "
                "%s) and downloading it failed: %s: %s"
                % (dirname, ", ".join(repr(p) for p in tried),
                   type(exc).__name__, exc),
                dirname=dirname, tried=tried)
    raise not_found(
        "the %s model content is not in this kernel's filesystem; looked in %s"
        % (dirname, ", ".join(repr(p) for p in tried)),
        dirname=dirname, tried=tried)


def sample_paths():
    """Diagnostic: what the kernel can actually see, right now.

    Called from the Console this answers "where did the contents land?" without
    guessing -- which is the only way MODEL_ROOTS stays honest. It is also what
    the bootstrap prints in MODELX_BRIDGE_WARNING when the model does not open.
    """
    out = []
    for path in _candidate_dirs("BasicTerm_S"):
        entry = {"path": path, "exists": False, "is_model_dir": False,
                 "listing": None}
        try:
            entry["exists"] = os.path.exists(path)
            entry["is_model_dir"] = _is_model_dir(path)
            if entry["exists"]:
                entry["listing"] = sorted(os.listdir(path))[:20]
        except Exception as exc:
            entry["error"] = repr(exc)
        out.append(entry)
    info = {"cwd": os.getcwd(), "platform": sys.platform,
            "root_listing": None, "base_url": None, "staged": dict(STAGED),
            "candidates": out}
    try:
        info["root_listing"] = sorted(os.listdir("/"))
    except Exception as exc:
        info["root_listing"] = repr(exc)
    try:
        info["base_url"] = site_base_url()
    except Exception as exc:
        info["base_url"] = repr(exc)
    return info


def read_shipped_model(dirname, model_name):
    """``mx.read_model`` a shipped model directory under its registered name.

    If a MOUNTED copy is found but cannot be read through, the download path is
    tried once more before giving up. A drive that can list a directory but not
    serve every byte in it is a real shape of failure here -- the contents API
    JSON that a Windows build writes contains backslashes in nested paths -- and
    it would otherwise surface as an unreadable .xlsx from deep inside modelx.
    """
    import modelx as mx

    path = find_model_dir(dirname)
    try:
        return mx.read_model(path, name=model_name)
    except Exception as exc:
        if sys.platform != "emscripten":
            raise
        staged = stage_model(dirname)
        if os.path.normpath(staged) == os.path.normpath(path):
            raise
        print("modelx-bridge: reading %s from %s failed (%s: %s); retrying from "
              "the downloaded copy at %s"
              % (dirname, path, type(exc).__name__, exc, staged), file=sys.stderr)
        half_open = mx.get_models().get(model_name)
        if half_open is not None:
            half_open.close()
        return mx.read_model(staged, name=model_name)


def build_basicterm_s(model_name="BasicTerm_S"):
    """lifelib basiclife/BasicTerm_S, read from the shipped model content."""
    return read_shipped_model("BasicTerm_S", model_name)


# The canonical id is "BasicTerm_S". That is what docs/bridge-protocol-v0.md
# section 6.2's normative example sends, what apps/lite/jupyter-lite.json's
# `lifelibStudioSamples` lists, and what the Welcome gallery's cards request.
# Keeping the id equal to the model name is also what makes the `available`
# list on a not_found usable directly as gallery content.
register_sample(
    "BasicTerm_S", "BasicTerm_S", build_basicterm_s,
    title="BasicTerm_S",
    summary="lifelib's simple term life projection: one model point at a time, "
            "monthly cashflows, real mortality, lapse and discount tables read "
            "from Excel.")

# Aliases, never in the gallery. "termlife" is what the panel asked for before
# the real model shipped; "basicterm_s" is the lower-case spelling this registry
# briefly used while the frontend and the kernel were built in parallel. Both
# resolve to the real model rather than failing -- and never to the invented one.
register_alias("termlife", "BasicTerm_S")
register_alias("basicterm_s", "BasicTerm_S")


# --- the synthetic fixture ----------------------------------------------
#
# NOT a product surface: invented mortality, invented model points, premiums
# tuned so every point has a positive pv_net_cf. It exists so the codec and
# dispatcher tests have a model with no file dependencies, and so the bootstrap
# still has something to open if the shipped content is missing.

MODEL_POINTS = {
    1: {"issue_age": 47, "sex": "M", "sum_assured": 500000,
        "policy_term": 10, "premium_pp": 320},
    2: {"issue_age": 29, "sex": "M", "sum_assured": 500000,
        "policy_term": 20, "premium_pp": 98},
    3: {"issue_age": 51, "sex": "F", "sum_assured": 250000,
        "policy_term": 10, "premium_pp": 200},
    4: {"issue_age": 32, "sex": "F", "sum_assured": 750000,
        "policy_term": 15, "premium_pp": 130},
    5: {"issue_age": 28, "sex": "M", "sum_assured": 1000000,
        "policy_term": 20, "premium_pp": 172},
}

# Annual mortality per 1000, by attained age. INVENTED. Test fixture only.
MORT_TABLE = dict(
    (age, round(0.00035 * 1.098 ** (age - 20), 6)) for age in range(18, 101))

_FORMULAS = [
    ("model_point", '''\
def model_point():
    """The model point being projected.

    A dict of the policy's attributes, picked out of ``model_points`` by the
    Space parameter ``point_id``.
    """
    return model_points[point_id]
'''),
    ("proj_len", '''\
def proj_len():
    """Number of monthly projection steps, including t=0."""
    return 12 * model_point()["policy_term"] + 1
'''),
    ("mort_rate", '''\
def mort_rate(t):
    """Annual mortality rate at the attained age in month ``t``."""
    age = model_point()["issue_age"] + t // 12
    factor = 1.0 if model_point()["sex"] == "M" else 0.85
    return mort_table[age] * factor
'''),
    ("lapse_rate", '''\
def lapse_rate(t):
    """Annual lapse rate, highest in the first policy year."""
    year = t // 12
    return 0.10 if year == 0 else max(0.02, 0.10 - 0.01 * year)
'''),
    ("pols_if", '''\
def pols_if(t):
    """Policies in force at the start of month ``t``.

    Time-recursive: the survivors of month ``t-1`` less that month's deaths and
    lapses, and zero once the policy term is over.
    """
    if t == 0:
        return 1.0
    elif t >= proj_len():
        return 0.0
    else:
        return pols_if(t - 1) - pols_death(t - 1) - pols_lapse(t - 1)
'''),
    ("pols_death", '''\
def pols_death(t):
    """Policies that die during month ``t``."""
    return pols_if(t) * mort_rate(t) / 12
'''),
    ("pols_lapse", '''\
def pols_lapse(t):
    """Policies that lapse during month ``t``, after deaths."""
    return (pols_if(t) - pols_death(t)) * (1 - (1 - lapse_rate(t)) ** (1 / 12))
'''),
    ("claims", '''\
def claims(t):
    """Death claims paid in month ``t``."""
    return model_point()["sum_assured"] * pols_death(t)
'''),
    ("premiums", '''\
def premiums(t):
    """Premiums collected in month ``t``."""
    return model_point()["premium_pp"] * pols_if(t)
'''),
    ("expenses", '''\
def expenses(t):
    """Acquisition expense at t=0 plus a maintenance expense thereafter."""
    acq = 300.0 if t == 0 else 0.0
    return acq + expense_pp * pols_if(t)
'''),
    ("disc_factor", '''\
def disc_factor(t):
    """Monthly discount factor. Time-recursive off ``disc_rate``."""
    if t == 0:
        return 1.0
    else:
        return disc_factor(t - 1) / (1 + disc_rate / 12)
'''),
    ("net_cf", '''\
def net_cf(t):
    """Net cashflow in month ``t``: premiums less claims and expenses."""
    return premiums(t) - claims(t) - expenses(t)
'''),
    ("pv_net_cf", '''\
def pv_net_cf():
    """Present value of the net cashflows over the whole projection."""
    return sum(net_cf(t) * disc_factor(t) for t in range(proj_len()))
'''),
]


def build_termlife(model_name="TermLife_S"):
    """A small monthly term-life projection, built entirely with modelx calls."""
    import modelx as mx

    model = mx.new_model(name=model_name)
    space = model.new_space("Projection", formula="lambda point_id: None")
    # point_id is both the Space parameter and a Reference with a default, so the
    # base Space is evaluable on its own and Projection[3] overrides it -- the
    # same arrangement lifelib's BasicTerm_S uses.
    space.point_id = 1
    space.model_points = MODEL_POINTS
    space.mort_table = MORT_TABLE
    space.disc_rate = 0.03
    space.expense_pp = 5.0
    for name, source in _FORMULAS:
        space.new_cells(name=name, formula=source)
    return model


register_sample("termlife_synthetic", "TermLife_S", build_termlife,
                title="TermLife_S (synthetic fixture)",
                summary="Hand-built, invented mortality. Test fixture, not a demo.",
                gallery=False)
