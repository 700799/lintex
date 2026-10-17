"""Project configuration discovery and parsing.

lintex looks for configuration in the first directory (walking upwards from
the start path) that contains one of, in order of precedence:

* ``.lintex.yaml`` / ``.lintex.yml`` (requires ``pip install lintex[yaml]``)
* ``.lintex.toml``
* ``pyproject.toml`` with a ``[tool.lintex]`` table

All keys accept either ``kebab-case`` or ``snake_case``.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lintex.models import Severity

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on older interpreters only
    import tomli as tomllib


CONFIG_FILENAMES = (".lintex.yaml", ".lintex.yml", ".lintex.toml", "pyproject.toml")

DEFAULT_EXCLUDE = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "*.egg-info",
)

_TOP_LEVEL_KEYS = {
    "select",
    "ignore",
    "enable",
    "exclude",
    "extend-exclude",
    "per-file-ignores",
    "severity",
    "fail-on",
    "rules",
}


class ConfigError(Exception):
    """Raised for unreadable or invalid configuration."""


def _norm_key(key: str) -> str:
    return str(key).strip().replace("_", "-").lower()


def _str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ConfigError(f"`{key}` must be a string or a list of strings, got {value!r}")


@dataclass
class Config:
    select: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    enable: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    per_file_ignores: dict[str, list[str]] = field(default_factory=dict)
    severity: dict[str, Severity] = field(default_factory=dict)
    fail_on: Severity = Severity.ERROR
    rule_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    root: Path = field(default_factory=Path.cwd)
    source: Path | None = None

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], root: Path | None = None, source: Path | None = None
    ) -> Config:
        if not isinstance(data, dict):
            raise ConfigError("lintex configuration must be a table/mapping")
        data = {_norm_key(k): v for k, v in data.items()}
        unknown = set(data) - _TOP_LEVEL_KEYS
        if unknown:
            raise ConfigError(
                f"unknown lintex config key(s): {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(_TOP_LEVEL_KEYS))}"
            )
        cfg = cls(root=(root or Path.cwd()).resolve(), source=source)
        cfg.select = [s.upper() for s in _str_list(data.get("select", []), "select")]
        cfg.ignore = [s.upper() for s in _str_list(data.get("ignore", []), "ignore")]
        cfg.enable = _str_list(data.get("enable", []), "enable")
        # Both keys add to DEFAULT_EXCLUDE: replacing the defaults would
        # silently start linting .venv/ and build/ trees.
        cfg.exclude += _str_list(data.get("exclude", []), "exclude")
        cfg.exclude += _str_list(data.get("extend-exclude", []), "extend-exclude")

        pfi = data.get("per-file-ignores", {})
        if not isinstance(pfi, dict):
            raise ConfigError("`per-file-ignores` must map glob patterns to lists of codes")
        cfg.per_file_ignores = {
            pat: [c.upper() for c in _str_list(codes, f"per-file-ignores.{pat}")]
            for pat, codes in pfi.items()
        }

        sev = data.get("severity", {})
        if not isinstance(sev, dict):
            raise ConfigError("`severity` must map rule codes to 'error' or 'warning'")
        try:
            cfg.severity = {str(k).upper(): Severity.parse(v) for k, v in sev.items()}
            cfg.fail_on = Severity.parse(data.get("fail-on", "error"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from None

        rules = data.get("rules", {})
        if not isinstance(rules, dict) or not all(isinstance(v, dict) for v in rules.values()):
            raise ConfigError("`rules` must map rule names/codes to tables of options")
        cfg.rule_options = {
            _norm_key(name): {_norm_key(k): v for k, v in opts.items()}
            for name, opts in rules.items()
        }
        return cfg

    # -- path filtering ---------------------------------------------------

    def relpath(self, path: str | os.PathLike[str]) -> str:
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    def is_excluded(self, path: str | os.PathLike[str]) -> bool:
        return _matches_any(self.relpath(path), self.exclude)

    def ignored_codes_for(self, path: str | os.PathLike[str]) -> list[str]:
        rel = self.relpath(path)
        codes: list[str] = []
        for pattern, pcodes in self.per_file_ignores.items():
            if _matches_any(rel, [pattern]):
                codes.extend(pcodes)
        return codes


def _matches_any(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        pat = pat.rstrip("/")
        if pat.startswith("./"):
            pat = pat[2:]
        if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(rel, pat + "/*"):
            return True
        # gitignore-style: a bare name matches any path component.
        if "/" not in pat and any(fnmatch.fnmatchcase(part, pat) for part in parts):
            return True
    return False


# -- discovery ------------------------------------------------------------


def _read_file(path: Path) -> dict[str, Any] | None:
    """Return the lintex table from ``path``, or None if it has none."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None

    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ConfigError(
                f"{path.name} requires PyYAML. Install it with `pip install lintex[yaml]` "
                "or move the configuration into pyproject.toml under [tool.lintex]."
            ) from None
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from None
        return data or {}

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None
    if path.name == "pyproject.toml":
        return data.get("tool", {}).get("lintex")
    return data


def find_config_file(start: Path) -> tuple[Path, dict[str, Any]] | None:
    start = start.resolve()
    if start.is_file():
        start = start.parent
    for directory in (start, *start.parents):
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                data = _read_file(candidate)
                if data is not None:
                    return candidate, data
    return None


def load_config(
    start: str | os.PathLike[str] | None = None, explicit: str | os.PathLike[str] | None = None
) -> Config:
    """Load configuration from ``explicit`` or by searching upwards from ``start``."""
    if explicit is not None:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        data = _read_file(path)
        return Config.from_dict(data or {}, root=path.parent, source=path)

    start_path = Path(start) if start is not None else Path.cwd()
    found = find_config_file(start_path)
    if found is None:
        root = start_path.resolve()
        return Config(root=root.parent if root.is_file() else root)
    path, data = found
    return Config.from_dict(data, root=path.parent, source=path)
