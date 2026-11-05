"""The Claude Code plugin under plugin/ and the marketplace file that points at it."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lintex import __version__

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
SCRIPT = PLUGIN / "scripts" / "check_edit.py"


def test_manifest_marketplace_and_hooks_are_consistent():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())

    assert manifest["name"] == "lintex"
    assert manifest["version"] == __version__, "bump plugin.json with the package version"
    [entry] = marketplace["plugins"]
    assert entry["name"] == manifest["name"] and entry["source"] == "./plugin"
    assert marketplace["name"] == "lintex"

    [post] = hooks["hooks"]["PostToolUse"]
    assert post["matcher"] == "Edit|Write|MultiEdit"
    [hook] = post["hooks"]
    assert (
        hook["type"] == "command"
        and "${CLAUDE_PLUGIN_ROOT}/scripts/check_edit.py" in hook["command"]
    )
    assert SCRIPT.exists()


def test_every_command_has_frontmatter_and_names_lintex():
    commands = sorted((PLUGIN / "commands").glob("*.md"))
    assert [c.stem for c in commands] == ["check", "doctor", "report"]
    for path in commands:
        text = path.read_text()
        assert text.startswith("---\n") and "description:" in text
        assert f"lintex {path.stem}" in text


def _run(payload: dict, python: str, path: str, env_extra: dict | None = None):
    env = {**os.environ, "PATH": path, **(env_extra or {})}
    return subprocess.run(
        [python, str(SCRIPT)], input=json.dumps(payload), text=True, capture_output=True, env=env
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "bad.py").write_text("from collections import Mapping\n")
    (tmp_path / "good.py").write_text("x = 1\n")
    return tmp_path


def _payload(project: Path, name: str) -> dict:
    return {
        "session_id": "s1",
        "tool_name": "Edit",
        "cwd": str(project),
        "tool_input": {"file_path": name},
    }


def test_hook_script_with_lintex_importable(project):
    # sys.executable is the test interpreter, which has lintex installed.
    proc = _run(_payload(project, "bad.py"), sys.executable, os.environ["PATH"])
    assert proc.returncode == 2 and "LX101" in proc.stderr and "fix:" in proc.stderr
    proc = _run(_payload(project, "good.py"), sys.executable, os.environ["PATH"])
    assert proc.returncode == 0 and proc.stderr == ""


def test_hook_script_falls_back_to_lintex_on_path(project):
    bin_dir = Path(sys.executable).parent
    system_python = "/usr/bin/python3"
    if not Path(system_python).exists():
        pytest.skip("no system python")
    # The venv's python is often the same binary as the system one; what matters
    # is that this interpreter does *not* have lintex importable.
    if subprocess.run([system_python, "-c", "import lintex"], capture_output=True).returncode == 0:
        pytest.skip("system python has lintex installed")
    proc = _run(_payload(project, "bad.py"), system_python, f"{bin_dir}{os.pathsep}/usr/bin:/bin")
    assert proc.returncode == 2 and "LX101" in proc.stderr


def test_hook_script_nudges_once_when_lintex_is_missing(project, tmp_path):
    system_python = "/usr/bin/python3"
    if not Path(system_python).exists():
        pytest.skip("no system python")
    try:
        subprocess.run([system_python, "-c", "import lintex"], check=True, capture_output=True)
        pytest.skip("system python has lintex installed")
    except subprocess.CalledProcessError:
        pass
    data = {"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    first = _run(_payload(project, "bad.py"), system_python, "/usr/bin:/bin", data)
    assert first.returncode == 2 and "pip install lintex" in first.stderr
    second = _run(_payload(project, "bad.py"), system_python, "/usr/bin:/bin", data)
    assert second.returncode == 0 and second.stderr == ""
    # A different session gets its own nudge.
    other = _run(
        {**_payload(project, "bad.py"), "session_id": "s2"}, system_python, "/usr/bin:/bin", data
    )
    assert other.returncode == 2
