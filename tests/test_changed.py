import json
import shutil
import subprocess

import pytest

from lintex.cli import main
from lintex.hooks.git import changed_lines, changed_python_files, default_base

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BASE = (
    "import pickle\n"
    "\n"
    "def old(b):\n"
    "    return pickle.loads(b)\n"  # pre-existing finding on line 4
    "\n"
    "def helper():\n"
    "    return 1\n"
)
EDITED = (
    "import pickle\n"
    "\n"
    "def old(b):\n"
    "    return pickle.loads(b)\n"
    "\n"
    "def helper():\n"
    "    return eval('1')\n"  # new finding on line 7
)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(cwd),
        },
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "pyproject.toml").write_text("[tool.lintex]\n")
    (tmp_path / "app.py").write_text(BASE)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_changed_reports_only_touched_lines_and_new_files(repo, capsys):
    (repo / "app.py").write_text(EDITED)
    (repo / "new.py").write_text("x = 1\nexec('2')\n")  # untracked: checked in full

    assert default_base(repo) == "main"
    base, files = changed_python_files(repo, "main")
    assert files == {"app.py": {7}, "new.py": None}
    assert changed_lines(repo, "main", "app.py") == {7}

    assert main(["check", "--changed", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    found = sorted((v["path"], v["line"], v["code"]) for v in payload["violations"])
    assert found == [("app.py", 7, "LX203"), ("new.py", 2, "LX203")]
    assert payload["files_checked"] == 2

    assert main(["check", "--changed", "--no-color"]) == 1
    out = capsys.readouterr().out
    assert "lines changed since main" in out and "app.py" in out and ":4" not in out


def test_changed_with_explicit_ref_and_no_changes(repo, capsys):
    assert main(["check", "--changed=main", "--no-color"]) == 0
    assert "All checks passed" in capsys.readouterr().out

    (repo / "app.py").write_text(EDITED)
    _git(repo, "commit", "-qam", "edit")
    assert main(["check", "--changed=HEAD~1", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [v["line"] for v in payload["violations"]] == [7]


def test_multiline_finding_counts_if_any_of_its_lines_changed(repo, capsys):
    body = "".join(f"    x{i} = {i}\n" for i in range(60))
    (repo / "big.py").write_text(f"def f():\n{body}")
    _git(repo, "add", "big.py")
    _git(repo, "commit", "-qm", "big")
    # Relative to the last commit, touching one line inside the long function
    # makes its length warning yours.
    (repo / "big.py").write_text(f"def f():\n{body}    x60 = 60\n")
    assert main(["check", "--changed=HEAD", "--json", "--fail-on", "warning"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert [v["code"] for v in payload["violations"]] == ["LX301"]
    # Leaving it alone and touching another file does not surface it.
    (repo / "big.py").write_text(f"def f():\n{body}")
    (repo / "other.py").write_text("y = 2\n")
    assert main(["check", "--changed=HEAD", "--json", "--fail-on", "warning"]) == 0
    # Relative to main, big.py is a new file and is checked in full.
    assert main(["check", "--changed", "--json", "--fail-on", "warning"]) == 1


def test_changed_outside_a_repo_is_a_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["check", "--changed"]) == 2
    assert "lintex: error" in capsys.readouterr().err
