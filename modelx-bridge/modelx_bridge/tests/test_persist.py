"""The save-and-come-back loop -- and the two defects that made it unreachable.

    python -m modelx_bridge.tests.test_persist

THE LOOP, which is the whole point of having files at all:

    open the sample -> change something observable -> Save As a new path ->
    restart the kernel -> open THAT path -> get the change back.

Every step runs here against the REAL lifelib BasicTerm_S, found by
shipped.sample_dir() -- in lifelib Studio the same bytes the site ships -- and
the load-bearing assertion is a NUMBER: the model that comes back on the far
side must evaluate to the value the user changed it to, not to the pristine
sample's. A test that checked "a model opened" would have passed on both of the
bugs below.

WHAT IT PINS, stated as the two failures it was written from, both observed by
driving the running app on 2026-09-22:

  DEFECT 1  A saved model named BasicTerm_S could never be re-opened. The
            bootstrap called build_sample() unconditionally, so a model of that
            name was always open, and model.open took a reuse-by-name branch that
            returned the in-memory sample WITHOUT READING THE FILE. The Files
            panel reported success. Since BasicTerm_S is the only shipped sample,
            that is every model a demo visitor ever saves.

  DEFECT 2  Worse: that same branch did `_homes.setdefault(wanted, path)`, so
            opening a file silently gave the OTHER model the file as its save
            location. setdefault protected a model that already had one -- and a
            sample-opened model has none. The next plain Save then wrote the
            pristine sample over the visitor's work.

Both are checked here in the shape they actually occurred (fresh kernel, pristine
sample open, double click a saved file), not as unit tests of the fix.
"""

import json
import os
import re
import shutil
import sys
import tempfile

from modelx_bridge import Bridge, BridgeError, files, samples

from .shipped import sample_dir

SHIPPED = sample_dir()
#: The source the structural check in section 7 reads: python/modelx_bridge/.
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METHODS = os.path.join(PACKAGE, "methods.py")

#: Where the shipped sample content lands in the drive -- put there by the SITE,
#: not by the visitor, and therefore excluded from the boot scan (protocol 9.7).
SHIPPED_PATH = "/models/BasicTerm_S"

#: The path from the bug report, spelled exactly as the live run spelled it.
SAVE_PATH = "/models/BasicTerm_S_persist_test"

#: Native ground truth for BasicTerm_S with Projection.point_id = 1.
PV_NET_CF = 910.92066093366

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-56s %s" % ("ok  " if condition else "FAIL", label, detail))


def fails(label, code, call):
    """The call must raise BridgeError with `code`, and nothing else."""
    try:
        call()
    except BridgeError as err:
        check(label, err.code == code, "%s: %s" % (err.code, err.message[:66]))
        return err
    except Exception as exc:
        check(label, False, "raised %r instead of BridgeError" % (exc,))
        return None
    check(label, False, "did not raise")
    return None


def _tree_bytes(root):
    """Every byte under ``root``. The quota is what a browser visitor runs out
    of, so the backup claim is checked as a size, not as a folder count."""
    total = 0
    for base, _dirs, names in os.walk(root):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def pv_net_cf(model, point=None):
    if point is not None:
        model.Projection.point_id = point
    return float(model.Projection.pv_net_cf())


def restart():
    """A fresh kernel: nothing open, nothing remembered, a brand new Bridge.

    The sample registry is module state in the kernel, so it is cleared too --
    a new kernel has never built a sample, whatever the last one did.
    """
    import modelx as mx

    for name in list(mx.get_models()):
        mx.get_models()[name].close()
    for name in list(samples.opened_samples()):
        samples.forget_sample(name)
    bridge = Bridge()
    bridge.prime()
    return bridge


def user_cell(bridge, edit):
    """Run one edit as the USER's Console cell does: the edit, then post_execute.

    In the Pyodide kernel IPython's post_execute is what tells the bridge a model
    moved, and it is the only thing that can -- v0 has no mutating method. Going
    through it here is what makes `dirty` mean the same in this test as in the
    browser.
    """
    edit()
    bridge.on_execute()


def main():
    import modelx as mx

    root = tempfile.mkdtemp(prefix="lifelib-persist-")
    drive = os.path.join(root, "drive")
    memfs = os.path.join(root, "memfs")
    os.makedirs(drive)
    # The site ships its model content INTO the drive, so a first-time visitor's
    # storage is not empty. Reproducing that is load-bearing for the boot scan.
    shutil.copytree(SHIPPED, os.path.join(drive, "models", "BasicTerm_S"))

    files.EMSCRIPTEN = True
    files.DRIVE_ROOT = drive
    files.FALLBACK_ROOT = memfs
    files.forget_storage()
    info = files.storage_info(refresh=True)
    check("a mounted drive: persistent storage, as the browser has it",
          info["mode"] == "drive" and info["persistent"],
          json.dumps({k: info[k] for k in ("mode", "persistent")}))

    # === 1. FIRST VISIT: the kernel boots ================================
    restart()
    report = samples.boot("BasicTerm_S")
    check("first visit: the bootstrap opens the demo sample",
          report["reason"] == "sample" and report["model"] == "BasicTerm_S",
          json.dumps({k: report[k] for k in ("reason", "model")}))
    check("the site's own shipped content is not mistaken for saved work",
          report["saved"] == [] and os.path.isdir(os.path.join(drive, "models",
                                                               "BasicTerm_S")),
          "shipped %s is present and excluded" % SHIPPED_PATH)

    bridge = Bridge()
    bridge.prime()
    entry = bridge.dispatch("session.info", {})["models"][0]
    check("the sample has NO save location, and says which sample it is",
          entry["path"] is None and entry["sample"] == "BasicTerm_S"
          and entry["dirty"] is False, json.dumps(entry))

    # Read a value the way the panel does -- through the bridge, then the
    # post_execute the Pyodide kernel fires for every comm message. Being looked
    # at is NOT user work: a model that went dirty from being read would need a
    # confirmation for every close and would never step aside for a file.
    model = mx.get_models()["BasicTerm_S"]
    values = bridge.dispatch("value.get", {"nodes": [
        {"obj": "Projection.pv_net_cf", "args": []}]})
    pristine = values["values"][0]["value"]["v"]
    bridge.on_execute()
    check("the sample is the real lifelib model",
          abs(pristine - PV_NET_CF) < 1e-12, repr(pristine))
    check("reading values in a panel does not make a model dirty",
          bridge.dispatch("session.info", {})["models"][0]["dirty"] is False,
          "evaluate != edit")

    # === 2. CHANGE SOMETHING OBSERVABLE ==================================
    user_cell(bridge, lambda: setattr(model.Projection, "point_id", 2))
    changed = float(model.Projection.pv_net_cf())
    check("the change is observable in a number",
          changed != pristine and changed > 0,
          "point 1 %r -> point 2 %r" % (pristine, changed))
    check("user work in the Console marks the model dirty",
          bridge.dispatch("session.info", {})["models"][0]["dirty"] is True,
          "post_execute saw the model move")

    # === 3. SAVE AS ======================================================
    saved = bridge.dispatch("model.save", {"path": SAVE_PATH, "verify": "read"})
    check("Save As wrote a real model and read it back to prove it",
          saved["verified"] == "read" and (saved["bytes"] or 0) > 300000
          and saved["persistent"] is True,
          "%s bytes, verified %s" % (saved["bytes"], saved["verified"]))
    check("saving clears dirty and records the save location",
          saved["dirty"] is False
          and bridge.dispatch("session.info", {})["models"][0]["path"] == SAVE_PATH,
          SAVE_PATH)

    # === 3b. AND SAVING AGAIN MUST NOT DOUBLE THE QUOTA ==================
    #
    # From the same browser run as the loop above: "Saving over
    # /models/throwaway produced /models/throwaway_BAK1, 307 KiB, same
    # timestamp - modelx's backup-on-write. Nothing in the UI mentions it."
    # modelx keeps THREE rotations, so a visitor who saves four times had four
    # copies of their model in a quota they cannot see and a Files panel full of
    # folders they never made. Measured as bytes, because that is the thing that
    # runs out (section 9.8).
    before_bytes = _tree_bytes(drive)
    resaved = bridge.dispatch("model.save", {"path": SAVE_PATH})
    after_bytes = _tree_bytes(drive)
    check("a second Save over the same path leaves NO _BAK copy",
          not os.path.isdir(os.path.join(drive, "models",
                                         "BasicTerm_S_persist_test_BAK1"))
          and resaved["backup"]["paths"] == [],
          "nothing named *_BAK1 beside the visitor's model")
    check("...so the storage footprint does not grow on a re-save",
          after_bytes <= before_bytes * 1.01,
          "%d -> %d bytes in the drive" % (before_bytes, after_bytes))
    check("...and the reply says what happened instead of staying silent",
          resaved["backup"]["overwrote"] is True
          and resaved["backup"]["enabled"] is False
          and resaved["backup"]["kept"] is None
          and "replaced" in resaved["backup"]["note"],
          resaved["backup"]["note"])
    asked = bridge.dispatch("model.save", {"path": SAVE_PATH, "backup": True})
    check("a visitor who ASKS for a backup gets one, named and measured",
          asked["backup"]["kept"] == SAVE_PATH + "_BAK1"
          and os.path.isdir(os.path.join(drive, "models",
                                         "BasicTerm_S_persist_test_BAK1"))
          and asked["backup"]["bytes"] > 300000,
          asked["backup"]["note"])
    shutil.rmtree(os.path.join(drive, "models",
                               "BasicTerm_S_persist_test_BAK1"))
    check("the loop's own value survived all of that",
          float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf()) == changed,
          "still the visitor's number, not the sample's")

    # === 4. COME BACK: a new kernel, a new session =======================
    bridge = restart()
    report = samples.boot("BasicTerm_S")
    check("coming back: the bootstrap does NOT open the sample over their work",
          report["reason"] == "saved-models" and mx.get_models() == {},
          json.dumps(report["reason"]))
    check("...and reports what the visitor saved, so the UI can offer it",
          [e["path"] for e in report["saved"]] == [SAVE_PATH],
          json.dumps([e["path"] for e in report["saved"]]))
    check("...with the shipped sample content still excluded",
          SHIPPED_PATH not in [e["path"] for e in report["saved"]],
          "%d saved model(s) found" % len(report["saved"]))
    samples.build_sample("BasicTerm_S")
    rerun = samples.boot("BasicTerm_S")
    check("re-running the bootstrap cell touches nothing that is open",
          rerun["reason"] == "models-open" and sorted(mx.get_models())
          == ["BasicTerm_S"], json.dumps(rerun["reason"]))

    bridge = restart()
    opened = bridge.dispatch("model.open", {"path": SAVE_PATH})
    check("opening the saved path READS it", opened["reused"] is False
          and opened["path"] == SAVE_PATH and opened["model"] == "BasicTerm_S",
          json.dumps({k: opened[k] for k in ("model", "reused", "path")}))
    came_back = float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf())
    check("*** THE LOOP: the change comes back, not the pristine sample ***",
          came_back == changed and came_back != pristine,
          "%r (saved %r, pristine %r)" % (came_back, changed, pristine))
    check("...and the reply carries the file's own name",
          opened["saved_name"] == "BasicTerm_S" and "opened_as" not in opened,
          json.dumps(opened.get("saved_name")))

    # A second double click must NOT re-read: the model already IS that file,
    # and re-reading would throw away whatever the visitor has done since.
    user_cell(bridge, lambda: setattr(
        mx.get_models()["BasicTerm_S"].Projection, "point_id", 5))
    again = bridge.dispatch("model.open", {"path": SAVE_PATH})
    check("double-clicking the file that is already open reuses it",
          again["reused"] is True and again["path"] == SAVE_PATH,
          json.dumps({k: again[k] for k in ("reused", "path")}))
    check("...so live changes are never discarded by a double click",
          int(mx.get_models()["BasicTerm_S"].Projection.point_id) == 5,
          "point_id survived")

    # === 5. DEFECT 1: the sample squatting the name ======================
    # Exactly the live situation: a kernel that booted the sample, a visitor who
    # double-clicks their own saved file. 0.2.0 answered with the in-memory
    # sample and never touched the file.
    bridge = restart()
    samples.build_sample("BasicTerm_S")        # the old unconditional bootstrap
    bridge = Bridge()
    bridge.prime()
    check("DEFECT 1 setup: a pristine sample holds the name",
          abs(pv_net_cf(mx.get_models()["BasicTerm_S"], 1) - PV_NET_CF) < 1e-12
          and bridge.dispatch("session.info", {})["models"][0]["path"] is None,
          "BasicTerm_S open, no save location")
    opened = bridge.dispatch("model.open", {"path": SAVE_PATH})
    check("DEFECT 1: the file is read even though its name was taken",
          opened["reused"] is False and opened["path"] == SAVE_PATH,
          json.dumps({k: opened[k] for k in ("reused", "path")}))
    check("...the unchanged sample stepped aside, and said so",
          opened["replaced"]["model"] == "BasicTerm_S"
          and opened["replaced"]["sample"] == "BasicTerm_S"
          and "warning" in opened, opened.get("warning", "")[:66])
    check("...leaving exactly one model: the visitor's",
          sorted(mx.get_models()) == ["BasicTerm_S"]
          and float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf()) == changed,
          json.dumps(sorted(mx.get_models())))

    # === 6. DEFECT 2: the save location adopted by opening ===============
    # The sample is TOUCHED this time, so it is not the kernel's to close. The
    # file must still open -- beside it -- and the sample must come nowhere near
    # the visitor's path.
    bridge = restart()
    samples.build_sample("BasicTerm_S")
    bridge = Bridge()
    bridge.prime()
    sample = mx.get_models()["BasicTerm_S"]
    user_cell(bridge, lambda: setattr(sample.Projection, "point_id", 3))
    opened = bridge.dispatch("model.open", {"path": SAVE_PATH})
    check("a model the visitor has touched is never closed for a name",
          opened["opened_as"] == "BasicTerm_S_2"
          and sorted(mx.get_models()) == ["BasicTerm_S", "BasicTerm_S_2"],
          json.dumps(sorted(mx.get_models())))
    check("the file's own model is what was opened, and it is the file",
          opened["model"] == "BasicTerm_S_2" and opened["path"] == SAVE_PATH
          and float(mx.get_models()["BasicTerm_S_2"].Projection.pv_net_cf())
          == changed, json.dumps({k: opened[k] for k in ("model", "path")}))
    homes = bridge.dispatch("session.info", {})["models"]
    sample_entry = [m for m in homes if m["name"] == "BasicTerm_S"][0]
    check("*** DEFECT 2: opening a file gave the OTHER model no save location ***",
          sample_entry["path"] is None, json.dumps(sample_entry))
    fails("...so a plain Save of it is refused instead of overwriting the file",
          "bad_request",
          lambda: bridge.dispatch("model.save", {"model": "BasicTerm_S"}))
    on_disk = mx.read_model(os.path.join(drive, "models",
                                         "BasicTerm_S_persist_test"),
                            name="OnDisk")
    check("...and the visitor's file on disk still holds the visitor's value",
          float(on_disk.Projection.pv_net_cf()) == changed,
          "%r" % float(on_disk.Projection.pv_net_cf()))
    on_disk.close()

    # The explicit "give me what is already open" route must report the model's
    # OWN location. Echoing the request back is what the panel rendered as the
    # model moving to the visitor's file.
    reuse = bridge.dispatch("model.open", {"path": SAVE_PATH, "name": "BasicTerm_S",
                                           "on_conflict": "reuse"})
    check("on_conflict=reuse returns the open model, reading nothing",
          reuse["reused"] is True and reuse["model"] == "BasicTerm_S",
          json.dumps({k: reuse[k] for k in ("model", "reused")}))
    check("...and reports ITS save location (none), not the path asked for",
          reuse["path"] is None and reuse["requested"] == SAVE_PATH,
          json.dumps({"path": reuse["path"], "requested": reuse["requested"]}))
    check("...and still did not adopt it",
          bridge.dispatch("session.info", {})["models"][0]["path"] is None,
          "no save location may be created by opening something else")

    # === 7. The rule is structural, not a check ==========================
    with open(METHODS, encoding="utf-8") as handle:
        source = handle.read()
    writes = [line.strip() for line in source.splitlines()
              if re.search(r"self\._homes\[[^\]]*\]\s*=", line)]
    check("the save-location map has exactly ONE writer in the source",
          len(writes) == 1, " | ".join(writes) or "(none)")
    callers = re.findall(r"self\._set_home\([^,]+,[^,]+,\s*(\"[a-z]+\")", source)
    check("...reachable only from the read path and the save path",
          sorted(callers) == ['"read"', '"saved"'], ", ".join(callers) or "(none)")
    check("...and the setdefault that lost the work is gone",
          "_homes.setdefault" not in source, "_homes.setdefault")
    fails("a third way to create a save location is refused at runtime",
          "internal", lambda: bridge._set_home("BasicTerm_S", SAVE_PATH, "opened"))

    # === 8. model.close, the affordance the protocol had no method for ===
    fails("close refuses a model with changes and no save location",
          "bad_request",
          lambda: bridge.dispatch("model.close", {"model": "BasicTerm_S"}))
    refusal = None
    try:
        bridge.dispatch("model.close", {"model": "BasicTerm_S"})
    except BridgeError as err:
        refusal = err
    check("...and the refusal says exactly what is at stake",
          refusal is not None and refusal.data["dirty"] is True
          and refusal.data["path"] is None
          and refusal.data["sample"] == "BasicTerm_S",
          json.dumps(refusal.data if refusal else None))
    closed = bridge.dispatch("model.close", {"model": "BasicTerm_S", "force": True})
    check("force closes it, and reports that it was forced",
          closed["closed"] and closed["was_dirty"] and closed["forced"]
          and closed["models"] == ["BasicTerm_S_2"], json.dumps(closed))
    events = [e for e in bridge.drain_events()
              if e["params"]["model"] == "BasicTerm_S"]
    check("...and emits model.changed with reason closed",
          events and events[-1]["params"]["reason"] == "closed",
          json.dumps(events[-1]["params"]) if events else "(no event)")

    clean = bridge.dispatch("model.close", {"model": "BasicTerm_S_2"})
    check("a saved, unchanged model closes with no force at all",
          clean["closed"] and clean["was_dirty"] is False
          and clean["forced"] is False and mx.get_models() == {},
          json.dumps({k: clean[k] for k in ("was_dirty", "forced")}))

    opened_sample = bridge.dispatch("model.open_sample", {"sample": "BasicTerm_S"})
    check("open_sample reports the model has no save location",
          opened_sample["path"] is None and opened_sample["dirty"] is False,
          json.dumps(opened_sample))
    sample_close = bridge.dispatch("model.close", {})
    check("an untouched sample closes freely: open_sample rebuilds it",
          sample_close["closed"] and sample_close["forced"] is False,
          json.dumps({k: sample_close[k] for k in ("model", "forced")}))
    scratch = mx.new_model(name="Scratch")
    bridge.prime()
    fails("a model built in the Console cannot be closed unasked",
          "bad_request", lambda: bridge.dispatch("model.close", {"model": "Scratch"}))
    forced = bridge.dispatch("model.close", {"model": "Scratch", "force": True})
    check("...but force closes it and says it was forced",
          forced["closed"] and forced["forced"] is True and mx.get_models() == {},
          json.dumps({k: forced[k] for k in ("model", "forced")}))

    # === 9. on_conflict=replace is Revert, and it is guarded =============
    bridge = restart()
    bridge.dispatch("model.open", {"path": SAVE_PATH})
    reverted = mx.get_models()["BasicTerm_S"]
    user_cell(bridge, lambda: setattr(reverted.Projection, "point_id", 4))
    drifted = float(reverted.Projection.pv_net_cf())
    check("a change moves the value away from the saved one",
          drifted != changed, "%r vs saved %r" % (drifted, changed))
    fails("replace refuses to throw away unsaved changes", "bad_request",
          lambda: bridge.dispatch("model.open", {"path": SAVE_PATH,
                                                 "on_conflict": "replace"}))
    fails("the 0.2.0 spelling reload:true is refused too, not obeyed",
          "bad_request",
          lambda: bridge.dispatch("model.open", {"path": SAVE_PATH, "reload": True}))
    back = bridge.dispatch("model.open", {"path": SAVE_PATH,
                                          "on_conflict": "replace", "force": True})
    check("with force, replace re-reads the file: Revert",
          back["reused"] is False
          and float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf()) == changed,
          json.dumps({"reused": back["reused"]}))
    check("...and the re-read model is clean again",
          bridge.dispatch("session.info", {})["models"][0]["dirty"] is False,
          "dirty cleared by the read")

    # === 10. session.info is still the one source for the save location ==
    fresh_entry = bridge.dispatch("session.info", {})["models"][0]
    resaved = bridge.dispatch("model.save", {})
    check("a plain Save writes exactly what session.info shows",
          resaved["path"] == fresh_entry["path"] == SAVE_PATH,
          resaved["path"])
    check("...and the bridge advertises the methods that make this reachable",
          set(bridge.dispatch("session.info", {})["features"])
          >= {"model.close", "open.conflict"},
          json.dumps(bridge.dispatch("session.info", {})["features"]))

    # === 11. a model closed in the CONSOLE takes its save location with it ==
    # Nothing stops a visitor typing `BasicTerm_S.close()`. If the session kept
    # the save location behind, the next model to take that name would inherit
    # a path nobody pointed it at -- the same shape of bug as 0.2.0's, arriving
    # from the other direction.
    mx.get_models()["BasicTerm_S"].close()
    bridge.on_execute()
    check("a Console close drops the save location, the baseline and the sample",
          "BasicTerm_S" not in bridge._homes
          and "BasicTerm_S" not in bridge._baseline
          and samples.sample_of("BasicTerm_S") is None,
          json.dumps(sorted(bridge._homes)))
    shipped = bridge.dispatch("model.open", {"path": SHIPPED_PATH})
    check("...so the next file to take that name gets ITS own location",
          shipped["path"] == SHIPPED_PATH and shipped["reused"] is False,
          shipped["path"])
    check("...and it really is the other file: the pristine values are back",
          float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf()) == pristine,
          "%r" % float(mx.get_models()["BasicTerm_S"].Projection.pv_net_cf()))

    for name in list(mx.get_models()):
        mx.get_models()[name].close()
    files.set_storage_root(None)
    files.forget_storage()
    shutil.rmtree(root, ignore_errors=True)

    print("\n%d checks failed" % len(FAILURES))
    for label in FAILURES:
        print("  " + label)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
