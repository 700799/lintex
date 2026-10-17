"""Git integration: staged-file discovery and pre-commit hook installation."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

HOOK_MARKER = "# installed by lintex"

HOOK_TEMPLATE = """#!/bin/sh
{marker}: lint staged Python files before committing.
# Re-install with `lintex install-hook git --force`; bypass once with `git commit --no-verify`.
if command -v lintex >/dev/null 2>&1; then
    exec lintex check --staged
fi
exec "{python}" -m lintex check --staged
"""


class GitError(Exception):
    pass


def _git(args: list[str], cwd: str | os.PathLike[str] | None = None) -> bytes:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    except FileNotFoundError:
        raise GitError("git is not installed or not on PATH") from None
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return proc.stdout


def repo_root(cwd: str | os.PathLike[str] | None = None) -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"], cwd).decode().strip())


def staged_python_files(root: Path) -> list[str]:
    """Paths (relative to ``root``) of added/copied/modified/renamed staged .py files."""
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], root)
    return [p for p in out.decode().split("\0") if p.endswith(".py")]


def staged_content(root: Path, rel_path: str) -> str:
    """The staged (index) version of a file, which is what will actually be committed."""
    return _git(["show", f":{rel_path}"], root).decode("utf-8-sig", errors="replace")


def hooks_dir(cwd: str | os.PathLike[str] | None = None) -> Path:
    # --git-path honours core.hooksPath and worktrees.
    rel = _git(["rev-parse", "--git-path", "hooks"], cwd).decode().strip()
    return (Path(cwd or Path.cwd()) / rel).resolve()


def install_git_hook(cwd: str | os.PathLike[str] | None = None, force: bool = False) -> Path:
    hook = hooks_dir(cwd) / "pre-commit"
    if hook.exists() and HOOK_MARKER not in hook.read_text(errors="replace") and not force:
        raise GitError(
            f"{hook} already exists and was not installed by lintex. Re-run with --force to "
            "replace it, or add `lintex check --staged` to it yourself."
        )
    hook.parent.mkdir(parents=True, exist_ok=True)
    # Always LF: a CRLF shebang line breaks `sh` on Git for Windows.
    hook.write_bytes(HOOK_TEMPLATE.format(marker=HOOK_MARKER, python=sys.executable).encode())
    mode = hook.stat().st_mode
    hook.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook
