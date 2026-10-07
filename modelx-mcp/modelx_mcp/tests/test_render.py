"""Codec values to text (spec 6), the output bound, and the import rules of
spec 3.1. Needs nothing.

    python -m modelx_mcp.tests.test_render

The import rules read CODE, not comments: they walk each module's syntax
tree, so a comment or docstring that explains why modelx is NOT imported can
never satisfy or trip them (CLAUDE.md: negative checks read code).
"""
import ast
import os

from modelx_mcp import render as R
from modelx_mcp.tests.checks import PACKAGE, check, finish, run_module, safe

PURE = ["refs.py", "render.py", "tools.py"]
FORBIDDEN = ("modelx", "modelx_bridge", "mcp", "numpy", "pandas")


def imported(path):
    """Every module name an import statement or an __import__/import_module
    call with a literal names, from the syntax tree only."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add(node.module or "")
        elif isinstance(node, ast.Call):
            fn = node.func
            called = (fn.id if isinstance(fn, ast.Name) else
                      fn.attr if isinstance(fn, ast.Attribute) else "")
            if called in ("__import__", "import_module") and node.args \
                    and isinstance(node.args[0], ast.Constant):
                names.add(str(node.args[0].value))
    return names


def main():
    # -- numbers and scalars (6.1) -------------------------------------------
    check("float: shortest round-trip repr", R.num(0.1 + 0.2) == "0.30000000000000004")
    check("int stays an int", R.scalar(622000) == "622000" and R.scalar(622000.0) == "622000.0")
    check("num tag NaN -> nan", R.scalar({"$t": "num", "v": "NaN"}) == "nan")
    check("num tag -Infinity -> -inf", R.scalar({"$t": "num", "v": "-Infinity"}) == "-inf")
    check("np int32 shows its dtype", R.scalar({"$t": "np", "dtype": "int32", "v": 3}) == "3 (int32)")
    check("np int64 does not", R.scalar({"$t": "np", "dtype": "int64", "v": 3}) == "3")
    check("tuple of one keeps its comma", R.scalar({"$t": "tuple", "v": [1]}) == "(1,)")
    check("None and bool as Python", R.scalar(None) == "None" and R.scalar(True) == "True")
    check("clipped str says how long it was",
          R.scalar({"$t": "str", "v": "abc", "len": 9000}) == "'abc'... [a string of 9000 chars, first 3 shown]")
    check("mx tag prints its display, never its obj",
          R.scalar({"$t": "mx", "kind": "ItemSpace", "obj": "Projection.__Space1",
                    "display": "Projection[2]"}) == "<ItemSpace Projection[2]>")
    check("an unknown tag prints its repr", R.scalar({"$t": "weird", "repr": "<W>"}) == "<W>")
    check("an unknown tag without repr prints its JSON",
          R.scalar({"$t": "weird2"}) == '{"$t": "weird2"}')
    check("a handle prints its head, not its id",
          R.scalar({"$t": "handle", "h": "h12", "kind": "Series", "dtype": "float64",
                    "shape": [4]}) == "Series float64 [4]")
    check("DataFrame head", R.head({"kind": "DataFrame", "shape": [1141, 5]}) == "DataFrame 1141x5")
    # -- the review of 2026-10-05: each check failed on the code it was found in --
    lab = {"$t": "tuple", "v": [{"$t": "opaque", "py": "builtins.int", "repr": "53"},
                                {"$t": "opaque", "py": "builtins.str", "repr": "'M'"}]}
    check("14: a MultiIndex label's opaque scalars print as the literal .loc[] takes",
          R.scalar(lab) == "(53, 'M')", R.scalar(lab))
    check("14: an opaque of any other type still says what it is",
          R.scalar({"$t": "opaque", "py": "decimal.Decimal", "repr": "Decimal('1.5')"})
          == "<decimal.Decimal Decimal('1.5')>")
    check("17: a dict with non-str keys says what it cut",
          R.scalar({"$t": "dict", "items": [[k, float(k)] for k in range(30)]}).endswith("19: 19.0, ... 10 more}"))
    big = {"$t": "handle", "h": "h9", "kind": "list", "dtype": None, "shape": None, "preview": None,
           "repr": "[" + ", ".join(str(k) for k in range(500)) + "..."}
    check("33: an undescribed handle's head never reads as empty",
          R.head(big) == "list (too large to send inline; its size is not sent)", R.head(big))
    check("33: its repr is cut at a fixed length, and says it was cut", R.unshaped(big) ==
          "list (too large to send inline; its size is not sent); its repr's first 300 chars, cut at that "
          "length even inside an element: " + big["repr"][:300], R.unshaped(big)[-60:])
    # A ", " inside a string element: the last one within 300 chars was cut at,
    # and "['a, b, ca, b, ..., b, ..." showed an element boundary that is none.
    strs = dict(big, repr=repr(["a, b, c" * 40 for _ in range(300)])[:2000] + "...")
    text = R.unshaped(strs)
    check("33: a cut never shows a fake element boundary: all 300 chars, and no ', ...' after them",
          text.endswith(strs["repr"][:R.REPR_CHARS]) and "cut at that length even inside an element" in text,
          text[-60:])
    z = {"$t": "handle", "h": "h3", "kind": "ndarray", "dtype": "float64", "shape": [], "preview": None,
         "repr": "array(5.)"}
    check("33: a 0-d ndarray's head says it holds one value, never '[]'",
          R.head(z) == "ndarray float64 0-d (one value)", R.head(z))
    got = safe(lambda: R.zero_d(z))
    check("33: a 0-d ndarray prints the value it holds, said to be read from its repr",
          got == "ndarray float64 0-d (one value): 5.0, read from its repr array(5.)", got)
    for rep, dtype, want in (("array(-3)", "int64", "-3"), ("array(nan)", "float64", "nan"),
                             ("array(True)", "bool", "True"), ("array(0.1, dtype=float32)", "float32", "0.1"),
                             ("array('x, dtype=y', dtype='<U10')", "<U10", "'x, dtype=y'")):
        got = safe(lambda: R.zero_d(dict(z, repr=rep, dtype=dtype)))
        check("33: 0-d %s -> %s" % (rep, want),
              got == "ndarray %s 0-d (one value): %s, read from its repr %s" % (dtype, want, rep), got)
    got = safe(lambda: R.zero_d(dict(z, dtype="object", repr="array([1, 2], dtype=object)")))
    check("33: a 0-d repr holding no plain value prints the repr as itself",
          got == "ndarray object 0-d (one value); its repr: array([1, 2], dtype=object)", got)
    from modelx_mcp.tools import formula_error_text
    tb = "\n".join(["Formula traceback:"] + ["%d: M.S.f(t=%d), line 2" % (k, -k) for k in range(8)] + ["..."]
                   + ["%d: M.S.f(t=%d), line 2" % (k, -k) for k in range(64991, 65001)]
                   + ["", "Formula source:", "def f(t):", "    return f(t - 1)"])
    text = formula_error_text("DeepReferenceError: too deep", {"formula_traceback": tb, "error_display": "M.S.f(t=-65000)"})
    check("10: a chain modelx elided counts the frames hidden from modelx's own numbers",
          "M.S.f(t=-5) -> ... 64990 more ... -> M.S.f(t=-64996)" in text, text)
    tb13 = "\n".join(["Formula traceback:"] + ["%d: M.S.f(t=%d), line 2" % (k, -k) for k in range(13)])
    text = formula_error_text("E", {"formula_traceback": tb13, "error_display": "M.S.f(t=-12)"})
    check("10: and a whole chain over 12 frames as before", "M.S.f(t=-5) -> ... 2 more ... -> M.S.f(t=-8)" in text, text)
    # -- statistics lines (B4 extremes) --------------------------------------
    st = {"kind": "numeric", "count": 151, "sum": 2.9656599999999997, "mean": 0.019640132450331124,
          "min": 0.0, "max": 0.03056000000000001, "argmin": 0, "argmax": 150,
          "argmin_label": 0, "argmax_label": 150, "nulls": 0}
    line = R.stats_line(st, 151, "whole column (all 151)", "year")
    check("argmax_label prints 'at year=150'", "max 0.03056000000000001 at year=150" in line, line)
    nd = dict(st, argmin=5279, argmax=1369, argmin_label=None, argmax_label=None)
    line = R.stats_line(nd, 10000, "whole column (all 10000)", None,
                        {1369: "policy_id 1370", 5279: "policy_id 5280"})
    check("an aligned ndarray prints 'at [1369] (policy_id 1370)'",
          "at [1369] (policy_id 1370)" in line and "at [5279] (policy_id 5280)" in line, line)
    line = R.stats_line(dict(nd, argmin=None, argmax=None), 4, "s")
    check("no position from the bridge: none printed", " at " not in line, line)
    check("count 0: 'no numbers', never zeros",
          R.stats_line({"kind": "numeric", "count": 0, "argmin": None}, 3, "s")
          == "s: no numbers (all 3 values are null)")
    check("integer stats arrive as strings and print as given",
          "sum 400" in R.stats_line({"kind": "integer", "count": 2, "sum": "400", "mean": 200.0,
                                      "min": "0", "max": "400"}, 2, "s"))
    check("text stats", R.stats_line({"kind": "text", "count": 4, "unique": 2, "nulls": 0}, 4, "s")
          == "s: 4 text values, 2 distinct, 0 null")
    # -- the bound (6.8) -----------------------------------------------------
    lines = ["x" * 99] * 300
    text = R.clip(lines, 12000, "get_value([...], offset=5)")
    check("clip stays within 12000", len(text) <= 12000, len(text))
    last = text.splitlines()[-1]
    check("clip counts what it dropped and names the call",
          last.startswith("[output bound 12000 chars reached: ") and "get_value([...], offset=5)]" in last
          and "more lines (" in last, last)
    dropped = int(last.split("reached: ")[1].split(" ")[0])
    check("clip's count is exact", dropped + len(text.splitlines()) - 1 == 300, dropped)
    check("clip of short text is the text", R.clip(["a", "b"], 2000, "x") == "a\nb")
    check("clip at the 2,000 minimum stays within it", len(R.clip(lines, 2000, "hint")) <= 2000)
    # calculate's cut hint makes a 219-char cut line; a fixed 200-char reserve
    # let this reach 12,019 chars.
    hint = ("NOT everything was computed: the lines at the top count what was refused or FAILED; "
            "get_value(refs) re-reads what was computed, without computing")
    text = R.clip(["x" * 99] * 118 + ["y" * 99] * 50, 12000, hint)
    check("clip stays within 12000 with calculate's long cut hint (19)",
          len(text) <= 12000 and text.splitlines()[-1].endswith(hint + "]"), len(text))
    from modelx_mcp import tools as TL
    check("that hint is calculate's", getattr(TL, "NOT_EVERYTHING", None) == hint)
    check("wrap keeps lines within 100 columns",
          all(len(x) <= 100 for x in R.wrap(["name%d(t)" % k for k in range(60)])))
    # -- import rules (3.1) --------------------------------------------------
    for name in PURE:
        got = imported(os.path.join(PACKAGE, name))
        bad = sorted(n for n in got if n.split(".")[0] in FORBIDDEN)
        check("%s imports none of modelx, modelx_bridge, mcp, numpy, pandas" % name, not bad, bad)
    for name in ("session.py", "python_tool.py", "server.py", "__main__.py"):
        got = imported(os.path.join(PACKAGE, name))
        check("%s does not import pandas or numpy" % name,
              not any(n.split(".")[0] in ("pandas", "numpy") for n in got), sorted(got))
    users = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py")
                   and any(m.split(".")[0] == "modelx_bridge"
                           for m in imported(os.path.join(PACKAGE, n))))
    check("only session.py imports modelx_bridge", users == ["session.py"], users)
    users = sorted(n for n in os.listdir(PACKAGE) if n.endswith(".py")
                   and any(m.split(".")[0] in ("modelx", "modelx_bridge")
                           for m in imported(os.path.join(PACKAGE, n))))
    check("only session.py and python_tool.py import modelx or modelx_bridge",
          users == ["python_tool.py", "session.py"], users)
    check("the rule would see a real import",
          "modelx_bridge" in imported(os.path.join(PACKAGE, "session.py")))
    return finish()


if __name__ == "__main__":
    run_module(main)
