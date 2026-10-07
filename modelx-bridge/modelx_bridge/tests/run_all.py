"""Run every kernel-side suite and print one pass/fail line per suite.

    python -m modelx_bridge.tests.run_all

Each suite runs in its own interpreter. They are not independent otherwise:
modelx keeps open models in a process-global registry, and test_bootstrap and
test_events both replace `comm` and `IPython` in sys.modules, so running them in
one process would make the order matter.

A check that cannot run here prints a line starting "skip " with the reason --
test_model's native_reference.json and lifelib drift checks do, outside lifelib
Studio's checkout. Those lines are counted and listed as SKIPPED, never as
passed.

Pass -v to see every check.
"""

import os
import subprocess
import sys

SUITES = ["test_v0", "test_tables", "test_cells_page", "test_events",
          "test_model", "test_files", "test_persist", "test_edit", "test_ref",
          "test_doc", "test_trace", "test_mcp_support",
          "test_bootstrap"]
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def main(argv):
    verbose = "-v" in argv
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    rows, failed = [], 0
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "modelx_bridge.tests." + suite],
            cwd=PACKAGE_ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + (proc.stderr or "")
        if verbose:
            print(out)
        lines = out.splitlines()
        ok = sum(1 for line in lines if line.startswith("ok  "))
        bad = sum(1 for line in lines if line.startswith("FAIL"))
        skips = [line[5:] for line in lines if line.startswith("skip ")]
        rows.append((suite, ok, bad, skips, proc.returncode))
        failed += bad or (1 if proc.returncode else 0)
        if proc.returncode and not bad:
            print(out[-2000:])

    print("\n%-16s %6s %6s %7s %5s" % ("suite", "passed", "failed", "skipped", "exit"))
    print("-" * 44)
    total_ok = total_bad = 0
    skipped = []
    for suite, ok, bad, skips, code in rows:
        total_ok += ok
        total_bad += bad
        skipped.extend((suite, why) for why in skips)
        print("%-16s %6d %6d %7d %5d" % (suite, ok, bad, len(skips), code))
    print("-" * 44)
    print("%-16s %6d %6d %7d" % ("total", total_ok, total_bad, len(skipped)))
    for suite, why in skipped:
        print("SKIPPED in %s: %s" % (suite, why))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
