"""LX4xx: artifacts typical of agent-written code.

Elided code ("# ... rest of the code unchanged"), exception handlers that
silence errors to make a failure go away, and bare ``except:`` clauses.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Iterator

from lintex.models import Severity, Violation
from lintex.rules.base import FileContext, Rule

_ELISION_SUBJECT = re.compile(
    r"\b(?:(?:rest|remainder) of (?:the |this |your )?"
    r"(?:code|implementation|function|file|class|method|logic|module|body)"
    r"|(?:existing|previous|original|other|remaining) "
    r"(?:code|implementation|logic|methods?|functions?|imports?|tests?))\b",
    re.IGNORECASE,
)
_ELISION_MARKER = re.compile(
    r"\.\.\.|\u2026|\bhere\b|\bunchanged\b|\bremains?\b|\bstays?\b|\bas (?:before|is)\b"
    r"|\bomitted\b|\bgoes\b",
    re.IGNORECASE,
)

# Elisions with words between the adjective and the noun: "existing validation logic",
# "rest of the request handling code", "other helper methods". The wider subject makes a
# chance match likelier ("the existing user guide is described here"), so these need a
# marker that only an elision uses, not a bare "here" or "remains".
_WIDE_NOUNS = (
    r"code|implementation|logic|methods?|functions?|imports?|tests?|classes|class|handlers?|"
    r"validation|handling|checks?|processing|parsing|setup|config(?:uration)?|fields?|routes?|"
    r"endpoints?|fixtures?|helpers?|decorators?|docstrings?|content|contents|sections?|"
    r"blocks?|steps?|statements?|cases?|body|module|file"
)
_ELISION_SUBJECT_WIDE = re.compile(
    r"\b(?:(?:rest|remainder) of (?:the |this |your )?"
    r"|(?:existing|previous|original|other|remaining) )"
    rf"(?:[\w'-]+ ){{0,3}}?(?:{_WIDE_NOUNS})\b",
    re.IGNORECASE,
)
_ELISION_MARKER_STRONG = re.compile(
    r"\.\.\.|\u2026|\bunchanged\b|\bremains? (?:the same|as is|as before|untouched)\b"
    r"|\bstays? (?:the same|as is|as before)\b|\bas (?:before|is)\b|\bomitted\b|\belided\b"
    r"|\bfor brevity\b|\bnot shown\b|\btruncated\b",
    re.IGNORECASE,
)


def is_elision(text: str) -> bool:
    """True if a comment stands in for code that is not there."""
    if _ELISION_SUBJECT.search(text) and _ELISION_MARKER.search(text):
        return True
    return bool(_ELISION_SUBJECT_WIDE.search(text) and _ELISION_MARKER_STRONG.search(text))


_STUB_PATTERNS = re.compile(
    r"(?:\b(?:your|actual|real) (?:code|logic|implementation) (?:goes )?here\b"
    r"|\b(?:code|logic|implementation) goes here\b"
    r"|\b(?:add|insert|put|implement|write) (?:your |the |actual |real )?"
    r"(?:code|logic|implementation)(?: \w+)? here\b"
    r"|\bTODO:?\s*implement(?: (?:this|me|logic|here))?\s*$)",
    re.IGNORECASE,
)
_BARE_ELLIPSIS = re.compile(r"^\s*(?:\.\.\.|…)\s*$")


def _comments(source: str) -> Iterator[tuple[int, int, str]]:
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                yield tok.start[0], tok.start[1], tok.string.lstrip("#").strip()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def _might_match(text: str) -> bool:
    return bool(
        _ELISION_SUBJECT.search(text)
        or _ELISION_SUBJECT_WIDE.search(text)
        or _STUB_PATTERNS.search(text)
        or _BARE_ELLIPSIS.match(text.lstrip("#"))
    )


def _neighbour_is_comment(ctx: FileContext, line: int) -> bool:
    for step in (-1, 1):
        n = line + step
        while 1 <= n <= len(ctx.lines) and not ctx.lines[n - 1].strip():
            n += step
        if ctx.line_text(n).lstrip().startswith("#"):
            return True
    return False


class PlaceholderCodeRule(Rule):
    code = "LX401"
    name = "placeholder-code"
    summary = "Comment that stands in for code: '... rest of the code unchanged', 'your code here'."
    explanation = (
        "When an agent rewrites a file it sometimes elides untouched parts with a comment such "
        "as `# ... existing code ...`. Saving that output silently deletes the real code. "
        "Stub markers like `# TODO: implement` are reported as warnings."
    )

    def check_file(self, ctx: FileContext) -> Iterator[Violation]:
        # Cheap prefilter on raw lines; tokenize (to rule out '#' inside strings)
        # only when some line could match.
        if not any("#" in line and _might_match(line[line.index("#") :]) for line in ctx.lines):
            return
        for line, col, text in _comments(ctx.source):
            elided = is_elision(text)
            if not elided and _BARE_ELLIPSIS.match(text):
                # A lone `# ...` between code lines; inside a comment block it is prose.
                elided = not _neighbour_is_comment(ctx, line)
            if elided:
                yield self.violation(
                    ctx,
                    line,
                    f"Placeholder comment {text!r} stands in for code that is not there.",
                    why="This looks like an elided edit: an LLM summarised existing code with a "
                    "comment instead of reproducing it, so saving the file deletes that code.",
                    fix="Restore the omitted code from the previous version of the file (e.g. "
                    "`git diff` / `git show HEAD:<path>`), then make only the intended change. "
                    "Never abbreviate unchanged code with comments.",
                    col=col,
                )
            elif _STUB_PATTERNS.search(text):
                yield self.violation(
                    ctx,
                    line,
                    f"Unimplemented stub marker {text!r}.",
                    why="The code around this comment was left unimplemented; it will silently "
                    "do nothing or return a placeholder value at runtime.",
                    fix="Implement the missing logic now, or raise `NotImplementedError` with a "
                    "message if it is genuinely out of scope.",
                    severity=Severity.WARNING,
                    col=col,
                )


_BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(e, ast.Name) and e.id in _BROAD for e in elts)


def _is_silent(body: list[ast.stmt]) -> bool:
    for stmt in body:
        if isinstance(stmt, (ast.Pass, ast.Continue)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # `...` or a string
        return False
    return True


class SwallowedExceptionRule(Rule):
    code = "LX402"
    name = "swallowed-exception"
    summary = "Broad `except` that silently discards the error (`pass`/`continue`/`...`)."
    explanation = (
        "`except Exception: pass` is a common agent shortcut for making an error disappear. It "
        "hides real bugs and leaves the program in an unknown state."
    )
    node_types = (ast.ExceptHandler,)

    def check_node(self, node: ast.ExceptHandler, ctx: FileContext) -> Iterator[Violation]:
        if _is_broad(node) and _is_silent(node.body):
            caught = ast.unparse(node.type) if node.type is not None else "everything"
            yield self.violation(
                ctx,
                node,
                f"Exception handler catches {caught} and silently discards it.",
                why="Silencing broad exceptions hides bugs (typos, None errors, bad data) and "
                "makes failures impossible to diagnose. Agents add this to make a traceback "
                "go away rather than fixing its cause.",
                fix="Catch only the specific exception you expect (e.g. `except "
                "FileNotFoundError:`), and handle it: log with `logger.exception(...)`, return a "
                "fallback, or re-raise. For an intentional ignore use "
                "`contextlib.suppress(SpecificError)`.",
            )


class BareExceptRule(Rule):
    code = "LX403"
    name = "bare-except"
    summary = "Bare `except:` clause."
    explanation = (
        "`except:` also catches `KeyboardInterrupt` and `SystemExit`, making programs hard to stop."
    )
    default_severity = Severity.WARNING
    node_types = (ast.ExceptHandler,)

    def check_node(self, node: ast.ExceptHandler, ctx: FileContext) -> Iterator[Violation]:
        if node.type is None and not _is_silent(node.body):  # silent ones are LX402
            yield self.violation(
                ctx,
                node,
                "Bare `except:` catches every exception, including KeyboardInterrupt and SystemExit.",
                why="Ctrl-C and `sys.exit()` get swallowed along with genuine bugs.",
                fix="Catch the specific exceptions you expect, or at most `except Exception:`. "
                "If you must catch everything (e.g. to clean up), re-raise with a bare `raise`.",
            )
