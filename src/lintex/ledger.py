"""The session ledger: what lintex caught while an agent worked, and what got fixed.

Every Claude Code hook run appends one line per linted file to a JSONL file.
Nothing is written inside the repository: ledgers live in the user's cache
directory, keyed by project root (override with ``LINTEX_LEDGER_DIR``).

``summarize`` replays those lines per file in order. A finding that appears
in a run but not in the previous run for the same file was *caught*; one
that was present before and is gone now was *fixed*; whatever the latest run
still reports is *open*. Findings are matched by rule and message with digits
masked, so "72 lines (limit 70)" and "65 lines (limit 70)" count as the same
finding rather than one fixed and one new.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lintex.models import Severity, Violation

LEDGER_ENV = "LINTEX_LEDGER_DIR"
MAX_LINES = 5000
_KEEP_LINES = 4000
_DIGITS = re.compile(r"\d+")


def ledger_dir() -> Path:
    override = os.environ.get(LEDGER_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "lintex" / "ledgers"


def ledger_path(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]
    return ledger_dir() / f"{digest}.jsonl"


@dataclass
class Entry:
    ts: datetime
    session: str
    tool: str
    path: str
    findings: list[dict]

    @property
    def health(self) -> str:
        severities = {f.get("severity") for f in self.findings}
        if "error" in severities:
            return "error"
        if "warning" in severities:
            return "warning"
        return "clean"


def _finding_dict(v: Violation) -> dict:
    return {
        "code": v.code,
        "rule": v.rule,
        "severity": v.severity.value,
        "line": v.line,
        "message": v.message,
    }


def record(
    root: Path,
    *,
    session: str,
    tool: str,
    files: list[str],
    violations: list[Violation],
) -> Path:
    """Append one entry per linted file. Files with no findings are recorded as clean."""
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    by_path: dict[str, list[Violation]] = {f: [] for f in files}
    for v in violations:
        by_path.setdefault(v.path, []).append(v)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        json.dumps(
            {
                "ts": now,
                "session": session,
                "tool": tool,
                "path": _rel(root, p),
                "findings": [_finding_dict(v) for v in vs],
            }
        )
        for p, vs in by_path.items()
    ]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _prune(path)
    return path


def _rel(root: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _prune(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > MAX_LINES:
        path.write_text("\n".join(lines[-_KEEP_LINES:]) + "\n", encoding="utf-8")


def load(root: Path, *, since: datetime | None = None, session: str | None = None) -> list[Entry]:
    path = ledger_path(root)
    if not path.exists():
        return []
    entries: list[Entry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            ts = datetime.fromisoformat(raw["ts"])
        except (ValueError, KeyError, TypeError):
            continue  # a torn write; skip the line rather than the whole ledger
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if since is not None and ts < since:
            continue
        if session is not None and raw.get("session") != session:
            continue
        entries.append(
            Entry(
                ts=ts,
                session=str(raw.get("session") or ""),
                tool=str(raw.get("tool") or ""),
                path=str(raw.get("path") or ""),
                findings=list(raw.get("findings") or []),
            )
        )
    return entries


def clear(root: Path) -> bool:
    path = ledger_path(root)
    if path.exists():
        path.unlink()
        return True
    return False


# -- analysis --------------------------------------------------------------------


@dataclass
class CodeStats:
    rule: str
    caught: int = 0
    fixed: int = 0
    open: int = 0


@dataclass
class Summary:
    runs: int = 0
    files: int = 0
    caught: int = 0
    fixed: int = 0
    open_findings: list[dict] = field(default_factory=list)
    by_code: dict[str, CodeStats] = field(default_factory=dict)
    #: health of the most recent runs, oldest first
    health: list[str] = field(default_factory=list)
    sessions: set[str] = field(default_factory=set)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    @property
    def open(self) -> int:
        return len(self.open_findings)

    @property
    def open_errors(self) -> int:
        return sum(1 for f in self.open_findings if f.get("severity") == "error")

    @property
    def open_warnings(self) -> int:
        return self.open - self.open_errors

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "files": self.files,
            "caught": self.caught,
            "fixed": self.fixed,
            "open": self.open,
            "open_errors": self.open_errors,
            "open_warnings": self.open_warnings,
            "sessions": sorted(self.sessions),
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "by_code": {
                code: {"rule": s.rule, "caught": s.caught, "fixed": s.fixed, "open": s.open}
                for code, s in self.by_code.items()
            },
            "open_findings": self.open_findings,
            "health": self.health,
        }


def fingerprint(finding: dict) -> tuple[str, str]:
    return finding.get("code", ""), _DIGITS.sub("#", finding.get("message", ""))


def summarize(entries: list[Entry], recent: int = 10) -> Summary:
    summary = Summary()
    if not entries:
        return summary
    entries = sorted(entries, key=lambda e: e.ts)
    summary.runs = len(entries)
    summary.first_ts, summary.last_ts = entries[0].ts, entries[-1].ts
    summary.health = [e.health for e in entries[-recent:]]
    summary.sessions = {e.session for e in entries if e.session}

    previous: dict[str, Counter] = {}
    latest: dict[str, list[dict]] = {}
    for entry in entries:
        current = Counter(fingerprint(f) for f in entry.findings)
        before = previous.get(entry.path, Counter())
        for key, n in (current - before).items():
            summary.caught += n
            summary.by_code.setdefault(key[0], CodeStats(rule=_rule_for(entry, key[0]))).caught += n
        for key, n in (before - current).items():
            summary.fixed += n
            summary.by_code.setdefault(key[0], CodeStats(rule=key[0])).fixed += n
        previous[entry.path] = current
        latest[entry.path] = [dict(f, path=entry.path) for f in entry.findings]

    summary.files = len(latest)
    for findings in latest.values():
        for f in findings:
            summary.open_findings.append(f)
            summary.by_code.setdefault(f["code"], CodeStats(rule=f.get("rule", ""))).open += 1
    summary.open_findings.sort(
        key=lambda f: (f.get("path", ""), f.get("line", 0), f.get("code", ""))
    )
    return summary


def _rule_for(entry: Entry, code: str) -> str:
    for f in entry.findings:
        if f.get("code") == code:
            return str(f.get("rule") or "")
    return ""


def parse_since(text: str) -> timedelta:
    """``90m``, ``24h``, ``7d`` (or a bare number of hours)."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhd]?)\s*", text)
    if not match:
        raise ValueError(f"invalid duration {text!r}; use e.g. 90m, 24h or 7d")
    value, unit = float(match.group(1)), match.group(2) or "h"
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: value})


__all__ = [
    "LEDGER_ENV",
    "CodeStats",
    "Entry",
    "Severity",
    "Summary",
    "clear",
    "fingerprint",
    "ledger_path",
    "load",
    "parse_since",
    "record",
    "summarize",
]
