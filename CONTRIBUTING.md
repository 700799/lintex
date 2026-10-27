# Contributing to lintex

Thanks for helping. The most valuable contributions, in order, are: a false positive with a
minimal reproduction, a new API knowledge entry, and a new rule.

## Set up

```bash
git clone https://github.com/700799/lintex && cd lintex
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest pyyaml ruff mypy
pytest                                    # the suite runs in well under a second
ruff check src tests && ruff format --check src tests
lintex check .                            # lintex lints itself and must stay clean
```

CI runs the same three commands on Python 3.9–3.13, Windows and macOS.

## Report a false positive

Open an issue with the smallest snippet that triggers it, the lintex version, and your
Python version. Precision matters more than recall here: a rule that cries wolf gets turned
off, so a confirmed false positive is a bug.

## Add an API knowledge entry

Removed, deprecated and Python-2-only names for LX101 live in
`src/lintex/knowledge/curated.toml`. An entry is:

```toml
[[api]]
name = "requests.packages.urllib3"
status = "removed"          # removed | deprecated | py2
package = "requests"        # omit for the standard library
version = "2.16"
fix = "Use `import urllib3` directly."
```

Write the `fix` as something an agent can paste: the replacement, not a description of it.
Add a line to `tests/test_rules_hallucinations.py` showing it fires. Stdlib module removals and
`@deprecated` names are generated into `typeshed.json`; don't edit that file, regenerate it
with `python -m lintex.knowledge.sync --from-mypy` (stdlib) or `--typeshed PATH` (with
third-party stubs). A curated entry overrides a generated one for the same name, which is the
way to give a generated entry a better fix.

## Add a rule

1. Subclass `lintex.rules.base.Rule` in the module for its family (`hallucinations`,
   `banned_patterns`, `bloat`, `agent_artifacts`). Set `code`, `name`, `summary`,
   `explanation`, `default_severity`, `node_types` and, if configurable, `options`.
2. Implement `check_node` (called for each node whose type is in `node_types`) and/or
   `check_file`. Use `ctx.resolve(node)` to turn a Name/Attribute chain into an
   import-qualified name, and `is_compat_guarded` to skip `try/except ImportError` blocks.
3. Every violation needs a `message` (what), a `why` (the failure mode) and a `fix` (what to
   write instead). Assume the reader is an LLM with no other context.
4. Register it in `ALL_RULES` in `src/lintex/rules/__init__.py` and add tests with both a
   triggering case and a clean case that looks similar.
5. Run it over a large real codebase before opening the PR (the CPython stdlib is a good
   known-good corpus: `lintex check $(python -c "import sysconfig;print(sysconfig.get_paths()['stdlib'])") --select LXnnn`).
   New rules default to `warning` unless the finding is almost never wrong.

## Keep the engine fast

Rules run inside a single AST walk, dispatched by node type. Don't re-walk the tree in a rule;
keep per-file state in `ctx.state[self.code]` and finish in `check_file`. Anything that reads
files or imports modules must be cached across files.

## Commit messages and PRs

Explain the *why* in the commit body; the diff already shows the what. Keep PRs to one change.
A PR that adds a rule should include the false-positive numbers from the corpus run.
