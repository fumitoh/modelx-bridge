"""The demo model is the REAL lifelib BasicTerm_S, and it computes the right numbers.

    python -m modelx_bridge.tests.test_model

Everything here goes through the bridge's own methods, over the wire shapes,
because "the model loads" and "the panel can read it" are different claims. The
expected values are native ground truth from spikes/probes/native_reference.json
(reproduced exactly in Pyodide during spike S1, 77 checks, worst relative error
0), with the headline figures hard-coded below so this file still has teeth when
that JSON is not reachable.

Tolerance is 1e-12 relative, and in practice the difference is 0.
"""

import json
import os
import sys

from .shipped import installed_copy, repo_root, sample_dir, stored_bytes, stored_size

FAILURES = []
NUMBERS = {}
TOL = 1e-12

REPO = repo_root()
SHIPPED = sample_dir()
#: The package's own folders, derived here rather than through samples.py, so
#: the lookup-order checks below compare the bridge against an independent
#: statement of where each layout keeps the model.
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO_COPY = os.path.join(REPO, "apps", "lite", "files", "models", "BasicTerm_S")
PACKAGE_COPY = os.path.join(PACKAGE, "models", "BasicTerm_S")
#: lifelib Studio keeps the package at <repo>/python/modelx_bridge, and only
#: there is STUDIO_COPY a candidate. Anywhere else (an installed wheel, a
#: `pip install --target` folder, the public repo) REPO is merely two folders
#: up, which nobody vouches for, and the lookup must not read it.
IN_STUDIO = os.path.basename(os.path.dirname(PACKAGE)) == "python"
LAYOUT_COPIES = ([STUDIO_COPY] if IN_STUDIO else []) + [PACKAGE_COPY]
# Both exist in lifelib Studio's checkout alone. Where they do not, the checks
# that read them print a "skip " line with the reason, which run_all.py counts
# as skipped -- never as passed.
REFERENCE = os.path.join(REPO, "spikes", "probes", "native_reference.json")
LIFELIB_SOURCE = os.path.join(
    os.path.dirname(REPO), "lifelib", "lifelib", "libraries", "basiclife",
    "BasicTerm_S")

# Measured natively with Projection.point_id = 1. The task FACTS, verbatim.
GROUND_TRUTH = {
    "proj_len()": 121,
    "pv_net_cf()": 910.92066093366,
    "pv_claims()": 5501.194898364312,
    "pv_premiums()": 8252.085855522228,
    "claims(0)": 34.18079328868595,
    "premium_pp()": 94.84,
}

# Files the shipped content must contain, and their measured sizes AS GIT STORES
# THEM: LF, byte-identical to lifelib 0.17.1's wheel, measured 2026-09-27. The
# two .py files used to be pinned at their CRLF sizes (19,758 and 129) from a
# Windows checkout; see shipped.py for why the checkout no longer matters.
SHIPPED_FILES = {
    os.path.join("Projection", "__init__.py"): 18980,
    "model_point_table.xlsx": 284765,
    "mort_table.xlsx": 13165,
    "disc_rate_ann.xlsx": 6816,
    os.path.join("_data", "data.pickle"): 663,
    "_system.json": 55,
    "__init__.py": 119,
}
SHIPPED_BYTES = 324563
# lifelib-0.17.1-py3-none-any.whl from PyPI, measured 2026-09-27. "27.9 MB" is
# decimal megabytes; this was 27.9 * 1024 * 1024 before, which printed 90x where
# samples.py says 86x.
LIFELIB_WHEEL_BYTES = 27862746


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-50s %s" % ("ok  " if condition else "FAIL", label, detail))


def close(got, want, tol=TOL):
    if want == 0:
        return abs(got) <= tol
    return abs(got - want) / abs(want) <= tol


def scalar(encoded):
    """Unwrap a codec value to a Python number (np and num tags nest, section 5)."""
    if isinstance(encoded, dict):
        tag = encoded.get("$t")
        if tag in ("np", "num"):
            v = encoded.get("v")
            if isinstance(v, str):
                return float({"NaN": "nan", "Infinity": "inf",
                              "-Infinity": "-inf"}[v])
            return scalar(v)
    return encoded


def read_reference():
    """spikes/probes/native_reference.json, or None after a skip line saying why.

    Absent is a layout (the public modelx-bridge repo has no spikes/) and is
    skipped. Present but unreadable is a defect in lifelib Studio's checkout and
    FAILS: it used to be reported as "unreachable" and skipped either way.
    """
    if not os.path.isfile(REFERENCE):
        print("skip the native_reference.json checks: %s does not exist (spike "
              "S1's native ground truth is in lifelib Studio's repo only; the "
              "headline values above are hard-coded from it)" % REFERENCE)
        return None
    try:
        with open(REFERENCE, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        check("native_reference.json is readable", False, repr(exc))
        return None


# --- the shipped content -------------------------------------------------

def content_checks():
    print("shipped content")
    missing = [rel for rel in SHIPPED_FILES
               if not os.path.isfile(os.path.join(SHIPPED, rel))]
    check("every BasicTerm_S file is shipped", not missing,
          "missing: %s" % missing if missing else "%d files" % len(SHIPPED_FILES))
    if missing:
        return
    sizes = dict((rel, stored_size(os.path.join(SHIPPED, rel)))
                 for rel in SHIPPED_FILES)
    wrong = dict((rel, (sizes[rel], want)) for rel, want in SHIPPED_FILES.items()
                 if sizes[rel] != want)
    check("shipped files are the measured sizes", not wrong, json.dumps(wrong))

    total = checkout_bytes = 0
    for root, dirs, files in os.walk(SHIPPED):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        total += sum(stored_size(os.path.join(root, f)) for f in files)
        checkout_bytes += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    NUMBERS["shipped_bytes"] = total
    check("the whole model directory is %s bytes" % format(SHIPPED_BYTES, ","),
          total == SHIPPED_BYTES,
          "%d bytes, %.0fx smaller than the %.1f MB lifelib wheel%s"
          % (total, LIFELIB_WHEEL_BYTES / total, LIFELIB_WHEEL_BYTES / 1e6,
             "" if checkout_bytes == total
             else "; %d in this CRLF checkout" % checkout_bytes))

    cached = [os.path.join(r, d) for r, ds, _ in os.walk(SHIPPED) for d in ds
              if d == "__pycache__"]
    if cached and installed_copy(SHIPPED):
        print("skip the no-__pycache__ check: %s is the copy pip installed from the "
              "wheel, and pip byte-compiles every .py it installs, so these are the "
              "installer's (%s); CI checks the wheel's own file list instead"
              % (SHIPPED, ", ".join(os.path.relpath(c, SHIPPED) for c in cached)))
    else:
        check("no __pycache__ was shipped", not cached, ", ".join(cached))

    # The HTTP fallback downloads by manifest, because a static site cannot list
    # a directory. If the manifest and the directory disagree, the browser gets a
    # model the desktop tests never saw -- so they are compared here.
    from modelx_bridge.samples import MODEL_MANIFEST
    on_disk = set()
    for root, dirs, files in os.walk(SHIPPED):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), SHIPPED)
            on_disk.add(rel.replace(os.sep, "/"))
    listed = set(MODEL_MANIFEST["BasicTerm_S"])
    check("the download manifest matches the shipped directory exactly",
          listed == on_disk,
          "only in manifest: %s; only on disk: %s"
          % (sorted(listed - on_disk), sorted(on_disk - listed)))

    # Drift guard: the shipped copy must be byte-identical to lifelib's, as git
    # stores both -- a lifelib checkout has line endings of its own choosing.
    if not os.path.isdir(LIFELIB_SOURCE):
        print("skip the drift check against lifelib's own copy: no lifelib "
              "checkout beside this one (looked for %s)" % LIFELIB_SOURCE)
        return
    differing = []
    for rel in SHIPPED_FILES:
        src, dst = os.path.join(LIFELIB_SOURCE, rel), os.path.join(SHIPPED, rel)
        try:
            if stored_bytes(src) != stored_bytes(dst):
                differing.append(rel)
        except OSError as exc:
            differing.append("%s (%r)" % (rel, exc))
    check("the shipped copy is byte-identical to lifelib's", not differing,
          ", ".join(differing) or os.path.basename(LIFELIB_SOURCE))


# --- where the bridge looks for it ----------------------------------------

def same_path(a, b):
    return (a is not None and b is not None and os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def layout_copy():
    """(path, why): the copy this layout keeps, ignoring $MODELX_BRIDGE_MODELS."""
    if IN_STUDIO and os.path.isdir(STUDIO_COPY):
        return STUDIO_COPY, "lifelib Studio's apps/lite/files"
    return PACKAGE_COPY, "the package's own models folder"


def lookup_checks():
    """samples._candidate_dirs' order outside the browser, and the override.

    Run from a real file, as here, the order is: $MODELX_BRIDGE_MODELS, then
    lifelib Studio's apps/lite/files (in its checkout only), then the package's
    own models/ folder, and nothing else -- never the browser's MODEL_ROOTS, whose "" is the
    working directory, nor STAGE_ROOT under /tmp (samples.py says what that let
    through). tests/test_bootstrap.py pins the browser's order.
    """
    import shutil
    import tempfile

    from modelx_bridge import BridgeError, find_model_dir, samples

    print("sample lookup")
    env = samples.MODELS_ENV
    override = os.environ.get(env)
    # The override is a candidate, not a pin: one that holds no BasicTerm_S
    # falls through to the layout's copy, so only one that holds it is expected.
    if override and os.path.isfile(os.path.join(override, "BasicTerm_S",
                                                 "_system.json")):
        want, why = os.path.join(override, "BasicTerm_S"), "$" + env
    else:
        want, why = layout_copy()
        if override:
            why += ", because $%s=%s holds no BasicTerm_S" % (env, override)
    path = find_model_dir("BasicTerm_S")
    check("find_model_dir locates the shipped content", same_path(path, want),
          "%s, from %s" % (path, why))
    check("the suites read the copy find_model_dir found", same_path(SHIPPED, path),
          SHIPPED)

    saved = os.environ.pop(env, None)
    scratch = tempfile.mkdtemp(prefix="bridge-lookup-")
    try:
        plain = samples._candidate_dirs("BasicTerm_S")
        check("unset, the lookup is the layout's %d %s and nothing else"
              % (len(LAYOUT_COPIES), "copies" if IN_STUDIO else "copy"),
              plain == LAYOUT_COPIES, " > ".join(plain))
        if not IN_STUDIO:
            check("...not the apps/lite folder two levels above the package",
                  STUDIO_COPY not in plain, STUDIO_COPY)
        browser = set(os.path.join(root, "models", "BasicTerm_S") if root
                      else os.path.join("models", "BasicTerm_S")
                      for root in samples.MODEL_ROOTS)
        leaked = [p for p in plain if p in browser or not os.path.isabs(p)
                  or p.startswith(samples.STAGE_ROOT)]
        check("...none of them the working directory, a browser root or STAGE_ROOT",
              not leaked, ", ".join(leaked) or "%d absolute paths" % len(plain))
        layout = samples.shipped_model_dir("BasicTerm_S")

        os.environ[env] = scratch
        tried = samples._candidate_dirs("BasicTerm_S")
        check("$%s is tried first, then the layout's own" % env,
              tried == [os.path.join(scratch, "BasicTerm_S")] + plain,
              "%d candidates" % len(tried))
        check("an override that holds no model falls through to the layout's copy",
              same_path(samples.shipped_model_dir("BasicTerm_S"), layout), layout)

        copy = os.path.join(scratch, "BasicTerm_S")
        shutil.copytree(SHIPPED, copy)
        check("an override that holds the model wins",
              same_path(find_model_dir("BasicTerm_S"), copy)
              and same_path(samples.shipped_model_dir("BasicTerm_S"), copy),
              find_model_dir("BasicTerm_S"))

        staged = dict(samples.STAGED)
        missing = samples.shipped_model_dir("NoSuchModel")
        try:
            find_model_dir("NoSuchModel")
            message = None
        except BridgeError as err:
            message = err.message
        check("a model found nowhere: None from shipped_model_dir, and nothing staged",
              missing is None and samples.STAGED == staged, repr(missing))
        check("...and find_model_dir's not_found names the override first",
              message is not None and "looked in %r" % os.path.join(scratch, "NoSuchModel")
              in message, (message or "(no error)")[:140])
    finally:
        os.environ.pop(env, None)
        if saved is not None:
            os.environ[env] = saved
        shutil.rmtree(scratch, ignore_errors=True)


#: pv_net_cf's last line in the shipped Projection/__init__.py, and what the
#: planted copy below says instead.
PV_NET_CF_BODY = ("    return pv_premiums() - pv_claims() - pv_expenses()"
                  " - pv_commissions()\n")
PLANTED_BODY = "    return 12345.0\n"


def planted_cwd_check():
    """model.open_sample from a working directory holding a planted model.

    The folder holds models/BasicTerm_S, a copy of the sample with pv_net_cf's
    body changed to return 12345.0. Before the fix the bridge's lookup tried
    "models/BasicTerm_S" relative to the working directory ahead of its own
    copy, opened that folder as "the shipped sample" and ran its pickle; this
    check then saw 12345.0 (measured against the old samples.py). It must see
    the real 910.92066093366.
    """
    import shutil
    import tempfile

    import modelx as mx
    from modelx_bridge import Bridge, samples

    print("a planted models/BasicTerm_S in the working directory")
    env = samples.MODELS_ENV
    saved = os.environ.pop(env, None)       # the default lookup, not a chosen one
    here = os.getcwd()
    scratch = tempfile.mkdtemp(prefix="bridge-planted-")
    try:
        planted = os.path.join(scratch, "models", "BasicTerm_S")
        shutil.copytree(SHIPPED, planted,
                        ignore=shutil.ignore_patterns("__pycache__"))
        source = os.path.join(planted, "Projection", "__init__.py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        with open(source, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.replace(PV_NET_CF_BODY, PLANTED_BODY))
        for model in list(mx.get_models().values()):
            model.close()

        # The plant has teeth: read directly, that folder IS a model, and it
        # says 12345.0. Without this a broken plant would pass the check below.
        decoy = mx.read_model(planted, name="Planted")
        try:
            decoy_value = decoy.Projection.pv_net_cf()
        finally:
            decoy.close()
        check("the planted copy is a model whose pv_net_cf() is 12345.0",
              text.count(PV_NET_CF_BODY) == 1 and decoy_value == 12345.0,
              repr(decoy_value))

        os.chdir(scratch)
        bridge = Bridge()
        opened = bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"})
        entry = bridge.dispatch("value.get", {"model": opened["model"], "nodes": [
            {"obj": "Projection.pv_net_cf", "args": []}]})["values"][0]
        got = scalar(entry.get("value"))
        want = GROUND_TRUTH["pv_net_cf()"]
        check("from there model.open_sample still opens the real sample",
              entry["ok"] and isinstance(got, float) and close(got, want),
              "pv_net_cf() = %r (want %r; the planted copy says 12345.0)"
              % (got, want))
        found = samples.find_model_dir("BasicTerm_S")
        layout, why = layout_copy()
        check("...read from %s, not from the working directory" % why,
              same_path(found, layout), found)
    finally:
        os.chdir(here)
        for model in list(mx.get_models().values()):
            model.close()
        if saved is not None:
            os.environ[env] = saved
        shutil.rmtree(scratch, ignore_errors=True)


# --- the model, through the bridge ---------------------------------------

def main():
    import modelx as mx
    from modelx_bridge import Bridge, BridgeError

    content_checks()
    print()
    lookup_checks()
    print()
    planted_cwd_check()
    print()

    for model in list(mx.get_models().values()):
        model.close()

    bridge = Bridge()

    import time
    t0 = time.perf_counter()
    opened = bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"})
    NUMBERS["open_sample_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    check("model.open_sample opens the real lifelib model",
          opened["model"] == "BasicTerm_S",
          "%s in %s ms" % (opened["model"], NUMBERS["open_sample_ms"]))
    check("model.open_sample is idempotent",
          bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"}) == opened)
    # The id the Welcome gallery sends is the canonical one above; these two are
    # the spellings that shipped while the kernel and the panel were built in
    # parallel. Both must land on the SAME model, never a second one.
    check("the legacy 'termlife' id is an alias for it, not a second model",
          bridge.dispatch("model.open_sample", {"sample": "termlife"})["model"]
          == "BasicTerm_S" and list(mx.get_models()) == ["BasicTerm_S"],
          ", ".join(mx.get_models()))
    check("the lower-case 'basicterm_s' id is an alias for it too",
          bridge.dispatch("model.open_sample", {"sample": "basicterm_s"})["model"]
          == "BasicTerm_S" and list(mx.get_models()) == ["BasicTerm_S"],
          ", ".join(mx.get_models()))

    model = mx.get_models()["BasicTerm_S"]
    check("point_id is 1, as the ground truth assumes",
          model.Projection.point_id == 1, repr(model.Projection.point_id))

    # -- tree.get: a real tree, with real inheritance and real IO -----------
    tree = bridge.dispatch("tree.get", {})
    text = json.dumps(tree, allow_nan=False)
    NUMBERS["tree_bytes"] = len(text)
    root = tree["root"]
    check("tree root is the model", root["kind"] == "Model"
          and root["name"] == "BasicTerm_S", root["display"])
    proj = root["spaces"][0]
    names = sorted(c["name"] for c in proj["cells"])
    check("tree has lifelib's Projection cells", len(names) == 40
          and "pv_net_cf" in names and "check_pv_net_cf" in names,
          "%d cells, %d bytes of tree" % (len(names), NUMBERS["tree_bytes"]))
    check("tree exposes point_id as both parameter and reference",
          proj["parameters"] == ["point_id"]
          and "point_id" in [r["name"] for r in proj["refs"]],
          proj["display"])
    refs = sorted(r["name"] for r in proj["refs"])
    check("tree lists the three Excel-backed references",
          all(n in refs for n in ("disc_rate_ann", "model_point_table",
                                  "mort_table")), ", ".join(refs))
    check("tree.get still does not evaluate", len(model.Projection.claims) == 0,
          "cached=%d" % len(model.Projection.claims))

    # -- formula.get: lifelib's own source, verbatim ------------------------
    formula = bridge.dispatch("formula.get", {"obj": "Projection.pv_net_cf"})
    check("formula.get returns lifelib's source",
          formula["source"].startswith("def pv_net_cf():")
          and "pv_premiums()" in formula["source"],
          repr(formula["source"].splitlines()[0]))
    check("formula.get returns lifelib's docstring",
          "present value" in (formula["doc"] or "").lower(),
          repr((formula["doc"] or "").splitlines()[0][:60]))

    # -- value.get: the ground truth ----------------------------------------
    nodes = [
        ("proj_len()", {"obj": "Projection.proj_len", "args": []}),
        ("pv_net_cf()", {"obj": "Projection.pv_net_cf", "args": []}),
        ("pv_claims()", {"obj": "Projection.pv_claims", "args": []}),
        ("pv_premiums()", {"obj": "Projection.pv_premiums", "args": []}),
        ("claims(0)", {"obj": "Projection.claims", "args": [0]}),
        ("premium_pp()", {"obj": "Projection.premium_pp", "args": []}),
    ]
    t0 = time.perf_counter()
    values = bridge.dispatch("value.get", {"nodes": [n for _, n in nodes]})
    NUMBERS["value_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    json.dumps(values, allow_nan=False)      # must serialise with no default=
    worst = 0.0
    for (label, _), entry in zip(nodes, values["values"]):
        want = GROUND_TRUTH[label]
        got = scalar(entry.get("value"))
        ok = entry["ok"] and isinstance(got, (int, float)) and close(got, want)
        if ok and want:
            worst = max(worst, abs(got - want) / abs(want))
        check("value.get %s" % label, ok,
              "%r (native %r) %s" % (got, want, entry.get("display", "")))
    NUMBERS["worst_relative_error"] = worst
    check("every ground-truth value matches to better than 1e-12",
          worst <= TOL, "worst relative error %r" % worst)

    # -- the reference file, if it is here ----------------------------------
    reference = read_reference()
    if reference:
        check("the reference is the same model and point",
              reference["model"]["name"] == "BasicTerm_S"
              and reference["model"]["point_id"] == 1,
              json.dumps(reference["model"]))
        extra = [(k, v) for k, v in reference["values"].items()
                 if "(" in k and isinstance(v, (int, float))
                 and not isinstance(v, bool)]
        specs, wants = [], []
        for key, want in extra:
            name, _, rest = key.partition("(")
            arg = rest.rstrip(")")
            args = [int(arg)] if arg else []
            specs.append({"obj": "Projection." + name, "args": args})
            wants.append((key, want))
        got = bridge.dispatch("value.get", {"nodes": specs})["values"]
        bad = [(k, scalar(e.get("value")), w)
               for (k, w), e in zip(wants, got)
               if not (e["ok"] and close(scalar(e.get("value")), w))]
        check("all %d reference values match through value.get" % len(wants),
              not bad, json.dumps(bad[:4], default=repr))

        # ItemSpaces, addressed the way the panel has to address them.
        by_point = reference.get("values_by_point", {})
        wrong = []
        for point, expected in sorted(by_point.items(), key=lambda kv: int(kv[0])):
            space = model.Projection[int(point)]
            obj = space.pv_net_cf._idstr
            entry = bridge.dispatch(
                "value.get", {"nodes": [{"obj": obj, "args": []}]})["values"][0]
            value = scalar(entry.get("value"))
            if not (entry["ok"] and close(value, expected["pv_net_cf"])):
                wrong.append((point, value, expected["pv_net_cf"]))
        check("all %d ItemSpaces match through their wire obj" % len(by_point),
              not wrong and len(by_point) > 1, json.dumps(wrong[:3], default=repr))
        if by_point:
            space = model.Projection[2]
            entry = bridge.dispatch("value.get", {"nodes": [
                {"obj": space.pv_net_cf._idstr, "args": []}]})["values"][0]
            check("an ItemSpace value displays as Projection[2], not as __SpaceN",
                  entry["display"].startswith("BasicTerm_S.Projection[2]"),
                  entry["display"])
            check("an ItemSpace obj is the internal name the tree cannot show",
                  space.pv_net_cf._idstr.startswith("Projection.__Space"),
                  space.pv_net_cf._idstr)

    # -- the Excel-backed references come back as handles -------------------
    entry = bridge.dispatch("value.get", {"nodes": [
        {"obj": "Projection.model_point_table", "args": []}]})["values"][0]
    tag = entry.get("value") or {}
    check("an Excel-backed DataFrame reference is a bounded handle",
          tag.get("$t") == "handle" and tag.get("kind") == "DataFrame"
          and len(json.dumps(tag)) < 8192,
          "shape=%s cols=%s, %d bytes"
          % (tag.get("shape"), tag.get("columns"), len(json.dumps(tag))))
    check("the model point table is lifelib's 10,000 rows",
          tag.get("shape") == [10000, 5], json.dumps(tag.get("shape")))

    # -- trace.preds on a real node -----------------------------------------
    trace = bridge.dispatch("trace.preds", {"obj": "Projection.claims", "args": [0]})
    preds = [p["obj"] for p in trace["preds"]]
    check("trace.preds walks lifelib's real dependencies",
          "Projection.claim_pp" in preds and "Projection.pols_death" in preds,
          ", ".join(preds))

    # -- a bad sample id still says what is available -----------------------
    try:
        bridge.dispatch("model.open_sample", {"sample": "BasicTerm_XL"})
        check("unknown sample -> not_found", False)
    except BridgeError as err:
        check("unknown sample -> not_found",
              err.code == "not_found" and "BasicTerm_S" in err.message,
              err.message)
        # The Welcome gallery re-renders itself from data.available when a card
        # misses, so the canonical id -- the one the gallery cards already send
        # -- has to be in that list.
        available = (err.data or {}).get("available") or []
        check("not_found carries data.available naming the canonical id",
              "BasicTerm_S" in available, ", ".join(available))

    print("\n--- numbers ---")
    for key in sorted(NUMBERS):
        print("  %-24s %s" % (key, NUMBERS[key]))
    print("\n%d checks failed" % len(FAILURES))
    for name in FAILURES:
        print("  " + name)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
