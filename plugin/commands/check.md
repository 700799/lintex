---
description: Lint the files changed on this branch with lintex and fix what it reports
argument-hint: [paths or --changed=REF]
allowed-tools: Bash(lintex check *), Read, Edit
---

Run `lintex check --changed --format full $ARGUMENTS` in the project root. If that fails
because there is no base branch to compare with, run `lintex check --format full` on the
paths given (or the current directory) instead.

For every finding, read its `why` and `fix` lines and apply the fix in the code. Do not
suppress a finding with `# lintex: ignore[...]` unless the code is deliberately an exception,
and say why in the comment when you do. When all findings are resolved, re-run the same
command to confirm it reports "All checks passed", then summarise what you changed.
