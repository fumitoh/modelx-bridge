"""Storage detection and path handling for the Files workflow
(docs/bridge-protocol-v0.md section 9).

THE PROBLEM THIS MODULE EXISTS FOR, stated plainly because every design choice
below follows from it:

JupyterLite mounts the site's contents at ``/drive`` from its **service worker**,
and it unregisters then immediately re-registers that worker on every page load.
It intermittently loses that race. When it loses, ``/drive`` is simply ABSENT and
the kernel has no persistent filesystem at all -- silently. Measured 2026-09-22;
registering the identical script by hand succeeds every time, so the script, its
MIME type and its scope are all fine. The defect is registration reliability, not
capability: when the worker IS registered the full round trip works, proven, not
assumed (``read_model`` -> native-identical values -> ``write_model`` -> re-read
-> identical value, 314,270 bytes).

So the kernel must work in BOTH worlds and must never lie about which one it is
in. Three decisions implement that:

1. **One virtual path space.** The wire speaks paths rooted at ``/`` --
   ``/models/BasicTerm_S`` -- never real kernel paths. In ``drive`` mode that
   resolves under ``/drive``; in ``temporary`` mode under ``/tmp/lifelib-studio``,
   which is MEMFS and dies with the tab. The frontend does no path arithmetic and
   the same request works in either mode.

2. **Every reply carries the storage block**, with ``persistent`` true or false
   and a ``reason`` when it is false. A UI that shows "Saved" over a write to
   MEMFS is the worst bug this product could ship (design/UI-DESIGN.md 4.1), so
   the truth travels with the answer rather than having to be asked for.

3. **The probe re-runs while degraded.** A worker that wins the race on a retry,
   or registers late, flips the mode on the next file operation without anyone
   having to ask. Once ``drive`` is up we cache it -- a mounted drive does not
   spontaneously unmount, and the probe costs a write + unlink.

``storage_probe`` writes and deletes a dot file rather than trusting ``isdir``:
a mount that lists but cannot be written to would otherwise be reported ready and
fail at save time, which is the same lie one step later.
"""

import os
import re
import sys
import time

from .errors import bad_request, not_found

#: Where JupyterLite's service worker mounts the site contents.
DRIVE_ROOT = "/drive"

#: Where we fall back when there is no drive. Under /tmp, which is MEMFS: thrown
#: away with the kernel, never persisted, so a bad file cannot brick a later boot.
#: MUST equal samples.STAGE_ROOT -- the staged model content lands there and the
#: Files panel has to be able to see it. Asserted by tests/test_files.py rather
#: than imported, so the two modules stay independent in the flat bundle.
FALLBACK_ROOT = "/tmp/lifelib-studio"

MODE_DRIVE = "drive"
MODE_TEMPORARY = "temporary"
MODE_LOCAL = "local"

#: Are we in the browser? A module-level flag rather than an inline
#: ``sys.platform`` test so tests can exercise BOTH browser branches -- drive
#: present and drive absent -- on a desktop, with DRIVE_ROOT and FALLBACK_ROOT
#: pointed at temporary directories. The race this module exists for cannot be
#: reproduced any other way, and a branch nothing can reach is a branch nothing
#: has checked.
EMSCRIPTEN = sys.platform == "emscripten"

#: Name of the file modelx's serializer writes at the root of every model.
MODEL_MARKER = "_system.json"

#: Probe file; dot-prefixed, and listings hide dotfiles, so a failed unlink
#: cannot show up as a mystery entry in the user's Files panel.
PROBE_NAME = ".lifelib-studio-probe"

#: Stop a directory-size walk here and report the size as unknown. A listing must
#: stay O(what is shown); an accidental recursion into a huge tree must not hang
#: the only kernel thread the UI has.
MAX_WALK_FILES = 5000

#: Cap on how many entries one files.list returns.
MAX_ENTRIES = 1000

#: modelx's backup naming. ``write_model``/``zip_model`` take ``backup=True`` by
#: default and, when something is already at the target, rotate it to
#: ``<path>_BAK1``, pushing an existing ``_BAK1`` to ``_BAK2`` and so on up to
#: ``modelx.serialize.DEFAULT_MAX_BACKUPS`` (3), after which the oldest is
#: deleted. The suffix is appended to the WHOLE path, extension included, so a
#: zip export backs up as ``BasicTerm_S.zip_BAK1`` -- which is why this matches on
#: the full basename rather than on a stem.
BACKUP_RE = re.compile(r"^(?P<base>.+)_BAK(?P<slot>\d+)$")

#: How many rotation slots ``backup_slots`` looks for. modelx keeps 3; scanning a
#: few more costs three ``os.path.exists`` calls and means a host that raised the
#: limit still gets the truth instead of a count that is quietly short.
BACKUP_SLOTS = 8

#: Set by ``set_storage_root`` -- a host or a test pinning the root explicitly.
#: On CPython this is the only way to get a root at all: there is no /drive on a
#: desktop and /tmp/lifelib-studio is not a sensible default outside a browser.
_ROOT_OVERRIDE = ""
_ROOT_MODE = MODE_LOCAL

_CACHE = None


def set_storage_root(path, mode=MODE_LOCAL):
    """Pin the storage root. Clears the cached probe. ``None`` unpins."""
    global _ROOT_OVERRIDE, _ROOT_MODE, _CACHE
    _ROOT_OVERRIDE = os.path.abspath(path) if path else ""
    _ROOT_MODE = mode
    _CACHE = None
    return _ROOT_OVERRIDE


def forget_storage():
    """Drop the cached probe, so the next call re-checks the filesystem."""
    global _CACHE
    _CACHE = None


def _writable(root):
    """Can we actually create a file here? Returns (bool, reason)."""
    if not root:
        return False, "no storage root"
    probe = os.path.join(root, PROBE_NAME)
    try:
        os.makedirs(root, exist_ok=True)
        with open(probe, "wb") as handle:
            handle.write(b"1")
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    try:
        os.remove(probe)
    except Exception:
        pass                # the write is what mattered; a stray dotfile is hidden
    return True, ""


def storage_probe():
    """Check the filesystem now, with no cache. Returns the storage block."""
    if _ROOT_OVERRIDE:
        writable, reason = _writable(_ROOT_OVERRIDE)
        return _block(_ROOT_MODE, _ROOT_OVERRIDE, writable,
                      persistent=(_ROOT_MODE != MODE_TEMPORARY),
                      reason=reason)

    if not EMSCRIPTEN:
        # Desktop, CI, the in-process tests. There is no /drive here, and
        # FALLBACK_ROOT is a POSIX path that Windows resolves drive-relative --
        # probing it CREATED C:\tmp\lifelib-studio on a developer machine, which
        # is not ours to claim. A host running the bridge outside a browser
        # knows where its files live and says so.
        return _block(
            MODE_LOCAL, "", False, persistent=False,
            reason="no storage root is configured. Outside the browser the "
                   "bridge does not guess one: call "
                   "modelx_bridge.set_storage_root(path) first.")

    drive = False
    try:
        drive = os.path.isdir(DRIVE_ROOT)
    except Exception:
        drive = False
    if drive:
        writable, reason = _writable(DRIVE_ROOT)
        if writable:
            return _block(MODE_DRIVE, DRIVE_ROOT, True, persistent=True, reason="")
        # Mounted but read-only: a real shape of failure, and reporting it as
        # "ready" would be the save-that-did-not-persist lie one step later.
        fallback_ok, _ = _writable(FALLBACK_ROOT)
        return _block(
            MODE_TEMPORARY, FALLBACK_ROOT, fallback_ok, persistent=False,
            reason="%s is mounted but not writable (%s); using in-memory storage, "
                   "which is lost when this tab closes" % (DRIVE_ROOT, reason))

    writable, _reason = _writable(FALLBACK_ROOT)
    # WHY THIS SENTENCE NAMES TWO OUTCOMES AND PROMISES NEITHER. It used to end
    # "...a service worker whose registration is racy; reloading the page
    # usually fixes it", which is true on an ordinary browser -- the registration
    # really does lose a race against the kernel's first /drive probe, and a
    # reload really does win it. It is not true everywhere: an adversarial run
    # measured a host that CANNOT register a service worker at all (registering
    # a script that does not exist returned the identical error as registering
    # the real one, where a working browser distinguishes them with a 404), and
    # there "reloading usually fixes it" is a promise the product cannot keep.
    # The kernel cannot tell the two apart -- all it can see is that /drive is
    # absent -- so it says what it knows and names both endings. The browser
    # half, which CAN tell them apart, is `storageVerdict` in
    # packages/lab-ext/src/topbar.tsx: it reads `ServiceWorkerWatch` and offers
    # "Reload to enable saving", "try again" or plain "Saving will not persist"
    # accordingly. A sentence that promised a reload here would contradict the
    # button underneath it.
    return _block(
        MODE_TEMPORARY, FALLBACK_ROOT, writable, persistent=False,
        reason="%s is not mounted in this kernel, so nothing saved here survives "
               "a reload. JupyterLite mounts it from a service worker: if the "
               "registration merely lost a race, reloading the page fixes it; "
               "if this browser will not register one at all, nothing in this "
               "tab can." % DRIVE_ROOT)


def backup_default(mode):
    """Should a write keep modelx's rotating backup here? (enabled, why).

    THE PRODUCT DECISION, and it differs by where the bridge is running, because
    the same behaviour is a safety net in one place and a silent leak in the
    other (protocol section 9.8):

    ``drive`` / ``temporary`` -- **off**. Browser storage is a single finite
        quota shared by the whole origin, and modelx keeps up to three
        rotations, so an ordinary Save can leave FOUR copies of the model where
        the user believes there is one. Worse, the copies are visible: the Files
        panel grows ``BasicTerm_S_BAK1`` folders the visitor never created, which
        reads as corruption rather than as a feature. A backup nobody can see,
        nobody asked for and nobody can afford is not a backup.

    ``local`` -- **on**, which is modelx's own default. A desktop install writes
        to a real filesystem with real space, the backup lands in the same folder
        where its owner can see and delete it, and overwriting a model by mistake
        there costs work that nothing else can bring back.

    Either way the caller may say ``backup: true`` / ``false`` explicitly and the
    reply reports which happened -- the default is a default, not a policy the
    frontend cannot escape.
    """
    if mode == MODE_LOCAL:
        return True, ("this is a local install, so modelx's usual backup is "
                      "kept beside the model")
    if mode == MODE_TEMPORARY:
        return False, ("this kernel has no persistent filesystem, so a backup "
                       "would only spend memory that dies with the tab")
    return False, ("browser storage is one finite quota and modelx keeps up to "
                   "%d rotations, so backups are off unless you ask for one"
                   % MAX_BACKUPS)


#: Mirrors ``modelx.serialize.DEFAULT_MAX_BACKUPS``. Read from modelx when it can
#: be imported (``backup_limit``); this constant is the answer for a caller that
#: only has the storage block, and for files.py's own prose.
MAX_BACKUPS = 3


def backup_limit():
    """modelx's rotation count, asked of modelx rather than assumed."""
    try:
        from modelx.serialize import DEFAULT_MAX_BACKUPS
        return int(DEFAULT_MAX_BACKUPS)
    except Exception:
        return MAX_BACKUPS


def backup_slots(real):
    """Which of modelx's rotating backups of ``real`` are on disk right now.

    Returns the slot numbers, ascending. ``_BAK1`` is the most recent. This is an
    existence check per slot rather than a directory scan: the names are exactly
    ``<real>_BAK<n>``, so there is nothing to search for.
    """
    slots = []
    for slot in range(1, BACKUP_SLOTS + 1):
        try:
            if os.path.exists(real + "_BAK%d" % slot):
                slots.append(slot)
        except Exception:
            break
    return slots


def backup_of(name):
    """``("BasicTerm_S", 1)`` for ``"BasicTerm_S_BAK1"``, else ``(None, 0)``."""
    match = BACKUP_RE.match(name or "")
    if not match:
        return None, 0
    try:
        return match.group("base"), int(match.group("slot"))
    except ValueError:
        return None, 0


def _block(mode, root, writable, persistent, reason):
    backup, backup_reason = backup_default(mode)
    return {
        "mode": mode,
        "root": root,
        "writable": bool(writable),
        "persistent": bool(persistent),
        "reason": reason or None,
        "drive_root": DRIVE_ROOT,
        "platform": sys.platform,
        "checked_at": _now(),
        # What a save with no `backup` param will do here, so a UI can render an
        # honest Save dialog BEFORE the write rather than explaining afterwards.
        "backup_default": backup,
        "backup_reason": backup_reason,
        "max_backups": backup_limit(),
    }


def storage_info(refresh=False):
    """The storage block, cached.

    Re-probes when asked, and **whenever the last answer was ``temporary``** --
    that is the service-worker race healing itself: a worker that registers late,
    or after the frontend's retry, flips the mode on the next file operation with
    nobody having to ask. A drive that is already up is not re-probed; it does not
    spontaneously unmount, and the probe costs a write and an unlink.
    """
    global _CACHE
    if (_CACHE is None or refresh
            or _CACHE.get("mode") == MODE_TEMPORARY
            or not _CACHE.get("root")):
        _CACHE = storage_probe()
    return dict(_CACHE)


def storage_root():
    return storage_info()["root"]


def require_writable():
    """The storage block, or bad_request if nothing at all can be written."""
    info = storage_info()
    if not info["writable"]:
        raise bad_request(
            "this kernel has no writable filesystem (%s)"
            % (info["reason"] or info["root"]), storage=info)
    return info


# --- the virtual path space ---------------------------------------------

def to_virtual(path, info=None):
    """Normalise a wire path to an absolute virtual path under the root.

    Accepts ``/models/x``, ``models/x`` and the real rooted form
    ``/drive/models/x``; the last is stripped to ``/models/x`` so a path echoed
    back from a listing, or copied out of a Console session, keeps working when
    the mode changes underneath it.

    ``..`` is resolved and an escape above the root is a bad_request, not a
    clamp: a caller that asked for something outside the root asked for something
    we will not do, and quietly serving a different path is how directory
    traversal bugs get written.
    """
    if not isinstance(path, str):
        raise bad_request("path must be a string")
    if "\x00" in path:
        raise bad_request("path must not contain a NUL byte")
    info = info or storage_info()
    text = path.replace("\\", "/").strip()
    root = info["root"].replace("\\", "/").rstrip("/")
    for prefix in (root, DRIVE_ROOT, FALLBACK_ROOT):
        prefix = prefix.rstrip("/")
        if prefix and (text == prefix or text.startswith(prefix + "/")):
            text = text[len(prefix):]
            break
    parts = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise bad_request(
                    "path %r points above the storage root" % path, path=path)
            parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def to_real(virtual, info=None):
    """Map an absolute virtual path to the kernel path it names."""
    info = info or storage_info()
    parts = [p for p in virtual.split("/") if p]
    return os.path.join(info["root"], *parts) if parts else info["root"]


def resolve(path, info=None):
    """(virtual, real) for one wire path."""
    info = info or storage_info()
    virtual = to_virtual(path, info)
    return virtual, to_real(virtual, info)


def parent_of(virtual):
    if virtual in ("", "/"):
        return None
    head = virtual.rsplit("/", 1)[0]
    return head or "/"


def join_virtual(virtual, name):
    return (virtual.rstrip("/") + "/" + name) if virtual != "/" else "/" + name


# --- what is a model ----------------------------------------------------

def is_model_dir(real):
    try:
        return os.path.isfile(os.path.join(real, MODEL_MARKER))
    except Exception:
        return False


def is_model_zip(real):
    """True if ``real`` is a zip file with modelx's marker at its root.

    Reads the central directory only -- no extraction -- so listing a folder of
    large zips stays cheap.
    """
    if not real.lower().endswith(".zip"):
        return False
    try:
        import zipfile
        with zipfile.ZipFile(real) as archive:
            names = archive.namelist()
    except Exception:
        return False
    if MODEL_MARKER in names:
        return True
    # Tolerate one wrapping directory: some zip tools add the folder name.
    tops = set(n.split("/", 1)[0] for n in names if "/" in n)
    return any((top + "/" + MODEL_MARKER) in names for top in tops)


def model_format(real):
    """``"folder"``, ``"zip"`` or None."""
    if os.path.isdir(real):
        return "folder" if is_model_dir(real) else None
    return "zip" if is_model_zip(real) else None


#: The model's own name lives in its ``__init__.py`` as ``_name = "..."``.
#: NOT in _system.json, which carries only the serializer and modelx versions.
NAME_RE = re.compile(r'^_name\s*=\s*(["\'])(.*?)\1\s*$', re.M)

#: Enough of __init__.py to reach _name, which the serializer writes first.
NAME_SCAN_BYTES = 8192


def model_name(real, fmt=None):
    """The name modelx will give this model when it is read, or None.

    Worth the small read. ``mx.read_model`` on a name that is already open does
    NOT raise -- it renames the OPEN model to ``<name>_BAK1`` and warns, which
    would silently leave a stale duplicate behind every re-open. Knowing the name
    in advance is what lets model.open be idempotent instead.
    """
    fmt = fmt or model_format(real)
    try:
        if fmt == "folder":
            with open(os.path.join(real, "__init__.py"), "rb") as handle:
                raw = handle.read(NAME_SCAN_BYTES)
        elif fmt == "zip":
            import zipfile
            with zipfile.ZipFile(real) as archive:
                names = archive.namelist()
                member = "__init__.py"
                if member not in names:
                    member = next((n for n in names
                                   if n.endswith("/__init__.py")
                                   and n.count("/") == 1), None)
                    if member is None:
                        return None
                raw = archive.read(member)[:NAME_SCAN_BYTES]
        else:
            return None
    except Exception:
        return None
    match = NAME_RE.search(raw.decode("utf-8", "replace"))
    return match.group(2) if match else None


# --- listing ------------------------------------------------------------

def _now(stamp=None):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(stamp if stamp is not None else None))
    except Exception:
        return None


def dir_size(real):
    """(bytes, capped). Walks, but stops at MAX_WALK_FILES and says so."""
    total = seen = 0
    try:
        for root, _dirs, names in os.walk(real):
            for name in names:
                seen += 1
                if seen > MAX_WALK_FILES:
                    return None, True
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:
        return None, False
    return total, False


def entry_for(virtual, real, name):
    """One listing row. Never raises: an unreadable entry is still shown."""
    # A `_BAK<n>` sibling is modelx's backup rotation, not something the user
    # made. It is still listed -- it is their data and it is taking their quota
    # -- but it is labelled, so the Files panel can group or dim it instead of
    # showing a folder the visitor is sure they never created (section 9.8).
    base, slot = backup_of(name)
    entry = {"name": name, "path": join_virtual(virtual, name), "kind": "file",
             "size": None, "size_capped": False, "modified": None,
             "is_model": False, "model_format": None,
             "is_backup": base is not None,
             "backup_of": join_virtual(virtual, base) if base else None,
             "backup_slot": slot or None}
    try:
        entry["modified"] = _now(os.path.getmtime(real))
    except Exception:
        pass
    try:
        if os.path.isdir(real):
            entry["kind"] = "directory"
            fmt = "folder" if is_model_dir(real) else None
            if fmt:
                entry["kind"] = "model"
                entry["size"], entry["size_capped"] = dir_size(real)
        else:
            entry["size"] = os.path.getsize(real)
            fmt = "zip" if is_model_zip(real) else None
            if fmt:
                entry["kind"] = "model"
        entry["is_model"] = fmt is not None
        entry["model_format"] = fmt
        if fmt:
            # The folder name and the model's own name need not match, and the
            # Explorer shows the latter. Cheap: one bounded read of __init__.py.
            entry["model_name"] = model_name(real, fmt)
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
    return entry


def list_dir(path="/", info=None):
    """List one directory. Dotfiles are hidden; entries are sorted models first,
    then directories, then files, each alphabetically, with modelx's ``_BAK``
    backups last -- the Files panel's order."""
    info = info or storage_info()
    virtual, real = resolve(path, info)
    if not os.path.exists(real):
        raise not_found("no such directory: %s" % virtual, path=virtual,
                        real_path=real, storage=info)
    if not os.path.isdir(real):
        raise bad_request("%s is a file, not a directory" % virtual, path=virtual)
    try:
        names = sorted(os.listdir(real))
    except Exception as exc:
        raise bad_request("cannot list %s (%s: %s)"
                          % (virtual, type(exc).__name__, exc), path=virtual)
    names = [n for n in names if not n.startswith(".")]
    truncated = len(names) > MAX_ENTRIES
    entries = [entry_for(virtual, os.path.join(real, n), n)
               for n in names[:MAX_ENTRIES]]
    rank = {"model": 0, "directory": 1, "file": 2}
    # Backups sink below everything the user made, whatever kind they are: a
    # rotation of BasicTerm_S must not sort immediately under BasicTerm_S and
    # read as a second model.
    entries.sort(key=lambda e: (bool(e.get("is_backup")),
                                rank.get(e["kind"], 3), e["name"].lower()))
    return {"storage": info, "path": virtual, "real_path": real,
            "parent": parent_of(virtual), "entries": entries,
            "truncated": truncated, "count": len(entries)}


def make_dirs(real):
    directory = os.path.dirname(real)
    if directory:
        os.makedirs(directory, exist_ok=True)


def tree_bytes(real):
    """Bytes actually on disk at ``real`` -- a file's size, or a tree's total.
    Used to verify a write rather than to report an estimate."""
    if os.path.isfile(real):
        return os.path.getsize(real)
    total, _capped = dir_size(real)
    return total
