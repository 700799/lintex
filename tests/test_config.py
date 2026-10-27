import pytest

from lintex import ConfigError, Linter, Severity, load_config


def test_pyproject_discovery_from_subdirectory(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lintex]\nignore = ["LX403"]\nfail_on = "warning"\n'
        "[tool.lintex.rules.function-too-long]\nmax_lines = 10\n"
    )
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    cfg = load_config(sub)
    assert cfg.root == tmp_path.resolve()
    assert cfg.ignore == ["LX403"]
    assert cfg.fail_on is Severity.WARNING
    linter = Linter(cfg)
    rule = next(r for r in linter.rules if r.code == "LX301")
    assert rule.opts["max-lines"] == 10


def test_pyproject_without_lintex_table_is_skipped(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\nignore = ['LX1']\n")
    child = tmp_path / "child"
    child.mkdir()
    (child / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert load_config(child).ignore == ["LX1"]


def test_yaml_config_takes_precedence(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\nignore = ['LX1']\n")
    (tmp_path / ".lintex.yaml").write_text(
        "ignore: [LX3]\nrules:\n  banned-api:\n    apis:\n      requests: Use httpx\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.ignore == ["LX3"]
    assert cfg.rule_options["banned-api"]["apis"] == {"requests": "Use httpx"}


def test_lintex_toml(tmp_path):
    (tmp_path / ".lintex.toml").write_text("select = ['LX2']\n")
    assert load_config(tmp_path).select == ["LX2"]


@pytest.mark.parametrize(
    "toml, message",
    [
        ("[tool.lintex]\nignroe = ['LX1']\n", "unknown lintex config key"),
        ("[tool.lintex]\nfail-on = 'fatal'\n", "invalid severity"),
        ("[tool.lintex.rules.not-a-rule]\nx = 1\n", "unknown rule"),
        ("[tool.lintex.rules.LX301]\nmax-lens = 1\n", "unknown option"),
        ("[tool.lintex\n", "invalid TOML"),
    ],
)
def test_invalid_config_errors_are_helpful(tmp_path, toml, message):
    (tmp_path / "pyproject.toml").write_text(toml)
    with pytest.raises(ConfigError, match=message):
        Linter(load_config(tmp_path))


def test_exclude_patterns(tmp_path):
    (tmp_path / ".lintex.toml").write_text("extend-exclude = ['migrations', 'gen/*.py']\n")
    cfg = load_config(tmp_path)
    assert cfg.is_excluded(tmp_path / "app" / "migrations" / "0001.py")
    assert cfg.is_excluded(tmp_path / "gen" / "x.py")
    assert cfg.is_excluded(tmp_path / ".venv" / "lib.py")
    assert not cfg.is_excluded(tmp_path / "app" / "models.py")


def test_exclude_adds_to_defaults(tmp_path):
    (tmp_path / ".lintex.toml").write_text("exclude = ['scratch']\n")
    cfg = load_config(tmp_path)
    assert cfg.is_excluded(tmp_path / "scratch" / "x.py")
    assert cfg.is_excluded(tmp_path / ".venv" / "x.py")
