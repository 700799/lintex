from conftest import codes, lint, requires_stdlib_names

from lintex import Severity


def test_removed_stdlib_module_and_names():
    assert codes("import distutils.core\n") == ["LX101"]
    assert codes("from collections import Mapping\n") == ["LX101"]
    assert codes("import imp\n") == ["LX101"]
    [v] = lint("from collections import Mapping\n")
    assert "collections.abc" in v.fix


def test_removed_third_party_apis_via_alias():
    src = """
        import numpy as np
        import openai
        from pydantic import BaseSettings

        x = np.float(1)
        openai.ChatCompletion.create(model="m", messages=[])
    """
    vs = lint(src)
    assert [(v.code, v.line) for v in vs] == [("LX101", 4), ("LX101", 6), ("LX101", 7)]
    assert "pydantic_settings" in vs[0].fix
    assert "client.chat.completions.create" in vs[2].fix


def test_deprecated_api_is_a_warning():
    [v] = lint("from datetime import datetime\nnow = datetime.utcnow()\n")
    assert v.code == "LX101"
    assert v.severity is Severity.WARNING
    assert "timezone.utc" in v.fix


def test_python2_methods():
    assert codes("d = {}\nfor k, v in d.iteritems():\n    pass\n") == ["LX101"]
    assert codes("class T:\n    def t(self):\n        self.assertEquals(1, 1)\n") == ["LX101"]


def test_method_defined_in_file_is_not_flagged():
    src = """
        class Box:
            def iteritems(self):
                return []
            def all(self):
                return list(self.iteritems())
    """
    assert codes(src) == []


def test_compat_guards_are_respected():
    src = """
        import sys
        try:
            from collections import Mapping
        except ImportError:
            from collections.abc import Mapping
        if sys.version_info < (3, 0):
            import urllib2
    """
    assert codes(src) == []


@requires_stdlib_names
def test_nonexistent_stdlib_names_with_suggestion():
    vs = lint("""
        import os
        from os.path import joinpath
        from typing import Json
        os.path.joinn("a")
        import json.encoderz
    """)
    assert [(v.code, v.line) for v in vs] == [
        ("LX102", 3),
        ("LX102", 4),
        ("LX102", 5),
        ("LX102", 6),
    ]
    assert "`os.path.join`" in vs[2].fix


@requires_stdlib_names
def test_existing_stdlib_names_pass():
    src = """
        import os
        import xml.etree.ElementTree as ET
        from concurrent import futures
        from datetime import datetime, timezone
        os.path.join("a", "b")
        ET.fromstring("<a/>")
        datetime.now(timezone.utc).isoformat()
        os.environ.get("X")
    """
    assert codes(src) == []


@requires_stdlib_names
def test_platform_specific_name_is_only_a_warning():
    [v] = lint("import os\nos.startfile_please('x')\n")
    assert v.severity is Severity.WARNING
    assert "sys.platform" in v.fix


def test_unresolvable_import_opt_in(tmp_path, monkeypatch):
    (tmp_path / "localmod.py").write_text("")
    monkeypatch.chdir(tmp_path)
    src = "import localmod\nimport pytest\nimport fastjsonx_not_real\n"
    vs = lint(src, path=str(tmp_path / "x.py"), enable=["LX103"])
    assert [(v.code, v.line) for v in vs] == [("LX103", 3)]
    known = {
        "enable": ["LX103"],
        "rules": {"unresolvable-import": {"known-modules": ["fastjsonx_not_real"]}},
    }
    assert lint(src, path=str(tmp_path / "x.py"), **known) == []
