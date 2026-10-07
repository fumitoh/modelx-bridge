"""Where the shipped demo model is, and its size as git stores it.

THE CANONICAL BYTES ARE LF. lifelib 0.17.1's own wheel ships BasicTerm_S as LF,
7 files and 324,563 bytes, byte-identical to what git stores here (compared file
by file, 2026-09-27), and `.gitattributes` checks the model out as LF on every
platform.

A Windows checkout made BEFORE `.gitattributes` pinned `eol=lf` has the two .py
files in CRLF (core.autocrlf is true on the authoring machine): one byte more
per line, 778 in Projection/__init__.py and 10 in __init__.py, 325,351 in all --
the number these suites used to pin. Git does not rewrite a working tree when
attributes change, so that checkout stays CRLF until the files are checked out
again. It is not broken -- modelx reads either -- so the suites measure what git
stores rather than what this particular checkout holds.

Only TEXT files are folded. The .xlsx files and the pickle contain CR LF byte
pairs of their own (271 in model_point_table.xlsx, measured), and folding those
would change the count of a file no checkout ever converts.
"""

import os
import sys

#: `binary` in .gitattributes; everything else under apps/lite/files/models is
#: `text eol=lf`.
BINARY_SUFFIXES = (".xlsx", ".pickle")


def sample_dir(name="BasicTerm_S"):
    """The shipped model the suites read, found the way the bridge finds it.

    samples.shipped_model_dir, so the suites run unchanged in lifelib Studio
    (apps/lite/files/models/<name>), in the public modelx-bridge repo
    (modelx_bridge/models/<name>), and against a copy named by
    $MODELX_BRIDGE_MODELS. It never skips: both layouts ship the model, so a
    missing one is a broken checkout, and the suite exits with a FAIL line
    naming every path that was tried.
    """
    from modelx_bridge import samples
    path = samples.shipped_model_dir(name)
    if path is None:
        print("FAIL the shipped %s model was not found; looked in %s"
              % (name, ", ".join(samples._candidate_dirs(name))))
        sys.exit(1)
    return path


def repo_root():
    """The checkout that holds the package: lifelib Studio's root (above
    python/modelx_bridge) or the public repo's (above modelx-bridge/modelx_bridge).
    Only for files that exist in lifelib Studio alone -- the site build, the
    spike S1 ground truth, a sibling lifelib checkout -- and a suite that reads
    one must say out loud when it is absent."""
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.dirname(package))


def installed_copy(path):
    """Whether the model at `path` is the copy pip installed from the
    modelx-bridge wheel, i.e. a file the distribution's RECORD lists.

    pip byte-compiles every .py file a wheel installs, the model's two among
    them. MEASURED 2026-10-06, the 0.10.0 wheel installed by pip into a fresh
    CPython 3.11 venv: BasicTerm_S/__pycache__ and Projection/__pycache__
    appear, the wheel holds neither, and copying the folder with them makes it
    353,614 bytes instead of 324,563. An installer wrote them, so a check that
    nothing was SHIPPED compiled cannot be made on an installed copy; the
    public repo's CI holds the wheel's own file list free of __pycache__.
    """
    try:
        from importlib import metadata
        dist = metadata.distribution("modelx-bridge")
    except Exception:
        return False
    target = os.path.realpath(os.path.join(path, "__init__.py"))
    return any(os.path.realpath(str(f.locate())) == target for f in dist.files or ())


def stored_bytes(path):
    """The file at `path` with CRLF folded to LF, unless it is binary."""
    with open(path, "rb") as handle:
        body = handle.read()
    if not path.endswith(BINARY_SUFFIXES):
        body = body.replace(b"\r\n", b"\n")
    return body


def stored_size(path):
    return len(stored_bytes(path))
