"""The ref grammar (spec 5.1) and derived arithmetic (8.4). Needs nothing.

    python -m modelx_mcp.tests.test_refs
"""
from modelx_mcp.refs import MAX_NODES, Range, RefError, clean, parse
from modelx_mcp.tests.checks import check, finish, run_module
from modelx_mcp.tools import derived_expr, eval_derived


def refused(label, text, fragment):
    try:
        parse(text)
    except RefError as e:
        check(label, fragment in str(e), str(e))
        return
    check(label, False, "parsed")


def main():
    P = "BasicTerm_S.Projection."
    # -- the section 5.1 table ------------------------------------------------
    check("model, Space and keyword call",
          parse(P + "claims(t=3)") == [("name", "BasicTerm_S"), ("name", "Projection"),
                                       ("name", "claims"), ("call", ([], {"t": 3}))])
    check("positional call", parse("Projection.claims(3)")[-1] == ("call", ([3], {})))
    check("subscript on a name is a sub segment", parse("claims[3]") == [("name", "claims"), ("sub", [3])])
    check("ItemSpace by subscript",
          parse(P[:-1] + "[2].pv_net_cf()")[2:] == [("sub", [2]), ("name", "pv_net_cf"),
                                                    ("call", ([], {}))])
    check("ItemSpace by call, keyword",
          parse("Projection(point_id=2).pv_net_cf()")[:2] == [("name", "Projection"),
                                                               ("call", ([], {"point_id": 2}))])
    check("both quote styles read the same",
          parse("claims(t=5, kind='DEATH')") == parse('claims(t=5, kind="DEATH")'))
    check("a slice", parse("pols_if[0:121]")[-1] == ("slice", (0, 121, None)))
    seg = parse("av_pp_at(t=range(0, 121), timing='BEF_PREM')")[-1][1][1]
    check("range() in one argument is a Range of 121 values",
          isinstance(seg["t"], Range) and len(seg["t"].values()) == 121 and seg["timing"] == "BEF_PREM")
    check("range(5) means range(0, 5)", repr(parse("f(range(5))")[-1][1][0][0]) == "range(0, 5)")
    check(".loc[label]", parse("premiums(t=0).loc[7342]")[-1] == ("loc", 7342))
    check(".loc[(53, 10)] keeps the tuple", parse("premium_table.loc[(53, 10)]")[-1] == ("loc", (53, 10)))
    check(".iloc[0]", parse("x().iloc[0]")[-1] == ("iloc", 0))
    check("[i] after a call", parse("pv_net_cf()[7341]")[-1] == ("sub", [7341]))
    check('["col"] after a call', parse('result_pols()["pols_lapse"]')[-1] == ("sub", ["pols_lapse"]))
    check("np.int64(3) reads as 3 (numpy 2 reprs)", parse("claims(t=np.int64(3))")[-1] == ("call", ([], {"t": 3})))
    check("None is an argument", parse("pv_claims(kind=None)")[-1] == ("call", ([], {"kind": None})))
    # -- clean(): what a model pastes --------------------------------------
    check("clean: backticks and a cited value",
          clean("`BasicTerm_S.Projection.pv_claims()` = 5501.19") == "BasicTerm_S.Projection.pv_claims()")
    check("clean: a Reference with its value", clean(P + "point_id = 1") == P + "point_id")
    check("clean: claims(t = 3) keeps its keyword", clean("claims(t = 3)") == "claims(t = 3)")
    check("clean: bare backticks", clean("`claims(3)`") == "claims(3)")
    # -- refusals, each naming the fix --------------------------------------
    check("two ranges parse; resolution refuses them (test_basicterm_s)",
          len(parse("f(range(0, 2), range(0, 2))")[-1][1][0]) == 2)
    refused("**kwargs", "claims(**{'t': 3})", "**kwargs are not refs")
    refused("a non-literal argument", "claims(t=x)", "arguments are Python literals")
    refused("an unfinished call", "Projection.claims(t=", "is not a ref. Write refs like")
    refused("arithmetic outside calculate names calculate", "A() / B()", "only calculate evaluates")
    refused("range() of floats", "f(range(0.5, 2))", "range() takes integers")
    refused("a range step of 0", "f(range(0, 5, 0))", "step must not be 0")
    refused("an empty ref", "  ", "empty ref")
    refused("a lambda is not a ref", "lambda: 1", "is not a ref")
    check("MAX_NODES is 200 (spec 10)", MAX_NODES == 200)
    # -- the review of 2026-10-05: each check failed on the code it was found in --
    import time
    t0 = time.time()
    n = len(parse("f(t=range(0, 10 ** 12))".replace("10 ** 12", str(10 ** 12)))[-1][1][1]["t"])
    check("12: a range is counted without building it (10**8 took 19 s and 3.9 GB)",
          n == 10 ** 12 and time.time() - t0 < 0.5, "%d in %.2f s" % (n, time.time() - t0))
    check("12: an empty range counts 0, a descending one with its step counts its values",
          len(Range(5, 5)) == 0 and len(Range(120, 0)) == 0 and len(Range(199, 0, -1)) == 199)
    check("11: a descending Range prints its step", repr(Range(69, 0, -1)) == "range(69, 0, -1)")
    check("28: a Space as get_tree prints it parses as its signature",
          parse("BasicTerm_S.Projection[point_id]")[-1] == ("params", ["point_id"]))
    check("28: and a Cells signature", parse("pols_if_at(t, timing)")[-1] == ("params", ["t", "timing"]))
    refused("28: a bare name mixed with a value is still not an argument", "claims(t, 3)",
            "cannot read 't' in 'claims(t, 3)' as an argument")
    try:
        parse("Projection.claims(t=0..3)", "X.S.c()")
        check("22: a refusal's examples are the caller's", False, "parsed")
    except RefError as e:
        check("22: a refusal's examples are the caller's", str(e).endswith("Write refs like X.S.c()"), str(e))
    # -- derived arithmetic -------------------------------------------------
    check("a plain ref is not derived", derived_expr(P + "pv_net_cf()") is None)
    tree, leaves = derived_expr("X.pv_net_cf() / X.pv_premiums()")
    check("A / B has two operands", leaves == ["X.pv_net_cf()", "X.pv_premiums()"])
    check("A / B evaluates", eval_derived(tree, {"X.pv_net_cf()": 1.0, "X.pv_premiums()": 4.0}) == 0.25)
    tree, leaves = derived_expr("-A() + 2 * B()")
    check("-A + 2 * B", eval_derived(tree, {"A()": 1, "B()": 3}) == 5)
    for text, fragment in (("A() ** 2", "+ - * / only"), ("A() // 2", "+ - * / only"),
                           ("A() + 'x'", "must be a number"), ("1 + 2", "names no node"),
                           (" + ".join("A%d()" % k for k in range(21)), "at most 20 operands")):
        try:
            derived_expr(text)
            check("derived refused: " + text[:30], False, "accepted")
        except RefError as e:
            check("derived refused: " + text[:30], fragment in str(e), str(e))
    check("20 operands are accepted", len(derived_expr(" + ".join("A%d()" % k for k in range(20)))[1]) == 20)
    return finish()


if __name__ == "__main__":
    run_module(main)
