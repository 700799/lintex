import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lintex import ledger
from lintex.cli import main
from lintex.hooks.claude_code import run_claude_code_hook
from lintex.hooks.statusline import render_segment, render_statusline
from lintex.models import Severity, Violation
from lintex.reporters import Style

PLAIN = Style(color=False, unicode=False)
UNI = Style(color=True, unicode=True)


def _v(code: str, message: str, path: str, line: int = 1, severity=Severity.ERROR) -> Violation:
    return Violation(
        code=code,
        rule=code.lower(),
        message=message,
        path=path,
        line=line,
        col=1,
        severity=severity,
    )


def _hook(project: Path, path: str, session: str = "s1") -> int:
    payload = {
        "session_id": session,
        "tool_name": "Edit",
        "cwd": str(project),
        "tool_input": {"file_path": path},
    }
    return run_claude_code_hook(io.StringIO(json.dumps(payload)), io.StringIO())


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\n")
    return tmp_path


def test_summarize_tracks_caught_fixed_and_open(tmp_path):
    root = tmp_path
    a = str(root / "a.py")
    ledger.record(
        root,
        session="s",
        tool="Edit",
        files=[a],
        violations=[
            _v("LX203", "`eval()` executes arbitrary code.", a, 3),
            _v("LX301", "Function `f()` has 72 lines of code (limit 70).", a, 1, Severity.WARNING),
        ],
    )
    # Agent shrinks f() a bit (still too long) and removes the eval.
    ledger.record(
        root,
        session="s",
        tool="Edit",
        files=[a],
        violations=[
            _v("LX301", "Function `f()` has 71 lines of code (limit 70).", a, 1, Severity.WARNING),
        ],
    )
    # Then introduces a new problem.
    ledger.record(
        root,
        session="s",
        tool="Write",
        files=[a],
        violations=[
            _v("LX301", "Function `f()` has 71 lines of code (limit 70).", a, 1, Severity.WARNING),
            _v("LX402", "Exception handler catches Exception and silently discards it.", a, 9),
        ],
    )
    s = ledger.summarize(ledger.load(root))
    assert (s.runs, s.files, s.caught, s.fixed, s.open) == (3, 1, 3, 1, 2)
    assert s.by_code["LX203"].fixed == 1 and s.by_code["LX203"].open == 0
    assert s.by_code["LX301"].caught == 1  # digits masked: not counted as fixed + new
    assert s.health == ["error", "warning", "error"]
    assert [f["code"] for f in s.open_findings] == ["LX301", "LX402"]
    assert s.open_errors == 1 and s.open_warnings == 1


def test_load_filters_by_session_and_time(tmp_path):
    a = str(tmp_path / "a.py")
    ledger.record(tmp_path, session="old", tool="Edit", files=[a], violations=[])
    ledger.record(tmp_path, session="new", tool="Edit", files=[a], violations=[])
    assert len(ledger.load(tmp_path)) == 2
    assert [e.session for e in ledger.load(tmp_path, session="new")] == ["new"]
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    assert ledger.load(tmp_path, since=future) == []


def test_torn_lines_are_skipped_and_ledger_is_pruned(tmp_path, monkeypatch):
    path = ledger.ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"ts": "2026-01-01T00:00:00+00:00", "path": "a.py", "findings": []}\n{broken\n'
    )
    assert len(ledger.load(tmp_path)) == 1
    monkeypatch.setattr(ledger, "MAX_LINES", 10)
    monkeypatch.setattr(ledger, "_KEEP_LINES", 5)
    for _ in range(12):
        ledger.record(tmp_path, session="s", tool="Edit", files=["a.py"], violations=[])
    assert len(path.read_text().splitlines()) <= 10


def test_hook_records_and_statusline_reflects_it(project):
    bad = project / "bad.py"
    bad.write_text("import pickle\nx = pickle.loads(b'')\n")
    assert _hook(project, "bad.py") == 2
    bad.write_text("import json\nx = json.loads('1')\n")
    assert _hook(project, "bad.py") == 0

    payload = {
        "session_id": "s1",
        "model": {"display_name": "Opus"},
        "workspace": {"current_dir": str(project), "project_dir": str(project)},
    }
    line = render_statusline(payload, PLAIN)
    assert line.startswith("Opus - " + project.name + " - lintex x.")
    assert "OK clean" in line and "1 fixed" in line

    # Another session sees nothing from this one.
    assert "no edits checked yet" in render_statusline({**payload, "session_id": "other"}, PLAIN)
    # Only the segment, for embedding in an existing status line.
    assert render_statusline(payload, PLAIN, segment_only=True).startswith("lintex x.")


def test_segment_colours_blocks_and_counts(tmp_path):
    a = str(tmp_path / "a.py")
    ledger.record(
        tmp_path,
        session="s",
        tool="Edit",
        files=[a],
        violations=[_v("LX203", "bad", a), _v("LX403", "meh", a, 2, Severity.WARNING)],
    )
    seg = render_segment(ledger.summarize(ledger.load(tmp_path)), UNI)
    assert "\033[31m▮" in seg and "✖ 1" in seg and "▲ 1" in seg
    assert "fixed" not in seg


def test_ledger_can_be_disabled(project):
    (project / "pyproject.toml").write_text("[tool.lintex]\nledger = false\n")
    (project / "bad.py").write_text("eval('1')\n")
    assert _hook(project, "bad.py") == 2
    assert ledger.load(project) == []
    (project / "pyproject.toml").write_text("[tool.lintex]\nledger = 'yes'\n")
    assert _hook(project, "bad.py") == 0  # config error: hook stands down, never blocks


def test_report_command(project, capsys):
    assert main(["report", "--no-color"]) == 0
    assert "Nothing recorded yet" in capsys.readouterr().out
    (project / "bad.py").write_text("eval('1')\nexec('2')\n")
    _hook(project, "bad.py")
    (project / "bad.py").write_text("exec('2')\n")
    _hook(project, "bad.py")

    assert main(["report", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "2 caught across 2 edits of 1 file" in out
    assert "1 fixed by the agent" in out and "1 still open" in out
    assert "LX203 dangerous-call" in out and "Still open" in out and "bad.py:1" in out

    assert main(["report", "--json", "--session", "s1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["caught"] == 2 and data["fixed"] == 1 and data["open"] == 1
    assert main(["report", "--since", "soon"]) == 2
    assert main(["report", "--clear"]) == 0
    assert main(["report", "--json", "--all"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])["runs"] == 0


def test_install_statusline_respects_existing(project, capsys):
    assert main(["install-hook", "statusline"]) == 0
    settings = project / ".claude" / "settings.json"
    assert json.loads(settings.read_text())["statusLine"]["command"] == "lintex statusline"
    assert main(["install-hook", "statusline"]) == 0
    assert "already configured" in capsys.readouterr().out
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "~/mine.sh"}}))
    assert main(["install-hook", "statusline"]) == 2
    assert "--segment" in capsys.readouterr().err


def test_parse_since():
    assert ledger.parse_since("90m") == timedelta(minutes=90)
    assert ledger.parse_since("2") == timedelta(hours=2)
    assert ledger.parse_since("7d") == timedelta(days=7)
    with pytest.raises(ValueError):
        ledger.parse_since("tomorrow")
