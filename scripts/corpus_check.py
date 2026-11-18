#!/usr/bin/env python3
"""Run lintex over a fixed set of widely used packages and compare with a baseline.

Unit tests show a rule fires where it should. They cannot show that it stays quiet
everywhere else. This script lints pinned releases of mature, well-reviewed packages
and counts findings per rule. Their code does not change, so any change in the counts
comes from lintex: a jump means a rule or knowledge entry started flagging real-world
code it did not flag before, which is almost always a false positive.

    python scripts/corpus_check.py            # compare with tests/corpus/baseline.json
    python scripts/corpus_check.py --update   # accept the current counts as the baseline
    python scripts/corpus_check.py --show LX201   # print the findings behind one code

The check fails when any (package, rule) count rises above its baseline. A drop passes
but is reported, so the baseline can be tightened with --update.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE.parent / "tests" / "corpus" / "baseline.json"

# Pure-Python wheels, pinned so the code under test never changes. Chosen for breadth:
# web, CLI, data modelling, HTTP, templating, ORM-style SQL, terminal output, packaging.
PACKAGES = {
    "attrs": "24.2.0",
    "click": "8.1.7",
    "flask": "3.0.3",
    "httpx": "0.27.2",
    "jinja2": "3.1.4",
    "requests": "2.32.3",
    "rich": "13.9.2",
    "sqlalchemy": "2.0.35",
    "packaging": "24.1",
    "werkzeug": "3.0.4",
}
# Rules about style and size are a matter of taste in someone else's codebase; the
# corpus is for the rules that claim something is *wrong*.
SELECT = ["LX0", "LX1", "LX2", "LX4"]


def download(dest: Path) -> None:
    specs = [f"{name}=={version}" for name, version in PACKAGES.items()]
    wheels = dest / "_wheels"
    subprocess.run(
        [
            sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
            "--only-binary", ":all:", "--implementation", "py", "--abi", "none",
            "--platform", "any", "--dest", str(wheels), *specs,
        ],
        check=True,
    )  # fmt: skip
    for wheel in sorted(wheels.glob("*.whl")):
        name = wheel.name.split("-")[0].lower()
        with zipfile.ZipFile(wheel) as archive:
            members = [m for m in archive.namelist() if m.endswith(".py")]
            archive.extractall(dest / name, members)


def lint(root: Path) -> dict[str, list[dict]]:
    """Findings per package, linted with default configuration."""
    findings: dict[str, list[dict]] = {}
    for package in sorted(p.name for p in root.iterdir() if not p.name.startswith("_")):
        proc = subprocess.run(
            [
                sys.executable, "-m", "lintex", "check", package, "--format", "json",
                "--select", ",".join(SELECT),
            ],
            cwd=root, capture_output=True, text=True,
        )  # fmt: skip
        if proc.returncode not in (0, 1):
            sys.exit(f"lintex failed on {package}:\n{proc.stderr}")
        findings[package] = json.loads(proc.stdout)["violations"]
    return findings


def counts(findings: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for package, violations in findings.items():
        per_code: dict[str, int] = {}
        for v in violations:
            per_code[v["code"]] = per_code.get(v["code"], 0) + 1
        out[package] = dict(sorted(per_code.items()))
    return out


def compare(current: dict, baseline: dict) -> tuple[list[str], list[str]]:
    worse, better = [], []
    for package in sorted(set(current) | set(baseline)):
        now, then = current.get(package, {}), baseline.get(package, {})
        for code in sorted(set(now) | set(then)):
            a, b = now.get(code, 0), then.get(code, 0)
            if a != b:
                (worse if a > b else better).append(f"{package:<12} {code}  {b} -> {a}")
    return worse, better


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="write the current counts")
    parser.add_argument("--show", metavar="CODE", help="print the findings for one rule code")
    parser.add_argument("--dir", type=Path, help="reuse an already downloaded corpus")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = args.dir or Path(tmp)
        if not args.dir or not any(root.iterdir()):
            root.mkdir(parents=True, exist_ok=True)
            download(root)
        findings = lint(root)

    if args.show:
        for package, violations in findings.items():
            for v in violations:
                if v["code"] == args.show.upper():
                    print(f"{package}/{v['path']}:{v['line']}  {v['message']}\n    {v['snippet']}")
        return 0

    current = counts(findings)
    total = sum(sum(c.values()) for c in current.values())
    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"packages": PACKAGES, "select": SELECT, "counts": current}
        BASELINE.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Baseline written: {total} findings across {len(current)} packages.")
        return 0

    saved = json.loads(BASELINE.read_text())
    if saved.get("packages") != PACKAGES or saved.get("select") != SELECT:
        print("The corpus definition changed; rerun with --update and review the diff.")
        return 1
    worse, better = compare(current, saved["counts"])
    print(f"{total} findings across {len(current)} packages.")
    if better:
        print("\nFewer findings than the baseline (run --update to lock this in):")
        print("\n".join(f"  {line}" for line in better))
    if worse:
        print("\nMore findings than the baseline:")
        print("\n".join(f"  {line}" for line in worse))
        print(
            "\nInspect them with `python scripts/corpus_check.py --show CODE`. If they are "
            "real problems in that package, accept them with --update; if they are false "
            "positives, fix the rule."
        )
        return 1
    print("No increase over the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
