"""Core data types shared by the engine, rules and reporters."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class Severity(str, enum.Enum):
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return 2 if self is Severity.ERROR else 1

    @classmethod
    def parse(cls, value: str | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        try:
            return cls(str(value).lower())
        except ValueError:
            raise ValueError(f"invalid severity {value!r}; expected 'error' or 'warning'") from None


@dataclass(frozen=True)
class Violation:
    """A single lint finding.

    ``message`` says what is wrong, ``why`` explains the failure mode and
    ``fix`` says what to write instead. The three together are meant to be
    enough for an LLM to repair the code without further context.
    """

    code: str
    rule: str
    message: str
    path: str
    line: int
    col: int
    severity: Severity
    why: str = ""
    fix: str = ""
    end_line: int | None = None
    #: 1-based, exclusive; only meaningful when ``end_line == line``.
    end_col: int | None = None
    #: The offending source line with its indentation (trailing whitespace removed).
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule": self.rule,
            "severity": self.severity.value,
            "path": self.path,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "end_col": self.end_col,
            "message": self.message,
            "why": self.why,
            "fix": self.fix,
            "snippet": self.snippet.strip(),
        }
