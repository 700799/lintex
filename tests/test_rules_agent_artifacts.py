from conftest import codes, lint

from lintex import Severity


def test_elided_code_comments():
    for comment in (
        "# ... rest of the code unchanged ...",
        "# ... existing code ...",
        "# Rest of the implementation remains the same",
        "# (previous methods omitted for brevity)",
    ):
        [v] = lint(f"def f():\n    x = 1\n    {comment}\n    return x\n")
        assert v.code == "LX401" and v.severity is Severity.ERROR, comment
        assert "git" in v.fix


def test_bare_ellipsis_comment_only_between_code():
    assert codes("x = 1\n# ...\ny = 2\n") == ["LX401"]
    assert codes("# Example:\n#   foo()\n# ...\n#   bar()\nx = 1\n") == []


def test_stub_markers_are_warnings():
    [v] = lint("def f():\n    # TODO: implement\n    return None\n")
    assert v.code == "LX401" and v.severity is Severity.WARNING
    assert codes("def f():\n    pass  # your code goes here\n") == ["LX401"]


def test_ordinary_comments_pass():
    src = """
        # The rest of the function assumes the list is sorted.
        # The code here handles the leap year case.
        # Keep compatibility with existing code paths.
        s = "# ... rest of the code unchanged"
    """
    assert codes(src) == []


def test_swallowed_and_bare_except():
    src = """
        def f():
            try:
                g()
            except Exception:
                pass
            try:
                g()
            except:
                log()
            try:
                g()
            except ValueError:
                pass
            try:
                g()
            except:
                pass
    """
    assert [(v.code, v.line) for v in lint(src)] == [("LX402", 5), ("LX403", 9), ("LX402", 17)]
