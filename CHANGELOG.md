# Changelog

All notable changes to lintex are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). While the version is 0.x, rule defaults and
severities may change between minor versions as they are tuned on real codebases.

## [Unreleased]

## [0.1.0] - 2026-10-23

First release.

### Rules

- **LX001** syntax-error: the file does not parse (often a truncated edit).
- **LX101** hallucinated-api: removed, deprecated or Python-2-only APIs, from a curated pack
  and a pack generated from typeshed. Stdlib removals are judged against the project's minimum
  Python (`requires-python` or `target-version`).
- **LX102** nonexistent-stdlib-name: names that don't exist in a stdlib module, with
  "did you mean" suggestions (Python 3.10+).
- **LX103** unresolvable-import (opt-in): imports that aren't installed, stdlib or local.
- **LX201** sql-string-interpolation: SQL built with f-strings, `.format()`, `%` or `+`.
- **LX202** raw-sql (opt-in): any raw SQL, for ORM-only projects.
- **LX203** dangerous-call: `eval`/`exec`, unsafe deserialization, `yaml.load` without a safe
  loader, `os.system`, `shell=True`, `verify=False`.
- **LX204** insecure-file-operation: `tempfile.mktemp`, world-writable modes, tar
  `extractall()` without `filter=`, hard-coded `/tmp` paths.
- **LX205** banned-api: project-specific banned modules and functions with custom messages.
- **LX301–LX305** agent bloat: function length, cyclomatic complexity, parameter count,
  nesting depth, module length.
- **LX401** placeholder-code: comments standing in for elided code, stub markers.
- **LX402** swallowed-exception, **LX403** bare-except.

### Tooling

- `lintex check` with `--format pretty|full|concise|github|json`, `--staged`, `--changed`,
  `--select`/`--ignore`/`--enable`, `--fail-on`, inline `# lintex: ignore[CODE]` and
  `# lintex: skip-file`.
- Configuration in `pyproject.toml` `[tool.lintex]`, `.lintex.toml` or `.lintex.yaml`.
- Claude Code `PostToolUse` hook that feeds findings (with `why` and `fix`) back to the agent.
- Session ledger with `lintex report` and a Claude Code status line (`lintex statusline`).
- Git pre-commit hook (`lintex install-hook git`) and a `.pre-commit-hooks.yaml`.
- `python -m lintex.knowledge.sync` to regenerate the typeshed knowledge pack.

[Unreleased]: https://github.com/700799/lintex/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/700799/lintex/releases/tag/v0.1.0
