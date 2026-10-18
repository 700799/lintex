"""Command-line interface.

lintex [check] [PATHS...] [--json] [--staged]   lint files (default: .)
lintex rules [--json]                            list rules
lintex explain CODE                              document a rule
lintex report [--since 24h] [--session ID]        what the hook caught and what got fixed
lintex statusline [--segment]                    Claude Code status line segment
lintex install-hook git|claude-code|statusline   set up automatic checks
lintex hook claude-code                          Claude Code PostToolUse entry point
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from lintex import __version__, ledger
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
from lintex.hooks.statusline import (
    install_statusline,
    read_payload,
    render_segment,
    render_statusline,
)
from lintex.models import Severity
from lintex.reporters import FORMATS, Style, dim_note, render
from lintex.rules import ALL_RULES, get_rule, is_rule_active

COMMANDS = ("check", "rules", "explain", "report", "statusline", "install-hook", "hook")
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

    _add_ledger_parsers(sub)

    install = sub.add_parser(
        "install-hook", help="install lintex as a git hook, Claude Code hook or status line"
    )
    install.add_argument("target", choices=["git", "claude-code", "statusline"])
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


def _add_ledger_parsers(sub: argparse._SubParsersAction) -> None:
    """`report` and `statusline`: the commands that read the session ledger."""
    report = sub.add_parser("report", help="what the Claude Code hook caught, and what got fixed")
    report.add_argument("paths", nargs="?", default=".", help="a path inside the project")
    report.add_argument("--since", default="24h", help="look-back window, e.g. 90m, 24h, 7d")
    report.add_argument("--all", action="store_true", help="the whole ledger, ignoring --since")
    report.add_argument("--session", metavar="ID", help="only this Claude Code session")
    report.add_argument("--json", action="store_true")
    report.add_argument("--clear", action="store_true", help="delete this project's ledger")
    rcolor = report.add_mutually_exclusive_group()
    rcolor.add_argument("--color", dest="color", action="store_true", default=None)
    rcolor.add_argument("--no-color", dest="color", action="store_false")

    status = sub.add_parser("statusline", help="Claude Code status line (reads payload on stdin)")
    status.add_argument(
        "--segment", action="store_true", help="print only the lintex segment, for embedding"
    )
    scolor = status.add_mutually_exclusive_group()
    scolor.add_argument("--color", dest="color", action="store_true", default=None)
    scolor.add_argument("--no-color", dest="color", action="store_false")


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
        if args.command == "report":
            return _cmd_report(args)
        if args.command == "statusline":
            # Claude Code renders ANSI in the status line even though stdout is a
            # pipe, so colour defaults to on here (NO_COLOR and --no-color still win).
            color = args.color if args.color is not None else "NO_COLOR" not in os.environ
            style = Style.detect(sys.stdout, color=color)
            print(render_statusline(read_payload(sys.stdin), style, segment_only=args.segment))
            return EXIT_OK
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


def _cmd_report(args: argparse.Namespace) -> int:
    root = load_config(args.paths).root
    style = Style.detect(sys.stdout, color=args.color)
    if args.clear:
        removed = ledger.clear(root)
        print("Ledger cleared." if removed else "No ledger for this project.")
        return EXIT_OK
    since = None
    if not args.all:
        try:
            since = datetime.now(timezone.utc) - ledger.parse_since(args.since)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
    summary = ledger.summarize(ledger.load(root, since=since, session=args.session))
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
        return EXIT_OK
    scope = "all time" if args.all else f"last {args.since}"
    if args.session:
        scope += f", session {args.session[:8]}"
    print(
        style.paint(
            f"lintex ledger {style.sym['dot']} {root.name} {style.sym['dot']} {scope}", "bold"
        )
    )
    if summary.runs == 0:
        print(
            dim_note(
                "Nothing recorded yet. The ledger fills as the Claude Code hook checks edits "
                "(`lintex install-hook claude-code`).",
                style,
            )
        )
        return EXIT_OK
    print(render_segment(summary, style))
    print(_report_headline(summary, style))
    if summary.by_code:
        print()
        width = max(len(s.rule) for s in summary.by_code.values())
        ranked = sorted(summary.by_code.items(), key=lambda kv: -(kv[1].caught + kv[1].open))
        for code, stats in ranked:
            state = (
                style.paint("all fixed", "green")
                if stats.open == 0
                else style.paint(f"{stats.open} open", "red" if stats.open else "dim")
            )
            print(
                f"  {style.paint(code, 'cyan')} {stats.rule.ljust(width)}  "
                f"caught {stats.caught:>3}  fixed {stats.fixed:>3}  {state}"
            )
    if summary.open_findings:
        print()
        print(style.paint("Still open", "bold"))
        for f in summary.open_findings[:20]:
            sev = style.paint(
                style.sym[f.get("severity", "error")],
                "red" if f.get("severity") == "error" else "yellow",
            )
            print(
                f"  {sev} {f.get('path')}:{f.get('line')}  {style.paint(f['code'], 'cyan')} {f.get('message')}"
            )
        if len(summary.open_findings) > 20:
            print(style.paint(f"  … and {len(summary.open_findings) - 20} more", "dim"))
    return EXIT_OK


def _report_headline(summary: ledger.Summary, style: Style) -> str:
    runs = f"{summary.runs} edit{'s' if summary.runs != 1 else ''}"
    files = f"{summary.files} file{'s' if summary.files != 1 else ''}"
    caught = style.paint(f"{summary.caught} caught", "bold")
    fixed = style.paint(f"{summary.fixed} fixed by the agent", "green", "bold")
    if summary.open:
        open_ = style.paint(f"{summary.open} still open", "red", "bold")
    else:
        open_ = style.paint("nothing open", "green")
    return (
        f"{caught} across {runs} of {files} {style.sym['dot']} {fixed} {style.sym['dot']} {open_}"
    )


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
    if args.target == "statusline":
        path, changed = install_statusline(project, scope=args.scope)
        if changed:
            print(f"Set Claude Code statusLine to `lintex statusline` in {path}")
        else:
            print(f"lintex status line already configured in {path}")
        return EXIT_OK
    path, changed = install_claude_code_hook(project, scope=args.scope)
    if changed:
        print(f"Added lintex PostToolUse hook to {path}")
    else:
        print(f"lintex hook already present in {path}")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
