from __future__ import annotations

import pytest

from lintex import versions
from lintex.config import ConfigError, load_config
from lintex.engine import Linter
from lintex.versions import Ceiling, ceiling_from_specifier, parse

SRC = "from pydantic import BaseSettings\nimport openai\nopenai.ChatCompletion.create()\n"


@pytest.fixture(autouse=True)
def _fresh_cache():
    versions.discover.cache_clear()
    yield
    versions.discover.cache_clear()


def flagged(root) -> set[str]:
    """Which packages LX101 still reports for SRC in a project rooted at ``root``."""
    (root / "a.py").write_text(SRC)
    found = Linter(load_config(root)).lint_file(root / "a.py")
    return {"pydantic" if "pydantic" in v.message else "openai" for v in found}


@pytest.mark.parametrize(
    ("spec", "version", "inclusive"),
    [
        ("==1.10.13", (1, 10, 13), True),
        ("==1.10.*", (1, 11), False),
        (">=1.10,<2", (2,), False),
        ("<2.0,>=1.0", (2, 0), False),
        ("<=1.10", (1, 10), True),
        ("~=1.10", (2,), False),
        ("~=1.10.3", (1, 11), False),
        ("^1.10", (2,), False),
        ("^0.28", (0, 29), False),
        ("~1.10", (1, 11), False),
        ("<3,<2", (2,), False),
    ],
)
def test_ceiling_from_specifier(spec, version, inclusive):
    assert ceiling_from_specifier(spec, "t") == Ceiling(version, inclusive, "t")


@pytest.mark.parametrize("spec", [">=1.10", "!=1.5", ">1", ""])
def test_lower_bounds_put_no_ceiling(spec):
    assert ceiling_from_specifier(spec, "t") is None


def test_below_compares_across_lengths():
    assert Ceiling((2,), False, "t").below((2, 0))  # <2 is below 2.0
    assert not Ceiling((2,), True, "t").below((2, 0))  # ==2 is not
    assert Ceiling((1, 10, 13), True, "t").below((2, 0))
    assert not Ceiling((2, 7, 1), True, "t").below((2, 0))
    assert parse("2.0.1rc1") == (2, 0, 1) and parse("nope") == ()


def test_no_information_keeps_the_findings(tmp_path):
    assert flagged(tmp_path) == {"pydantic", "openai"}


def test_requirements_pin_drops_only_that_package(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# deps\n-r other.txt\nPydantic[email]==1.10.13 ; python_version >= '3.8'\nopenai>=0.27\n"
    )
    assert flagged(tmp_path) == {"openai"}


def test_pyproject_upper_bounds(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["pydantic>=1.10,<2", "openai~=0.28"]\n'
    )
    assert flagged(tmp_path) == set()


def test_lower_bound_alone_still_flags(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["pydantic>=1.10", "openai"]\n'
    )
    assert flagged(tmp_path) == {"pydantic", "openai"}


def test_lockfile_wins_over_pyproject_range(tmp_path):
    # pyproject allows pydantic 1.x or 2.x; the lock says which one is installed.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["pydantic>=1.10,<3", "openai<1"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pydantic"\nversion = "2.7.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "openai"\nversion = "0.28.1"\n'
    )
    assert flagged(tmp_path) == {"pydantic"}


def test_poetry_tables_and_lock(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.9"\npydantic = "^1.10"\n'
        'openai = { version = "0.28.1", extras = ["x"] }\n'
    )
    assert flagged(tmp_path) == set()


def test_pipfile_lock(tmp_path):
    (tmp_path / "Pipfile.lock").write_text(
        '{"default": {"pydantic": {"version": "==1.10.2"}}, "develop": {}}'
    )
    assert flagged(tmp_path) == {"openai"}


def test_explicit_package_versions_override_lockfiles(tmp_path):
    (tmp_path / "requirements.txt").write_text("pydantic==2.7.0\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lintex.package-versions]\nPydantic = "1.10"\nopenai = "<1"\n'
    )
    assert flagged(tmp_path) == set()


def test_bad_package_versions_is_a_config_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.lintex]\npackage-versions = ["pydantic"]\n')
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_stdlib_findings_are_never_affected(tmp_path):
    (tmp_path / "requirements.txt").write_text("python==2.7\ncollections==0.1\n")
    (tmp_path / "a.py").write_text("from collections import Mapping\n")
    assert Linter(load_config(tmp_path)).lint_file(tmp_path / "a.py")


def test_unreadable_or_broken_files_are_ignored(tmp_path):
    (tmp_path / "pyproject.toml").write_text("not [valid toml\n")
    (tmp_path / "Pipfile.lock").write_text("{broken")
    assert versions.discover(tmp_path) == {}


def test_lockfile_is_found_from_a_subdirectory_without_config(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\nopenai==0.28.1\n")
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    assert flagged(sub) == set()


def test_search_stops_at_the_repository_boundary(tmp_path):
    (tmp_path / "requirements.txt").write_text("pydantic==1.10.13\nopenai==0.28.1\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert flagged(repo) == {"pydantic", "openai"}
