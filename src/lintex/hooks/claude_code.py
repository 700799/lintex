"""Claude Code integration.

``lintex hook claude-code`` is meant to run as a Claude Code ``PostToolUse``
hook on ``Edit|Write|MultiEdit``. It reads the hook payload from stdin, lints
the file the agent just wrote, and if anything is found exits with status 2
and prints the findings to stderr - which Claude Code feeds straight back to
the model so it can fix the problems in its next step.

Any problem with the hook itself (bad payload, unreadable config) exits 0 so
lintex can never wedge the agent loop.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import IO, Any

from lintex import ledger
from lintex.config import ConfigError, load_config
from lintex.engine import Linter, LintResult
from lintex.models import Severity
from lintex.reporters import format_violation

HOOK_COMMAND = "lintex hook claude-code"
MATCHER = "Edit|Write|MultiEdit"
BLOCKING_EXIT = 2


def _edited_files(payload: dict[str, Any]) -> list[str]:
    tool_input = payload.get("tool_input") or {}
    paths = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    return [p for p in paths if p.endswith(".py")]


def run_claude_code_hook(
    stdin: IO[str], stderr: IO[str], fail_on: Severity = Severity.WARNING
) -> int:
    try:
        payload = json.load(stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    files = [str(Path(cwd, f)) for f in _edited_files(payload)]
    if not files:
        return 0
    try:
        config = load_config(files[0])
        linter = Linter(config)
    except ConfigError as exc:
        stderr.write(f"lintex: configuration error, skipping checks: {exc}\n")
        return 0
    result = LintResult()
    linted: list[str] = []
    for path in linter.iter_files(files):
        result.files_checked += 1
        try:
            result.violations.extend(linter.lint_file(path, str(path)))
            linted.append(str(path))
        except OSError:
            continue  # e.g. the file was deleted again
    if config.ledger and linted:
        try:
            ledger.record(
                config.root,
                session=str(payload.get("session_id") or ""),
                tool=str(payload.get("tool_name") or ""),
                files=linted,
                violations=result.violations,
            )
        except OSError:
            pass  # bookkeeping must never block the agent
    relevant = [v for v in result.violations if v.severity.rank >= fail_on.rank]
    if not relevant:
        return 0
    stderr.write(
        f"lintex found {len(relevant)} issue(s) in the file(s) you just edited. "
        "Fix them before continuing:\n\n"
    )
    stderr.write("\n\n".join(format_violation(v) for v in relevant))
    stderr.write("\n")
    return BLOCKING_EXIT


def settings_path(scope: str, project_dir: Path) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    name = "settings.local.json" if scope == "local" else "settings.json"
    return project_dir / ".claude" / name


def install_claude_code_hook(
    project_dir: Path, scope: str = "project", command: str = HOOK_COMMAND
) -> tuple[Path, bool]:
    """Merge the PostToolUse hook into Claude Code settings. Returns (path, changed)."""
    path = settings_path(scope, project_dir)
    settings: dict[str, Any] = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    hooks = settings.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    for entry in post:
        for hook in entry.get("hooks", []):
            if "lintex hook claude-code" in hook.get("command", ""):
                return path, False
    post.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path, True
