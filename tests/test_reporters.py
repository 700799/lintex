import json

from conftest import lint

from lintex.engine import LintResult
from lintex.models import Severity, Violation
from lintex.reporters import Style, format_github, format_json, format_pretty, format_summary

PLAIN = Style(color=False, unicode=False, width=100)


def _result(*violations: Violation, files: int = 1) -> LintResult:
    return LintResult(violations=list(violations), files_checked=files, duration_ms=12.0)


def test_caret_covers_span_and_handles_indentation_and_unicode():
    [v] = lint("def f(données):\n    return données.iteritems()\n")
    assert (v.line, v.col, v.end_col) == (2, 12, 29)  # character offsets, not bytes
    assert v.snippet == "    return données.iteritems()"
    text = format_pretty(_result(v), PLAIN)
    assert "2 | return données.iteritems()" in text
    assert "  |        ^^^^^^^^^^^^^^^^^" in text  # exactly under `données.iteritems`


def test_multiline_node_underlines_rest_of_first_line():
    src = "def f(a, b, c, d, e, f, g, h):\n    pass\n"
    [v] = lint(src)
    assert v.code == "LX303" and v.end_col is None
    text = format_pretty(_result(v), PLAIN)
    caret_line = next(line for line in text.splitlines() if "^" in line)
    assert caret_line.count("^") == len("def f(a, b, c, d, e, f, g, h):")


def test_pretty_groups_by_file_and_shows_why_once_per_rule():
    a = lint("import pickle\npickle.loads(b'')\npickle.loads(b'')\n", path="a.py")
    b = lint("eval('1')\n", path="b.py")
    text = format_pretty(_result(*a, *b, files=2), PLAIN)
    assert text.index("a.py") < text.index("b.py")
    assert text.count("why ") == 1  # LX203 three times, rationale once
    assert text.count("fix ") == 3
    verbose = format_pretty(_result(*a, *b, files=2), PLAIN, verbose=True)
    assert verbose.count("why ") == 3


def test_summary_breakdown_and_success_line():
    vs = lint("import pickle\neval('1')\npickle.loads(b'')\nexec('1')\nexec('2')\n")
    summary = format_summary(_result(*vs), PLAIN)
    assert "Found 4 errors in 1 file" in summary
    assert "LX203 dangerous-call" in summary and "x4" in summary
    assert "lintex explain CODE" in summary
    # Code spans are never broken across wrapped lines.
    assert "`# lintex: ignore[CODE]`" in summary
    three = format_summary(_result(*vs[:3]), PLAIN)
    assert "dangerous-call" not in three  # one rule, few findings: the headline is enough
    assert "OK All checks passed" in format_summary(_result(files=4), PLAIN)


def test_long_lines_keep_caret_visible():
    long = "x = " + "a + " * 60 + "eval('1')\n"
    [v] = lint(long)
    narrow = Style(color=False, unicode=False, width=60)
    text = format_pretty(_result(v), narrow)
    code_line, caret_line = (line for line in text.splitlines() if " | " in line)
    assert all(len(line) <= 60 for line in (code_line, caret_line))
    assert "eval" in code_line and "^" in caret_line


def test_github_annotations_escape_newlines_and_carry_fix():
    v = Violation(
        code="LX999",
        rule="demo",
        message="first line\nsecond: line",
        path="pkg/x.py",
        line=3,
        col=4,
        end_line=5,
        severity=Severity.WARNING,
        fix="Do, this",
    )
    out = format_github(_result(v)).splitlines()[0]
    assert out.startswith("::warning file=pkg/x.py,line=3,col=4,endLine=5,title=LX999 demo::")
    assert "first line%0Asecond: line Fix: Do, this" in out


def test_json_snippet_is_stripped_and_has_end_col():
    [v] = lint("def f():\n    eval('1')\n")
    payload = json.loads(format_json(_result(v)))["violations"][0]
    assert payload["snippet"] == "eval('1')"
    assert payload["col"] == 5 and payload["end_col"] == 14


def test_style_detection_respects_env(monkeypatch):
    class Tty:
        encoding = "utf-8"

        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert Style.detect(Tty()).color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert Style.detect(Tty()).color is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert Style.detect(Tty(), color=False).color is False  # explicit flag wins
