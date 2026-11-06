import json

from conftest import lint

from lintex.cli import main
from lintex.fixer import apply_autofixes, fix_files
from lintex.models import Autofix, Severity, Violation


def _fixes(src: str):
    return {v.code: v.autofix for v in lint(src)}


def test_pure_renames_get_an_autofix_and_others_do_not():
    [v] = lint("from collections import Mapping\n")
    assert v.autofix == Autofix(
        1, 1, 32, "from collections.abc import Mapping", "from collections import Mapping"
    )

    [v] = lint("import sys\nx = sys.maxint\n")
    assert v.autofix == Autofix(
        2, 9, 15, "maxsize", "maxint"
    )  # same module: rename the attribute only

    [v] = lint("import numpy as np\nx = np.float(1)\n")
    assert v.autofix == Autofix(2, 5, 13, "float", "np.float")  # builtin replaces the whole chain

    [v] = lint("import numpy as np\nx = np.float_(1)\n")
    assert v.autofix == Autofix(2, 8, 14, "float64", "float_")  # keeps the user's alias

    [v] = lint("d = {}\nfor k in d.iteritems():\n    pass\n")
    assert v.autofix == Autofix(2, 12, 21, "items", "iteritems")

    # Needs a new import (math.gcd): text-only fix, no rewrite.
    [v] = lint("import fractions\nfractions.gcd(2, 4)\n")
    assert v.autofix is None and "math.gcd" in v.fix
    # Deprecated-with-judgement: no rewrite.
    [v] = lint("from datetime import datetime\ndatetime.utcnow()\n")
    assert v.autofix is None


def test_import_with_several_names_or_an_alias():
    [v] = lint("from collections import Mapping as M\n")
    assert v.autofix.replacement == "from collections.abc import Mapping as M"
    # Two names on one line: rewriting one would break the other, so none.
    [v] = lint("from collections import Mapping, OrderedDict\n")
    assert v.autofix is None


def test_apply_autofixes_is_right_to_left_and_skips_overlaps():
    src = "a = sys.maxint + sys.maxint\n"
    fixes = [
        Autofix(1, 5, 15, "sys.maxsize", "sys.maxint"),
        Autofix(1, 18, 28, "sys.maxsize", "sys.maxint"),
    ]
    out, applied, skipped = apply_autofixes(src, fixes)
    assert out == "a = sys.maxsize + sys.maxsize\n" and (applied, skipped) == (2, 0)
    out, applied, skipped = apply_autofixes(
        src, [Autofix(1, 5, 15, "X", "sys.maxint"), Autofix(1, 10, 20, "Y", "maxint + sy")]
    )
    assert (applied, skipped) == (1, 1)
    assert apply_autofixes("x\n", [Autofix(9, 1, 2, "y", "x")])[1:] == (0, 1)


def test_fix_files_preserves_bom_and_crlf(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"\xef\xbb\xbfimport sys\r\nx = sys.maxint\r\n")
    vs = lint(f.read_text(encoding="utf-8-sig"), path=str(f))
    report = fix_files(vs)
    assert report.files == {str(f): 1}
    assert f.read_bytes() == b"\xef\xbb\xbfimport sys\r\nx = sys.maxsize\r\n"


def test_check_fix_rewrites_and_reports_the_rest(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\n")
    src = (
        "from collections import Mapping\n"
        "import sys, pickle\n"
        "big = sys.maxint\n"
        "d = {}\n"
        "for k, v in d.iteritems():\n"
        "    pickle.loads(v)\n"
    )
    (tmp_path / "m.py").write_text(src)

    assert main(["check", "m.py", "--json"]) == 1
    before = json.loads(capsys.readouterr().out)
    assert [v["fixable"] for v in before["violations"]] == [True, True, True, False]
    assert (
        before["violations"][0]["autofix"]["replacement"] == "from collections.abc import Mapping"
    )

    assert main(["check", "m.py", "--fix", "--no-color"]) == 1  # pickle.loads remains
    out = capsys.readouterr().out
    assert "Fixed 3 finding(s) in 1 file(s)." in out
    assert "LX203" in out and "LX101" not in out
    assert (tmp_path / "m.py").read_text() == (
        "from collections.abc import Mapping\n"
        "import sys, pickle\n"
        "big = sys.maxsize\n"
        "d = {}\n"
        "for k, v in d.items():\n"
        "    pickle.loads(v)\n"
    )
    # Idempotent: nothing left to fix, file untouched.
    mtime = (tmp_path / "m.py").stat().st_mtime_ns
    main(["check", "m.py", "--fix", "--no-color"])
    assert (tmp_path / "m.py").stat().st_mtime_ns == mtime


def test_fix_refuses_staged(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--staged", "--fix"]) == 2
    assert "--staged" in capsys.readouterr().err


def test_fixable_tag_in_pretty_and_concise(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text("import sys\nx = sys.maxint\n")
    main(["check", "m.py", "--no-color"])
    out = capsys.readouterr().out
    assert "[fixable]" in out and "1 fixable with `lintex check --fix`" in out
    main(["check", "m.py", "--format", "concise", "--no-color"])
    assert (
        "sys.maxint` is Python 2 only and does not exist in Python 3. [fixable]"
        in capsys.readouterr().out
    )


def test_autofix_is_skipped_when_text_at_span_differs():
    out, applied, skipped = apply_autofixes("x = foo(1)\n", [Autofix(1, 5, 8, "bar", "baz")])
    assert (out, applied, skipped) == ("x = foo(1)\n", 0, 1)


def test_fix_files_never_leaves_an_unparseable_file(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("x = 1\n")
    # A span that matches its `original` but would break the syntax is not written.
    v = Violation(
        "LX101", "r", "m", str(p), 1, 1, Severity.ERROR, autofix=Autofix(1, 1, 2, "(", "x")
    )
    report = fix_files([v])
    assert p.read_text() == "x = 1\n" and report.skipped == 1 and not report.files
