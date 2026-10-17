"""Output formats. Text output is written to be read by an LLM as much as a human."""

from __future__ import annotations

import json

from lintex.engine import LintResult
from lintex.models import Violation

_COLORS = {
    "error": "\033[31m",
    "warning": "\033[33m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _c(text: str, style: str, color: bool) -> str:
    return f"{_COLORS[style]}{text}{_COLORS['reset']}" if color else text


def format_violation(v: Violation, color: bool = False) -> str:
    sev = _c(v.severity.value, v.severity.value, color)
    head = f"{_c(f'{v.path}:{v.line}:{v.col}', 'bold', color)}: {v.code} [{sev}] {v.message}"
    lines = [head]
    if v.snippet:
        lines.append(_c(f"    | {v.snippet}", "dim", color))
    if v.why:
        lines.append(f"    why: {v.why}")
    if v.fix:
        lines.append(f"    fix: {v.fix}")
    return "\n".join(lines)


def summary_line(result: LintResult) -> str:
    if not result.violations:
        noun = "file" if result.files_checked == 1 else "files"
        return f"All checks passed ({result.files_checked} {noun}, {result.duration_ms:.0f} ms)."
    files = len({v.path for v in result.violations})
    parts = []
    if result.error_count:
        parts.append(f"{result.error_count} error{'s' * (result.error_count != 1)}")
    if result.warning_count:
        parts.append(f"{result.warning_count} warning{'s' * (result.warning_count != 1)}")
    return (
        f"Found {' and '.join(parts)} in {files} file{'s' * (files != 1)} "
        f"({result.files_checked} checked, {result.duration_ms:.0f} ms)."
    )


def format_text(result: LintResult, color: bool = False) -> str:
    blocks = [format_violation(v, color) for v in result.violations]
    blocks.append(summary_line(result))
    if result.violations:
        blocks.append(
            "Fix each issue as described in its `fix:` line, then re-run lintex on the same "
            "files. Suppress a deliberate exception with a trailing `# lintex: ignore[CODE]` comment."
        )
    return "\n\n".join(blocks)


def format_json(result: LintResult) -> str:
    return json.dumps(result.to_dict(), indent=2)
