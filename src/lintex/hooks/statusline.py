"""Claude Code status line segment.

``lintex statusline`` reads the status-line payload Claude Code pipes to it
(session id, model, workspace) and prints one line. The lintex segment is a
health strip of the last edits in this session - one block per hook run,
green/yellow/red for clean/warnings/errors - followed by what is still open
and how much the agent has already fixed:

    lintex ▮▮▮▮▮▮▮▮▮▮  ✖ 1 ▲ 2 · 9 fixed

It is pure bookkeeping (one small file read), so it is safe to run on every
status-line refresh.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any

from lintex import ledger
from lintex.config import ConfigError, load_config
from lintex.reporters import Style

STATUSLINE_COMMAND = "lintex statusline"
_BLOCK_COLOR = {"clean": "green", "warning": "yellow", "error": "red"}
_BLOCK_ASCII = {"clean": ".", "warning": "!", "error": "x"}


def read_payload(stdin: IO[str]) -> dict[str, Any]:
    try:
        payload = json.load(stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _root(payload: dict[str, Any]) -> Path:
    workspace = payload.get("workspace") or {}
    start = workspace.get("project_dir") or workspace.get("current_dir") or payload.get("cwd")
    try:
        return load_config(start).root
    except ConfigError:
        return Path(start or ".").resolve()


def render_segment(summary: ledger.Summary, style: Style) -> str:
    label = style.paint("lintex", "bold")
    if summary.runs == 0:
        return f"{label} {style.paint('no edits checked yet', 'dim')}"
    if style.unicode:
        blocks = "".join(style.paint("▮", _BLOCK_COLOR[h]) for h in summary.health)
    else:
        blocks = "".join(_BLOCK_ASCII[h] for h in summary.health)
    parts = [label, blocks]
    if summary.open:
        status = []
        if summary.open_errors:
            status.append(style.paint(f"{style.sym['error']} {summary.open_errors}", "red", "bold"))
        if summary.open_warnings:
            status.append(
                style.paint(f"{style.sym['warning']} {summary.open_warnings}", "yellow", "bold")
            )
        parts.append(" ".join(status))
    else:
        parts.append(style.paint(f"{style.sym['ok']} clean", "green", "bold"))
    if summary.fixed:
        parts.append(style.paint(f"{style.sym['dot']} {summary.fixed} fixed", "dim"))
    return " ".join(parts)


def render_statusline(payload: dict[str, Any], style: Style, *, segment_only: bool = False) -> str:
    root = _root(payload)
    session = payload.get("session_id") or None
    # A session filter keeps the strip about *this* conversation; without a
    # session id (run by hand) fall back to the last few hours.
    since = None if session else datetime.now(timezone.utc) - timedelta(hours=8)
    entries = ledger.load(root, since=since, session=session)
    summary = ledger.summarize(entries)
    segment = render_segment(summary, style)
    if segment_only:
        return segment
    model = (payload.get("model") or {}).get("display_name")
    workspace = payload.get("workspace") or {}
    cwd = workspace.get("current_dir") or payload.get("cwd")
    lead = [p for p in (model, Path(cwd).name if cwd else None) if p]
    sep = f" {style.sym['dot']} "
    return sep.join([*[style.paint(p, "dim") for p in lead], segment])


def install_statusline(project_dir: Path, scope: str = "project") -> tuple[Path, bool]:
    """Set ``statusLine`` in Claude Code settings. Returns (path, changed).

    Refuses to replace a status line lintex did not install: the user should
    append ``lintex statusline --segment`` to their own script instead.
    """
    from lintex.hooks.claude_code import settings_path

    path = settings_path(scope, project_dir)
    settings: dict[str, Any] = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text() or "{}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    existing = settings.get("statusLine")
    if isinstance(existing, dict):
        if STATUSLINE_COMMAND in str(existing.get("command", "")):
            return path, False
        raise ConfigError(
            f"{path} already has a statusLine ({existing.get('command', existing)!s}). "
            f"Add `{STATUSLINE_COMMAND} --segment` to that script's output instead of "
            "replacing it."
        )
    settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path, True
