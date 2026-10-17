"""The lint engine: one parse and one tree walk per file, rules dispatched by node type."""

from __future__ import annotations

import ast
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lintex.config import Config, load_config
from lintex.models import Severity, Violation
from lintex.rules import SyntaxErrorRule, build_rules, get_rule, is_rule_active
from lintex.rules.base import FileContext, Rule

AST = ast.AST

_SUPPRESS_RE = re.compile(r"#\s*lintex:\s*ignore(?:\[([^\]]*)\])?", re.IGNORECASE)
_SKIP_FILE_RE = re.compile(
    r"^\s*#\s*lintex:\s*(?:skip|ignore)-file\b", re.IGNORECASE | re.MULTILINE
)


@dataclass
class LintResult:
    violations: list[Violation] = field(default_factory=list)
    files_checked: int = 0
    duration_ms: float = 0.0

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity is Severity.WARNING)

    def failed(self, fail_on: Severity) -> bool:
        return any(v.severity.rank >= fail_on.rank for v in self.violations)

    def to_dict(self) -> dict[str, Any]:
        from lintex import __version__

        return {
            "tool": "lintex",
            "version": __version__,
            "files_checked": self.files_checked,
            "duration_ms": round(self.duration_ms, 2),
            "summary": {
                "total": len(self.violations),
                "errors": self.error_count,
                "warnings": self.warning_count,
            },
            "violations": [v.to_dict() for v in self.violations],
        }


def _suppressions(lines: list[str]) -> dict[int, set[str] | None]:
    """Map line number -> suppressed codes/names (None means all)."""
    result: dict[int, set[str] | None] = {}
    for lineno, text in enumerate(lines, start=1):
        if "lintex" not in text:
            continue
        match = _SUPPRESS_RE.search(text)
        if match:
            codes = match.group(1)
            result[lineno] = (
                None
                if codes is None
                else {c.strip().upper() for c in codes.split(",") if c.strip()}
            )
    return result


def _is_suppressed(v: Violation, suppressed: dict[int, set[str] | None]) -> bool:
    if v.line not in suppressed:
        return False
    codes = suppressed[v.line]
    if codes is None:
        return True
    return any(v.code.startswith(c) or v.rule.upper() == c for c in codes)


class Linter:
    """Lints Python source with a fixed set of configured rules.

    Construct once and reuse: rules are instantiated and the node-type
    dispatch table is built a single time.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config()
        self.rules: list[Rule] = build_rules(self.config)
        self._dispatch: dict[type[ast.AST], list[Rule]] = defaultdict(list)
        for rule in self.rules:
            for node_type in rule.node_types:
                self._dispatch[node_type].append(rule)
        self._file_rules = [r for r in self.rules if type(r).check_file is not Rule.check_file]
        self._report_syntax = is_rule_active(SyntaxErrorRule, self.config)

    # -- single file ------------------------------------------------------

    def lint_source(self, source: str, path: str = "<string>") -> list[Violation]:
        if _SKIP_FILE_RE.search(source):
            return []
        lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError) as exc:
            return self._syntax_error(exc, path, lines)

        ctx = FileContext(path=path, source=source, tree=tree, config=self.config, lines=lines)
        nodes = self._index(tree, ctx)

        violations: list[Violation] = []
        dispatch = self._dispatch
        for node in nodes:
            handlers = dispatch.get(type(node))
            if handlers:
                for rule in handlers:
                    violations.extend(rule.check_node(node, ctx))
        for rule in self._file_rules:
            violations.extend(rule.check_file(ctx))
        return self._finalize(violations, path, lines)

    def lint_file(
        self, path: str | os.PathLike[str], display_path: str | None = None
    ) -> list[Violation]:
        data = Path(path).read_bytes()
        source = data.decode("utf-8-sig", errors="replace")
        return self.lint_source(source, display_path or str(path))

    # -- many files -------------------------------------------------------

    def iter_files(self, paths: Iterable[str | os.PathLike[str]]) -> Iterator[Path]:
        seen: set[Path] = set()
        for raw in paths:
            path = Path(raw)
            candidates = self._walk(path) if path.is_dir() else [path]
            for candidate in candidates:
                if candidate not in seen and not self.config.is_excluded(candidate):
                    seen.add(candidate)
                    yield candidate

    def _walk(self, root: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if not self.config.is_excluded(Path(dirpath, d))
            )
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield Path(dirpath, filename)

    def lint_paths(self, paths: Iterable[str | os.PathLike[str]]) -> LintResult:
        start = time.perf_counter()
        result = LintResult()
        for path in self.iter_files(paths):
            result.files_checked += 1
            try:
                result.violations.extend(self.lint_file(path, _display(path)))
            except OSError as exc:
                result.violations.append(_io_error(path, exc))
        result.duration_ms = (time.perf_counter() - start) * 1000
        return result

    # -- internals --------------------------------------------------------

    @staticmethod
    def _index(tree: ast.Module, ctx: FileContext) -> list[ast.AST]:  # lintex: ignore[LX304]
        """Walk the tree once, recording parents and import bindings."""
        nodes: list[ast.AST] = []
        parents = ctx.parents
        imports = ctx.imports
        rebound = ctx.rebound
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            nodes.append(node)
            if isinstance(node, ast.Name):
                if not isinstance(node.ctx, ast.Load):
                    rebound.add(node.id)
            elif isinstance(node, ast.arg):
                rebound.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rebound.add(node.name)
                ctx.defined_functions.add(node.name)
            elif isinstance(node, ast.ClassDef):
                rebound.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                rebound.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        imports[alias.asname] = alias.name
                    else:
                        root = alias.name.partition(".")[0]
                        imports[root] = root
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name != "*":
                        imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            # Inlined ast.iter_child_nodes: this loop is the engine's hot path.
            for name in node._fields:
                value = getattr(node, name, None)
                if isinstance(value, list):
                    for child in value:
                        if isinstance(child, AST):
                            parents[child] = node
                            stack.append(child)
                elif isinstance(value, AST):
                    parents[value] = node
                    stack.append(value)
        return nodes

    def _syntax_error(self, exc: Exception, path: str, lines: list[str]) -> list[Violation]:
        if not self._report_syntax:
            return []
        lineno = getattr(exc, "lineno", None) or 1
        offset = getattr(exc, "offset", None) or 1
        msg = getattr(exc, "msg", None) or str(exc)
        snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        lo, hi = max(1, lineno - 2), lineno + 2
        violation = Violation(
            code=SyntaxErrorRule.code,
            rule=SyntaxErrorRule.name,
            message=f"File does not parse: {msg}.",
            path=path,
            line=lineno,
            col=offset,
            severity=Severity.ERROR,
            why="The file is not valid Python, so it cannot be imported or run, and no other "
            "checks were performed. This often happens when an edit was truncated or applied "
            "to the wrong place.",
            fix=f"Re-read lines {lo}-{hi} and fix the syntax (unbalanced brackets/quotes, "
            "indentation, a missing colon, or a partially applied edit).",
            snippet=snippet,
        )
        return self._finalize([violation], path, lines)

    def _finalize(
        self, violations: list[Violation], path: str, lines: list[str]
    ) -> list[Violation]:
        if not violations:
            return violations
        suppressed = _suppressions(lines)
        file_ignores = self.config.ignored_codes_for(path) if path != "<string>" else []
        overrides = self.config.severity
        out = []
        for v in violations:
            if _is_suppressed(v, suppressed):
                continue
            if any(
                v.code.startswith(c) or (get_rule(c) is not None and get_rule(c).code == v.code)
                for c in file_ignores
            ):
                continue
            override = overrides.get(v.code) or overrides.get(v.rule.upper())
            if override is not None and override is not v.severity:
                v = replace(v, severity=override)
            out.append(v)
        out.sort(key=lambda v: (v.line, v.col, v.code))
        return out


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)


def _io_error(path: Path, exc: OSError) -> Violation:
    return Violation(
        code=SyntaxErrorRule.code,
        rule="io-error",
        message=f"Could not read file: {exc.strerror or exc}.",
        path=_display(path),
        line=1,
        col=1,
        severity=Severity.ERROR,
    )


# -- convenience API -------------------------------------------------------


def lint_source(
    source: str, path: str = "<string>", config: Config | None = None
) -> list[Violation]:
    """Lint a string of Python source. Uses default configuration unless given one."""
    return Linter(config).lint_source(source, path)


def lint_paths(paths: Iterable[str | os.PathLike[str]], config: Config | None = None) -> LintResult:
    """Lint files/directories, discovering configuration from the first path if not given."""
    paths = list(paths)
    if config is None:
        config = load_config(paths[0] if paths else None)
    return Linter(config).lint_paths(paths)
