import ast

from conftest import codes, lint

from lintex.rules.bloat import cyclomatic_complexity, max_nesting


def _fn(src):
    return ast.parse(src).body[0]


def test_function_too_long_counts_code_lines_only():
    body = "\n".join(f"    x{i} = {i}" for i in range(10))
    src = f'def f():\n    """Doc\n    string."""\n    # comment\n\n{body}\n'
    assert codes(src, rules={"function-too-long": {"max-lines": 11}}) == []
    [v] = lint(src, rules={"function-too-long": {"max-lines": 10}})
    assert v.code == "LX301" and "11 lines of code (limit 10)" in v.message


def test_long_function_fix_points_at_blocks():
    loop = "\n".join(f"        y += {i}" for i in range(8))
    src = f"def f(xs):\n    y = 0\n    for x in xs:\n{loop}\n    return y\n"
    [v] = lint(src, rules={"function-too-long": {"max-lines": 5}})
    assert "lines 3-11 (for block)" in v.fix


def test_complexity():
    src = "def f(a, b):\n" + "".join(
        f"    if a == {i} or b:\n        return {i}\n" for i in range(6)
    )
    assert cyclomatic_complexity(_fn(src)) == 13
    [v] = lint(src)
    assert v.code == "LX302" and "complexity 13" in v.message
    assert codes(src, rules={"high-complexity": {"max-complexity": 13}}) == []


def test_nested_function_complexity_is_separate():
    inner = "".join(f"        if x == {i}:\n            return {i}\n" for i in range(11))
    src = f"def outer():\n    def inner(x):\n{inner}\n    return inner\n"
    vs = lint(src)
    assert [v.message.split()[1] for v in vs] == ["`inner()`"]


def test_too_many_parameters_ignores_self():
    assert codes("class A:\n    def m(self, a, b, c, d, e, f, g):\n        pass\n") == []
    assert codes("def m(a, b, c, d, e, f, g, h):\n    pass\n") == ["LX303"]


def test_deep_nesting_and_elif_chains():
    deep = """
        def f(xs):
            for x in xs:
                if x:
                    while x:
                        with open(x) as fh:
                            try:
                                pass
                            except OSError:
                                pass
    """
    assert codes(deep) == ["LX304"]
    flat = (
        "def f(x):\n"
        + "    if x == 0:\n        pass\n"
        + "".join(f"    elif x == {i}:\n        pass\n" for i in range(1, 8))
    )
    assert max_nesting(_fn(flat).body)[0] == 1


def test_file_too_long():
    src = "\n".join(f"x{i} = {i}" for i in range(30))
    assert codes(src, rules={"file-too-long": {"max-lines": 29}}) == ["LX305"]
