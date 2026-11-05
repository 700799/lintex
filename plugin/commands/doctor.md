---
description: Check that lintex is installed and its hooks, status line and config are wired up
allowed-tools: Bash(lintex doctor *), Bash(lintex install-hook *), Bash(lintex init *)
---

Run `lintex doctor` in the project root and show the output.

For each line marked with a warning or failure, explain the one-line hint it prints and ask
whether to run the suggested command. Note that when this plugin is enabled the edit hook
runs from the plugin, so a "claude code hook: not installed" warning is expected and can be
ignored. If `lintex` itself is not installed, say that `pip install lintex` (or
`pipx install lintex`) is the only step that must happen outside Claude Code.
