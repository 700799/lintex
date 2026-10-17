from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from lintex import Config, Violation, lint_source

requires_stdlib_names = pytest.mark.skipif(
    not hasattr(sys, "stdlib_module_names"), reason="LX102 needs Python 3.10+"
)


def lint(source: str, path: str = "example.py", **config) -> list[Violation]:
    cfg = Config.from_dict(config, root=Path.cwd()) if config else None
    return lint_source(textwrap.dedent(source), path, cfg)


def codes(source: str, **config) -> list[str]:
    return [v.code for v in lint(source, **config)]
