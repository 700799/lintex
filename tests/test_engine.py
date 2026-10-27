import json

from conftest import codes, lint

from lintex import Config, Linter, Severity, lint_paths
from lintex.reporters import format_json, format_text


def test_clean_code_has_no_violations():
    assert (
        codes("""
        import os

        def main() -> str:
            return os.path.join("a", "b")
    """)
        == []
    )


def test_syntax_error_is_reported_with_location():
    [v] = lint("def broken(:\n    pass\n")
    assert v.code == "LX001"
    assert v.line == 1
    assert v.severity is Severity.ERROR
    assert "Re-read lines" in v.fix


def test_inline_suppression_all_and_by_code():
    src = """
        import pickle
        a = pickle.loads(b"")  # lintex: ignore
        b = pickle.loads(b"")  # lintex: ignore[LX203]
        c = pickle.loads(b"")  # lintex: ignore[LX301]
        d = pickle.loads(b"")  # lintex: ignore[dangerous-call]
    """
    assert [v.line for v in lint(src)] == [5]


def test_skip_file_comment():
    assert codes("# lintex: skip-file\neval('1')\n") == []


def test_select_ignore_and_enable():
    src = "import pickle\neval('1')\npickle.loads(b'')\n"
    assert codes(src, ignore=["LX203"]) == []
    assert codes(src, select=["LX1"]) == []
    assert "LX103" not in codes("import surely_not_a_real_pkg_xyz\n")
    assert "LX103" in codes("import surely_not_a_real_pkg_xyz\n", enable=["unresolvable-import"])


def test_severity_override_and_per_file_ignores():
    src = "def f():\n    try:\n        x = 1\n    except:\n        x = 2\n"
    [v] = lint(src, severity={"LX403": "error"})
    assert v.severity is Severity.ERROR
    assert codes(src, **{"per-file-ignores": {"tests/*": ["LX403"]}}) == ["LX403"]
    assert lint(src, path="tests/test_x.py", **{"per-file-ignores": {"tests/*": ["LX403"]}}) == []


def test_violations_sorted_and_fields_populated():
    vs = lint("import pickle\nx = eval('1')\ny = pickle.loads(b'')\n")
    assert [v.line for v in vs] == sorted(v.line for v in vs)
    v = vs[0]
    assert v.message and v.why and v.fix and v.snippet == "x = eval('1')"
    assert v.col == 5


def test_shadowed_builtin_is_not_resolved():
    # `object` here is a parameter, not builtins.object
    assert codes("def f(object):\n    return object.whatever\n") == []
    assert codes("def eval(x):\n    return x\neval('1')\n") == []


def test_lint_paths_respects_excludes_and_reports_json(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "bad.py").write_text("eval('1')\n")
    (tmp_path / "pkg" / "notes.txt").write_text("eval('1')\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "bad.py").write_text("eval('1')\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "bad.py").write_text("eval('1')\n")

    cfg = Config.from_dict({"extend-exclude": ["generated"]}, root=tmp_path)
    result = lint_paths([tmp_path], cfg)
    assert result.files_checked == 1
    assert [v.code for v in result.violations] == ["LX203"]
    assert result.failed(Severity.ERROR)

    payload = json.loads(format_json(result))
    assert payload["summary"] == {"total": 1, "errors": 1, "warnings": 0}
    assert set(payload["violations"][0]) >= {"code", "path", "line", "message", "why", "fix"}
    assert "Found 1 error in 1 file" in format_text(result)


def test_missing_file_is_reported_not_raised(tmp_path):
    result = Linter(Config(root=tmp_path)).lint_paths([tmp_path / "nope.py"])
    assert result.violations[0].rule == "io-error"
