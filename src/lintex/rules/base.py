"""Rule base class and the per-file context rules operate on."""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from lintex.config import Config
from lintex.models import Severity, Violation

_BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass
class FileContext:
    """Everything a rule may need about the file being linted.

    Built once per file by the engine: the AST is walked a single time to
    collect the node list, the parent map and the import alias table, so
    individual rules never have to re-walk the tree to answer those questions.
    """

    path: str
    source: str
    tree: ast.Module
    config: Config
    lines: list[str] = field(default_factory=list)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    # local binding -> fully qualified name, e.g. {"np": "numpy", "Path": "pathlib.Path"}
    imports: dict[str, str] = field(default_factory=dict)
    # names bound anywhere in the file by something other than an import
    # (assignment, parameter, def, class, except-as). Resolution treats them
    # as shadowed: `def f(object): object.x` is not about builtins.object.
    rebound: set[str] = field(default_factory=set)
    # names of functions/methods defined in this file
    defined_functions: set[str] = field(default_factory=set)
    # scratch space for rules that need per-file state, keyed by rule code
    state: dict[str, Any] = field(default_factory=dict)

    def parent(self, node: ast.AST) -> ast.AST | None:
        return self.parents.get(node)

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        current = self.parents.get(node)
        while current is not None:
            yield current
            current = self.parents.get(current)

    def line_text(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def resolve(self, node: ast.AST) -> tuple[str, int] | None:
        """Resolve a Name/Attribute chain to a dotted, import-qualified name.

        Returns ``(qualified_name, binding_parts)`` where ``binding_parts`` is
        how many leading components came from the import binding itself. For
        ``import numpy as np`` the expression ``np.float`` resolves to
        ``("numpy.float", 1)``. Builtins resolve to ``builtins.<name>``.
        Returns None for anything not rooted in an import or builtin.
        """
        attrs: list[str] = []
        while isinstance(node, ast.Attribute):
            attrs.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id in self.rebound:
            return None
        base = self.imports.get(node.id)
        if base is None:
            if node.id not in _BUILTIN_NAMES:
                return None
            base = f"builtins.{node.id}"
        attrs.reverse()
        return ".".join([base, *attrs]), base.count(".") + 1


class Rule:
    """Base class for lint rules.

    Subclasses set the class attributes below and implement
    :meth:`check_node` (called for every node whose type is listed in
    ``node_types``) and/or :meth:`check_file` (called once per file).
    """

    code: ClassVar[str]
    name: ClassVar[str]
    summary: ClassVar[str]
    #: Long-form documentation shown by ``lintex explain``.
    explanation: ClassVar[str] = ""
    default_severity: ClassVar[Severity] = Severity.ERROR
    #: Opt-in rules only run when listed in the config's ``enable``.
    default_enabled: ClassVar[bool] = True
    node_types: ClassVar[tuple[type[ast.AST], ...]] = ()
    #: Option name -> default value. Overridable via ``[tool.lintex.rules.<name>]``.
    options: ClassVar[dict[str, Any]] = {}

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        options = dict(options or {})
        unknown = set(options) - set(self.options)
        if unknown:
            from lintex.config import ConfigError

            valid = ", ".join(sorted(self.options)) or "(none)"
            raise ConfigError(
                f"unknown option(s) for rule {self.code} ({self.name}): "
                f"{', '.join(sorted(unknown))}. Valid options: {valid}"
            )
        self.opts: dict[str, Any] = {**self.options, **options}

    def check_node(self, node: Any, ctx: FileContext) -> Iterable[Violation]:
        return ()

    def check_file(self, ctx: FileContext) -> Iterable[Violation]:
        return ()

    def violation(
        self,
        ctx: FileContext,
        node: ast.AST | int,
        message: str,
        *,
        why: str = "",
        fix: str = "",
        severity: Severity | None = None,
        col: int | None = None,
    ) -> Violation:
        if isinstance(node, int):
            line, column, end_line = node, (col or 0), None
        else:
            line = getattr(node, "lineno", 1)
            column = getattr(node, "col_offset", 0) if col is None else col
            end_line = getattr(node, "end_lineno", None)
        return Violation(
            code=self.code,
            rule=self.name,
            message=message,
            path=ctx.path,
            line=line,
            col=column + 1,
            severity=severity or self.default_severity,
            why=why,
            fix=fix,
            end_line=end_line,
            snippet=ctx.line_text(line).strip(),
        )


# -- shared AST helpers ---------------------------------------------------

_GUARD_EXCEPTIONS = frozenset(
    {"ImportError", "ModuleNotFoundError", "AttributeError", "Exception", "BaseException"}
)


_GUARD_TEST_RE = re.compile(
    r"version_info|sys\.platform|platform\.system|os\.name|hasattr\(|mswindows|WINDOWS"
    r"|is_windows|IS_WINDOWS|win32|pypy|implementation\.name"
)


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    names = set()
    for elt in elts:
        if isinstance(elt, ast.Name):
            names.add(elt.id)
        elif isinstance(elt, ast.Attribute):
            names.add(elt.attr)
    return names


def is_compat_guarded(node: ast.AST, ctx: FileContext) -> bool:
    """True if ``node`` sits inside an import/version/feature compatibility guard.

    Recognises ``try: ... except ImportError`` (and AttributeError/Exception)
    blocks and ``if`` tests mentioning ``sys.version_info``, ``sys.platform``
    or ``hasattr``. Code in such blocks is deliberately probing for APIs that
    may not exist, so hallucination rules must not flag it.
    """
    child = node
    for anc in ctx.ancestors(node):
        if isinstance(anc, ast.Try) or type(anc).__name__ == "TryStar":
            if child in anc.body and any(
                _handler_names(h) & _GUARD_EXCEPTIONS for h in anc.handlers
            ):
                return True
        elif isinstance(anc, (ast.If, ast.IfExp)) and _GUARD_TEST_RE.search(ast.unparse(anc.test)):
            return True
        child = anc
    return False


def call_kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def is_const_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def is_const_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False
