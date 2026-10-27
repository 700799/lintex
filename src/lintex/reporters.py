"""Output formats.

* ``pretty``  grouped by file with source context and carets; the default for people.
* ``full``    one self-contained block per finding with why and fix; what hooks hand to an agent.
* ``concise`` one ``path:line:col: CODE message`` line per finding, for editors and grep.
* ``github``  GitHub Actions workflow annotations (show up inline on the PR diff).
* ``json``    machine-readable.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from collections import Counter
from typing import IO

from lintex.engine import LintResult
from lintex.models import Severity, Violation

FORMATS = ("pretty", "full", "concise", "github", "json")

_ANSI = {
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "underline": "\033[4m",
    "reset": "\033[0m",
}
_UNICODE = {"error": "✖", "warning": "▲", "ok": "✔", "bar": "│", "caret": "^", "dot": "·"}
_ASCII = {"error": "x", "warning": "!", "ok": "OK", "bar": "|", "caret": "^", "dot": "-"}


class Style:
    """Terminal styling decisions, made once per run."""

    def __init__(self, color: bool = False, unicode: bool = True, width: int = 100) -> None:
        self.color = color
        self.unicode = unicode
        self.sym = _UNICODE if unicode else _ASCII
        self.width = max(60, min(width, 120))

    @classmethod
    def detect(cls, stream: IO[str] | None = None, color: bool | None = None) -> Style:
        stream = stream if stream is not None else sys.stdout
        if color is None:
            if os.environ.get("FORCE_COLOR"):
                color = True
            elif "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
                color = False
            else:
                color = bool(getattr(stream, "isatty", lambda: False)())
        encoding = (getattr(stream, "encoding", None) or "").lower()
        width = shutil.get_terminal_size((100, 24)).columns
        return cls(color=color, unicode="utf" in encoding, width=width)

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        return "".join(_ANSI[s] for s in styles) + text + _ANSI["reset"]

    def sev_color(self, severity: Severity) -> str:
        return "red" if severity is Severity.ERROR else "yellow"


PLAIN = Style(color=False, unicode=False)


# -- pretty ------------------------------------------------------------------

_INDENT = " " * 9  # aligns body text under the message column


def _wrap(label: str, text: str, style: Style, color: str) -> str:
    """``label text...`` wrapped at the terminal width with a hanging indent."""
    head = f"{_INDENT}{style.paint(label, color, 'bold')} "
    width = style.width - len(_INDENT) - len(label) - 1
    lines = textwrap.wrap(text, width=max(width, 30)) or [""]
    pad = " " * (len(_INDENT) + len(label) + 1)
    return head + f"\n{pad}".join(lines)


def _source_context(v: Violation, style: Style) -> list[str]:
    """The offending line with a caret run under the reported span."""
    if not v.snippet.strip():
        return []
    lead = len(v.snippet) - len(v.snippet.lstrip())
    text = v.snippet.lstrip().replace("\t", " ")
    start = max(v.col - 1 - lead, 0)
    if v.end_col is not None and v.end_col > v.col:
        end = max(v.end_col - 1 - lead, start + 1)
    else:
        end = len(text)  # multi-line node: underline the rest of its first line
    end = min(end, len(text)) if text else 0

    gutter_w = len(str(v.line))
    avail = style.width - len(_INDENT) - gutter_w - 3
    if len(text) > avail:
        # Keep the caret visible: trim the left side when the span starts far right.
        offset = 0
        if start > avail * 2 // 3:
            offset = start - avail // 3
            text, start, end = "…" + text[offset + 1 :], start - offset, end - offset
        if len(text) > avail:
            text = text[: avail - 1] + "…"
            end = min(end, len(text))
    bar = style.paint(style.sym["bar"], "dim")
    carets = style.sym["caret"] * max(end - start, 1)
    return [
        f"{_INDENT}{style.paint(str(v.line).rjust(gutter_w), 'dim')} {bar} {text}",
        f"{_INDENT}{' ' * gutter_w} {bar} {' ' * start}{style.paint(carets, style.sev_color(v.severity), 'bold')}",
    ]


def format_pretty_violation(v: Violation, style: Style, show_why: bool = True) -> str:
    pos = f"{v.line}:{v.col}".ljust(7)
    sev_text = f"{style.sym[v.severity.value]} {v.severity.value}"
    # Pad on the visible text, then colour, so escape codes never skew the columns.
    sev = style.paint(sev_text, style.sev_color(v.severity), "bold") + " " * (9 - len(sev_text))
    code = style.paint(v.code, "cyan", "bold")
    head = f"  {style.paint(pos, 'dim')} {sev} {code}  "
    head_width = 2 + 7 + 1 + 9 + 1 + len(v.code) + 2
    msg_lines = textwrap.wrap(v.message, width=max(style.width - head_width, 30)) or [v.message]
    lines = [head + msg_lines[0]] + [f"{' ' * head_width}{m}" for m in msg_lines[1:]]
    lines += _source_context(v, style)
    if show_why and v.why:
        lines.append(_wrap("why", v.why, style, "magenta"))
    if v.fix:
        lines.append(_wrap("fix", v.fix, style, "green"))
    return "\n".join(lines)


def _counts(violations: list[Violation]) -> tuple[int, int]:
    errors = sum(1 for v in violations if v.severity is Severity.ERROR)
    return errors, len(violations) - errors


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _count_phrase(errors: int, warnings: int, style: Style, colored: bool = True) -> str:
    parts = []
    if errors:
        text = _plural(errors, "error")
        parts.append(style.paint(text, "red", "bold") if colored else text)
    if warnings:
        text = _plural(warnings, "warning")
        parts.append(style.paint(text, "yellow", "bold") if colored else text)
    return " and ".join(parts)


def format_pretty(result: LintResult, style: Style, verbose: bool = False) -> str:
    by_file: dict[str, list[Violation]] = {}
    for v in sorted(result.violations, key=lambda v: (v.path, v.line, v.col, v.code)):
        by_file.setdefault(v.path, []).append(v)

    out: list[str] = []
    seen_why: set[str] = set()
    for path, vs in by_file.items():
        errors, warnings = _counts(vs)
        header = style.paint(path, "bold", "underline")
        out.append(
            f"{header}  {style.paint(_count_phrase(errors, warnings, style, colored=False), 'dim')}"
        )
        for v in vs:
            out.append(
                format_pretty_violation(v, style, show_why=verbose or v.code not in seen_why)
            )
            out.append("")
            seen_why.add(v.code)
    out.append(format_summary(result, style))
    return "\n".join(out)


def dim_note(text: str, style: Style) -> str:
    """A secondary remark, wrapped to the terminal and indented two spaces.

    Spaces inside backtick spans become non-breaking so a command or comment
    such as ``# lintex: ignore[CODE]`` is never split across lines.
    """
    parts = text.split("`")
    for i in range(1, len(parts), 2):
        parts[i] = parts[i].replace(" ", " ")
    wrapped = textwrap.fill(
        "`".join(parts), style.width, initial_indent="  ", subsequent_indent="  "
    )
    return style.paint(wrapped.replace(" ", " "), "dim")


def format_summary(result: LintResult, style: Style) -> str:
    timing = f"checked {_plural(result.files_checked, 'file')} in {result.duration_ms:.0f} ms"
    if not result.violations:
        return style.paint(f"{style.sym['ok']} All checks passed", "green", "bold") + style.paint(
            f"  {style.sym['dot']} {timing}", "dim"
        )
    errors, warnings = _counts(result.violations)
    files = len({v.path for v in result.violations})
    worst = Severity.ERROR if errors else Severity.WARNING
    glyph = style.paint(style.sym[worst.value], style.sev_color(worst), "bold")
    lines = [
        f"{glyph} Found {_count_phrase(errors, warnings, style)} in {_plural(files, 'file')}"
        + style.paint(f"  {style.sym['dot']} {timing}", "dim")
    ]
    by_code = Counter((v.code, v.rule) for v in result.violations)
    if len(by_code) > 1 or len(result.violations) > 3:
        times = "×" if style.sym is _UNICODE else "x"
        name_w = max(len(rule) for _, rule in by_code)
        for (code, rule), n in by_code.most_common(6):
            lines.append(
                f"  {style.paint(code, 'cyan')} {rule.ljust(name_w)}  {style.paint(f'{times}{n}', 'bold')}"
            )
        if len(by_code) > 6:
            lines.append(style.paint(f"  … and {len(by_code) - 6} more rules", "dim"))
    lines.append(
        dim_note(
            "Each finding's `fix` line says what to write instead. "
            "`lintex explain CODE` has the full story; "
            "`# lintex: ignore[CODE]` silences a line on purpose.",
            style,
        )
    )
    return "\n".join(lines)


# -- full (agent-oriented) ---------------------------------------------------


def format_violation(v: Violation, color: bool = False) -> str:
    style = Style(color=color)
    sev = style.paint(v.severity.value, style.sev_color(v.severity))
    head = f"{style.paint(f'{v.path}:{v.line}:{v.col}', 'bold')}: {v.code} [{sev}] {v.message}"
    lines = [head]
    if v.snippet.strip():
        lines.append(style.paint(f"    | {v.snippet.strip()}", "dim"))
    if v.why:
        lines.append(f"    why: {v.why}")
    if v.fix:
        lines.append(f"    fix: {v.fix}")
    return "\n".join(lines)


def format_full(result: LintResult, style: Style) -> str:
    blocks = [format_violation(v, style.color) for v in result.violations]
    blocks.append(format_summary(result, style))
    return "\n\n".join(blocks)


# -- concise -----------------------------------------------------------------


def format_concise(result: LintResult, style: Style) -> str:
    lines = [
        f"{v.path}:{v.line}:{v.col}: {style.paint(v.code, style.sev_color(v.severity), 'bold')} {v.message}"
        for v in sorted(result.violations, key=lambda v: (v.path, v.line, v.col, v.code))
    ]
    lines.append(format_summary(result, style))
    return "\n".join(lines)


# -- GitHub Actions ------------------------------------------------------------


def _gh_escape(text: str, property_: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def format_github(result: LintResult) -> str:
    lines = []
    for v in result.violations:
        level = "error" if v.severity is Severity.ERROR else "warning"
        props = f"file={_gh_escape(v.path, True)},line={v.line},col={v.col}"
        if v.end_line:
            props += f",endLine={v.end_line}"
        props += f",title={_gh_escape(f'{v.code} {v.rule}', True)}"
        message = v.message + (f" Fix: {v.fix}" if v.fix else "")
        lines.append(f"::{level} {props}::{_gh_escape(message)}")
    lines.append(format_summary(result, PLAIN))
    return "\n".join(lines)


# -- JSON ----------------------------------------------------------------------


def format_json(result: LintResult) -> str:
    return json.dumps(result.to_dict(), indent=2)


# -- dispatch ------------------------------------------------------------------


def render(result: LintResult, fmt: str, style: Style, verbose: bool = False) -> str:
    if fmt == "json":
        return format_json(result)
    if fmt == "github":
        return format_github(result)
    if fmt == "concise":
        return format_concise(result, style)
    if fmt == "full":
        return format_full(result, style)
    return format_pretty(result, style, verbose=verbose)


# Backwards-compatible name used by earlier code and tests.
def format_text(result: LintResult, color: bool = False) -> str:
    return format_pretty(result, Style(color=color, unicode=False))
