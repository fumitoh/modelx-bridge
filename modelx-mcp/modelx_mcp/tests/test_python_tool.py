"""run_python (spec 9): its output, and its change report.

    python -m modelx_mcp.tests.test_python_tool

Each case runs in a process of its own on a fresh BasicTerm_S, because the
report is about what moved and modelx's registry is process-wide.

The report is the Console's: Bridge.on_execute() after the code, an event
only for a model whose fingerprint moved. That detector is conservative, so
"no model change detected" must never read as proof, and the `dirty` flag
(the bridge's touch key, which sees a Reference assigned on a model with
nothing computed) is cross-checked.
"""
import os
import subprocess
import sys
import warnings

from modelx_mcp.tests.checks import PYTHON_ROOT, SHIPPED, check, finish

M = "BasicTerm_S"
P = M + ".Projection."
NOT_PROOF = "no model change detected -- which is not proof of none"
#: Each case's stdin: over stdio, fd 0 is the protocol, and input() in the
#: first cut read the client's next request (measured).
STDIN = "the next JSON-RPC request, which run_python must not read\n"


def session():
    from modelx_mcp.python_tool import PythonTool
    from modelx_mcp.session import Session
    journal = []
    S = Session(samples=[M], journal=journal.append)
    return S, PythonTool(S), journal


def case_report():
    S, py, journal = session()
    t = py.run("BasicTerm_S.Projection.point_id = 2")
    check("output starts with the time and 'NOT a citation'",
          t.startswith("ran in ") and "output is NOT a citation: re-read any number you will state "
                                      "with get_value or calculate" in t.splitlines()[0], t)
    check("a Reference set on a model with nothing computed: no event, and 'not proof of none'",
          NOT_PROOF in t and "model changed" not in t, t)
    check("... but dirty is cross-checked, so the change is still reported",
          t.endswith(". But BasicTerm_S now reports unsaved changes, so something did change"), t)
    t = py.run("m = BasicTerm_S\nprint(len(m.Projection.claims))\nm.Projection.pv_net_cf()")
    check("stdout and the final expression's value",
          "stdout:\n0\nvalue: 1181.5470031411787" in t, t)
    check("code that computes is reported as the user's own work (execute), with computed",
          "model changed: BasicTerm_S rev 2 (execute); computed nodes 0 -> 3632. Values and refs "
          "read before this call may be stale." in t, t)
    t = py.run("BasicTerm_S.Projection.disc_rate_ann")
    check("a Series value is rendered like any other, with whole-column statistics",
          "value: Series float64 [151] year {0: 0.0, 1: 0.00555" in t and
          "whole column (all 151): sum 2.9656599999999997" in t and "get_value(" not in t, t)
    check("... and says its rows were cut", "\n    more rows not shown\n" in t, t)
    t = S.tools.get_value([P + "point_id", P + "pv_net_cf()"])
    check("the readers see what run_python did: point_id = 2, and its value",
          "the Reference point_id = 2 selects" in t and
          P + "pv_net_cf() = 1181.5470031411787  [reads 4; read by 0 computed]" in t, t)
    check("run_python is journaled", [r["tool"] for r in journal][-2:] == ["run_python", "get_value"]
          and journal[-2]["args"] == {"code": "BasicTerm_S.Projection.disc_rate_ann"}, [r["tool"] for r in journal])
    # MEASURED on the first cut, which dropped every line holding a get_value
    # hint: a 10x20 ndarray printed 5 rows, 10 columns and 12 columns'
    # statistics, and nothing said what was cut.
    t = py.run("import numpy as np\nnp.arange(200.0).reshape(10, 20)")
    check("a value cut by rows and columns says what was not shown, and names no get_value call",
          "\n    5 more rows not shown\n" in t and "\n    statistics for 8 more columns not shown\n" in t
          and "\n    columns shown: 10 of 20\n" in t and "get_value(" not in t
          and "get_value cannot page this value (it has no ref)" in t, t)


def case_errors():
    S, py, _ = session()
    t = py.run("x = 1\n1/0")
    tb = t.split("exception:\n", 1)[-1]
    check("an exception is output, with the code's own frames only",
          "exception:\nTraceback (most recent call last):\n  File \"<run_python>\", line 2, in <module>\n"
          "ZeroDivisionError: division by zero" in t and "python_tool" not in tb, t)
    t = py.run("x + 41")
    check("the namespace survives between calls", "value: 42" in t, t)
    t = py.run("mx.__name__, sorted(n for n in globals() if n in ('mx', 'BasicTerm_S'))")
    check("mx and each open model are defined", "value: ('modelx', ['BasicTerm_S', 'mx'])" in t, t)
    t = py.run("print('y' * 5000)")
    check("output over 4,000 characters is cut, and the cut is counted",
          "\n... 1001 more characters of output not shown" in t and len(t) < 4600, len(t))
    t = py.run("raise SystemExit(3)")
    check("SystemExit is caught: it cannot stop the server", "SystemExit: 3" in t, t)
    t = py.run("raise ValueError('x' * 100000)")
    check("a 100,000-character exception message is cut to the bound (12,000), and the cut counted",
          len(t) <= 12000 and "ValueError: xxx" in t and " characters not shown ..." in t
          and NOT_PROOF in t, len(t))
    t = py.run("import sys; sys.stderr.write('to stderr\\n'); 7")
    check("stderr is captured with stdout", "stdout:\nto stderr\nvalue: 7" in t, t)
    t = py.run("def broken(:\n  pass")
    check("a syntax error is output, not raised", "exception:" in t and "SyntaxError" in t, t)


def case_flush():
    """Without bridge B1, and with one fingerprint per evaluating dispatch,
    what calculate computed is reported by the NEXT post_execute -- which
    must not be this code's report."""
    S, py, _ = session()
    S.tools.calculate([P + "pv_net_cf()"])
    t = S.tools.calculate([P + "pols_if(t=200)", P + "premiums(t=130)", P + "claims(t=140)"])
    check("calculate of three new nodes (one dispatch) computed them", "3 computed now" in t, t)
    t = py.run("1")
    check("a multi-node calculate is not reported as this code's change",
          t.startswith("ran in ") and "\nbefore this code ran: BasicTerm_S rev 4 (execute) -- earlier "
          "calculate calls computed more than the bridge had recorded" in t and NOT_PROOF in t
          and "model changed" not in t, t)
    t = S.tools.calculate([P + "claims(t=9999)"])
    check("a failed calculate", "formula error: KeyError" in t, t)
    t = py.run("2")
    check("what a failed calculate computed is not reported as this code's change either",
          "before this code ran: BasicTerm_S rev 5 (execute)" in t and "model changed" not in t, t)
    t = py.run("3")
    check("and once flushed, nothing is reported before the next call",
          "before this code ran" not in t and NOT_PROOF in t, t)


def case_models():
    S, py, _ = session()
    t = py.run("m = mx.new_model('Extra')")
    check("a model opened by the code is reported, and the instructions called out of date",
          "open models changed: opened Extra. The model list in the server instructions is now out of date." in t, t)
    t = py.run("Extra")
    check("the namespace picks up a model the code opened", "value: <" in t and "Extra" in t, t)
    t = py.run("Extra.close()")
    check("a closed model is reported, as unreadable now",
          "open models changed: closed Extra, which no other tool can read now. run_python no "
          "longer defines Extra. The model list in the server instructions is now out of date." in t, t)
    t = py.run("Extra")
    check("... and the namespace drops it: the first cut still computed a closed model",
          "NameError: name 'Extra' is not defined" in t, t)
    py.run("mx.new_model('Twice')")
    t = py.run("old = Twice\nTwice.close()\nnew = mx.new_model('Twice')")
    check("closed and opened again under one name: both reported, and the name said to move",
          "closed Twice, which no other tool can read now; opened Twice. Twice now names a different "
          "model: a ref printed before this call that starts Twice. reads the new one." in t
          and "no longer defines" not in t, t)
    t = py.run("Twice is new, old is new")
    check("... and the namespace binds the name to the new model", "value: (True, False)" in t, t)


def case_rename():
    """mx.read_model of an open model renames that model <name>_BAK1 and
    gives <name> to the new one; the first cut compared names and reported
    "opened BasicTerm_S_BAK1" and "computed nodes 1832 -> 0" (measured)."""
    S, py, _ = session()
    S.tools.calculate([P + "pv_net_cf()"])
    t = py.run("fresh = mx.read_model(%r)\nfresh.name" % SHIPPED)
    check("the rename is reported as one, with the model's own computed count",
          "renamed BasicTerm_S to BasicTerm_S_BAK1 (the same model; computed nodes 1832); opened "
          "BasicTerm_S." in t, t)
    check("... not as opening BasicTerm_S_BAK1, nor as BasicTerm_S losing its values",
          "opened BasicTerm_S_BAK1" not in t and "1832 -> 0" not in t, t)
    check("... and the old name is said to read a different model now",
          "BasicTerm_S now names a different model: a ref printed before this call that starts "
          "BasicTerm_S. reads the new one; the model it read is now BasicTerm_S_BAK1." in t, t)
    check("each event names the model it is about",
          "BasicTerm_S (opened by this code) rev " in t and "BasicTerm_S_BAK1 (was BasicTerm_S) rev " in t, t)
    check("modelx's own warning is in the output",
          "UserWarning: Existing model 'BasicTerm_S' renamed to 'BasicTerm_S_BAK1'" in t, t)
    t = py.run("BasicTerm_S is fresh, BasicTerm_S_BAK1.Projection.pv_net_cf()")
    check("the namespace follows the objects", "value: (True, 910.92066093366)" in t, t)


def case_stdin():
    """The code has no stdin. site's exit() and quit() close sys.stdin before
    they raise SystemExit: over stdio that was the protocol's input, and the
    server died with exit code 1, the call unanswered (measured)."""
    S, py, _ = session()
    t = py.run("x = input()\nx")
    check("input() raises EOFError, and reads nothing from the process's stdin",
          "EOFError: EOF when reading a line" in t and "must not read" not in t, t)
    for code in ("exit()", "quit()", "if True:\n    exit(3)"):
        t = py.run(code)
        check("%s is reported as SystemExit, and the process's stdin stays open"
              % code.split("\n")[-1].strip(), "\nSystemExit: " in t and not sys.stdin.closed, t)
    t = py.run("import sys\nsys.stdin.read()")
    check("sys.stdin reads empty", "value: ''" in t, t)
    check("the process's stdin is untouched", sys.stdin.readline() == STDIN)


def case_output():
    """Output is captured at the file-descriptor level: the first cut swapped
    sys.stdout and sys.stderr only, so os.system and a child process wrote
    past the capture, onto the MCP channel (measured)."""
    S, py, _ = session()
    t = py.run("import subprocess, sys\n"
               "subprocess.run([sys.executable, '-c', 'print(\"FROM-A-CHILD\")']).returncode")
    check("a child process's output is the code's output", "stdout:\nFROM-A-CHILD\nvalue: 0" in t, t)
    t = py.run("import os, sys\nprint('a', end='')\nos.write(1, b'b')\nsys.stderr.write('c')\n"
               "os.write(2, b'd')\nsys.__stdout__.write('e'); sys.__stdout__.flush()\nprint()")
    check("print, fd 1, stderr, fd 2 and sys.__stdout__, in the order written",
          "stdout:\nabcde\n" in t, t)
    # As mcp's lowlevel Server wraps every request: it records each warning and
    # logs it to the server's stderr, where the model never sees it.
    with warnings.catch_warnings(record=True) as recorded:
        t = py.run("import warnings\nwarnings.warn('VISIBLE-WARNING')\n1")
        t2 = py.run("import warnings\nwarnings.warn('VISIBLE-WARNING')\n2")
        t3 = py.run("import numpy as np\nnp.log(np.array([0.0, 1.0]))")
    check("a warning is the code's output, also under the server's catch_warnings(record=True)",
          "stdout:\n<run_python>:2: UserWarning: VISIBLE-WARNING\nvalue: 1" in t and not recorded,
          (t, [str(w.message) for w in recorded]))
    check("... and again on the next call: no registry hides a repeat",
          "UserWarning: VISIBLE-WARNING\nvalue: 2" in t2, t2)
    check("numpy's divide-by-zero RuntimeWarning is shown with the -inf it explains",
          "RuntimeWarning: divide by zero encountered in log" in t3 and "-inf" in t3, t3)


def case_bound():
    """--max-chars bounds run_python too. MEASURED on the first cut: a
    100,000-character exception came back as 100,420 characters, and at
    --max-chars 2000 print('z' * 50000) as 4,373."""
    from modelx_mcp.python_tool import PythonTool
    from modelx_mcp.session import Session
    S = Session(samples=[M], max_chars=2000)
    py = PythonTool(S)
    for code, what in (("raise ValueError('x' * 100000)", "an exception"),
                       ("print('z' * 50000)", "stdout"),
                       ("'y' * 50000", "a value"),
                       ("print('z' * 50000)\nraise ValueError('x' * 100000)", "stdout and an exception"),
                       ("print('z' * 5000)\n'y' * 50000", "stdout and a value")):
        t = py.run(code)
        check("--max-chars 2000 bounds %s; the cut is counted and the report kept" % what,
              len(t) <= 2000 and "not shown" in t and NOT_PROOF in t, "%d chars" % len(t))


CASES = {"report": case_report, "errors": case_errors, "flush": case_flush, "models": case_models,
         "rename": case_rename, "stdin": case_stdin, "output": case_output, "bound": case_bound}


def main(argv):
    if argv:
        CASES[argv[0]]()
        return finish()
    env = dict(os.environ)
    env["PYTHONPATH"] = PYTHON_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    code = 0
    for name in CASES:
        proc = subprocess.run([sys.executable, "-m", "modelx_mcp.tests.test_python_tool", name],
                              cwd=PYTHON_ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", input=STDIN)
        lines = proc.stdout.splitlines()
        for line in lines:
            if line.startswith(("ok  ", "FAIL")):
                print(line.replace("ok   ", "ok   [%s] " % name, 1).replace("FAIL ", "FAIL [%s] " % name, 1))
        if proc.returncode:
            code = 1
            if not any(x.startswith("FAIL") for x in lines):
                print("FAIL [%s] the case did not finish" % name)
                print((proc.stdout + proc.stderr)[-1500:])
    print("\n" + ("some case failed" if code else "all passed"))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
