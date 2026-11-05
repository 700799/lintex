"""Terminal-facing behaviour of the CLI: colour flags and pipes closing early."""

import io

import pytest

from lintex.cli import main


class _ClosedPipe(io.StringIO):
    """A stdout whose reader has gone away: every write raises BrokenPipeError."""

    def write(self, s):
        raise BrokenPipeError(32, "Broken pipe")

    def fileno(self):
        return 1


@pytest.mark.parametrize("argv", [["rules"], ["explain", "LX101"], ["check", "src/lintex/cli.py"]])
def test_broken_pipe_exits_quietly(argv, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout", _ClosedPipe())
    # No traceback, no non-zero exit: `lintex ... | head` is a normal thing to do.
    assert main(argv) == 0
    assert "Traceback" not in capsys.readouterr().err


def test_keyboard_interrupt_is_reported_briefly(monkeypatch, capsys):
    def boom(args):
        raise KeyboardInterrupt

    monkeypatch.setattr("lintex.cli._cmd_rules", boom)
    assert main(["rules"]) == 130
    assert capsys.readouterr().err.strip() == "lintex: interrupted"


@pytest.mark.parametrize(
    "argv",
    [["rules"], ["explain", "LX101"], ["check", "--help"]],
)
def test_every_output_command_accepts_color_flags(argv, capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    if argv[-1] == "--help":
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
        assert "--no-color" in capsys.readouterr().out
        return
    assert main([*argv, "--no-color"]) == 0
    plain = capsys.readouterr().out
    assert "\033[" not in plain
    assert main([*argv, "--color"]) == 0
    assert "\033[" in capsys.readouterr().out


def test_explain_mentions_how_to_silence_the_rule(capsys):
    assert main(["explain", "swallowed-exception", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("LX402 swallowed-exception")
    assert "# lintex: ignore[LX402]" in out
