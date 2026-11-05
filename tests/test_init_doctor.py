import json
import shutil
import subprocess

import pytest

from lintex import doctor
from lintex.cli import main
from lintex.config import load_config

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_init_appends_to_pyproject_and_the_result_loads(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "Wrote lintex configuration to" in out and "pyproject.toml" in out
    text = (tmp_path / "pyproject.toml").read_text()
    assert text.startswith('[project]\nname = "x"') and "[tool.lintex]" in text
    cfg = load_config(tmp_path)
    assert cfg.source == tmp_path / "pyproject.toml" and cfg.target_version == (3, 11)
    # Second run refuses rather than appending a duplicate table.
    assert main(["init"]) == 2
    assert "already configured" in capsys.readouterr().err


def test_init_without_pyproject_writes_lintex_toml(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert (tmp_path / ".lintex.toml").exists()
    assert "[rules.function-too-long]" in (tmp_path / ".lintex.toml").read_text()
    assert load_config(tmp_path).source == tmp_path / ".lintex.toml"


@needs_git
def test_init_with_hooks_installs_everything(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert main(["init", "--hooks"]) == 0
    out = capsys.readouterr().out
    assert "Added Claude Code hook" in out
    assert "Set Claude Code status line" in out
    assert "Installed git pre-commit hook" in out
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["statusLine"]["command"] == "lintex statusline"
    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()


def test_doctor_reports_each_check_and_hints(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["doctor", "--no-color"]) == 0  # warnings, no failures
    out = capsys.readouterr().out
    for name in (
        "lintex",
        "python",
        "config",
        "knowledge",
        "claude code hook",
        "status line",
        "git hook",
        "ledger",
    ):
        assert name in out
    assert "lintex init" in out and "lintex install-hook claude-code" in out

    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\nignroe = ['x']\n")
    assert main(["doctor", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "unknown lintex config key" in out and "problem(s) to fix" in out


@needs_git
def test_doctor_is_green_after_init_hooks(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert main(["init", "--hooks"]) == 0
    capsys.readouterr()
    assert main(["doctor", "--json"]) == 0
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)}
    for name in ("config", "claude code hook", "status line", "git hook", "ledger"):
        assert checks[name]["status"] == "ok", checks[name]


def test_doctor_flags_a_foreign_status_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "~/mine.sh"}})
    )
    checks = {c.name: c for c in doctor.run_checks(tmp_path)}
    assert checks["status line"].status == "warn"
    assert "--segment" in checks["status line"].hint
