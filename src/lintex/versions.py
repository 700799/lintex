"""Which versions of third-party packages a project actually uses.

LX101 knows that ``pydantic.BaseSettings`` left pydantic in 2.0. In a project pinned to
pydantic 1.10 that import is correct, and flagging it is a false alarm. This module
answers one question for the rule: *what is the newest version of this package the
project can be running?* If that is older than the version an API was removed in, the
API still exists for this project and the finding is dropped.

Sources, most specific first (the first one that mentions the package wins):

1. ``package-versions`` in lintex's own configuration (an explicit statement).
2. Lockfiles and pins in the project root: ``uv.lock``, ``poetry.lock``, ``pdm.lock``,
   ``Pipfile.lock``, ``requirements*.txt`` / ``constraints*.txt`` (``==`` pins).
3. Upper bounds declared in ``pyproject.toml``: ``[project] dependencies``,
   optional dependencies, dependency groups and ``[tool.poetry]`` dependencies
   (``pydantic<2``, ``~=1.10``, ``^1.10``).

A lower bound alone (``pydantic>=1.10``) says nothing: a fresh install resolves to the
latest release, so the finding stands. The environment lintex itself runs in is never
consulted, since lintex is often installed separately from the project (pipx, uvx,
pre-commit), where it would describe the wrong environment.

Everything is read lazily, at most once per project root, and only when a finding about
a third-party package is about to be reported.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

Version = tuple[int, ...]

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_CLAUSE_RE = re.compile(r"(===|==|~=|<=|>=|!=|<|>|\^|~)\s*([0-9][0-9A-Za-z.*+!-]*)")
# `name = "x"` then `version = "y"` inside a `[[package]]` table (uv, poetry, pdm).
_LOCK_PACKAGE_RE = re.compile(
    r'^\[\[package\]\]\s*\n(?:(?!\[\[).*\n)*?name\s*=\s*"([^"]+)"\s*\n(?:(?!\[\[).*\n)*?'
    r'version\s*=\s*"([^"]+)"',
    re.MULTILINE,
)


def normalize(name: str) -> str:
    """PEP 503 normalisation: ``Scikit_Learn`` and ``scikit-learn`` are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(text: str) -> Version:
    """Leading release numbers of a version string: ``"2.0.1rc1"`` -> ``(2, 0, 1)``."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(text))
    return tuple(int(p) for p in match.group(1).split(".")) if match else ()


@dataclass(frozen=True)
class Ceiling:
    """The newest version of a package a project can be on."""

    version: Version
    #: False for an exclusive bound (``<2``): the project is strictly below ``version``.
    inclusive: bool
    #: Where this was learned, for messages and `lintex doctor` (e.g. ``uv.lock``).
    source: str

    def below(self, other: Version) -> bool:
        """True when every version the project can be on is older than ``other``."""
        if not self.version or not other:
            return False
        width = max(len(self.version), len(other))
        mine = self.version + (0,) * (width - len(self.version))
        theirs = other + (0,) * (width - len(other))
        return mine < theirs if self.inclusive else mine <= theirs


def _bump(version: Version, index: int) -> Version:
    index = max(0, min(index, len(version) - 1))
    return (*version[:index], version[index] + 1)


def _caret(version: Version) -> Version:
    """Poetry's ``^``: up to the next change of the left-most non-zero part."""
    for i, part in enumerate(version):
        if part:
            return _bump(version, i)
    return _bump(version, len(version) - 1)


def ceiling_from_specifier(spec: str, source: str) -> Ceiling | None:
    """The tightest upper bound in a specifier such as ``>=1.10,<2`` or ``~=1.10``."""
    best: Ceiling | None = None
    for op, raw in _CLAUSE_RE.findall(spec):
        version = parse(raw)
        if not version:
            continue
        if op in ("==", "===") and raw.endswith(".*"):
            found = Ceiling(_bump(version, len(version) - 1), False, source)
        elif op in ("==", "===", "<="):
            found = Ceiling(version, True, source)
        elif op == "<":
            found = Ceiling(version, False, source)
        elif op == "~=" and len(version) > 1:
            found = Ceiling(_bump(version, len(version) - 2), False, source)
        elif op == "^":
            found = Ceiling(_caret(version), False, source)
        elif op == "~":  # poetry tilde: ~1.2 -> <1.3, ~1 -> <2
            found = Ceiling(_bump(version, min(1, len(version) - 1)), False, source)
        else:
            continue  # lower bounds and exclusions put no ceiling on anything
        if best is None or (found.version, found.inclusive) < (best.version, best.inclusive):
            best = found
    return best


def _requirement(line: str) -> tuple[str, str] | None:
    """Split ``pydantic[email]>=1.10,<2 ; python_version < "3.12"`` into name and specifier."""
    line = line.split(" #")[0].strip()
    if not line or line.startswith(("#", "-", "git+", "http:", "https:", ".", "/")):
        return None
    line = line.split(";")[0]
    if "@" in line:  # direct reference: `pkg @ https://...`
        return None
    match = _NAME_RE.match(line)
    return (normalize(match.group(1)), match.group(2)) if match else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _from_toml_lock(path: Path, found: dict[str, Ceiling]) -> None:
    # A regex scan, not a TOML parse: lockfiles run to megabytes and only two keys matter.
    for name, version in _LOCK_PACKAGE_RE.findall(_read(path)):
        parsed = parse(version)
        if parsed:
            found.setdefault(normalize(name), Ceiling(parsed, True, path.name))


def _from_pipfile_lock(path: Path, found: dict[str, Ceiling]) -> None:
    try:
        data = json.loads(_read(path) or "{}")
    except ValueError:
        return
    for section in ("default", "develop"):
        for name, info in (data.get(section) or {}).items():
            version = parse(str((info or {}).get("version", "")).lstrip("="))
            if version:
                found.setdefault(normalize(name), Ceiling(version, True, path.name))


def _from_requirements(path: Path, found: dict[str, Ceiling]) -> None:
    for line in _read(path).splitlines():
        req = _requirement(line)
        if req is None:
            continue
        ceiling = ceiling_from_specifier(req[1], path.name)
        if ceiling is not None:
            found.setdefault(req[0], ceiling)


def _poetry_spec(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("version", "")
    if not isinstance(value, str):
        return ""
    # A bare poetry version ("1.10") means exactly that version.
    return f"=={value}" if re.fullmatch(r"\s*\d[\w.]*\s*", value) else value


def _from_pyproject(path: Path, found: dict[str, Ceiling]) -> None:
    try:
        data = tomllib.loads(_read(path))
    except tomllib.TOMLDecodeError:
        return
    project = data.get("project") or {}
    requirements: list[Any] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        requirements += group or []
    for group in (data.get("dependency-groups") or {}).values():
        requirements += group or []
    for line in requirements:
        req = _requirement(line) if isinstance(line, str) else None
        if req is None:
            continue
        ceiling = ceiling_from_specifier(req[1], "pyproject.toml")
        if ceiling is not None:
            found.setdefault(req[0], ceiling)

    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    tables += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        for name, value in table.items():
            ceiling = ceiling_from_specifier(_poetry_spec(value), "pyproject.toml")
            if ceiling is not None and normalize(name) != "python":
                found.setdefault(normalize(name), ceiling)


_MANIFESTS = ("pyproject.toml", "uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock")


def _dependency_dir(start: Path) -> Path:
    """The nearest directory at or above ``start`` that declares dependencies.

    With no lintex config, a file's own directory is the config root, but the lockfile
    lives at the top of the repository. The search stops at the repository boundary.
    """
    for directory in (start, *start.parents):
        try:
            if any((directory / name).is_file() for name in _MANIFESTS) or any(
                p.name.startswith(("requirements", "constraints")) and p.suffix == ".txt"
                for p in directory.iterdir()
            ):
                return directory
        except OSError:
            break
        if (directory / ".git").exists():
            break
    return start


@lru_cache(maxsize=64)
def discover(start: Path) -> dict[str, Ceiling]:
    """Every package ceiling the project around ``start`` establishes, by normalised name."""
    root = _dependency_dir(start)
    found: dict[str, Ceiling] = {}
    for name in ("uv.lock", "poetry.lock", "pdm.lock"):
        if (root / name).is_file():
            _from_toml_lock(root / name, found)
    if (root / "Pipfile.lock").is_file():
        _from_pipfile_lock(root / "Pipfile.lock", found)
    try:
        candidates = sorted(p for p in root.iterdir() if p.suffix == ".txt" and p.is_file())
    except OSError:
        candidates = []
    for path in candidates:
        if path.name.startswith(("requirements", "constraints")):
            _from_requirements(path, found)
    if (root / "pyproject.toml").is_file():
        _from_pyproject(root / "pyproject.toml", found)
    return found


def ceiling_for(package: str, root: Path, explicit: dict[str, str] | None = None) -> Ceiling | None:
    """The newest version of ``package`` the project at ``root`` can be on, if known."""
    key = normalize(package)
    if explicit and key in explicit:
        spec = explicit[key].strip()
        if re.fullmatch(r"\d[\w.]*", spec):  # a bare version is a statement of fact
            return Ceiling(parse(spec), True, "package-versions")
        return ceiling_from_specifier(spec, "package-versions")
    return discover(root).get(key)


__all__ = ["Ceiling", "ceiling_for", "ceiling_from_specifier", "discover", "normalize", "parse"]
