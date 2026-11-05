"""``lintex doctor``: is lintex wired up, and will the hooks actually run?

Most "it doesn't work" reports for hook-based tools are installation problems:
the hook isn't in the settings file Claude Code reads, `lintex` isn't on PATH
for the shell the hook runs in, the config has a typo. This checks each of
those and says what to run.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from lintex import __version__, knowledge, ledger
from lintex.config import Config, ConfigError, load_config
from lintex.hooks.claude_code import settings_path
from lintex.hooks.git import HOOK_MARKER, GitError, hooks_dir, repo_root
from lintex.reporters import Style

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def _settings(scope: str, project: Path) -> tuple[Path, dict]:
    path = settings_path(scope, project)
    if not path.exists():
        return path, {}
    try:
        return path, json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return path, {"__invalid__": True}


def _hook_present(settings: dict) -> bool:
    for entry in (settings.get("hooks") or {}).get("PostToolUse") or []:
        for hook in entry.get("hooks") or []:
            if "lintex hook claude-code" in str(hook.get("command", "")):
                return True
    return False


def check_lintex() -> Check:
    on_path = shutil.which("lintex")
    if on_path is None:
        return Check(
            "lintex",
            WARN,
            f"{__version__} (python {sys.executable})",
            "`lintex` is not on PATH, so hooks that call it by name will fail. Install it "
            "with `pipx install lintex` / `uv tool install lintex`, or use `python -m lintex` "
            "in the hook command.",
        )
    return Check("lintex", OK, f"{__version__} at {on_path}")


def check_python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 10):
        return Check(
            "python",
            WARN,
            version,
            "LX102 (nonexistent-stdlib-name) needs Python 3.10+; it is skipped on this interpreter.",
        )
    return Check("python", OK, version)


def check_config(start: Path) -> tuple[Check, Config | None]:
    try:
        config = load_config(start)
    except ConfigError as exc:
        return Check(
            "config", FAIL, str(exc), "Fix the configuration; `lintex rules` lists valid keys."
        ), None
    if config.source is None:
        return (
            Check(
                "config",
                WARN,
                "none found; using defaults",
                "Run `lintex init` to write a [tool.lintex] block you can tune.",
            ),
            config,
        )
    target = config.target_version
    target_text = f"py{target[0]}{target[1]}" if target else "the running interpreter"
    return Check("config", OK, f"{config.source} (minimum Python: {target_text})"), config


def check_knowledge() -> Check:
    kb = knowledge.load()
    counts = kb.counts()
    via = kb.sources.get("typeshed", {}).get("via", "no typeshed pack")
    return Check(
        "knowledge",
        OK,
        f"{sum(counts.values())} API entries ({counts.get('removed', 0)} removed, "
        f"{counts.get('deprecated', 0)} deprecated, {counts.get('py2', 0)} py2); {via}",
    )


def check_claude_hook(project: Path) -> Check:
    found = []
    for scope in ("project", "local", "user"):
        path, settings = _settings(scope, project)
        if settings.get("__invalid__"):
            return Check("claude code hook", FAIL, f"{path} is not valid JSON", "Fix the file.")
        if _hook_present(settings):
            found.append(str(path))
    if found:
        return Check("claude code hook", OK, f"PostToolUse hook in {', '.join(found)}")
    return Check(
        "claude code hook",
        WARN,
        "not installed",
        "Run `lintex install-hook claude-code` so edits are checked as the agent makes them.",
    )


def check_statusline(project: Path) -> Check:
    for scope in ("project", "local", "user"):
        _, settings = _settings(scope, project)
        line = settings.get("statusLine")
        if isinstance(line, dict) and "lintex statusline" in str(line.get("command", "")):
            return Check("status line", OK, f"configured ({scope} settings)")
        if isinstance(line, dict):
            return Check(
                "status line",
                WARN,
                f"another status line is configured ({scope} settings)",
                "Add `lintex statusline --segment` to that script's output to see the health strip.",
            )
    return Check("status line", WARN, "not configured", "Run `lintex install-hook statusline`.")


def check_git_hook(project: Path) -> Check:
    try:
        hook = hooks_dir(project) / "pre-commit"
    except GitError:
        return Check("git hook", WARN, "not a git repository")
    if hook.exists() and HOOK_MARKER in hook.read_text(errors="replace"):
        return Check("git hook", OK, f"pre-commit at {hook}")
    if hook.exists():
        return Check(
            "git hook",
            WARN,
            "a pre-commit hook exists but it is not lintex's",
            "Add `lintex check --staged` to it, or use the pre-commit framework entry.",
        )
    return Check("git hook", WARN, "not installed", "Run `lintex install-hook git`.")


def check_ledger(config: Config | None, project: Path) -> Check:
    if config is not None and not config.ledger:
        return Check("ledger", OK, "disabled by config (`ledger = false`)")
    path = ledger.ledger_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".write-test"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        return Check(
            "ledger",
            FAIL,
            f"cannot write {path.parent}: {exc}",
            f"Set {ledger.LEDGER_ENV} to a writable directory, or `ledger = false` in config.",
        )
    entries = len(ledger.load(project))
    return Check("ledger", OK, f"{path} ({entries} entries)")


def run_checks(start: Path | None = None) -> list[Check]:
    start = (start or Path.cwd()).resolve()
    try:
        project = repo_root(start)
    except GitError:
        project = start
    config_check, config = check_config(start)
    return [
        check_lintex(),
        check_python(),
        config_check,
        check_knowledge(),
        check_claude_hook(project),
        check_statusline(project),
        check_git_hook(project),
        check_ledger(config, project),
    ]


def render(checks: list[Check], style: Style) -> str:
    glyph = {
        OK: style.paint(style.sym["ok"], "green", "bold"),
        WARN: style.paint(style.sym["warning"], "yellow", "bold"),
        FAIL: style.paint(style.sym["error"], "red", "bold"),
    }
    width = max(len(c.name) for c in checks)
    lines = [style.paint(f"lintex doctor {style.sym['dot']} {Path.cwd().name}", "bold")]
    for c in checks:
        lines.append(f"  {glyph[c.status]} {c.name.ljust(width)}  {c.detail}")
        if c.hint and c.status != OK:
            lines.append(f"    {style.paint(c.hint, 'dim')}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    if fails:
        lines.append(style.paint(f"\n{fails} problem(s) to fix.", "red", "bold"))
    elif warns:
        lines.append(style.paint(f"\nWorking; {warns} optional step(s) not done.", "yellow"))
    else:
        lines.append(style.paint("\nEverything is wired up.", "green", "bold"))
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


__all__ = ["Check", "exit_code", "render", "run_checks"]

_ = os  # keep the import used on platforms where no check needs it
