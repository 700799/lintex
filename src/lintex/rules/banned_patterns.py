"""LX2xx: banned and insecure patterns.

Covers SQL built by string interpolation, raw SQL (when a project forbids it),
dangerous calls such as ``eval`` / ``pickle.loads`` / ``shell=True``, insecure
file operations, and project-specific banned APIs from configuration.
"""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Iterator
from typing import Any

from lintex.models import Severity, Violation
from lintex.rules.base import FileContext, Rule, call_kwarg, is_const_false, is_const_true

_SQL_RE = re.compile(
    r"""^[\s(]*(?:
        select\s.+?\sfrom\s
      | insert\s+(?:or\s+\w+\s+)?into\s
      | update\s+[\w."`\[\]]+\s+set\s
      | delete\s+from\s
      | replace\s+into\s
      | merge\s+into\s
      | (?:drop|truncate)\s+table\s
      | (?:create|alter)\s+(?:unique\s+)?(?:table|index|view|database)\s
      | with\s+\w+\s+as\s*\(
    )""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_PLACEHOLDER = "?"
# Interpolating a pre-built list of bind placeholders ("?, ?, ?") is the
# standard way to parameterise IN (...) clauses, so don't flag it.
_SAFE_INTERPOLATION_RE = re.compile(r"placeholder|qmarks|bind|marks", re.IGNORECASE)


def looks_like_sql(text: str) -> bool:
    return bool(_SQL_RE.search(text))


def _template_text(node: ast.AST) -> tuple[str, list[ast.expr]] | None:
    """Return (literal text with ``?`` for holes, interpolated exprs) for string-building expressions."""
    if isinstance(node, ast.JoinedStr):
        text, holes = [], []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                text.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                text.append(_PLACEHOLDER)
                holes.append(part.value)
        return ("".join(text), holes) if holes else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        holes = list(node.args) + [kw.value for kw in node.keywords]
        return (
            (re.sub(r"\{[^{}]*\}", _PLACEHOLDER, node.func.value.value), holes) if holes else None
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            if isinstance(node.right, ast.Constant):
                return None
            text = re.sub(r"%(\([^)]*\))?[sdifr]", _PLACEHOLDER, node.left.value)
            return text, [node.right]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        pieces: list[ast.expr] = []
        stack: list[ast.expr] = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
                stack.extend([cur.right, cur.left])
            else:
                pieces.append(cur)
        text, holes = [], []
        for piece in pieces:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                text.append(piece.value)
            else:
                text.append(_PLACEHOLDER)
                holes.append(piece)
        if holes and len(holes) < len(pieces):
            return "".join(text), holes
    return None


class SqlInterpolationRule(Rule):
    code = "LX201"
    name = "sql-string-interpolation"
    summary = "SQL statement assembled with an f-string, .format(), % or + concatenation."
    explanation = (
        "Detects SQL text (SELECT ... FROM, INSERT INTO, UPDATE ... SET, DELETE FROM, DDL) "
        "built by interpolating values into a string. This is the classic SQL injection "
        "vector and one of the most common security bugs in generated code. Interpolating a "
        "variable whose name contains `placeholder`/`bind`/`marks` (an IN-clause bind list) "
        "is allowed."
    )
    node_types = (ast.JoinedStr, ast.Call, ast.BinOp)

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        parent = ctx.parent(node)
        if isinstance(node, ast.JoinedStr) and isinstance(
            parent, (ast.FormattedValue, ast.JoinedStr)
        ):
            return
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(parent, ast.BinOp)
            and isinstance(parent.op, ast.Add)
        ):
            return  # only report the outermost `a + b + c`
        template = _template_text(node)
        if template is None:
            return
        text, holes = template
        if not looks_like_sql(text):
            return
        if all(_SAFE_INTERPOLATION_RE.search(ast.unparse(h)) for h in holes):
            return
        kind = {
            ast.JoinedStr: "an f-string",
            ast.Call: "str.format()",
        }.get(
            type(node),
            "% formatting"
            if isinstance(getattr(node, "op", None), ast.Mod)
            else "string concatenation",
        )
        yield self.violation(
            ctx,
            node,
            f"SQL statement built with {kind}; interpolated values are not escaped.",
            why="Interpolating values into SQL text allows SQL injection whenever any value "
            "is influenced by user input, and breaks on quotes in ordinary data.",
            fix="Use bound parameters and pass values separately, e.g. "
            '`cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))` '
            "(placeholder style depends on the driver: `?` for sqlite3, `%s` for "
            "psycopg/MySQLdb, `:name` with SQLAlchemy `text()`). Identifiers such as table "
            "or column names cannot be bound - check them against an explicit allow-list.",
        )


class RawSqlRule(Rule):
    code = "LX202"
    name = "raw-sql"
    summary = "Raw SQL passed to execute()/text() in a project that forbids it."
    explanation = (
        "Opt-in (add `LX202` to `enable`). For projects that require all database access to "
        "go through an ORM or query builder, flags string SQL passed to `.execute()`, "
        "`.executemany()`, `.executescript()`, `.raw()` or `sqlalchemy.text()`. Set the "
        "`hint` option to tell the agent which data-access layer to use instead."
    )
    default_enabled = False
    node_types = (ast.Call,)
    options = {
        "hint": "Use the project's ORM / query builder instead of hand-written SQL.",
        "methods": ["execute", "executemany", "executescript", "raw"],
    }

    def check_node(self, node: ast.Call, ctx: FileContext) -> Iterator[Violation]:
        if not node.args:
            return
        func = node.func
        is_text = (ctx.resolve(func) or ("",))[0] == "sqlalchemy.text"
        is_method = isinstance(func, ast.Attribute) and func.attr in self.opts["methods"]
        if not (is_text or is_method):
            return
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            text = arg.value
        else:
            template = _template_text(arg)
            if template is None:
                return
            text = template[0]
        if not looks_like_sql(text):
            return
        yield self.violation(
            ctx,
            node,
            "Raw SQL is forbidden in this project.",
            why="The project configuration enables LX202: database access must go through "
            "the shared data-access layer so queries stay consistent, typed and reviewable.",
            fix=self.opts["hint"],
        )


# -- LX203: dangerous calls -----------------------------------------------

_UNTRUSTED_DESERIALIZE_FIX = (
    "Use a data-only format such as `json` for anything that crosses a trust boundary. "
    "Only unpickle data your own process wrote and authenticated (e.g. with `hmac`)."
)

_DANGEROUS_CALLS: dict[str, tuple[str, str]] = {
    "builtins.eval": (
        "`eval()` executes arbitrary code.",
        "Use `ast.literal_eval` for Python literals, `json.loads` for data, or an explicit "
        "dispatch dict / small parser for expressions.",
    ),
    "builtins.exec": (
        "`exec()` executes arbitrary code.",
        "Call the code directly, use a dispatch dict of functions, or `importlib` to load "
        "modules by name.",
    ),
    "pickle.load": (
        "`pickle.load()` can execute arbitrary code from its input.",
        _UNTRUSTED_DESERIALIZE_FIX,
    ),
    "pickle.loads": (
        "`pickle.loads()` can execute arbitrary code from its input.",
        _UNTRUSTED_DESERIALIZE_FIX,
    ),
    "dill.load": (
        "`dill.load()` can execute arbitrary code from its input.",
        _UNTRUSTED_DESERIALIZE_FIX,
    ),
    "dill.loads": (
        "`dill.loads()` can execute arbitrary code from its input.",
        _UNTRUSTED_DESERIALIZE_FIX,
    ),
    "marshal.load": ("`marshal.load()` is unsafe on untrusted input.", _UNTRUSTED_DESERIALIZE_FIX),
    "marshal.loads": (
        "`marshal.loads()` is unsafe on untrusted input.",
        _UNTRUSTED_DESERIALIZE_FIX,
    ),
    "yaml.unsafe_load": (
        "`yaml.unsafe_load()` can construct arbitrary Python objects.",
        "Use `yaml.safe_load()`.",
    ),
    "os.system": (
        "`os.system()` runs its argument through the shell.",
        'Use `subprocess.run(["cmd", arg1, arg2], check=True)` with an argument list.',
    ),
    "os.popen": (
        "`os.popen()` runs its argument through the shell.",
        'Use `subprocess.run(["cmd", arg], capture_output=True, text=True, check=True)`.',
    ),
    "ssl._create_unverified_context": (
        "`ssl._create_unverified_context()` disables TLS certificate verification.",
        "Use `ssl.create_default_context()`; to trust a private CA pass `cafile=`.",
    ),
}

_HTTP_CLIENT_ROOTS = ("requests", "httpx", "aiohttp", "urllib3")


class DangerousCallRule(Rule):
    code = "LX203"
    name = "dangerous-call"
    summary = "eval/exec, unsafe deserialization, shell=True, or disabled TLS verification."
    explanation = (
        "Flags calls that turn data into code or disable a security control: `eval`, `exec`, "
        "`pickle`/`marshal`/`dill` loads, `yaml.load` without a safe Loader, `os.system`, "
        "`subprocess.*(shell=True)`, and `verify=False` on HTTP clients."
    )
    node_types = (ast.Call,)

    def check_node(self, node: ast.Call, ctx: FileContext) -> Iterator[Violation]:
        resolved = ctx.resolve(node.func)
        if resolved is None:
            return
        name = resolved[0]
        known = _DANGEROUS_CALLS.get(name)
        if known is not None:
            message, fix = known
            yield self.violation(ctx, node, message, why=self._why(name), fix=fix)
            return

        if name == "yaml.load":
            loader = call_kwarg(node, "Loader")
            if loader is None and len(node.args) > 1:
                loader = node.args[1]
            if loader is None or "Safe" not in ast.unparse(loader):
                yield self.violation(
                    ctx,
                    node,
                    "`yaml.load()` without a safe Loader can construct arbitrary Python objects.",
                    why="PyYAML's full loaders instantiate any Python class named in the "
                    "document, which allows code execution on untrusted input.",
                    fix="Use `yaml.safe_load(stream)` (or `Loader=yaml.SafeLoader`).",
                )
        elif name.startswith("subprocess.") and is_const_true(call_kwarg(node, "shell")):
            literal = bool(node.args) and isinstance(node.args[0], ast.Constant)
            yield self.violation(
                ctx,
                node,
                f"`{name}(..., shell=True)` passes the command through the shell.",
                why="With `shell=True`, any interpolated value can inject extra shell commands, "
                "and quoting/escaping bugs are easy to introduce.",
                fix='Pass an argument list and drop `shell=True`: `subprocess.run(["git", '
                '"log", path], check=True)`. Use `shlex.split` to convert a fixed command string.',
                severity=Severity.WARNING if literal else Severity.ERROR,
            )
        elif name.partition(".")[0] in _HTTP_CLIENT_ROOTS and is_const_false(
            call_kwarg(node, "verify")
        ):
            yield self.violation(
                ctx,
                node,
                f"`{name}(..., verify=False)` disables TLS certificate verification.",
                why="Without certificate verification any network attacker can intercept or "
                "modify the traffic. Agents often add this to 'fix' an SSL error.",
                fix='Remove `verify=False`. For a private CA, pass `verify="/path/to/ca.pem"` '
                "or set `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`.",
            )

    @staticmethod
    def _why(name: str) -> str:
        if name in ("builtins.eval", "builtins.exec"):
            return (
                "If any part of the input is influenced by a user, file or network, this is "
                "remote code execution. It also hides logic from linters and type checkers."
            )
        if name.startswith(("os.", "ssl.")):
            return (
                "This bypasses a safer standard API and is a common source of injection "
                "and man-in-the-middle vulnerabilities."
            )
        return "Deserialising untrusted bytes with this module can run attacker-chosen code."


# -- LX204: insecure file operations --------------------------------------

_MODE_FUNCS = {"os.chmod": 1, "os.fchmod": 1, "os.lchmod": 1, "os.mkdir": 1, "os.makedirs": 1}


def _const_int(node: ast.AST | None) -> int | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    return None


class InsecureFileOperationRule(Rule):
    code = "LX204"
    name = "insecure-file-operation"
    summary = "Race-prone temp files, world-writable permissions, unsafe archive extraction."
    explanation = (
        "Flags `tempfile.mktemp`, hard-coded `/tmp/...` paths, permission modes that make "
        "files world-writable, `os.umask(0)`, and `tarfile` `extractall()` without a `filter=`."
    )
    node_types = (ast.Call,)

    def check_node(self, node: ast.Call, ctx: FileContext) -> Iterator[Violation]:
        resolved = ctx.resolve(node.func)
        name = resolved[0] if resolved else ""
        for check in (
            self._mktemp,
            self._world_writable,
            self._umask,
            self._tar_extract,
            self._hardcoded_tmp,
        ):
            violation = check(node, name, ctx)
            if violation is not None:
                yield violation
                return

    def _mktemp(self, node: ast.Call, name: str, ctx: FileContext) -> Violation | None:
        if name != "tempfile.mktemp":
            return None
        return self.violation(
            ctx,
            node,
            "`tempfile.mktemp()` is insecure and deprecated.",
            why="It returns a name without creating the file, so another process can "
            "create (or symlink) that path first.",
            fix="Use `tempfile.NamedTemporaryFile()`, `tempfile.mkstemp()` or "
            "`tempfile.TemporaryDirectory()`.",
        )

    def _world_writable(self, node: ast.Call, name: str, ctx: FileContext) -> Violation | None:
        mode = None
        if name in _MODE_FUNCS:
            mode = call_kwarg(node, "mode")
            if mode is None and len(node.args) > _MODE_FUNCS[name]:
                mode = node.args[_MODE_FUNCS[name]]
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "chmod"
            and not name.startswith("os.")
        ):
            mode = call_kwarg(node, "mode") or (node.args[0] if node.args else None)
        value = _const_int(mode)
        if value is None or not value & 0o002:
            return None
        return self.violation(
            ctx,
            node,
            f"Permission mode {oct(value)} makes the path world-writable.",
            why="Any local user or compromised process could modify or replace the file.",
            fix="Use the narrowest mode that works: `0o600` for private files, `0o644` for "
            "readable files, `0o700`/`0o755` for directories.",
        )

    def _umask(self, node: ast.Call, name: str, ctx: FileContext) -> Violation | None:
        if name != "os.umask" or _const_int(node.args[0] if node.args else None) != 0:
            return None
        return self.violation(
            ctx,
            node,
            "`os.umask(0)` makes every file created afterwards world-writable.",
            why="The umask applies process-wide, affecting files created by all libraries.",
            fix="Set explicit modes on the files you create instead (e.g. `os.open(path, "
            "flags, 0o600)`), or use a restrictive umask such as `0o077`.",
            severity=Severity.WARNING,
        )

    def _tar_extract(self, node: ast.Call, name: str, ctx: FileContext) -> Violation | None:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("extractall", "extract")
            and call_kwarg(node, "filter") is None
            and "tar" in ast.unparse(node.func.value).lower()
        ):
            return None
        return self.violation(
            ctx,
            node,
            f"`{node.func.attr}()` on a tar archive without `filter=`.",
            why="Tar members can contain absolute paths, `..` components or links that "
            "write outside the destination directory (CVE-2007-4559).",
            fix='Pass `filter="data"`: `tar.extractall(dest, filter="data")` '
            "(Python 3.12+, backported to 3.8.17+/3.9.17+/3.10.12+/3.11.4+).",
        )

    def _hardcoded_tmp(self, node: ast.Call, name: str, ctx: FileContext) -> Violation | None:
        if name not in ("builtins.open", "pathlib.Path", "io.open") or not node.args:
            return None
        first = node.args[0]
        if not (
            isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and first.value.startswith(("/tmp/", "/var/tmp/"))
        ):
            return None
        return self.violation(
            ctx,
            node,
            f"Hard-coded temporary path {first.value!r}.",
            why="Predictable paths in shared temp directories allow symlink attacks and "
            "collide when several processes run at once.",
            fix="Use `tempfile.NamedTemporaryFile()` / `tempfile.mkdtemp()` or "
            "`tempfile.gettempdir()` with a unique name.",
            severity=Severity.WARNING,
        )


# -- LX205: project-specific banned APIs ----------------------------------


class ProjectBannedApiRule(Rule):
    code = "LX205"
    name = "banned-api"
    summary = "Use of a module or API that the project configuration bans."
    explanation = (
        "Configure with a mapping of dotted names to the message the agent should see:\n\n"
        "    [tool.lintex.rules.banned-api]\n"
        '    apis = { "requests" = "Use app.http.client (retries, timeouts, tracing).",\n'
        '             "print" = "Use the structured logger: log = structlog.get_logger()" }\n\n'
        "A banned module also bans everything inside it. Builtins can be given bare (`print`)."
    )
    node_types = (ast.Import, ast.ImportFrom, ast.Attribute, ast.Name)
    options = {"apis": {}}

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        apis = self.opts["apis"]
        if not isinstance(apis, dict):
            from lintex.config import ConfigError

            raise ConfigError("rules.banned-api.apis must map dotted names to messages")
        self.banned: dict[str, str] = {}
        for key, msg in apis.items():
            key = str(key)
            if "." not in key and hasattr(builtins, key):
                key = f"builtins.{key}"
            self.banned[key] = str(msg)

    def _match(self, qualified: str, prefix: bool) -> tuple[str, str] | None:
        if qualified in self.banned:
            return qualified, self.banned[qualified]
        if prefix:
            parts = qualified.split(".")
            for i in range(len(parts) - 1, 0, -1):
                key = ".".join(parts[:i])
                if key in self.banned:
                    return key, self.banned[key]
        return None

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        if not self.banned:
            return
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names if a.name != "*"]
        elif isinstance(node, ast.Name):
            if node.id in ctx.imports:
                return  # the import itself was reported
            names = [f"builtins.{node.id}"] if isinstance(node.ctx, ast.Load) else []
        else:
            resolved = ctx.resolve(node)
            names = [resolved[0]] if resolved else []
        for name in names:
            match = self._match(name, prefix=isinstance(node, (ast.Import, ast.ImportFrom)))
            if match:
                banned, message = match
                shown = banned.removeprefix("builtins.")
                yield self.violation(
                    ctx,
                    node,
                    f"`{shown}` is banned in this project.",
                    why="The project configuration bans this API (rule LX205).",
                    fix=message,
                )
                return
