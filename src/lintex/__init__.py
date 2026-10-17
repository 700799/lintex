"""lintex: a fast linter for code written by LLM coding agents."""

from __future__ import annotations

__version__ = "0.1.0"

from lintex.config import Config, ConfigError, load_config
from lintex.engine import Linter, LintResult, lint_paths, lint_source
from lintex.models import Severity, Violation

__all__ = [
    "Config",
    "ConfigError",
    "LintResult",
    "Linter",
    "Severity",
    "Violation",
    "__version__",
    "lint_paths",
    "lint_source",
    "load_config",
]
