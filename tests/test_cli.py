import io
import json
import os
import shutil
import subprocess

import pytest

from lintex.cli import main
from lintex.hooks.claude_code import run_claude_code_hook
from lintex.models import Severity

BAD = "import pickle\n\ndef load(b):\n    return pickle.loads(b)\n"
WARN_ONLY = "def f():\n    try:\n        x = 1\n    except:\n        x = 2\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\n")
    return tmp_path


def test_check_text_and_exit_codes(project, capsys):
    (project / "good.py").write_text("x = 1\n")
    assert main(["good.py"]) == 0
    assert "All checks passed" in capsys.readouterr().out

    (project / "bad.py").write_text(BAD)
    assert main(["check", "bad.py", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "bad.py" in out and "4:12" in out and "LX203" in out
    assert "fix" in out and "why" in out
    assert "No lintex config found" not in out  # the fixture has a pyproject [tool.lintex]
    assert main(["bad.py", "--exit-zero"]) == 0

    assert main(["check", "bad.py", "--format", "concise", "--no-color"]) == 1
    assert "bad.py:4:12: LX203 `pickle.loads()`" in capsys.readouterr().out

    assert main(["check", "bad.py", "--format", "full", "--no-color"]) == 1
    assert "bad.py:4:12: LX203 [error]" in capsys.readouterr().out


def test_no_config_hint_shows_only_without_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_text(BAD)
    assert main(["bad.py", "--no-color"]) == 1
    assert "No lintex config found" in capsys.readouterr().out


def test_check_json(project, capsys):
    (project / "bad.py").write_text(BAD)
    main(["bad.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "lintex"
    assert payload["violations"][0]["code"] == "LX203"


def test_fail_on_warning(project, capsys):
    (project / "w.py").write_text(WARN_ONLY)
    assert main(["w.py"]) == 0
    assert main(["w.py", "--fail-on", "warning"]) == 1


def test_config_error_exit_code(project, capsys):
    (project / "pyproject.toml").write_text("[tool.lintex]\nbogus = 1\n")
    assert main(["."]) == 2
    assert "unknown lintex config key" in capsys.readouterr().err


def test_rules_and_explain(project, capsys):
    assert main(["rules", "--json"]) == 0
    rules = json.loads(capsys.readouterr().out)
    assert {r["code"] for r in rules} >= {"LX101", "LX201", "LX301", "LX401"}
    assert main(["explain", "LX301"]) == 0
    assert "max-lines" in capsys.readouterr().out
    assert main(["explain", "nope"]) == 2


def test_claude_code_hook_feeds_back_violations(project):
    (project / "bad.py").write_text(BAD)
    payload = {"tool_name": "Write", "cwd": str(project), "tool_input": {"file_path": "bad.py"}}
    err = io.StringIO()
    assert run_claude_code_hook(io.StringIO(json.dumps(payload)), err) == 2
    assert "LX203" in err.getvalue() and "Fix them before continuing" in err.getvalue()

    (project / "w.py").write_text(WARN_ONLY)
    payload["tool_input"]["file_path"] = "w.py"
    assert run_claude_code_hook(io.StringIO(json.dumps(payload)), io.StringIO()) == 2
    assert (
        run_claude_code_hook(io.StringIO(json.dumps(payload)), io.StringIO(), Severity.ERROR) == 0
    )


def test_claude_code_hook_never_blocks_on_bad_input(project):
    assert run_claude_code_hook(io.StringIO("not json"), io.StringIO()) == 0
    payload = {"tool_input": {"file_path": "README.md"}}
    assert run_claude_code_hook(io.StringIO(json.dumps(payload)), io.StringIO()) == 0


def test_install_claude_code_hook_is_idempotent(project, capsys):
    settings = project / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    assert main(["install-hook", "claude-code"]) == 0
    assert main(["install-hook", "claude-code"]) == 0
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    [entry] = data["hooks"]["PostToolUse"]
    assert entry["matcher"] == "Edit|Write|MultiEdit"
    assert entry["hooks"][0]["command"] == "lintex hook claude-code"
    assert "already present" in capsys.readouterr().out


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@needs_git
def test_git_hook_install_and_staged_check(project, capsys):
    _git(project, "init", "-q")
    assert main(["install-hook", "git"]) == 0
    capsys.readouterr()
    hook = project / ".git" / "hooks" / "pre-commit"
    assert "lintex check --staged" in hook.read_text()
    assert os.name == "nt" or hook.stat().st_mode & 0o100

    # The *staged* content is linted, not the working tree.
    (project / "mod.py").write_text(BAD)
    _git(project, "add", "mod.py")
    (project / "mod.py").write_text("x = 1\n")
    assert main(["check", "--staged", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [v["path"] for v in payload["violations"]] == ["mod.py"]


@needs_git
def test_git_hook_refuses_to_clobber_foreign_hook(project, capsys):
    _git(project, "init", "-q")
    hook = project / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho mine\n")
    assert main(["install-hook", "git"]) == 2
    assert "--force" in capsys.readouterr().err
    assert main(["install-hook", "git", "--force"]) == 0
