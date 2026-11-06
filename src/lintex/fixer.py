"""Apply the mechanical rewrites behind ``lintex check --fix``.

A rewrite is a single-line span replacement attached to a violation. Edits are
applied right-to-left within a line so earlier columns stay valid; overlapping
edits are skipped (the second one would be reported again on the next run).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lintex.models import Autofix, Violation


@dataclass
class FixReport:
    files: dict[str, int] = field(default_factory=dict)  # path -> edits applied
    skipped: int = 0

    @property
    def applied(self) -> int:
        return sum(self.files.values())


def apply_autofixes(source: str, fixes: Iterable[Autofix]) -> tuple[str, int, int]:
    """Return ``(new_source, applied, skipped)``."""
    lines = source.splitlines(keepends=True)
    ordered = sorted(fixes, key=lambda f: (f.line, f.col), reverse=True)
    applied = skipped = 0
    last_start: dict[int, int] = {}  # line -> start col of the previous (later) edit
    for fix in ordered:
        index = fix.line - 1
        if not (0 <= index < len(lines)) or fix.end_col <= fix.col:
            skipped += 1
            continue
        if fix.line in last_start and fix.end_col > last_start[fix.line]:
            skipped += 1  # overlaps an edit already made on this line
            continue
        text = lines[index]
        if text[fix.col - 1 : fix.end_col - 1] != fix.original:
            skipped += 1  # the file is not what we linted, or the span was wrong
            continue
        lines[index] = text[: fix.col - 1] + fix.replacement + text[fix.end_col - 1 :]
        last_start[fix.line] = fix.col
        applied += 1
    return "".join(lines), applied, skipped


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return False
    return True


def fix_files(violations: Iterable[Violation], root: Path | None = None) -> FixReport:
    """Apply every autofix in ``violations``, grouped by file, writing files in place."""
    by_path: dict[str, list[Autofix]] = {}
    for v in violations:
        if v.autofix is not None:
            by_path.setdefault(v.path, []).append(v.autofix)
    report = FixReport()
    for path, fixes in by_path.items():
        file = Path(path) if root is None or Path(path).is_absolute() else root / path
        raw = file.read_bytes()
        source = raw.decode("utf-8-sig", errors="surrogateescape")
        new_source, applied, skipped = apply_autofixes(source, fixes)
        report.skipped += skipped
        if applied and _parses(source) and not _parses(new_source):
            # A last line of defence: a fix must never break a file that was valid.
            report.skipped += applied
            continue
        if applied:
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            file.write_bytes(bom + new_source.encode("utf-8", errors="surrogateescape"))
            report.files[path] = applied
    return report
