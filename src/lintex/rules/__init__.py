"""Rule registry."""

from __future__ import annotations

from lintex.config import Config, ConfigError
from lintex.models import Severity
from lintex.rules.agent_artifacts import BareExceptRule, PlaceholderCodeRule, SwallowedExceptionRule
from lintex.rules.banned_patterns import (
    DangerousCallRule,
    InsecureFileOperationRule,
    ProjectBannedApiRule,
    RawSqlRule,
    SqlInterpolationRule,
)
from lintex.rules.base import FileContext, Rule
from lintex.rules.bloat import (
    ComplexityRule,
    DeepNestingRule,
    FileTooLongRule,
    FunctionTooLongRule,
    TooManyParametersRule,
)
from lintex.rules.hallucinations import (
    KnownBadApiRule,
    NonexistentStdlibNameRule,
    UnresolvableImportRule,
)


class SyntaxErrorRule(Rule):
    """Pseudo-rule: the engine emits LX001 itself when a file does not parse."""

    code = "LX001"
    name = "syntax-error"
    summary = "File does not parse as Python."
    explanation = (
        "Reported when `ast.parse` fails. Often caused by a truncated or partially applied edit. "
        "No other rules run on a file that does not parse."
    )


ALL_RULES: tuple[type[Rule], ...] = (
    SyntaxErrorRule,
    KnownBadApiRule,
    NonexistentStdlibNameRule,
    UnresolvableImportRule,
    SqlInterpolationRule,
    RawSqlRule,
    DangerousCallRule,
    InsecureFileOperationRule,
    ProjectBannedApiRule,
    FunctionTooLongRule,
    ComplexityRule,
    TooManyParametersRule,
    DeepNestingRule,
    FileTooLongRule,
    PlaceholderCodeRule,
    SwallowedExceptionRule,
    BareExceptRule,
)

_BY_KEY: dict[str, type[Rule]] = {}
for _rule in ALL_RULES:
    _BY_KEY[_rule.code.lower()] = _rule
    _BY_KEY[_rule.name] = _rule


def get_rule(code_or_name: str) -> type[Rule] | None:
    return _BY_KEY.get(code_or_name.strip().lower().replace("_", "-"))


def _selected(code: str, patterns: list[str]) -> bool:
    return any(code.startswith(p) for p in patterns)


def is_rule_active(rule: type[Rule], config: Config) -> bool:
    explicitly_enabled = any(get_rule(e) is rule for e in config.enable)
    if config.select:
        active = _selected(rule.code, config.select) or explicitly_enabled
        # Opt-in rules still need an exact selection (e.g. "LX103"), not a prefix.
        if not rule.default_enabled and not explicitly_enabled:
            active = rule.code in config.select
    else:
        active = rule.default_enabled or explicitly_enabled
    return active and not _selected(rule.code, config.ignore)


def build_rules(config: Config) -> list[Rule]:
    """Instantiate the active rules with options from ``config``."""
    for key in config.rule_options:
        if get_rule(key) is None:
            raise ConfigError(f"unknown rule {key!r} in [rules]; run `lintex rules` to list rules")
    for key in config.enable:
        if get_rule(key) is None:
            raise ConfigError(f"unknown rule {key!r} in `enable`; run `lintex rules` to list rules")
    rules = []
    for cls in ALL_RULES:
        if cls is SyntaxErrorRule or not is_rule_active(cls, config):
            continue
        options: dict = {}
        for key, opts in config.rule_options.items():
            if get_rule(key) is cls:
                options.update(opts)
        rules.append(cls(options))
    return rules


__all__ = [
    "ALL_RULES",
    "FileContext",
    "Rule",
    "Severity",
    "build_rules",
    "get_rule",
    "is_rule_active",
]
