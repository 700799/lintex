"""LX1xx: hallucinated imports and APIs.

LLMs are trained on years of code, much of it written against APIs that have
since been removed or renamed. These rules catch the confidently-wrong import
or attribute before it becomes an ``ImportError`` in CI.
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
from typing import Any, NamedTuple

from lintex.models import Severity, Violation
from lintex.rules.base import FileContext, Rule, is_compat_guarded

# -- LX101 knowledge base --------------------------------------------------


class ApiNote(NamedTuple):
    message: str
    fix: str
    severity: Severity = Severity.ERROR


def _removed(version: str, fix: str) -> ApiNote:
    return ApiNote(f"was removed in Python {version}", fix)


def _py2(fix: str) -> ApiNote:
    return ApiNote("is Python 2 only and does not exist in Python 3", fix)


def _gone(where: str, fix: str) -> ApiNote:
    return ApiNote(f"was removed in {where}", fix)


def _deprecated(where: str, fix: str) -> ApiNote:
    return ApiNote(f"is deprecated ({where})", fix, Severity.WARNING)


_COLLECTIONS_ABCS = (
    "Awaitable Coroutine AsyncIterable AsyncIterator AsyncGenerator Hashable Iterable "
    "Iterator Generator Reversible Sized Container Callable Collection Set MutableSet "
    "Mapping MutableMapping MappingView KeysView ItemsView ValuesView Sequence "
    "MutableSequence ByteString"
).split()

_NUMPY_ALIASES = {
    "float": "the builtin `float` (or `numpy.float64`)",
    "int": "the builtin `int` (or `numpy.int64`)",
    "bool": "the builtin `bool` (or `numpy.bool_`)",
    "object": "the builtin `object`",
    "str": "the builtin `str`",
    "long": "the builtin `int`",
    "unicode": "the builtin `str`",
    "complex": "the builtin `complex`",
    "float_": "`numpy.float64`",
    "complex_": "`numpy.complex128`",
    "string_": "`numpy.bytes_`",
    "unicode_": "`numpy.str_`",
    "NaN": "`numpy.nan`",
    "NAN": "`numpy.nan`",
    "Inf": "`numpy.inf`",
    "Infinity": "`numpy.inf`",
    "PINF": "`numpy.inf`",
    "NINF": "`-numpy.inf`",
    "product": "`numpy.prod`",
    "cumproduct": "`numpy.cumprod`",
    "alltrue": "`numpy.all`",
    "sometrue": "`numpy.any`",
    "round_": "`numpy.round`",
    "asfarray": "`numpy.asarray(x, dtype=float)`",
}

KNOWN_BAD_APIS: dict[str, ApiNote] = {
    # Standard library modules removed by PEP 594 / PEP 632 and friends.
    "distutils": _removed(
        "3.12", "Use `setuptools`, `packaging.version`, `shutil.which` or `sysconfig` instead."
    ),
    "imp": _removed("3.12", "Use `importlib` (`importlib.import_module`, `importlib.util`)."),
    "asynchat": _removed("3.12", "Use `asyncio` streams."),
    "asyncore": _removed("3.12", "Use `asyncio`."),
    "smtpd": _removed("3.12", "Use the `aiosmtpd` package."),
    "cgi": _removed(
        "3.13",
        "Use `urllib.parse.parse_qs` for query strings, `email.message` for multipart "
        "bodies and `html.escape` for escaping - or a web framework.",
    ),
    "cgitb": _removed("3.13", "Use `traceback` or `faulthandler`."),
    "crypt": _removed("3.13", "Use `hashlib.scrypt` or the `bcrypt` / `argon2-cffi` packages."),
    "telnetlib": _removed("3.13", "Use the `telnetlib3` package or `asyncio` streams."),
    "pipes": _removed("3.13", "Use `shlex.quote` together with `subprocess`."),
    "imghdr": _removed("3.13", "Use the `filetype` package or `PIL.Image.open(...).format`."),
    "sndhdr": _removed("3.13", "Use the `filetype` package."),
    "nntplib": _removed("3.13", "Use a third-party NNTP client."),
    "audioop": _removed("3.13", "Use `numpy` or the `audioop-lts` package."),
    "uu": _removed("3.13", "Use `base64`."),
    "xdrlib": _removed("3.13", "Use `struct`."),
    "msilib": _removed("3.13", "Use a third-party MSI tool."),
    "nis": _removed("3.13", "Use a third-party NIS client."),
    "spwd": _removed("3.13", "Use the `python-pam` package."),
    "ossaudiodev": _removed("3.13", "Use a third-party audio library."),
    "aifc": _removed("3.13", "Use the `soundfile` package."),
    "sunau": _removed("3.13", "Use the `soundfile` package."),
    "chunk": _removed("3.13", "Parse the chunks with `struct`."),
    "mailcap": _removed("3.13", "Use `mimetypes`."),
    # Standard library names.
    "time.clock": _removed("3.8", "Use `time.perf_counter()` or `time.process_time()`."),
    "os.errno": _removed("3.7", "Use `import errno`."),
    "inspect.getargspec": _removed(
        "3.11", "Use `inspect.signature()` or `inspect.getfullargspec()`."
    ),
    "asyncio.coroutine": _removed("3.11", "Use an `async def` function."),
    "fractions.gcd": _removed("3.9", "Use `math.gcd`."),
    "base64.encodestring": _removed("3.9", "Use `base64.encodebytes`."),
    "base64.decodestring": _removed("3.9", "Use `base64.decodebytes`."),
    "xml.etree.cElementTree": _removed("3.9", "Use `xml.etree.ElementTree`."),
    "configparser.SafeConfigParser": _removed("3.12", "Use `configparser.ConfigParser`."),
    "ssl.wrap_socket": _removed("3.12", "Use `ssl.create_default_context().wrap_socket(...)`."),
    "locale.format": _removed("3.12", "Use `locale.format_string`."),
    "unittest.makeSuite": _removed(
        "3.13", "Use `unittest.TestLoader().loadTestsFromTestCase(...)`."
    ),
    "platform.linux_distribution": _removed(
        "3.8", "Use `platform.freedesktop_os_release()` or the `distro` package."
    ),
    "platform.dist": _removed("3.8", "Use `platform.freedesktop_os_release()` or `distro`."),
    "datetime.datetime.utcnow": _deprecated(
        "Python 3.12; returns a naive datetime",
        "Use `datetime.now(timezone.utc)` (`from datetime import datetime, timezone`).",
    ),
    "datetime.datetime.utcfromtimestamp": _deprecated(
        "Python 3.12; returns a naive datetime",
        "Use `datetime.fromtimestamp(ts, tz=timezone.utc)`.",
    ),
    "asyncio.get_event_loop": _deprecated(
        "Python 3.10-3.12 when no loop is running",
        "Use `asyncio.run(main())` at the top level, or `asyncio.get_running_loop()` "
        "inside a coroutine.",
    ),
    "logging.warn": _deprecated("since Python 3.3", "Use `logging.warning`."),
    "pkg_resources": _deprecated(
        "setuptools; slow to import and scheduled for removal",
        "Use `importlib.metadata` (versions, entry points) or `importlib.resources` (data files).",
    ),
    # Python 2 holdovers.
    "sys.maxint": _py2("Use `sys.maxsize`."),
    "os.getcwdu": _py2("Use `os.getcwd()`."),
    "string.letters": _py2("Use `string.ascii_letters`."),
    "string.lowercase": _py2("Use `string.ascii_lowercase`."),
    "string.uppercase": _py2("Use `string.ascii_uppercase`."),
    "string.maketrans": _py2("Use `str.maketrans`."),
    "itertools.izip": _py2("Use the builtin `zip`."),
    "itertools.imap": _py2("Use the builtin `map`."),
    "itertools.ifilter": _py2("Use the builtin `filter`."),
    "itertools.izip_longest": _py2("Use `itertools.zip_longest`."),
    "urllib.urlencode": _py2("Use `urllib.parse.urlencode`."),
    "urllib.quote": _py2("Use `urllib.parse.quote`."),
    "urllib.unquote": _py2("Use `urllib.parse.unquote`."),
    "urllib.urlopen": _py2("Use `urllib.request.urlopen` (or `requests`/`httpx`)."),
    "urllib2": _py2("Use `urllib.request` / `urllib.error` (or `requests`/`httpx`)."),
    "urlparse": _py2("Use `urllib.parse`."),
    "ConfigParser": _py2("Use `configparser`."),
    "Queue": _py2("Use `queue`."),
    "StringIO": _py2("Use `io.StringIO`."),
    "cStringIO": _py2("Use `io.StringIO` / `io.BytesIO`."),
    "cPickle": _py2("Use `pickle`."),
    "HTMLParser": _py2("Use `html.parser`."),
    "httplib": _py2("Use `http.client`."),
    "cookielib": _py2("Use `http.cookiejar`."),
    "SocketServer": _py2("Use `socketserver`."),
    "Tkinter": _py2("Use `tkinter`."),
    "__builtin__": _py2("Use `builtins`."),
    "commands": _py2("Use `subprocess.run(..., capture_output=True, text=True)`."),
    # Third-party APIs that moved or vanished.
    "sklearn.cross_validation": _gone("scikit-learn 0.20", "Use `sklearn.model_selection`."),
    "sklearn.grid_search": _gone("scikit-learn 0.20", "Use `sklearn.model_selection`."),
    "sklearn.externals.joblib": _gone("scikit-learn 0.23", "Use `import joblib`."),
    "pandas.Panel": _gone("pandas 1.0", "Use a MultiIndex `DataFrame` or `xarray`."),
    "pydantic.BaseSettings": _gone(
        "pydantic 2.0 (moved to a separate package)",
        "`pip install pydantic-settings` and use `from pydantic_settings import BaseSettings`.",
    ),
    "pydantic.validator": _deprecated(
        "pydantic 2.0", "Use `@field_validator` (`from pydantic import field_validator`)."
    ),
    "pydantic.root_validator": _deprecated(
        "pydantic 2.0", "Use `@model_validator` (`from pydantic import model_validator`)."
    ),
    "openai.ChatCompletion": _gone(
        "openai 1.0",
        "Use the client: `from openai import OpenAI; client = OpenAI(); "
        "client.chat.completions.create(model=..., messages=[...])`.",
    ),
    "openai.Completion": _gone(
        "openai 1.0", "Use `OpenAI().completions.create(...)` (or chat completions)."
    ),
    "openai.Embedding": _gone("openai 1.0", "Use `OpenAI().embeddings.create(...)`."),
    "openai.Image": _gone("openai 1.0", "Use `OpenAI().images.generate(...)`."),
    "openai.error": _gone(
        "openai 1.0",
        "Exceptions live at the top level, e.g. `openai.APIError`, `openai.RateLimitError`.",
    ),
    "langchain.llms.OpenAI": _gone(
        "langchain 0.2",
        "`pip install langchain-openai` and use `from langchain_openai import OpenAI`.",
    ),
    "langchain.chat_models.ChatOpenAI": _gone(
        "langchain 0.2",
        "`pip install langchain-openai` and use `from langchain_openai import ChatOpenAI`.",
    ),
    "langchain.embeddings.OpenAIEmbeddings": _gone(
        "langchain 0.2",
        "`pip install langchain-openai` and use `from langchain_openai import OpenAIEmbeddings`.",
    ),
    "flask.escape": _gone("Flask 2.3", "Use `from markupsafe import escape`."),
    "flask.Markup": _gone("Flask 2.3", "Use `from markupsafe import Markup`."),
    "flask.json.JSONEncoder": _gone(
        "Flask 2.3", "Subclass `flask.json.provider.DefaultJSONProvider` and set `app.json`."
    ),
    "jinja2.Markup": _gone("Jinja2 3.0", "Use `from markupsafe import Markup`."),
    "jinja2.escape": _gone("Jinja2 3.0", "Use `from markupsafe import escape`."),
    "werkzeug.secure_filename": _gone(
        "Werkzeug 1.0", "Use `from werkzeug.utils import secure_filename`."
    ),
    "werkzeug.cached_property": _gone(
        "Werkzeug 1.0", "Use `from werkzeug.utils import cached_property`."
    ),
    "django.conf.urls.url": _gone("Django 4.0", "Use `django.urls.re_path` (or `path`)."),
    "django.utils.translation.ugettext": _gone("Django 4.0", "Use `gettext`."),
    "django.utils.translation.ugettext_lazy": _gone("Django 4.0", "Use `gettext_lazy`."),
    "django.utils.encoding.force_text": _gone("Django 4.0", "Use `force_str`."),
    "django.utils.six": _gone("Django 3.0", "Drop Python 2 compatibility code."),
    "sqlalchemy.ext.declarative.declarative_base": _deprecated(
        "SQLAlchemy 2.0", "Subclass `sqlalchemy.orm.DeclarativeBase`."
    ),
    "tensorflow.Session": _gone(
        "TensorFlow 2.0", "Use eager execution / `tf.function`, or `tf.compat.v1.Session`."
    ),
    "tensorflow.placeholder": _gone(
        "TensorFlow 2.0", "Pass tensors directly to a `tf.function`, or use `tf.compat.v1`."
    ),
    "tensorflow.contrib": _gone("TensorFlow 2.0", "Use core TF APIs or TF Addons."),
    "boto": _deprecated(
        "unmaintained since 2018", "Use `boto3` (`import boto3; boto3.client('s3')`)."
    ),
}
KNOWN_BAD_APIS.update(
    {
        f"collections.{name}": _removed("3.10", f"Use `from collections.abc import {name}`.")
        for name in _COLLECTIONS_ABCS
    }
)
KNOWN_BAD_APIS.update(
    {
        f"numpy.{name}": _gone("NumPy 1.24/2.0", f"Use {replacement}.")
        for name, replacement in _NUMPY_ALIASES.items()
    }
)

#: Method names that are wrong on any receiver in modern Python / pandas.
KNOWN_BAD_METHODS: dict[str, ApiNote] = {
    "has_key": _py2("Use `key in mapping`."),
    "iteritems": ApiNote(
        "does not exist on Python 3 dicts or pandas >= 2.0 objects", "Use `.items()`."
    ),
    "iterkeys": _py2("Use `.keys()` or iterate the dict directly."),
    "itervalues": _py2("Use `.values()`."),
    "assertEquals": _removed("3.12", "Use `assertEqual`."),
    "assertNotEquals": _removed("3.12", "Use `assertNotEqual`."),
    "assertItemsEqual": _py2("Use `assertCountEqual`."),
    "assertRegexpMatches": _removed("3.12", "Use `assertRegex`."),
    "assertRaisesRegexp": _removed("3.12", "Use `assertRaisesRegex`."),
    "assertDictContainsSubset": _removed(
        "3.12", "Use `assertLessEqual(subset.items(), d.items())`."
    ),
    "as_matrix": _gone("pandas 1.0", "Use `.to_numpy()`."),
}

_HALLUCINATION_WHY = (
    "LLMs learn from years of code written against older library versions, so they "
    "confidently produce imports and calls that no longer exist. This will fail at "
    "import or call time (or emit warnings and misbehave, if deprecated)."
)


def _match_prefix(qualified: str) -> tuple[str, ApiNote] | None:
    parts = qualified.split(".")
    for i in range(len(parts), 0, -1):
        key = ".".join(parts[:i])
        note = KNOWN_BAD_APIS.get(key)
        if note is not None:
            return key, note
    return None


def _imported_names(node: ast.Import | ast.ImportFrom) -> Iterator[str]:
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
    elif node.level == 0 and node.module:
        yield node.module
        for alias in node.names:
            if alias.name != "*":
                yield f"{node.module}.{alias.name}"


class KnownBadApiRule(Rule):
    code = "LX101"
    name = "hallucinated-api"
    summary = "Import or use of an API that was removed, renamed, or never existed in Python 3."
    explanation = (
        "Flags a curated list of APIs that coding agents commonly produce from stale training "
        "data: stdlib modules removed by PEP 594, `collections.Mapping`, `numpy.float`, the "
        "pre-1.0 `openai.ChatCompletion` interface, `pydantic.BaseSettings`, Python 2 methods "
        "like `dict.iteritems()`, and more. Code inside `try/except ImportError` or "
        "`if sys.version_info` guards is ignored."
    )
    node_types = (ast.Import, ast.ImportFrom, ast.Attribute, ast.Call)

    def check_node(self, node: Any, ctx: FileContext) -> Iterator[Violation]:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seen: set[str] = set()
            for name in _imported_names(node):
                match = _match_prefix(name)
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
            note = KNOWN_BAD_APIS.get(qualified)
            # Skip the import binding itself; it was reported at the import.
            if (
                note
                and qualified.count(".") + 1 > binding_parts
                and not is_compat_guarded(node, ctx)
            ):
                yield self._report(ctx, node, qualified, note)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            note = KNOWN_BAD_METHODS.get(node.func.attr)
            if (
                note is not None
                and node.func.attr not in ctx.defined_functions  # the file implements it
                and ctx.resolve(node.func.value) is None  # a module function, not a method
                and not is_compat_guarded(node, ctx)
            ):
                yield self.violation(
                    ctx,
                    node.func,
                    f"`.{node.func.attr}()` {note.message}.",
                    why=_HALLUCINATION_WHY,
                    fix=note.fix,
                    severity=note.severity,
                )

    def _report(self, ctx: FileContext, node: ast.AST, name: str, note: ApiNote) -> Violation:
        return self.violation(
            ctx,
            node,
            f"`{name}` {note.message}.",
            why=_HALLUCINATION_WHY,
            fix=note.fix,
            severity=note.severity,
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
    if root in _STDLIB and root not in _UNSAFE_TO_IMPORT and root not in KNOWN_BAD_APIS:
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
        if _match_prefix(f"{owner_name}.{name}"):
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
            if root in known or root in _STDLIB or root in KNOWN_BAD_APIS or root == "__future__":
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
