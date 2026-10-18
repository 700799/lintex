"""Git integration: staged-file discovery and pre-commit hook installation."""

from __future__ import annotations

import os
import re
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


def default_base(root: Path) -> str:
    """The branch to diff against: the remote's default branch, else main/master."""
    try:
        ref = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], root)
        if ref.strip():
            return ref.decode().strip()
    except GitError:
        pass
    for candidate in ("origin/main", "origin/master", "main", "master"):
        try:
            _git(["rev-parse", "--verify", "--quiet", candidate], root)
            return candidate
        except GitError:
            continue
    raise GitError(
        "cannot work out a base branch to compare with; pass one explicitly, "
        "e.g. `lintex check --changed=main`"
    )


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def changed_lines(root: Path, base: str, rel_path: str) -> set[int]:
    """Line numbers in the working-tree file that differ from ``base``."""
    diff = _git(["diff", "-U0", base, "--", rel_path], root).decode(errors="replace")
    lines: set[int] = set()
    for match in _HUNK_RE.finditer(diff):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        lines.update(range(start, start + count))
    return lines


def changed_python_files(root: Path, base: str) -> tuple[str, dict[str, set[int] | None]]:
    """Python files that differ from the merge base with ``base``, and their changed lines.

    Returns ``(resolved_base, {rel_path: changed_lines})`` where ``None`` means
    the whole file is new (added or untracked) and should be checked in full.
    """
    try:
        merge_base = _git(["merge-base", base, "HEAD"], root).decode().strip()
    except GitError:
        merge_base = base  # a bare commit or a ref with no common history
    out = _git(["diff", "--name-only", "--diff-filter=ACMR", "-z", merge_base, "--"], root)
    files: dict[str, set[int] | None] = {}
    for rel in sorted(p for p in out.decode().split("\0") if p.endswith(".py")):
        files[rel] = changed_lines(root, merge_base, rel)
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    for rel in sorted(p for p in untracked.decode().split("\0") if p.endswith(".py")):
        files[rel] = None
    return merge_base[:12] if merge_base != base else base, files


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
