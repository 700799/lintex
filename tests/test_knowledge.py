import json
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import lint

from lintex import Config, Severity, knowledge
from lintex.config import load_config, parse_target_version, requires_python_minimum
from lintex.knowledge import sync

KB = knowledge.load()
RUNNING = sys.version_info[:2]


def test_packs_load_and_are_well_formed():
    assert KB.apis["distutils"].status == "removed" and KB.apis["distutils"].source == "curated"
    assert KB.apis["collections.Mapping"].fix == "Use `from collections.abc import Mapping`."
    assert KB.apis["numpy.float"].package == "NumPy"
    assert KB.methods["iteritems"].message.startswith("does not exist")
    assert all(e.fix for e in KB.apis.values()) and all(e.fix for e in KB.methods.values())
    assert all(e.status in knowledge.STATUSES for e in KB.apis.values())


def test_generated_pack_is_present_and_merged_under_curated():
    assert knowledge.TYPESHED_PATH.exists(), "run `python -m lintex.knowledge.sync --from-mypy`"
    assert KB.sources["typeshed"]["entries"] > 50
    # Generated-only entry: typeshed knows tkinter.tix went away in 3.13.
    tix = KB.apis["tkinter.tix"]
    assert (tix.status, tix.version, tix.source) == ("removed", "3.13", "typeshed")
    # A typeshed @deprecated name, with the upstream message as the fix.
    dep = KB.apis["logging.Logger.warn"]
    assert dep.status == "deprecated" and dep.source == "typeshed" and "warning" in dep.fix
    # Curated wins where both know the name.
    assert KB.apis["cgi"].source == "curated" and "urllib.parse.parse_qs" in KB.apis["cgi"].fix


def test_removal_severity_depends_on_target_version(tmp_path, monkeypatch):
    src = "import cgi\n"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\n')
    from lintex import lint_source

    [v] = lint_source(src, "a.py", load_config(tmp_path))
    assert v.severity is Severity.WARNING
    assert "minimum is Python 3.11" in v.message and "breaks on 3.13+" in v.message

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n[tool.lintex]\ntarget-version = "py313"\n'
    )
    [v] = lint_source(src, "a.py", load_config(tmp_path))
    assert v.severity is Severity.ERROR and v.message == "`cgi` was removed in Python 3.13."


def test_default_target_is_the_running_interpreter():
    [v] = lint("import cgi\n")
    expected = Severity.ERROR if RUNNING >= (3, 13) else Severity.WARNING
    assert v.severity is expected


def test_deprecated_entries_are_warnings_regardless_of_target():
    [v] = lint("from datetime import datetime\ndatetime.utcnow()\n", **{"target-version": "3.13"})
    assert v.severity is Severity.WARNING and "timezone.utc" in v.fix
    # Curated entry for the module function, typeshed-generated one for the method.
    [v] = lint("import logging\nlogging.warn('x')\n")
    assert v.severity is Severity.WARNING and v.fix == "Use `logging.warning`."
    [v] = lint("from logging import Logger\nLogger.warn\n")
    assert v.code == "LX101" and v.fix.startswith("typeshed says:") and "warning" in v.fix


def test_target_version_parsing():
    assert parse_target_version("py311") == (3, 11)
    assert parse_target_version("3.9") == (3, 9)
    assert parse_target_version("python3.12") == (3, 12)
    assert parse_target_version("latest") is None


def test_requires_python_minimum(tmp_path):
    def with_spec(spec):
        (tmp_path / "pyproject.toml").write_text(f'[project]\nrequires-python = "{spec}"\n')
        return requires_python_minimum(tmp_path)

    assert with_spec(">=3.9") == (3, 9)
    assert with_spec(">3.9,<4") == (3, 10)
    assert with_spec("~=3.11") == (3, 11)
    assert with_spec("==3.12.*") == (3, 12)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert requires_python_minimum(tmp_path) is None
    assert Config.from_dict({"target_version": "3.8"}, root=tmp_path).target_version == (3, 8)
    with pytest.raises(Exception, match="target-version"):
        Config.from_dict({"target-version": "soon"}, root=tmp_path)


# -- the sync script -----------------------------------------------------------


@pytest.fixture
def fake_typeshed(tmp_path):
    root = tmp_path / "typeshed"
    (root / "stdlib").mkdir(parents=True)
    (root / "stdlib" / "VERSIONS").write_text(
        textwrap.dedent(
            """
            # comment
            gone: 3.0-3.11
            gone.sub: 3.0-3.11
            _private: 3.0-3.10
            alive: 3.4-
            future: 3.0-3.14
            newest: 3.14-
            """
        )
    )
    (root / "stdlib" / "mod.pyi").write_text(
        textwrap.dedent(
            """
            import sys
            from typing import overload
            from typing_extensions import deprecated

            @deprecated("Deprecated since Python 3.3. Use `good()` instead.")
            def old() -> None: ...
            def good() -> None: ...

            @overload
            def mixed(x: int) -> int: ...
            @overload
            @deprecated("The str case is a mistake.")
            def mixed(x: str) -> str: ...

            class Thing:
                @classmethod
                @deprecated("Use Thing.create().")
                def make(cls) -> "Thing": ...
                def _hidden(self) -> None: ...

            if sys.version_info < (3, 14):
                @deprecated("Removed in Python 3.14.")
                def branchy() -> None: ...
            """
        )
    )
    pkg = root / "stubs" / "somedist" / "somepkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.pyi").write_text(
        'from typing_extensions import deprecated\n@deprecated("Use new_api().")\ndef old_api() -> None: ...\n'
    )
    return root


def test_sync_builds_expected_entries(fake_typeshed, tmp_path):
    data = sync.build(fake_typeshed, via="test")
    by_name = {e["name"]: e for e in data["api"]}
    assert data["source"]["latest_python"] == "3.14"
    assert by_name["gone"]["version"] == "3.12" and by_name["gone"]["status"] == "removed"
    assert "gone.sub" in by_name
    assert "_private" not in by_name and "alive" not in by_name
    assert "future" not in by_name  # 3.0-3.14 means removal in 3.15: not yet, per typeshed
    assert by_name["mod.old"]["version"] == "3.3" and "good()" in by_name["mod.old"]["fix"]
    assert "mod.mixed" not in by_name  # only one overload is deprecated
    assert by_name["mod.Thing.make"]["status"] == "deprecated"
    assert "mod.Thing._hidden" not in by_name
    assert "mod.branchy" in by_name
    assert by_name["somepkg.old_api"]["package"] == "somedist"

    out = tmp_path / "out.json"
    assert sync.main(["--typeshed", str(fake_typeshed), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["api"] == data["api"]
    assert sync.main(["--typeshed", str(fake_typeshed), "--out", str(out), "--check"]) == 0
    assert sync.main(["--typeshed", str(tmp_path), "--out", str(out)]) == 2


def test_shipped_pack_is_in_sync_with_mypy_typeshed():
    pytest.importorskip("mypy")
    data = sync.build(sync.mypy_typeshed(), via="test")
    shipped = json.loads(Path(knowledge.TYPESHED_PATH).read_text())
    # Same mypy version => identical entries; a different mypy may know more.
    if shipped["source"]["via"].startswith("mypy ") and shipped["source"]["via"] == (
        f"mypy {__import__('importlib.metadata').metadata.version('mypy')} bundled typeshed"
    ):
        assert shipped["api"] == data["api"]
