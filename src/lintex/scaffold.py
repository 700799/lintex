"""``lintex init``: write a starting configuration and, optionally, install the hooks."""

from __future__ import annotations

from pathlib import Path

from lintex.config import ConfigError, find_config_file
from lintex.hooks.claude_code import install_claude_code_hook
from lintex.hooks.git import GitError, install_git_hook, repo_root
from lintex.hooks.statusline import install_statusline

_BODY = """\
# lintex: a linter for code written by LLM coding agents. `lintex rules` lists the
# rules; `lintex explain CODE` documents one. Everything below is optional.
# ignore = ["LX305"]                            # turn a rule off
# enable = ["LX103"]                            # opt into unresolvable-import
# extend-exclude = ["migrations"]               # paths to skip (adds to the defaults)
# fail-on = "warning"                           # exit 1 on warnings too
# target-version = "py311"                      # minimum Python; default: requires-python
# per-file-ignores = {{ "tests/**" = ["LX203", "LX301"] }}

# [{prefix}rules.function-too-long]
# max-lines = 50

# [{prefix}rules.banned-api]
# apis = {{ "requests" = "Use app.http.client instead." }}
"""


def config_block(for_pyproject: bool) -> str:
    if for_pyproject:
        return "[tool.lintex]\n" + _BODY.format(prefix="tool.lintex.")
    return _BODY.format(prefix="")


def write_config(directory: Path) -> Path:
    """Append ``[tool.lintex]`` to pyproject.toml, or create ``.lintex.toml``."""
    found = find_config_file(directory)
    if found is not None:
        raise ConfigError(f"lintex is already configured in {found[0]}; edit that file instead")
    pyproject = directory / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        pyproject.write_text(text + sep + config_block(True), encoding="utf-8")
        return pyproject
    target = directory / ".lintex.toml"
    target.write_text(config_block(False), encoding="utf-8")
    return target


def run_init(directory: Path, hooks: bool) -> list[str]:
    """Return the lines to print: what was written and what to do next."""
    lines = []
    written = write_config(directory)
    lines.append(f"Wrote lintex configuration to {written}")
    if hooks:
        try:
            project = repo_root(directory)
        except GitError:
            project = directory
        path, changed = install_claude_code_hook(project)
        lines.append(f"{'Added' if changed else 'Already had'} Claude Code hook in {path}")
        try:
            path, changed = install_statusline(project)
            lines.append(f"{'Set' if changed else 'Already had'} Claude Code status line in {path}")
        except ConfigError as exc:
            lines.append(f"Status line not changed: {exc}")
        try:
            hook = install_git_hook(project)
            lines.append(f"Installed git pre-commit hook at {hook}")
        except GitError as exc:
            lines.append(f"Git hook not installed: {exc}")
    else:
        lines.append(
            "Next: `lintex init --hooks` installs the Claude Code hook, status line and git hook,"
        )
        lines.append("      or pick them one at a time with `lintex install-hook ...`.")
    lines.append("Then: `lintex check` to lint, `lintex doctor` to confirm everything is wired up.")
    return lines
