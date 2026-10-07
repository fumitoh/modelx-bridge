"""The Files workflow: storage detection, paths, open / save / export / import.

    python -m modelx_bridge.tests.test_files

Runs against the REAL lifelib BasicTerm_S, found by shipped.sample_dir() -- in
lifelib Studio the same bytes the site ships -- because the load-bearing claim here is a value:
save the model, read the saved copy back, and get the identical number. A test
that only checked "a file appeared" would pass over a corrupt write.

The service-worker race is exercised, not assumed. `files.EMSCRIPTEN`,
`DRIVE_ROOT` and `FALLBACK_ROOT` are pointed at temporary directories so BOTH
browser branches run on a desktop: drive mounted (mode `drive`, persistent) and
drive absent (mode `temporary`, NOT persistent). The second is the branch the
demo actually hits when JupyterLite loses the race, and it is the one where a UI
would otherwise quietly claim a save that cannot survive a reload.
"""

import json
import os
import shutil
import sys
import tempfile

from modelx_bridge import Bridge, BridgeError, files, samples, split_buffers
from modelx_bridge.bundle import MODULES

from .shipped import sample_dir, stored_size

SHIPPED = sample_dir()

# Native ground truth for BasicTerm_S with Projection.point_id = 1.
PV_NET_CF = 910.92066093366
CLAIMS_0 = 34.18079328868595

FAILURES = []


def check(label, condition, detail=""):
    if not condition:
        FAILURES.append(label)
    print("%s %-52s %s" % ("ok  " if condition else "FAIL", label, detail))


def fails(label, code, call):
    """The call must raise BridgeError with `code`, and nothing else."""
    try:
        call()
    except BridgeError as err:
        check(label, err.code == code, "%s: %s" % (err.code, err.message[:70]))
        return err
    except Exception as exc:
        check(label, False, "raised %r instead of BridgeError" % (exc,))
        return None
    check(label, False, "did not raise")
    return None


def pv_net_cf(model):
    model.Projection.point_id = 1
    return float(model.Projection.pv_net_cf())


def _modelx_max_backups():
    """modelx's own rotation count, read from modelx rather than restated here."""
    from modelx.serialize import DEFAULT_MAX_BACKUPS
    return int(DEFAULT_MAX_BACKUPS)


def info_block_has_backup(bridge):
    """The storage block must carry the backup policy, not only the save reply.

    A Save dialog has to say what it is about to do BEFORE the write; a field
    that only appears afterwards is an explanation, not a choice.
    """
    block = bridge.dispatch("storage.info", {})["storage"]
    return (block.get("backup_default") is False
            and isinstance(block.get("backup_reason"), str)
            and block.get("max_backups") == _modelx_max_backups())


def main():
    import modelx as mx

    root = tempfile.mkdtemp(prefix="lifelib-files-")
    drive = os.path.join(root, "drive")
    memfs = os.path.join(root, "memfs")
    os.makedirs(drive)
    # Without pip's __pycache__: an installed copy has two (shipped.installed_copy),
    # and "files.list sizes a model directory" then measured 353,614 bytes.
    shutil.copytree(SHIPPED, os.path.join(drive, "models", "BasicTerm_S"),
                    ignore=shutil.ignore_patterns("__pycache__"))

    # -- the flat-bundle namespace ----------------------------------------
    # The bootstrap concatenates every module into ONE namespace, so a name
    # defined twice is a silent override with no error anywhere. files.py and
    # samples.py each carry their own "is this a model directory" check for
    # exactly this reason; this check is what stops the next one colliding.
    # A collision is two DIFFERENT objects under one name. `sys` bound by five
    # modules, or MAX_MESSAGE_BYTES re-exported from codec into methods, is the
    # same object each time and the flattening is harmless; two different
    # functions called `_is_model_dir` is a silent override.
    seen, clashes = {}, []
    for name in MODULES:
        module = sys.modules["modelx_bridge." + name]
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            previous = seen.get(attr)
            if previous is not None and previous[1] is not value:
                clashes.append("%s (%s vs %s)" % (attr, previous[0], name))
            seen[attr] = (name, value)
    check("no name collides in the flat bundle namespace", not clashes,
          ", ".join(clashes) or "%d names across %d modules"
          % (len(seen), len(MODULES)))
    check("files.FALLBACK_ROOT equals samples.STAGE_ROOT",
          files.FALLBACK_ROOT == samples.STAGE_ROOT,
          "%s / %s" % (files.FALLBACK_ROOT, samples.STAGE_ROOT))

    # -- desktop: no root is guessed --------------------------------------
    files.set_storage_root(None)
    files.forget_storage()
    info = files.storage_info(refresh=True)
    check("desktop mode is local with no root", info["mode"] == "local"
          and not info["root"] and not info["writable"], json.dumps(info["mode"]))
    check("local mode explains what to do", "set_storage_root" in (info["reason"] or ""),
          (info["reason"] or "")[:60])
    bridge = Bridge()
    bridge.dispatch("model.open_sample", {"sample": "termlife_synthetic"})
    fails("save with no storage root is bad_request, not a silent write",
          "bad_request", lambda: bridge.dispatch("model.save", {"path": "/x"}))
    mx.get_models()["TermLife_S"].close()

    # -- browser, drive mounted -------------------------------------------
    files.EMSCRIPTEN = True
    files.DRIVE_ROOT = drive
    files.FALLBACK_ROOT = memfs
    files.forget_storage()
    info = files.storage_info(refresh=True)
    check("drive mounted -> mode drive, persistent",
          info["mode"] == "drive" and info["persistent"] and info["writable"],
          json.dumps({k: info[k] for k in ("mode", "persistent", "writable")}))
    check("a mounted drive leaves no probe file behind",
          files.PROBE_NAME not in os.listdir(drive), files.PROBE_NAME)

    bridge = Bridge()
    info = bridge.dispatch("session.info", {})
    check("session.info carries the storage block",
          info["storage"]["mode"] == "drive", json.dumps(info["storage"]["mode"]))
    check("session.info advertises features",
          set(info["features"]) >= {"table.get", "buffers", "files", "storage"},
          json.dumps(info["features"]))
    check("limits gained the buffer caps",
          info["limits"]["max_buffer_bytes"] > 0
          and info["limits"]["max_page_cells"] > 0,
          json.dumps({k: v for k, v in info["limits"].items() if "buffer" in k
                      or "page" in k}))

    # -- paths -------------------------------------------------------------
    check("virtual path from a bare name",
          files.to_virtual("models/BasicTerm_S") == "/models/BasicTerm_S",
          files.to_virtual("models/BasicTerm_S"))
    check("virtual path strips the real root",
          files.to_virtual(os.path.join(drive, "models")) == "/models",
          files.to_virtual(os.path.join(drive, "models")))
    # In production DRIVE_ROOT IS "/drive", and a frontend may hold a path from
    # a session in which the drive was mounted. It must keep working.
    patched, files.DRIVE_ROOT = files.DRIVE_ROOT, "/drive"
    check("virtual path strips /drive whatever the active root is",
          files.to_virtual("/drive/models/x") == "/models/x",
          files.to_virtual("/drive/models/x"))
    files.DRIVE_ROOT = patched
    check("interior .. resolves", files.to_virtual("/a/b/../c") == "/a/c",
          files.to_virtual("/a/b/../c"))
    for hostile in ("/..", "../etc", "/models/../../x"):
        fails("traversal %r is refused" % hostile, "bad_request",
              lambda p=hostile: files.to_virtual(p))
    fails("a NUL in a path is refused", "bad_request",
          lambda: files.to_virtual("/a\x00b"))

    # -- files.list --------------------------------------------------------
    listing = bridge.dispatch("files.list", {"path": "/models"})
    entry = listing["entries"][0]
    check("files.list finds the shipped model",
          entry["name"] == "BasicTerm_S" and entry["kind"] == "model"
          and entry["is_model"] and entry["model_format"] == "folder",
          json.dumps({k: entry[k] for k in ("name", "kind", "model_format")}))
    check("files.list reports the model's own name",
          entry["model_name"] == "BasicTerm_S", str(entry["model_name"]))
    # files.list sizes what is on disk, so it is held to an independent walk of
    # the same directory; the walk is held to the size git stores (LF, see
    # shipped.py), which is also the size of lifelib 0.17.1's own copy.
    listed_dir = os.path.join(drive, "models", "BasicTerm_S")
    walked = [os.path.join(root, name)
              for root, _dirs, names in os.walk(listed_dir) for name in names]
    checkout_total = sum(os.path.getsize(p) for p in walked)
    stored_total = sum(stored_size(p) for p in walked)
    check("files.list sizes a model directory",
          entry["size"] == checkout_total and not entry["size_capped"]
          and stored_total == 324563,
          "%s bytes on disk, %d as stored" % (entry["size"], stored_total))
    check("files.list carries storage and parent",
          listing["storage"]["mode"] == "drive" and listing["parent"] == "/",
          listing["parent"])
    fails("files.list of a missing directory is not_found", "not_found",
          lambda: bridge.dispatch("files.list", {"path": "/nope"}))

    # -- model.open --------------------------------------------------------
    opened = bridge.dispatch("model.open", {"path": "/models/BasicTerm_S"})
    check("model.open reads the shipped model",
          opened["model"] == "BasicTerm_S" and not opened["reused"]
          and opened["model_format"] == "folder",
          json.dumps({k: opened[k] for k in ("model", "reused", "path")}))
    model = mx.get_models()["BasicTerm_S"]
    value = pv_net_cf(model)
    check("the opened model matches native ground truth",
          abs(value - PV_NET_CF) < 1e-12, repr(value))
    again = bridge.dispatch("model.open", {"path": "/models/BasicTerm_S"})
    check("model.open is idempotent, not a silent _BAK rename",
          again["reused"] and sorted(mx.get_models()) == ["BasicTerm_S"],
          json.dumps(sorted(mx.get_models())))
    check("session.info reports where the model lives",
          bridge.dispatch("session.info", {})["models"][0]["path"]
          == "/models/BasicTerm_S",
          json.dumps(bridge.dispatch("session.info", {})["models"][0]))
    fails("model.open of a plain directory is not_found", "not_found",
          lambda: bridge.dispatch("model.open", {"path": "/models"}))

    # -- model.save: THE ROUND TRIP ----------------------------------------
    saved = bridge.dispatch("model.save", {"path": "/work/BT", "verify": "read"})
    check("model.save wrote bytes", (saved["bytes"] or 0) > 300000,
          "%s bytes" % saved["bytes"])
    check("model.save verified by reading it back",
          saved["verified"] == "read" and saved["verified_spaces"] == 1,
          json.dumps({k: saved[k] for k in ("verified", "verified_spaces")}))
    check("model.save reports persistence honestly",
          saved["persistent"] is True and saved["storage"]["mode"] == "drive",
          json.dumps(saved["persistent"]))
    check("the read-back left no extra model open",
          sorted(mx.get_models()) == ["BasicTerm_S"], json.dumps(sorted(mx.get_models())))

    reopened = mx.read_model(saved["real_path"], name="RoundTripFolder")
    value2 = pv_net_cf(reopened)
    check("ROUND TRIP write -> read -> identical value",
          value2 == value == PV_NET_CF, "%r vs %r" % (value2, value))
    check("round trip: claims(0) identical too",
          float(reopened.Projection.claims(0)) == CLAIMS_0,
          repr(float(reopened.Projection.claims(0))))
    reopened.close()

    check("save-as moved the save location",
          bridge._homes["BasicTerm_S"] == "/work/BT", bridge._homes["BasicTerm_S"])
    resaved = bridge.dispatch("model.save", {})
    check("model.save with no path saves in place",
          resaved["path"] == "/work/BT" and resaved["verified"] == "exists",
          resaved["path"])
    fails("an unknown verify level is refused", "bad_request",
          lambda: bridge.dispatch("model.save", {"verify": "probably"}))

    # -- export, with the archive as a binary buffer -----------------------
    envelope = bridge.handle({"type": "req", "id": "e1",
                              "method": "model.export_zip",
                              "params": {"download": True}})
    data, buffers = split_buffers(envelope)
    exported = data["result"]
    tag = exported["download"]
    check("model.export_zip wrote a zip into storage",
          exported["path"] == "/exports/BasicTerm_S.zip" and exported["bytes"] > 100000,
          "%s, %s bytes" % (exported["path"], exported["bytes"]))
    check("export returned the archive as buffer 0",
          tag["$t"] == "bin" and tag["buffer"] == 0
          and len(buffers) == 1 and len(buffers[0]) == tag["bytes"],
          json.dumps({k: tag[k] for k in ("$t", "buffer", "bytes", "filename")}))
    check("split_buffers leaves the data JSON-serialisable",
          "buffers" not in data and isinstance(json.dumps(data), str),
          "%d bytes of json" % len(json.dumps(data)))
    check("export did NOT move the save location",
          bridge._homes["BasicTerm_S"] == "/work/BT", bridge._homes["BasicTerm_S"])

    # -- import the very bytes we exported ---------------------------------
    envelope = bridge.handle(
        {"type": "req", "id": "i1", "method": "model.import_zip",
         "params": {"filename": "round trip/../BasicTerm_S.zip", "name": "Imported"}},
        buffers=[buffers[0]])
    imported = envelope["result"]
    check("model.import_zip staged the upload",
          imported["wrote"]["bytes"] == tag["bytes"]
          and imported["model"] == "Imported",
          json.dumps({"bytes": imported["wrote"]["bytes"],
                      "path": imported["wrote"]["path"]}))
    check("a hostile filename cannot escape /imports",
          imported["wrote"]["path"].startswith("/imports/")
          and "/" not in imported["wrote"]["path"][len("/imports/"):],
          imported["wrote"]["path"])
    value3 = pv_net_cf(mx.get_models()["Imported"])
    check("ROUND TRIP export -> buffer -> import -> identical value",
          value3 == PV_NET_CF, repr(value3))
    mx.get_models()["Imported"].close()

    reopen = bridge.dispatch("model.import_zip",
                             {"path": imported["wrote"]["path"], "name": "FromPath"})
    check("model.import_zip also opens a zip already in storage",
          reopen["model"] == "FromPath" and reopen["model_format"] == "zip",
          reopen["path"])
    check("a zip in storage is listed as a model",
          [e for e in bridge.dispatch("files.list", {"path": "/imports"})["entries"]
           if e["is_model"] and e["model_format"] == "zip"] != [],
          json.dumps(bridge.dispatch("files.list", {"path": "/imports"})["count"]))
    mx.get_models()["FromPath"].close()

    fails("import with neither a path nor a buffer is bad_request", "bad_request",
          lambda: bridge.dispatch("model.import_zip", {}))
    junk = bridge.handle({"type": "req", "id": "i2", "method": "model.import_zip",
                          "params": {"filename": "junk.zip"}},
                         buffers=[b"not a zip at all"])
    check("a non-model upload fails with bad_request, after being written",
          junk["error"]["code"] == "bad_request"
          and os.path.isfile(os.path.join(drive, "imports", "junk.zip")),
          junk["error"]["message"][:60])
    check("a failed request carries no buffers", "buffers" not in junk,
          json.dumps(sorted(junk)))

    # -- backups (section 9.8) ---------------------------------------------
    #
    # THE BUG: "Saving over /models/throwaway produced /models/throwaway_BAK1,
    # 307 KiB, same timestamp - modelx's backup-on-write. Nothing in the UI
    # mentions it." Two halves are checked here: that a browser kernel does not
    # do it unasked, and that whatever it DOES do is in the reply.
    check("backup naming is parsed off the whole basename, .zip included",
          files.backup_of("BasicTerm_S_BAK1") == ("BasicTerm_S", 1)
          and files.backup_of("BT.zip_BAK12") == ("BT.zip", 12)
          and files.backup_of("BasicTerm_S") == (None, 0)
          and files.backup_of("_BAK") == (None, 0)
          and files.backup_of("x_BAKn") == (None, 0),
          "modelx appends _BAK<n> to the path it was given, extension and all")
    check("the kernel asks modelx for the rotation count, not a guess",
          files.backup_limit() == _modelx_max_backups(),
          "max_backups = %d" % files.backup_limit())
    enabled, why = files.backup_default(files.MODE_DRIVE)
    check("BROWSER DEFAULT: a plain save keeps no backup", enabled is False,
          why[:66])
    check("...and says why in one line a UI can show",
          "quota" in why and str(files.backup_limit()) in why, why[:80])
    check("a kernel with no persistent filesystem keeps none either",
          files.backup_default(files.MODE_TEMPORARY)[0] is False,
          files.backup_default(files.MODE_TEMPORARY)[1][:60])
    check("LOCAL DEFAULT: a desktop install keeps modelx's backup",
          files.backup_default(files.MODE_LOCAL)[0] is True,
          files.backup_default(files.MODE_LOCAL)[1][:60])
    check("the storage block carries the default, so a UI can say it BEFORE saving",
          info_block_has_backup(bridge),
          json.dumps({k: v for k, v in
                      bridge.dispatch("storage.info", {})["storage"].items()
                      if k.startswith("backup") or k == "max_backups"}))

    first = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                           "path": "/work/BAK"})
    check("a first save reports no backup, and made none",
          first["backup"]["enabled"] is False
          and first["backup"]["overwrote"] is False
          and first["backup"]["kept"] is None
          and first["backup"]["paths"] == [],
          first["backup"]["note"])
    second = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                            "path": "/work/BAK"})
    check("SAVING TWICE IN A BROWSER DOES NOT DOUBLE STORAGE",
          second["backup"]["paths"] == []
          and not os.path.exists(os.path.join(drive, "work", "BAK_BAK1")),
          "no BAK_BAK1 beside %s" % os.path.join(drive, "work", "BAK"))
    check("...and the reply still says the old version is gone",
          second["backup"]["overwrote"] is True
          and second["backup"]["kept"] is None
          and "replaced" in second["backup"]["note"],
          second["backup"]["note"])

    kept = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                          "path": "/work/BAK", "backup": True})
    check("backup: true keeps one and NAMES it",
          kept["backup"]["kept"] == "/work/BAK_BAK1"
          and kept["backup"]["policy"] == "on"
          and os.path.isdir(os.path.join(drive, "work", "BAK_BAK1")),
          kept["backup"]["note"])
    check("...and reports what it costs, which is the whole point",
          kept["backup"]["paths"] == ["/work/BAK_BAK1"]
          and kept["backup"]["bytes"] > 300000,
          "%s bytes" % kept["backup"]["bytes"])
    rotated = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                             "path": "/work/BAK", "backup": True})
    check("a second backed-up save rotates, and both copies are reported",
          rotated["backup"]["paths"] == ["/work/BAK_BAK1", "/work/BAK_BAK2"]
          and rotated["backup"]["bytes"] > 600000,
          rotated["backup"]["note"])
    leftover = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                              "path": "/work/BAK"})
    check("backups left by earlier saves are still reported when the default is off",
          leftover["backup"]["enabled"] is False
          and leftover["backup"]["kept"] is None
          and len(leftover["backup"]["paths"]) == 2
          and "2 backups" in leftover["backup"]["note"],
          leftover["backup"]["note"])
    listing = bridge.dispatch("files.list", {"path": "/work"})
    names = [e["name"] for e in listing["entries"]]
    backups = [e for e in listing["entries"] if e["is_backup"]]
    check("files.list LABELS the _BAK folders the visitor never created",
          len(backups) == 2
          and backups[0]["backup_of"] == "/work/BAK"
          and backups[0]["backup_slot"] == 1,
          json.dumps([{"name": e["name"], "backup_of": e["backup_of"]}
                      for e in backups]))
    check("...and sorts them below the model they belong to",
          names.index("BAK") < names.index("BAK_BAK1"), ", ".join(names))
    check("the boot scan does not offer a _BAK folder as the visitor's own work",
          [m for m in samples.saved_models() if "_BAK" in m["path"]] == [],
          ", ".join(m["path"] for m in samples.saved_models()))
    fails("params.backup must be true, false or 'auto'", "bad_request",
          lambda: bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                                 "path": "/work/BAK",
                                                 "backup": "yes please"}))
    explicit = bridge.dispatch("model.save", {"model": "BasicTerm_S",
                                              "path": "/work/BAK",
                                              "backup": "auto"})
    check("'auto' is spelled out and means the same as leaving it out",
          explicit["backup"]["policy"] == "auto"
          and explicit["backup"]["enabled"] is False, "policy auto")

    zipped = bridge.dispatch("model.export_zip",
                             {"model": "BasicTerm_S", "path": "/work/BAK.zip"})
    again_zip = bridge.dispatch("model.export_zip",
                                {"model": "BasicTerm_S", "path": "/work/BAK.zip"})
    check("export_zip obeys the same policy: no _BAK1 archive appears",
          zipped["backup"]["enabled"] is False
          and again_zip["backup"]["paths"] == []
          and not os.path.exists(os.path.join(drive, "work", "BAK.zip_BAK1")),
          "re-exporting over an archive does not keep a second copy")

    # A LOCAL install is the other half of the decision: there the backup is
    # visible, affordable and worth having, so modelx's own default stands.
    local_root = os.path.join(root, "local")
    files.set_storage_root(local_root, files.MODE_LOCAL)
    local = Bridge()
    local.dispatch("model.save", {"model": "BasicTerm_S", "path": "/L"})
    local_saved = local.dispatch("model.save", {"model": "BasicTerm_S",
                                                "path": "/L"})
    check("LOCAL: the same plain save DOES keep a backup, unasked",
          local_saved["backup"]["enabled"] is True
          and local_saved["backup"]["kept"] == "/L_BAK1"
          and os.path.isdir(os.path.join(local_root, "L_BAK1")),
          local_saved["backup"]["note"])
    off = local.dispatch("model.save", {"model": "BasicTerm_S", "path": "/L2",
                                        "backup": False})
    check("...and backup: false turns it off there too",
          off["backup"]["policy"] == "off" and off["backup"]["enabled"] is False,
          off["backup"]["note"])
    files.set_storage_root(None)
    files.DRIVE_ROOT = drive
    files.forget_storage()
    files.storage_info(refresh=True)

    # -- browser, drive ABSENT: the service-worker race --------------------
    files.DRIVE_ROOT = os.path.join(root, "no-such-drive")
    files.forget_storage()
    info = files.storage_info(refresh=True)
    check("no drive -> mode temporary, NOT persistent",
          info["mode"] == "temporary" and info["persistent"] is False
          and info["writable"], json.dumps({k: info[k] for k in
                                            ("mode", "persistent", "writable")}))
    check("degraded mode names the service worker in its reason",
          "service worker" in (info["reason"] or ""), (info["reason"] or "")[:70])

    degraded = Bridge()
    model = mx.get_models()["BasicTerm_S"]
    saved = degraded.dispatch("model.save", {"model": "BasicTerm_S",
                                             "path": "/fallback/BT",
                                             "verify": "read"})
    check("save still WORKS with no drive",
          saved["verified"] == "read" and (saved["bytes"] or 0) > 300000,
          "%s bytes into %s" % (saved["bytes"], saved["storage"]["mode"]))
    check("...and says it will NOT survive the tab",
          saved["persistent"] is False
          and saved["storage"]["mode"] == "temporary",
          json.dumps({"persistent": saved["persistent"],
                      "mode": saved["storage"]["mode"]}))
    check("the fallback wrote under FALLBACK_ROOT, not the missing drive",
          os.path.isfile(os.path.join(memfs, "fallback", "BT", "_system.json")),
          os.path.join(memfs, "fallback", "BT"))
    fallback = mx.read_model(os.path.join(memfs, "fallback", "BT"), name="Fallback")
    check("ROUND TRIP in degraded mode: identical value",
          pv_net_cf(fallback) == PV_NET_CF, repr(pv_net_cf(fallback)))
    fallback.close()

    envelope = degraded.handle({"type": "req", "id": "e2",
                                "method": "model.export_zip",
                                "params": {"model": "BasicTerm_S",
                                           "download": True}})
    _data, degraded_buffers = split_buffers(envelope)
    check("export-to-buffer works with no drive: the only way out of the tab",
          len(degraded_buffers) == 1 and len(degraded_buffers[0]) > 100000,
          "%d bytes" % len(degraded_buffers[0]))

    # -- and the race healing ---------------------------------------------
    files.DRIVE_ROOT = drive
    healed = degraded.dispatch("storage.info", {})["storage"]
    check("a drive that appears later flips the mode with no restart",
          healed["mode"] == "drive" and healed["persistent"],
          json.dumps({k: healed[k] for k in ("mode", "persistent")}))

    for name in list(mx.get_models()):
        mx.get_models()[name].close()
    files.set_storage_root(None)
    shutil.rmtree(root, ignore_errors=True)

    print("\n%d checks failed" % len(FAILURES))
    for label in FAILURES:
        print("  " + label)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
