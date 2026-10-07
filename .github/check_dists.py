"""Check what `python -m build` wrote before anything is published.

    python .github/check_dists.py DIST_DIR [--tag bridge-v0.10.0]

For every wheel and sdist of modelx-bridge and modelx-mcp in DIST_DIR:

- its version is the one in the code: VERSION in modelx_bridge/methods.py,
  __version__ in modelx_mcp/__init__.py, read with ast as setuptools reads them;
- its file list is EXACTLY the expected one, derived from this checkout: the
  package's modules and data, the licence files and the metadata. A wheel holds
  no tests; nothing holds __pycache__ or a .pyc; anything else -- a stray
  folder, a scratch file -- is reported by name;
- every file taken from the checkout has the checkout's bytes.

With --tag, DIST_DIR must hold exactly the one package the tag names
(bridge-v* is modelx-bridge, mcp-v* is modelx-mcp), at the tag's version.

Prints each distribution's files, then one line per problem. Exit 1 if any.
"""

import argparse
import ast
import os
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PACKAGES = {
    # dist name: (project folder, import name, file holding the version, its name)
    "modelx-bridge": ("modelx-bridge", "modelx_bridge", "methods.py", "VERSION"),
    "modelx-mcp": ("modelx-mcp", "modelx_mcp", "__init__.py", "__version__"),
}
TAGS = {"bridge-v": "modelx-bridge", "mcp-v": "modelx-mcp"}
WHEEL_TAG = "py3-none-any"


def code_version(dist):
    folder, pkg, module, name = PACKAGES[dist]
    with open(os.path.join(ROOT, folder, pkg, module), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name):
            return ast.literal_eval(node.value)
    raise SystemExit("no %s in %s/%s/%s" % (name, folder, pkg, module))


def source_files(dist):
    """{path relative to the project folder: absolute path}, no __pycache__."""
    folder, pkg = PACKAGES[dist][:2]
    base = os.path.join(ROOT, folder)
    out = {}
    for here, dirs, files in os.walk(os.path.join(base, pkg)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(here, name)
            out[os.path.relpath(full, base).replace(os.sep, "/")] = full
    return out


def expected_wheel(dist, version):
    pkg = PACKAGES[dist][1]
    info = "%s-%s.dist-info/" % (pkg, version)
    files = {rel: full for rel, full in source_files(dist).items()
             if not rel.startswith(pkg + "/tests/")}
    if dist == "modelx-bridge":
        # Modules at the top of the package, and everything under models/.
        files = {rel: full for rel, full in files.items()
                 if rel.count("/") == 1 and rel.endswith(".py")
                 or rel.startswith(pkg + "/models/")}
        lifelib = pkg + "/models/LICENSE-lifelib.txt"
        files[info + "licenses/" + lifelib] = files[lifelib]
    else:
        files = {rel: full for rel, full in files.items()
                 if rel.count("/") == 1 and (rel.endswith(".py") or rel == pkg + "/tools.json")}
        files[info + "entry_points.txt"] = None
    files[info + "licenses/LICENSE"] = os.path.join(ROOT, PACKAGES[dist][0], "LICENSE")
    for name in ("METADATA", "WHEEL", "RECORD", "top_level.txt"):
        files[info + name] = None
    return files


def expected_sdist(dist, version):
    folder, pkg = PACKAGES[dist][:2]
    top = "%s-%s/" % (pkg, version)
    files = {top + rel: full for rel, full in source_files(dist).items()}
    for name in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        files[top + name] = os.path.join(ROOT, folder, name)
    for name in ("PKG-INFO", "setup.cfg"):
        files[top + name] = None
    egg = top + pkg + ".egg-info/"
    for name in ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "requires.txt",
                 "top_level.txt"):
        files[egg + name] = None
    if dist == "modelx-mcp":
        files[egg + "entry_points.txt"] = None
    return files


def compare(label, names, read, expected, problems):
    got = set(names)
    want = set(expected)
    for name in sorted(got - want):
        problems.append("%s ships %s, which it should not" % (label, name))
    for name in sorted(want - got):
        problems.append("%s lacks %s" % (label, name))
    for name in sorted(got & want):
        if expected[name] is not None:
            with open(expected[name], "rb") as handle:
                if read(name) != handle.read():
                    problems.append("%s: %s differs from the checkout's copy" % (label, name))
        if "__pycache__" in name or name.endswith((".pyc", ".pyo")):
            problems.append("%s ships compiled bytecode: %s" % (label, name))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dist_dir")
    ap.add_argument("--tag", help="the release tag, e.g. bridge-v0.10.0")
    args = ap.parse_args(argv)

    problems = []
    found = {}
    for name in sorted(os.listdir(args.dist_dir)):
        path = os.path.join(args.dist_dir, name)
        for dist, (_, pkg, _, _) in PACKAGES.items():
            version = code_version(dist)
            if name.startswith(pkg + "-"):
                found.setdefault(dist, []).append(name)
                if name.endswith(".whl"):
                    want = "%s-%s-%s.whl" % (pkg, version, WHEEL_TAG)
                    if name != want:
                        problems.append("%s: expected %s (the code says %s)" % (name, want, version))
                        continue
                    with zipfile.ZipFile(path) as whl:
                        names = whl.namelist()
                        print("%s (%d files, %d bytes)" % (name, len(names), os.path.getsize(path)))
                        for n in names:
                            print("    " + n)
                        compare(name, names, whl.read, expected_wheel(dist, version), problems)
                elif name.endswith(".tar.gz"):
                    want = "%s-%s.tar.gz" % (pkg, version)
                    if name != want:
                        problems.append("%s: expected %s (the code says %s)" % (name, want, version))
                        continue
                    with tarfile.open(path) as sdist:
                        members = [m for m in sdist.getmembers() if m.isfile()]
                        names = [m.name for m in members]
                        print("%s (%d files, %d bytes)" % (name, len(names), os.path.getsize(path)))
                        for n in names:
                            print("    " + n)
                        compare(name, names, lambda n: sdist.extractfile(n).read(),
                                expected_sdist(dist, version), problems)
                else:
                    problems.append("%s is neither a wheel nor an sdist" % name)
                break
        else:
            problems.append("%s is not a distribution of either package" % name)

    wanted = sorted(PACKAGES)
    if args.tag:
        prefix = next((p for p in TAGS if args.tag.startswith(p)), None)
        if prefix is None:
            problems.append("tag %s names neither package (bridge-v*, mcp-v*)" % args.tag)
            wanted = []
        else:
            dist = TAGS[prefix]
            wanted = [dist]
            if args.tag[len(prefix):] != code_version(dist):
                problems.append("tag %s, but the code says %s %s"
                                % (args.tag, dist, code_version(dist)))
            for other in sorted(set(found) - {dist}):
                problems.append("tag %s, but %s holds %s too" % (args.tag, args.dist_dir, other))
    for dist in wanted:
        kinds = sorted(n.rsplit(".", 1)[-1] for n in found.get(dist, ()))
        if kinds != ["gz", "whl"]:
            problems.append("%s: expected one wheel and one sdist, found %s"
                            % (dist, found.get(dist, "none")))

    print()
    for line in problems:
        print("FAIL " + line)
    print("%d problem(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
