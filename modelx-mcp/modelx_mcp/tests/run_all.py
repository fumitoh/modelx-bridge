"""Run every modelx-mcp suite and print one line per suite.

    python -m modelx_mcp.tests.run_all

modelx_bridge must be importable as well, installed or on PYTHONPATH. Every
suite runs with this package's parent folder first on its path.

Each suite runs in its own interpreter, because modelx keeps open models in a
process-global registry: a "fresh" model is only fresh in a fresh process.

A suite that cannot run here (test_lifelib without MODELX_MCP_MODELS, say)
prints a "skip" line and is listed as SKIPPED with its reason. It is never
counted as passed. Pass -v to see every check.

Each suite has SUITE_TIMEOUT seconds. Measured 2026-10-05: all eight together
take about 26 s on the cloud image. The first CI run of these suites was
cancelled at 15 minutes with no log to say which step hung, so a hang here
fails as TIMEOUT with what the suite printed, rather than lasting until the
job's own limit.
"""

import os
import subprocess
import sys

SUITE_TIMEOUT = 300

SUITES = ["test_refs", "test_render", "test_golden", "test_basicterm_s",
          "test_synthetic", "test_python_tool", "test_stdio", "test_lifelib"]
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def main(argv):
    verbose = "-v" in argv
    env = dict(os.environ)
    env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    rows, failed = [], 0
    for suite in SUITES:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "modelx_mcp.tests." + suite],
                cwd=PACKAGE_ROOT, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=SUITE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            print("TIMEOUT %s after %ds; its last output:\n%s"
                  % (suite, SUITE_TIMEOUT, partial[-2000:]))
            rows.append((suite, 0, 1, [], -1))
            failed += 1
            continue
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

    print("\n%-17s %6s %6s %8s %5s" % ("suite", "passed", "failed", "skipped", "exit"))
    print("-" * 46)
    total_ok = total_bad = 0
    skipped = []
    for suite, ok, bad, skips, code in rows:
        total_ok += ok
        total_bad += bad
        whole = bool(skips) and not ok and not bad
        if whole:
            skipped.append((suite, skips[0]))
        print("%-17s %6d %6d %8s %5d" % (suite, ok, bad,
                                          "SKIPPED" if whole else str(len(skips)), code))
    print("-" * 46)
    print("%-17s %6d %6d %8d" % ("total", total_ok, total_bad, len(skipped)))
    for suite, why in skipped:
        print("SKIPPED %s: %s" % (suite, why))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
