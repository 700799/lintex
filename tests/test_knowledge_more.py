"""Python-2 builtins, third-party migration entries, and the PyPI stub sync."""

import json
import textwrap

from conftest import codes, lint

from lintex import knowledge
from lintex.knowledge import sync
from lintex.models import Autofix, Severity

KB = knowledge.load()


def test_py2_builtins_are_flagged_with_rewrites():
    vs = lint("for i in xrange(3):\n    name = unicode(raw_input())\n")
    assert [(v.code, v.line, v.col) for v in vs] == [
        ("LX101", 1, 10),
        ("LX101", 2, 12),
        ("LX101", 2, 20),
    ]
    assert vs[0].autofix == Autofix(1, 10, 16, "range", "xrange")
    assert vs[1].autofix == Autofix(2, 12, 19, "str", "unicode")
    assert vs[2].autofix == Autofix(2, 20, 29, "input", "raw_input")
    assert all(v.severity is Severity.ERROR for v in vs)
    [v] = lint("total = reduce(lambda a, b: a + b, [1, 2])\n")
    assert v.autofix is None and "functools.reduce" in v.fix


def test_py2_builtins_respect_definitions_imports_and_guards():
    assert codes("def xrange(n):\n    return range(n)\nxrange(3)\n") == []
    assert codes("from six.moves import xrange\nxrange(3)\n") == []
    assert codes("unicode = str\nunicode('x')\n") == []
    assert codes("try:\n    unicode\nexcept NameError:\n    unicode = str\n") == []
    assert codes("def f(long):\n    return long\n") == []
    assert codes("x = {}\nx.file = 1\n") == []  # attribute, not a bare name


def test_py2_stdlib_modules_and_string_functions():
    vs = lint("import md5\nimport string\nstring.join(['a'], '')\nimport BaseHTTPServer\n")
    assert [v.line for v in vs] == [1, 3, 4]
    assert "hashlib.md5" in vs[0].fix and "sep.join(parts)" in vs[1].fix
    assert "http.server" in vs[2].fix


def test_third_party_migration_entries():
    vs = lint(
        textwrap.dedent(
            """
            import openai, pydantic, tensorflow as tf
            from langchain.chat_models import ChatOpenAI
            from pydantic import parse_obj_as
            openai.api_key = "k"
            openai.Audio.transcribe("whisper-1", f)
            sess = tf.Session()
            opt = tf.train.AdamOptimizer(0.1)
            m = Model.parse_obj(data)
            """
        )
    )
    found = {(v.line, v.message.split("`")[1]): v for v in vs}
    assert (3, "langchain.chat_models.ChatOpenAI") in found  # longest prefix wins
    assert (4, "pydantic.parse_obj_as") in found
    assert found[(5, "openai.api_key")].severity is Severity.WARNING
    assert "OPENAI_API_KEY" in found[(5, "openai.api_key")].fix
    assert (6, "openai.Audio") in found and "audio.transcriptions" in found[(6, "openai.Audio")].fix
    assert (7, "tensorflow.Session") in found
    assert (8, "tensorflow.train.AdamOptimizer") in found
    assert found[(9, ".parse_obj()")].autofix.replacement == "model_validate"


def test_knowledge_counts_include_builtins_and_pack_is_well_formed():
    counts = KB.counts()
    assert counts["py2"] >= 90 and len(KB.builtins) >= 15
    names = [e.name for e in KB.apis.values()]
    assert len(names) == len(set(names))
    assert all(e.fix for e in KB.builtins.values())


def test_generated_pack_includes_pypi_stub_deprecations():
    src = KB.sources["typeshed"]
    assert any(w.startswith("types_requests") for w in src.get("stub_packages", [])), (
        "regenerate with `python -m lintex.knowledge.sync --from-mypy --stub-packages default`"
    )
    third_party = [e for e in KB.apis.values() if e.source == "typeshed" and not e.is_python]
    assert len(third_party) >= 50
    assert {e.package for e in third_party} >= {"docutils", "setuptools", "jsonschema"}


def test_sync_reads_unpacked_stub_wheels(tmp_path):
    unpacked = tmp_path / "types-foo" / "unpacked"
    (unpacked / "foo-stubs").mkdir(parents=True)
    (unpacked / "foo_stubs-1.0.dist-info").mkdir()
    (unpacked / "foo-stubs" / "__init__.pyi").write_text(
        'from typing_extensions import deprecated\n@deprecated("Use bar()")\ndef old() -> None: ...\n'
    )
    entries = sync._third_party(unpacked, package="foo")
    assert entries == [
        {"name": "foo-stubs.old", "status": "deprecated", "package": "foo", "fix": "Use bar()"}
    ]
    typeshed = tmp_path / "ts"
    (typeshed / "stdlib").mkdir(parents=True)
    (typeshed / "stdlib" / "VERSIONS").write_text("gone: 3.0-3.11\nnewest: 3.12-\n")
    data = sync.build(typeshed, via="t", stub_dirs=[(unpacked, "foo", "types_foo-1.0.whl")])
    assert data["source"]["stub_packages"] == ["types_foo-1.0.whl"]
    assert {e["name"] for e in data["api"]} == {"gone", "foo-stubs.old"}
    json.dumps(data)
