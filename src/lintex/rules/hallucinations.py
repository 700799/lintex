"""LX1xx: hallucinated imports and APIs.

LLMs are trained on years of code, much of it written against APIs that have
since been removed or renamed. These rules catch the confidently-wrong import
or attribute before it becomes an ``ImportError`` in CI.

What counts as "known bad" lives in the knowledge packs (``lintex.knowledge``):
a curated TOML file and a JSON file generated from typeshed.
"""

from __future__ import annotations

import ast
import difflib
import importlib
import importlib.util
import inspect
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

from lintex import knowledge
from lintex.knowledge import ApiEntry
from lintex.models import Severity, Violation
from lintex.rules.base import FileContext, Rule, is_compat_guarded

KB = knowledge.load()

_HALLUCINATION_WHY = (
    "LLMs learn from years of code written against older library versions, so they "
    "confidently produce imports and calls that no longer exist. This will fail at "
    "import or call time (or emit warnings and misbehave, if deprecated)."
)


def _imported_names(node: ast.Import | ast.ImportFrom) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif node.level == 0 and node.module:
        yield node.module
        for alias in node.names:
            if alias.name != "*":
                yield f"{node.module}.{alias.name}"


def _target(ctx: FileContext) -> tuple[int, int]:
    """The project's minimum Python, else the interpreter running lintex."""
    return ctx.config.target_version or sys.version_info[:2]


class KnownBadApiRule(Rule):
    code = "LX101"
    name = "hallucinated-api"
    summary = "Import or use of an API that was removed, deprecated, or never existed in Python 3."
    explanation = (
        "Flags APIs that coding agents commonly produce from stale training data: stdlib "
        "modules removed by PEP 594, `collections.Mapping`, `numpy.float`, the pre-1.0 "
        "`openai.ChatCompletion` interface, `pydantic.BaseSettings`, Python 2 methods like "
        "`dict.iteritems()`, and every stdlib removal and `@deprecated` name typeshed knows. "
        "A stdlib removal is an error when the project's minimum Python (from "
        "`requires-python` or `target-version`) is at or past the removing version, and a "
        "warning before that. Code inside `try/except ImportError` or `if sys.version_info` "
        "guards is ignored. Knowledge lives in `lintex/knowledge/`; see the README to add to it."
    )
    node_types = (ast.Import, ast.ImportFrom, ast.Attribute, ast.Call)

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seen: set[str] = set()
            for name in _imported_names(node):
                match = KB.match_prefix(name)
                if match and match[0] not in seen and not is_compat_guarded(node, ctx):
                    seen.add(match[0])
                    yield self._report(ctx, node, *match)
        elif isinstance(node, ast.Attribute):
            # Exact match only: every sub-chain of `a.b.c` is visited as its
            # own Attribute node, so prefixes are reported where they occur.
            resolved = ctx.resolve(node)
            if resolved is None:
                return
            qualified, binding_parts = resolved
            entry = KB.apis.get(qualified)
            # Skip the import binding itself; it was reported at the import.
            if (
                entry
                and qualified.count(".") + 1 > binding_parts
                and not is_compat_guarded(node, ctx)
            ):
                yield self._report(ctx, node, qualified, entry)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            entry = KB.methods.get(node.func.attr)
            if (
                entry is not None
                and node.func.attr not in ctx.defined_functions  # the file implements it
                and ctx.resolve(node.func.value) is None  # a module function, not a method
                and not is_compat_guarded(node, ctx)
            ):
                target = _target(ctx)
                yield self.violation(
                    ctx,
                    node.func,
                    f"`.{node.func.attr}()` {entry.describe(target)}.",
                    why=_HALLUCINATION_WHY,
                    fix=entry.fix,
                    severity=entry.severity(target),
                )

    def _report(self, ctx: FileContext, node: ast.AST, name: str, entry: ApiEntry) -> Violation:
        target = _target(ctx)
        fix = entry.fix
        if entry.source == "typeshed" and entry.status == "deprecated":
            fix = f"typeshed says: {fix}"
        return self.violation(
            ctx,
            node,
            f"`{name}` {entry.describe(target)}.",
            why=_HALLUCINATION_WHY,
            fix=fix,
            severity=entry.severity(target),
        )


# -- LX102: names that do not exist in the stdlib ----------------------------

_STDLIB: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", ()))
# Modules with import-time side effects (opening browsers, GUIs, printing).
_UNSAFE_TO_IMPORT = frozenset(
    {"antigravity", "this", "__hello__", "__phello__", "tkinter", "turtle", "turtledemo", "idlelib"}
)
# Attributes that only exist once the runtime sets them.
_DYNAMIC_ATTRS = {
    "sys": {
        "ps1",
        "ps2",
        "last_type",
        "last_value",
        "last_traceback",
        "last_exc",
        "tracebacklimit",
        "frozen",
        "pycache_prefix",
        "winver",
        "dllhandle",
        "getwindowsversion",
        "gettotalrefcount",
        "getobjects",
        "pypy_version_info",
    },
    "curses": {"LINES", "COLS", "COLORS", "COLOR_PAIRS"},  # set by initscr()/start_color()
}
_DYNAMIC_PREFIXES = {"curses": ("ACS_",)}
# Modules whose contents differ by OS; a miss there may just be another platform's name.
_PLATFORM_DEPENDENT = frozenset(
    {
        "os",
        "posix",
        "nt",
        "signal",
        "errno",
        "socket",
        "select",
        "subprocess",
        "stat",
        "time",
        "mmap",
        "codecs",
        "ctypes",
        "curses",
        "fcntl",
        "termios",
        "resource",
        "msvcrt",
        "winreg",
        "tempfile",
        "shutil",
        "asyncio",
        "multiprocessing",
        "ssl",
        "locale",
        "sysconfig",
    }
)
_MISSING = object()
_module_cache: dict[str, ModuleType | None] = {}


def _load_stdlib(name: str) -> ModuleType | None:
    """Import a stdlib module, or return None if that is not possible/safe."""
    if name in _module_cache:
        return _module_cache[name]
    root = name.partition(".")[0]
    module = None
    if root in _STDLIB and root not in _UNSAFE_TO_IMPORT and root not in KB.apis:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                module = importlib.import_module(name)
            except Exception:
                module = None
    _module_cache[name] = module
    return module


def _submodule_exists(module: ModuleType, name: str) -> bool:
    if not hasattr(module, "__path__"):
        return False
    try:
        return importlib.util.find_spec(f"{module.__name__}.{name}") is not None
    except (ImportError, ValueError):
        return False


def _lookup(obj: Any, name: str) -> Any:
    value = getattr(obj, name, _MISSING)
    if value is _MISSING and isinstance(obj, ModuleType) and _submodule_exists(obj, name):
        return _load_stdlib(f"{obj.__name__}.{name}") or _MISSING
    return value


def _public_names(obj: Any) -> list[str]:
    return [n for n in dir(obj) if not n.startswith("_")]


class NonexistentStdlibNameRule(Rule):
    code = "LX102"
    name = "nonexistent-stdlib-name"
    summary = "Import or attribute access of a name that does not exist in a stdlib module."
    explanation = (
        "Imports stdlib modules (never third-party code) in the linting interpreter and checks "
        "that every `from X import Y` and `X.Y` reference actually exists, suggesting the "
        "closest real name. Catches invented helpers like `os.path.joinpath` or "
        "`from typing import Json`. Results reflect the Python version running lintex. "
        "Requires Python 3.10+ to run (it is a no-op on 3.9)."
    )
    node_types = (ast.ImportFrom, ast.Import, ast.Attribute)

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        if not _STDLIB:
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield from self._check_module_path(ctx, node, alias.name)
        elif isinstance(node, ast.ImportFrom):
            yield from self._check_from_import(ctx, node)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            yield from self._check_attribute(ctx, node)

    def _check_module_path(
        self, ctx: FileContext, node: ast.AST, dotted: str
    ) -> Iterator[Violation]:
        parts = dotted.split(".")
        parent = _load_stdlib(parts[0])
        if parent is None:
            return
        for i, part in enumerate(parts[1:], start=1):
            child_name = ".".join(parts[: i + 1])
            if not _submodule_exists(parent, part) and not hasattr(parent, part):
                if not is_compat_guarded(node, ctx):
                    yield from self._missing(ctx, node, part, parent, ".".join(parts[:i]))
                return
            child = _load_stdlib(child_name)
            if child is None:  # exists but not importable here (platform-specific)
                return
            parent = child

    def _check_from_import(self, ctx: FileContext, node: ast.ImportFrom) -> Iterator[Violation]:
        if node.level != 0 or not node.module:
            return
        problems = list(self._check_module_path(ctx, node, node.module))
        if problems:
            yield from problems
            return
        module = _load_stdlib(node.module)
        if module is None:
            return
        for alias in node.names:
            if alias.name != "*" and _lookup(module, alias.name) is _MISSING:
                if not is_compat_guarded(node, ctx):
                    yield from self._missing(ctx, node, alias.name, module, node.module)

    def _check_attribute(self, ctx: FileContext, node: ast.Attribute) -> Iterator[Violation]:
        resolved = ctx.resolve(node)
        if resolved is None:
            return
        qualified, binding_parts = resolved
        parts = qualified.split(".")
        obj: Any = _load_stdlib(parts[0])
        if obj is None:
            return
        for i, part in enumerate(parts[1:], start=1):
            if not (isinstance(obj, ModuleType) or inspect.isclass(obj)):
                return  # instances and functions are too dynamic to judge
            value = _lookup(obj, part)
            if value is _MISSING:
                # Only report at the node whose last component is missing,
                # and never for components that came from the import itself.
                if i == len(parts) - 1 and i >= binding_parts and not is_compat_guarded(node, ctx):
                    yield from self._missing(ctx, node, part, obj, ".".join(parts[:i]))
                return
            obj = value

    def _missing(
        self, ctx: FileContext, node: ast.AST, name: str, owner: Any, owner_name: str
    ) -> Iterator[Violation]:
        if KB.match_prefix(f"{owner_name}.{name}"):
            return  # LX101 already explains this one, with a better fix
        if (
            name.startswith("_")  # private/internal names are not what agents invent
            or owner_name.startswith("_")
            or name in _DYNAMIC_ATTRS.get(owner_name, ())
            or name.startswith(_DYNAMIC_PREFIXES.get(owner_name, ()))
        ):
            return
        platform_note = ""
        severity = None
        if owner_name.partition(".")[0] in _PLATFORM_DEPENDENT:
            severity = Severity.WARNING
            platform_note = (
                f" If `{name}` exists only on another platform (e.g. Windows), guard the code "
                'with `if sys.platform == "win32":` or `hasattr()`.'
            )
        close = difflib.get_close_matches(name, _public_names(owner), n=3, cutoff=0.6)
        if close:
            fix = "Did you mean " + " or ".join(f"`{owner_name}.{c}`" for c in close) + "?"
        else:
            fix = (
                f"Check the documentation for `{owner_name}`; the name you need may live in "
                "another module, or you may need to implement it yourself."
            )
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        yield self.violation(
            ctx,
            node,
            f"`{owner_name}` has no attribute or submodule `{name}` (checked against Python {version}).",
            why="This name looks plausible but does not exist; it will raise "
            "ImportError/AttributeError at runtime. Invented helper names are a classic "
            "LLM hallucination.",
            fix=fix + platform_note,
            severity=severity,
        )


# -- LX103: imports that cannot be resolved at all (opt-in) -----------------


class UnresolvableImportRule(Rule):
    code = "LX103"
    name = "unresolvable-import"
    summary = "Top-level import that is not installed, not stdlib and not a local module."
    explanation = (
        "Opt-in (add `LX103` to `enable`). Resolves each absolute import with "
        "`importlib.util.find_spec` in the interpreter running lintex, and against local "
        "modules in the project root, `src/`, and the linted file's directory. Run lintex "
        "inside the project's virtualenv for accurate results. Use the `known-modules` option "
        "to allow-list modules that are provided at runtime."
    )
    default_enabled = False
    node_types = (ast.Import, ast.ImportFrom)
    options = {"known-modules": []}

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                return
            roots = [node.module.partition(".")[0]]
        else:
            roots = [a.name.partition(".")[0] for a in node.names]
        known = set(self.opts["known-modules"])
        for root in dict.fromkeys(roots):
            if root in known or root in _STDLIB or root in KB.apis or root == "__future__":
                continue
            if self._is_local(root, ctx) or self._is_installed(root):
                continue
            if is_compat_guarded(node, ctx):
                continue
            close = difflib.get_close_matches(root, list(_STDLIB), n=1, cutoff=0.8)
            hint = f" Did you mean the stdlib module `{close[0]}`?" if close else ""
            yield self.violation(
                ctx,
                node,
                f"Cannot resolve import `{root}`: it is not installed, not in the standard "
                "library and not a module in this project.",
                why="Agents sometimes invent package names that sound right, or use a "
                "dependency the project does not declare. Either way the import fails at runtime.",
                fix="Use a module that exists, or add the real distribution to the project's "
                f"dependencies and install it. If it is provided at runtime, add `{root}` to "
                f"`known-modules` under [tool.lintex.rules.unresolvable-import].{hint}",
            )

    @staticmethod
    def _is_installed(root: str) -> bool:
        try:
            return importlib.util.find_spec(root) is not None
        except (ImportError, ValueError):
            return False

    @staticmethod
    def _is_local(root: str, ctx: FileContext) -> bool:
        project_root = ctx.config.root
        file_dir = Path(ctx.path).resolve().parent
        candidates = [project_root, project_root / "src", file_dir]
        candidates += [p for p in file_dir.parents if project_root in (p, *p.parents)]
        for base in candidates:
            if (base / f"{root}.py").exists() or (base / root).is_dir():
                return True
        return False
