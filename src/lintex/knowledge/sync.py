"""Regenerate ``typeshed.json`` from a typeshed checkout.

    python -m lintex.knowledge.sync --typeshed /path/to/typeshed
    python -m lintex.knowledge.sync --from-mypy      # mypy's bundled copy (stdlib only)
    python -m lintex.knowledge.sync --from-mypy --stub-packages default   # + PyPI types-*

Two sources are read:

* ``stdlib/VERSIONS``: ``module: 3.0-3.12`` means the module was removed in
  3.13. Only removals at or before the newest Python typeshed itself knows
  about are emitted, so planned removals never produce findings early.
* ``@deprecated("...")`` decorators (PEP 702) in ``stdlib/`` and, when
  present, ``stubs/<dist>/``. A name counts as deprecated only when *every*
  definition of it carries the decorator: a decorator on one ``@overload``
  marks a parameter combination, not the function.

This is a maintainer step. It never runs at lint time, needs no network when
pointed at a local checkout, and records its provenance in the output.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lintex.knowledge import TYPESHED_PATH

_VERSION_LINE = re.compile(r"^([\w.]+):\s*(\d+\.\d+)-(\d+\.\d+)?\s*$")
_SINCE = re.compile(r"(?:since|in) Python (3\.\d+)", re.IGNORECASE)
# Names another lintex rule already reports with a more specific fix.
SKIP = frozenset({"tempfile.mktemp"})

GENERIC_REMOVED_FIX = (
    'The module no longer exists on Python {next}+. See "What\'s New in Python {next}" '
    "for the replacement, or vendor the functionality."
)


def _ver(text: str) -> tuple[int, int]:
    major, minor = text.split(".")
    return int(major), int(minor)


def removed_modules(versions_file: Path) -> tuple[list[dict], str]:
    """Modules with an upper bound, as entries removed in the following version."""
    entries: list[dict] = []
    latest = (3, 0)
    rows: list[tuple[str, tuple[int, int] | None]] = []
    for line in versions_file.read_text(encoding="utf-8").splitlines():
        match = _VERSION_LINE.match(line.strip())
        if not match:
            continue
        name, low, high = match.groups()
        latest = max(latest, _ver(low))
        if high:
            latest = max(latest, _ver(high))
            rows.append((name, _ver(high)))
    for name, high in rows:
        if any(part.startswith("_") for part in name.split(".")):
            continue
        removed = (high[0], high[1] + 1)
        if removed > latest:
            continue  # planned, not yet happened as far as typeshed knows
        version = f"{removed[0]}.{removed[1]}"
        entries.append(
            {
                "name": name,
                "status": "removed",
                "version": version,
                "fix": GENERIC_REMOVED_FIX.format(next=version),
            }
        )
    return entries, f"{latest[0]}.{latest[1]}"


def _deprecation_message(node: ast.AST) -> str | None:
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name != "deprecated":
            continue
        if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
            return str(dec.args[0].value)
        return ""
    return None


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _walk_defs(body: list[ast.stmt], prefix: str, out: dict[str, list[str | None]]) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = f"{prefix}.{node.name}"
            out[qualname].append(_deprecation_message(node))
            if isinstance(node, ast.ClassDef):
                _walk_defs(node.body, qualname, out)
        elif isinstance(node, ast.If):
            # Version/platform branches: collect from both arms.
            _walk_defs(node.body, prefix, out)
            _walk_defs(node.orelse, prefix, out)


def deprecated_names(stub_root: Path, package: str = "python") -> list[dict]:
    """Public names whose every definition in the stubs is ``@deprecated``."""
    defs: dict[str, list[str | None]] = defaultdict(list)
    for path in sorted(stub_root.rglob("*.pyi")):
        if "@python2" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        _walk_defs(tree.body, _module_name(stub_root, path), defs)
    entries = []
    for qualname, messages in sorted(defs.items()):
        if any(m is None for m in messages) or qualname in SKIP:
            continue
        if any(part.startswith("_") for part in qualname.split(".")):
            continue
        text = next((m for m in messages if m), "")
        since = _SINCE.search(text)
        entry = {
            "name": qualname,
            "status": "deprecated",
            "fix": text
            or "Deprecated upstream; check the library's documentation for the replacement.",
        }
        if package != "python":
            entry["package"] = package
        if since:
            entry["version"] = since.group(1)
        entries.append(entry)
    return entries


def build(typeshed: Path, via: str, stub_dirs: list[tuple[Path, str, str]] | None = None) -> dict:
    stdlib = typeshed / "stdlib"
    removed, latest = removed_modules(stdlib / "VERSIONS")
    api = removed + deprecated_names(stdlib)
    stubs = typeshed / "stubs"
    if stubs.is_dir():
        for dist in sorted(p for p in stubs.iterdir() if p.is_dir()):
            api += _third_party(dist)
    for directory, package, _wheel in stub_dirs or []:
        api += _third_party(directory, package)
    seen: set[str] = set()
    unique = [e for e in api if not (e["name"] in seen or seen.add(e["name"]))]
    source = {
        "name": "typeshed",
        "via": via,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_python": latest,
        "entries": len(unique),
    }
    if stub_dirs:
        source["stub_packages"] = sorted(wheel for _, _, wheel in stub_dirs)
    return {"source": source, "api": unique}


def _third_party(dist: Path, package: str | None = None) -> list[dict]:
    """Deprecations in one ``stubs/<dist>`` directory, with modules rooted at the dist."""
    out: list[dict] = []
    package = package or dist.name
    for pkg_dir in sorted(
        p
        for p in dist.iterdir()
        if p.is_dir() and not p.name.startswith("@") and not p.name.endswith(".dist-info")
    ):
        defs: dict[str, list[str | None]] = defaultdict(list)
        for path in sorted(pkg_dir.rglob("*.pyi")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            _walk_defs(tree.body, _module_name(dist, path), defs)
        for qualname, messages in sorted(defs.items()):
            if any(m is None for m in messages) or any(
                part.startswith("_") for part in qualname.split(".")
            ):
                continue
            text = next((m for m in messages if m), "")
            out.append(
                {
                    "name": qualname,
                    "status": "deprecated",
                    "package": package,
                    "fix": text or "Deprecated upstream; check the library's documentation.",
                }
            )
    return out


# typeshed's third-party stubs are published to PyPI as `types-*` packages, so the
# deprecations in them can be synced without a typeshed checkout. These are the
# libraries agents reach for most; `--stub-packages` takes any list.
DEFAULT_STUB_PACKAGES = (
    "types-requests",
    "types-PyYAML",
    "types-six",
    "types-setuptools",
    "types-python-dateutil",
    "types-pytz",
    "types-Markdown",
    "types-docutils",
    "types-protobuf",
    "types-simplejson",
    "types-toml",
    "types-tabulate",
    "types-psutil",
    "types-paramiko",
    "types-pyOpenSSL",
    "types-cachetools",
    "types-ujson",
    "types-regex",
    "types-tqdm",
    "types-jsonschema",
    "types-Deprecated",
    "types-colorama",
    "types-mock",
    "types-openpyxl",
    "types-pexpect",
    "types-html5lib",
    "types-xmltodict",
    "types-pygments",
    "types-python-slugify",
)


def download_stubs(names: list[str], workdir: Path) -> list[tuple[Path, str, str]]:
    """Fetch ``types-*`` wheels from PyPI and unpack them. Returns (dir, package, wheel)."""
    import subprocess
    import zipfile

    workdir.mkdir(parents=True, exist_ok=True)
    result = []
    for name in names:
        dest = workdir / name
        dest.mkdir(exist_ok=True)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "-q",
                "--no-deps",
                "--only-binary=:all:",
                "-d",
                str(dest),
                name,
            ],
            capture_output=True,
            text=True,
        )
        wheels = sorted(dest.glob("*.whl"))
        if proc.returncode != 0 or not wheels:
            print(
                f"skipping {name}: {proc.stderr.strip().splitlines()[-1:] or 'no wheel'}",
                file=sys.stderr,
            )
            continue
        with zipfile.ZipFile(wheels[0]) as zf:
            members = [m for m in zf.namelist() if m.endswith(".pyi") and ".." not in m]
            zf.extractall(dest / "unpacked", members)
        package = name[len("types-") :] if name.startswith("types-") else name
        result.append((dest / "unpacked", package, wheels[0].name))
    return result


def mypy_typeshed() -> Path:
    import mypy  # optional, maintainer-only

    return Path(mypy.__file__).parent / "typeshed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--typeshed", type=Path, help="path to a typeshed checkout")
    source.add_argument("--from-mypy", action="store_true", help="use mypy's bundled typeshed")
    parser.add_argument("--out", type=Path, default=TYPESHED_PATH)
    parser.add_argument("--check", action="store_true", help="exit 1 if the output would change")
    parser.add_argument(
        "--stub-packages",
        metavar="NAMES",
        help="comma-separated `types-*` packages to fetch from PyPI for third-party "
        "deprecations, or `default` for the built-in list",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where to download stub wheels (default: a temporary directory)",
    )
    args = parser.parse_args(argv)

    if args.from_mypy:
        try:
            typeshed = mypy_typeshed()
        except ImportError:
            print("mypy is not installed; `pip install mypy` or pass --typeshed", file=sys.stderr)
            return 2
        from importlib.metadata import version

        via = f"mypy {version('mypy')} bundled typeshed"
    else:
        typeshed = args.typeshed
        head = typeshed / ".git" / "HEAD"
        via = (
            f"typeshed checkout ({head.read_text().strip()})"
            if head.exists()
            else "typeshed checkout"
        )
    if not (typeshed / "stdlib" / "VERSIONS").exists():
        print(f"{typeshed} does not look like typeshed (no stdlib/VERSIONS)", file=sys.stderr)
        return 2

    stub_dirs: list[tuple[Path, str, str]] = []
    if args.stub_packages:
        import tempfile

        names = (
            list(DEFAULT_STUB_PACKAGES)
            if args.stub_packages == "default"
            else [n.strip() for n in args.stub_packages.split(",") if n.strip()]
        )
        workdir = args.workdir or Path(tempfile.mkdtemp(prefix="lintex-stubs-"))
        stub_dirs = download_stubs(names, workdir)
        print(f"fetched {len(stub_dirs)} stub package(s) into {workdir}", file=sys.stderr)

    data = build(typeshed, via, stub_dirs)
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        same = json.loads(current).get("api") == data["api"] if current else False
        print("up to date" if same else "out of date")
        return 0 if same else 1
    args.out.write_text(text, encoding="utf-8")
    counts: dict[str, int] = {}
    for e in data["api"]:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print(f"wrote {args.out} ({data['source']['entries']} entries: {counts}) from {via}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
