"""Integrations that run lintex automatically: git pre-commit and Claude Code hooks."""

from lintex.hooks.claude_code import install_claude_code_hook, run_claude_code_hook
from lintex.hooks.git import GitError, install_git_hook, staged_content, staged_python_files

__all__ = [
    "GitError",
    "install_claude_code_hook",
    "install_git_hook",
    "run_claude_code_hook",
    "staged_content",
    "staged_python_files",
]
