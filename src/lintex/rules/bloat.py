"""LX3xx: agent bloat.

Coding agents tend to grow one function until the task is done instead of
factoring the work. These rules put numeric limits on size, branching,
parameter count and nesting, and say *where* to split.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import Union

from lintex.models import Severity, Violation
from lintex.rules.base import FileContext, Rule

FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _docstring_lines(fn: FunctionNode) -> set[int]:
    if (
        fn.body
        and isinstance(fn.body[0], ast.Expr)
        and isinstance(fn.body[0].value, ast.Constant)
        and isinstance(fn.body[0].value.value, str)
    ):
        doc = fn.body[0]
        return set(range(doc.lineno, (doc.end_lineno or doc.lineno) + 1))
    return set()


def _code_lines(fn: FunctionNode, ctx: FileContext) -> int:
    """Count non-blank, non-comment, non-docstring lines from `def` to the end."""
    skip = _docstring_lines(fn)
    count = 0
    for lineno in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1):
        if lineno in skip:
            continue
        text = ctx.line_text(lineno).strip()
        if text and not text.startswith("#"):
            count += 1
    return count


def _largest_blocks(fn: FunctionNode, limit: int = 3) -> list[str]:
    blocks = []
    for stmt in fn.body:
        span = (stmt.end_lineno or stmt.lineno) - stmt.lineno + 1
        if span >= 6 and not isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            kind = type(stmt).__name__.lower().replace("async", "async ")
            blocks.append((span, f"lines {stmt.lineno}-{stmt.end_lineno} ({kind} block)"))
    blocks.sort(key=lambda b: -b[0])
    return [desc for _, desc in blocks[:limit]]


_BRANCH_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.IfExp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.Assert,
    ast.BoolOp,
    ast.comprehension,
) + ((ast.match_case,) if hasattr(ast, "match_case") else ())


def branch_weight(node: ast.AST) -> int:
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs)
    return 1


def enclosing_function(node: ast.AST, ctx: FileContext) -> FunctionNode | None:
    for anc in ctx.ancestors(node):
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return anc
        if isinstance(anc, ast.ClassDef):
            return None
    return None


def cyclomatic_complexity(fn: FunctionNode) -> int:
    """Standalone McCabe complexity of one function (nested functions excluded)."""
    complexity = 1
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, _BRANCH_NODES):
            complexity += branch_weight(node)
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPE_NODES))
    return complexity


def _child_blocks(stmt: ast.stmt) -> list[tuple[list[ast.stmt], bool]]:
    """Return (statement list, adds_depth) pairs nested under ``stmt``."""
    if isinstance(stmt, ast.If):
        # `elif` chains are written flat, so they don't add depth.
        is_elif = len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If)
        return [(stmt.body, True), (stmt.orelse, not is_elif)]
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        return [(stmt.body, True), (stmt.orelse, True)]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [(stmt.body, True)]
    if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
        blocks = [(stmt.body, True), (stmt.orelse, True), (stmt.finalbody, True)]
        blocks += [(h.body, True) for h in stmt.handlers]
        return blocks
    if type(stmt).__name__ == "Match":
        return [(case.body, True) for case in stmt.cases]
    return []


def max_nesting(body: list[ast.stmt], depth: int = 0) -> tuple[int, int]:
    """Return (max depth, line of the deepest statement) for a statement list."""
    best = (depth, body[0].lineno if body else 0)
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for block, adds in _child_blocks(stmt):
            if block:
                result = max_nesting(block, depth + 1 if adds else depth)
                if result[0] > best[0]:
                    best = result
    return best


def _fn_label(fn: FunctionNode) -> str:
    return f"`{fn.name}()`"


_BLOAT_WHY = (
    "Agents tend to keep appending to one function until the task works, producing code "
    "that mixes several responsibilities. Large, branchy functions are harder to review, "
    "test and safely edit - for humans and for the next agent turn."
)


class FunctionTooLongRule(Rule):
    code = "LX301"
    name = "function-too-long"
    summary = "Function body exceeds the configured number of code lines."
    explanation = "Counts non-blank, non-comment lines excluding the docstring. Option: `max-lines` (default 50)."
    default_severity = Severity.WARNING
    node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    options = {"max-lines": 50}

    def check_node(self, node: FunctionNode, ctx: FileContext) -> Iterator[Violation]:
        limit = int(self.opts["max-lines"])
        lines = _code_lines(node, ctx)
        if lines <= limit:
            return
        blocks = _largest_blocks(node)
        where = f" Good candidates to extract: {'; '.join(blocks)}." if blocks else ""
        yield self.violation(
            ctx,
            node,
            f"Function {_fn_label(node)} has {lines} lines of code (limit {limit}).",
            why=_BLOAT_WHY,
            fix=f"Split {_fn_label(node)} into smaller, well-named helpers that each do one thing, "
            f"and keep it as a short orchestrator.{where} Do not just move code into nested "
            "functions or delete error handling to get under the limit.",
        )


class ComplexityRule(Rule):
    code = "LX302"
    name = "high-complexity"
    summary = "Function's cyclomatic complexity exceeds the configured limit."
    explanation = "McCabe complexity: 1 + one per branch, loop, handler, boolean operator and comprehension. Option: `max-complexity` (default 10)."
    default_severity = Severity.WARNING
    # Branches are tallied per enclosing function during the engine's single
    # walk, instead of re-walking every function body.
    node_types = _BRANCH_NODES
    options = {"max-complexity": 10}

    def check_node(self, node: ast.AST, ctx: FileContext) -> Iterator[Violation]:
        fn = enclosing_function(node, ctx)
        if fn is not None:
            scores = ctx.state.setdefault(self.code, {})
            scores[fn] = scores.get(fn, 1) + branch_weight(node)
        return ()

    def check_file(self, ctx: FileContext) -> Iterator[Violation]:
        limit = int(self.opts["max-complexity"])
        for fn, score in ctx.state.get(self.code, {}).items():
            if score > limit:
                yield self.violation(
                    ctx,
                    fn,
                    f"Function {_fn_label(fn)} has cyclomatic complexity {score} (limit {limit}).",
                    why=_BLOAT_WHY,
                    fix="Reduce branching: return early for edge cases (guard clauses), replace "
                    "long if/elif chains with a dict lookup or polymorphism, and move "
                    "independent branches into helper functions.",
                )


class TooManyParametersRule(Rule):
    code = "LX303"
    name = "too-many-parameters"
    summary = "Function takes more parameters than the configured limit."
    explanation = "Counts named parameters excluding `self`/`cls`, `*args` and `**kwargs`. Option: `max-params` (default 7)."
    default_severity = Severity.WARNING
    node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    options = {"max-params": 7}

    def check_node(self, node: FunctionNode, ctx: FileContext) -> Iterator[Violation]:
        args = node.args
        params = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        limit = int(self.opts["max-params"])
        if len(params) > limit:
            yield self.violation(
                ctx,
                node,
                f"Function {_fn_label(node)} takes {len(params)} parameters (limit {limit}).",
                why="Long parameter lists are easy to call with arguments in the wrong order and "
                "usually mean the function does too much or related values travel separately.",
                fix="Group related parameters into a dataclass/TypedDict, make optional settings "
                "keyword-only (after `*`), or split the function.",
            )


class DeepNestingRule(Rule):
    code = "LX304"
    name = "deep-nesting"
    summary = "Control flow nested deeper than the configured limit."
    explanation = "Counts nested if/for/while/with/try/match blocks (elif does not nest). Option: `max-depth` (default 4)."
    default_severity = Severity.WARNING
    node_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    options = {"max-depth": 4}

    def check_node(self, node: FunctionNode, ctx: FileContext) -> Iterator[Violation]:
        limit = int(self.opts["max-depth"])
        depth, line = max_nesting(node.body)
        if depth > limit:
            yield self.violation(
                ctx,
                node,
                f"Function {_fn_label(node)} nests blocks {depth} levels deep at line {line} "
                f"(limit {limit}).",
                why="Deeply nested code is hard to follow and is where edge-case bugs hide.",
                fix="Invert conditions and `return`/`continue` early, and extract the inner loop "
                "or block into a helper function.",
            )


class FileTooLongRule(Rule):
    code = "LX305"
    name = "file-too-long"
    summary = "Module exceeds the configured number of code lines."
    explanation = "Counts non-blank, non-comment lines. Option: `max-lines` (default 600)."
    default_severity = Severity.WARNING
    options = {"max-lines": 600}

    def check_file(self, ctx: FileContext) -> Iterator[Violation]:
        limit = int(self.opts["max-lines"])
        count = sum(1 for line in ctx.lines if line.strip() and not line.lstrip().startswith("#"))
        if count > limit:
            yield self.violation(
                ctx,
                1,
                f"Module has {count} lines of code (limit {limit}).",
                why=_BLOAT_WHY,
                fix="Split the module by responsibility into a package of smaller modules and "
                "re-export the public names from `__init__.py` if callers depend on them.",
            )
