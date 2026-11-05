"""PostToolUse hook entry point for the lintex Claude Code plugin.

Claude Code pipes the tool payload to stdin. This script hands it to lintex's
hook and relays the exit code: 2 (with findings on stderr) makes Claude see
and fix the problems; 0 is silence.

The plugin cannot install lintex itself, so it looks for it three ways: as an
importable module in this Python, as `lintex` on PATH, then as a `python -m
lintex` on PATH. If none works it tells Claude once per session how to
install it and stays quiet after that, so a missing tool never nags.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

INSTALL_HINT = (
    "The lintex plugin is enabled but the `lintex` command is not installed in this "
    "environment, so edits are not being checked. Ask the user to run "
    "`pip install lintex` (or `pipx install lintex`), then edits will be linted "
    "automatically. This message is shown once per session."
)


def main() -> int:
    payload = sys.stdin.read()

    try:
        from lintex.hooks.claude_code import run_claude_code_hook  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        import io

        return run_claude_code_hook(io.StringIO(payload), sys.stderr)

    exe = shutil.which("lintex")
    cmd = [exe, "hook", "claude-code"] if exe else None
    if cmd is None:
        for python in ("python3", "python"):
            path = shutil.which(python)
            if path and _can_import_lintex(path):
                cmd = [path, "-m", "lintex", "hook", "claude-code"]
                break
    if cmd is not None:
        proc = subprocess.run(cmd, input=payload, text=True, stderr=sys.stderr)
        return proc.returncode

    return _nudge_once(payload)


def _can_import_lintex(python: str) -> bool:
    try:
        return (
            subprocess.run(
                [python, "-c", "import lintex"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _nudge_once(payload: str) -> int:
    try:
        session = json.loads(payload).get("session_id") or "unknown"
    except (ValueError, AttributeError):
        session = "unknown"
    marker_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or tempfile.gettempdir()
    digest = hashlib.sha1(str(session).encode()).hexdigest()[:12]
    marker = os.path.join(marker_dir, f"lintex-nudged-{digest}")
    if os.path.exists(marker):
        return 0
    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker, "w"):
            pass
    except OSError:
        pass
    sys.stderr.write(INSTALL_HINT + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
