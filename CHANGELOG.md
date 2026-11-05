# Changelog

All notable changes to lintex are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). While the version is 0.x, rule defaults and
severities may change between minor versions as they are tuned on real codebases.

## [Unreleased]

### Added

- A Claude Code plugin (`/plugin marketplace add 700799/lintex`, then
  `/plugin install lintex@lintex`) that installs the edit hook plus `/lintex:check`,
  `/lintex:report` and `/lintex:doctor`. The hook finds lintex as a module, on PATH, or via
  `python -m lintex`, and if none exists tells Claude once per session how to install it.
- `lintex check --fix` applies the mechanical rewrites: pure renames of names that no
  longer exist (`collections.Mapping` → `collections.abc.Mapping`, `.iteritems()` → `.items()`,
  `sys.maxint` → `sys.maxsize`, `np.float` → `float`, `assertEquals` → `assertEqual`, ...).
  Findings that can be fixed are tagged `[fixable]` and carry an `autofix` in `--json`. Anything
  that would need a new import or a judgement call stays a text-only fix.
- `lintex init` writes a commented `[tool.lintex]` block into `pyproject.toml` (or a
  `.lintex.toml`), and `--hooks` installs the Claude Code hook, status line and git hook in one go.
- `lintex doctor` checks that lintex is on PATH, the config loads, which Python is targeted, the
  knowledge packs loaded, and whether the Claude Code hook, status line, git hook and ledger are
  wired up, with the command to run for each that isn't.

### Fixed

- Piping output into a pager or `head` no longer ends in a `BrokenPipeError` traceback;
  lintex exits quietly. Ctrl-C prints one line instead of a stack trace.
- `lintex rules` and `lintex explain` accept `--color` / `--no-color` like the other commands.

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
