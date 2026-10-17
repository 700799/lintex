"""Command-line interface.

lintex [check] [PATHS...] [--json] [--staged]   lint files (default: .)
lintex rules [--json]                            list rules
lintex explain CODE                              document a rule
lintex install-hook git|claude-code              set up automatic checks
lintex hook claude-code                          Claude Code PostToolUse entry point
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

from lintex import __version__
from lintex.config import Config, ConfigError, load_config
from lintex.engine import Linter, LintResult
from lintex.hooks.claude_code import install_claude_code_hook, run_claude_code_hook
from lintex.hooks.git import (
    GitError,
    install_git_hook,
    repo_root,
    staged_content,
    staged_python_files,
)
from lintex.models import Severity
from lintex.reporters import FORMATS, Style, dim_note, render
from lintex.rules import ALL_RULES, get_rule, is_rule_active

COMMANDS = ("check", "rules", "explain", "install-hook", "hook")
EXIT_OK, EXIT_VIOLATIONS, EXIT_USAGE = 0, 1, 2


def _csv(value: str) -> list[str]:
    return [v.strip().upper() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lintex",
        description="Fast linter for Python code written by LLM coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"lintex {__version__}")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="lint files or directories (the default command)")
    check.add_argument("paths", nargs="*", help="files or directories (default: current directory)")
    check.add_argument(
        "--format",
        choices=FORMATS,
        default=None,
        help="output style: pretty (default, grouped by file), full (every finding with why and "
        "fix, for agents), concise (one line per finding), github (Actions annotations), json",
    )
    check.add_argument("--json", action="store_true", help="shorthand for --format json")
    check.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the `why` for every finding, not just the first of each rule",
    )
    check.add_argument(
        "--staged",
        action="store_true",
        help="lint the staged version of staged .py files (for pre-commit)",
    )
    check.add_argument("--config", metavar="PATH", help="explicit config file")
    check.add_argument("--select", type=_csv, metavar="CODES", help="only run these codes/prefixes")
    check.add_argument("--ignore", type=_csv, metavar="CODES", help="skip these codes/prefixes")
    check.add_argument("--enable", type=_csv, metavar="CODES", help="enable opt-in rules")
    check.add_argument(
        "--fail-on",
        choices=["error", "warning"],
        help="lowest severity that makes the exit status non-zero (default: error)",
    )
    check.add_argument("--exit-zero", action="store_true", help="always exit 0")
    color = check.add_mutually_exclusive_group()
    color.add_argument("--color", dest="color", action="store_true", default=None)
    color.add_argument("--no-color", dest="color", action="store_false")

    rules = sub.add_parser("rules", help="list available rules")
    rules.add_argument("--json", action="store_true")
    rules.add_argument("--config", metavar="PATH")

    explain = sub.add_parser("explain", help="show documentation for a rule")
    explain.add_argument("rule", help="rule code or name, e.g. LX101 or hallucinated-api")

    install = sub.add_parser("install-hook", help="install lintex as a git or Claude Code hook")
    install.add_argument("target", choices=["git", "claude-code"])
    install.add_argument("--force", action="store_true", help="overwrite an existing git hook")
    install.add_argument(
        "--scope",
        choices=["project", "local", "user"],
        default="project",
        help="Claude Code settings file to modify (default: project)",
    )

    hook = sub.add_parser("hook", help="entry points invoked by hooks (reads stdin)")
    hook.add_argument("target", choices=["claude-code"])
    hook.add_argument(
        "--fail-on",
        choices=["error", "warning"],
        default="warning",
        help="lowest severity reported back to the agent (default: warning)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or (
        args_list[0] not in COMMANDS and args_list[0] not in ("-h", "--help", "--version")
    ):
        args_list.insert(0, "check")
    args = build_parser().parse_args(args_list)
    try:
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "rules":
            return _cmd_rules(args)
        if args.command == "explain":
            return _cmd_explain(args)
        if args.command == "install-hook":
            return _cmd_install(args)
        if args.command == "hook":
            return run_claude_code_hook(sys.stdin, sys.stderr, Severity.parse(args.fail_on))
    except (ConfigError, GitError) as exc:
        print(f"lintex: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_USAGE  # pragma: no cover


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    changes = {}
    if args.select:
        changes["select"] = args.select
    if args.ignore:
        changes["ignore"] = config.ignore + args.ignore
    if args.enable:
        changes["enable"] = config.enable + args.enable
    if args.fail_on:
        changes["fail_on"] = Severity.parse(args.fail_on)
    return dataclasses.replace(config, **changes) if changes else config


def _cmd_check(args: argparse.Namespace) -> int:
    if args.staged:
        root = repo_root()
        config = _apply_overrides(load_config(root, args.config), args)
        linter = Linter(config)
        result = _lint_staged(linter, root)
    else:
        paths = args.paths or ["."]
        config = _apply_overrides(load_config(paths[0], args.config), args)
        linter = Linter(config)
        result = linter.lint_paths(paths)

    fmt = "json" if args.json else (args.format or "pretty")
    style = Style.detect(sys.stdout, color=args.color)
    print(render(result, fmt, style, verbose=args.verbose))
    if fmt == "pretty" and result.violations and config.source is None:
        print(
            dim_note(
                "No lintex config found. Tune rules or exclude paths with a [tool.lintex] "
                "table in pyproject.toml (see `lintex rules`).",
                style,
            )
        )
    if args.exit_zero:
        return EXIT_OK
    return EXIT_VIOLATIONS if result.failed(config.fail_on) else EXIT_OK


def _lint_staged(linter: Linter, root: Path) -> LintResult:
    import time

    start = time.perf_counter()
    result = LintResult()
    for rel in staged_python_files(root):
        if linter.config.is_excluded(root / rel):
            continue
        result.files_checked += 1
        result.violations.extend(linter.lint_source(staged_content(root, rel), rel))
    result.duration_ms = (time.perf_counter() - start) * 1000
    return result


def _cmd_rules(args: argparse.Namespace) -> int:
    config = load_config(None, args.config)
    rows = []
    for rule in ALL_RULES:
        rows.append(
            {
                "code": rule.code,
                "name": rule.name,
                "severity": rule.default_severity.value,
                "enabled": is_rule_active(rule, config),
                "default_enabled": rule.default_enabled,
                "summary": rule.summary,
                "options": rule.options,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    style = Style.detect(sys.stdout)
    width = max(len(r["name"]) for r in rows)
    group = ""
    for r in rows:
        if r["code"][:3] != group:
            group = r["code"][:3]
            print(style.paint(_GROUP_TITLES.get(group, ""), "bold"))
        if r["enabled"]:
            state = style.paint(" on ", "green")
        else:
            state = style.paint("off ", "dim")
        sev = style.paint(r["severity"].ljust(7), "red" if r["severity"] == "error" else "yellow")
        name = r["name"].ljust(width)
        note = "" if r["default_enabled"] else style.paint("  (opt-in: add to `enable`)", "dim")
        print(
            f"  {style.paint(r['code'], 'cyan', 'bold')}  {name}  {state} {sev} {r['summary']}{note}"
        )
    print(style.paint("\nRun `lintex explain CODE` for a rule's rationale and options.", "dim"))
    return EXIT_OK


_GROUP_TITLES = {
    "LX0": "Parsing",
    "LX1": "Hallucinated imports and APIs",
    "LX2": "Banned and insecure patterns",
    "LX3": "Agent bloat",
    "LX4": "Agent artifacts",
}


def _cmd_explain(args: argparse.Namespace) -> int:
    rule = get_rule(args.rule)
    if rule is None:
        print(
            f"lintex: unknown rule {args.rule!r}; run `lintex rules` to list them", file=sys.stderr
        )
        return EXIT_USAGE
    style = Style.detect(sys.stdout)
    sev = rule.default_severity.value
    badge = style.paint(f" {sev} ", "bold", "red" if sev == "error" else "yellow")
    optin = "" if rule.default_enabled else style.paint("  opt-in", "dim")
    print(
        f"{style.paint(rule.code, 'cyan', 'bold')} {style.paint(rule.name, 'bold')}  {badge}{optin}\n"
    )
    print(textwrap.fill(rule.summary, style.width) + "\n")
    if rule.explanation:
        for para in rule.explanation.split("\n\n"):
            if para.startswith("    "):
                print(para + "\n")  # preformatted example
            else:
                print(textwrap.fill(para, style.width) + "\n")
    if rule.options:
        print(style.paint(f"Options  [tool.lintex.rules.{rule.name}]", "bold"))
        for key, value in rule.options.items():
            print(f"  {style.paint(key, 'cyan')} = {json.dumps(value)}")
        print()
    print(style.paint(f"Silence one line with  # lintex: ignore[{rule.code}]", "dim"))
    return EXIT_OK


def _cmd_install(args: argparse.Namespace) -> int:
    if args.target == "git":
        hook = install_git_hook(force=args.force)
        print(f"Installed git pre-commit hook at {hook}")
        return EXIT_OK
    project = Path.cwd()
    try:
        project = repo_root()
    except GitError:
        pass
    path, changed = install_claude_code_hook(project, scope=args.scope)
    if changed:
        print(f"Added lintex PostToolUse hook to {path}")
    else:
        print(f"lintex hook already present in {path}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
