"""Guards for `moko chat` rendering helpers (the live terminal loop is
exercised manually — it needs a real tty)."""

from types import SimpleNamespace

import moko.cli.chat as chat


def _config(*, expects_daemon: bool):
    return SimpleNamespace(runtime=SimpleNamespace(expects_daemon=expects_daemon))


def test_fmt_tool_summarises_remember():
    out = chat._fmt_tool(
        {"tool": "remember", "args": {"subjects": [1, 2], "speaker_sentiment": {"label": "x"}}}
    )
    assert "2 subjects" in out and "sentiment" in out


def test_fmt_tool_counts_recall_hits():
    out = chat._fmt_tool({"tool": "recall", "args": {"query": "Lisbon"}, "result": "- a\n- b"})
    assert "recall 'Lisbon'" in out and "2 hit(s)" in out


def test_fmt_tool_escapes_markup():
    out = chat._fmt_tool({"tool": "lookup", "args": {"name": "[bold]x"}, "result": "ok"})
    assert "\\[bold]" in out  # escaped, not a live tag


def test_status_bar_shows_shortcuts_and_mode():
    bar = chat._status("verbose")
    assert "send" in bar and "verbose" in bar


def test_thinking_line_is_a_moko_line():
    line = chat._thinking("⠋")
    assert "moko" in line and "thinking" in line


def test_no_daemon_does_not_block_chat(monkeypatch):
    monkeypatch.setattr(chat, "is_daemon_live", lambda: False)
    assert chat._refuse_if_daemon_live(_config(expects_daemon=False)) is False


def test_live_daemon_on_host_warns_but_proceeds(monkeypatch, capsys):
    monkeypatch.setattr(chat, "is_daemon_live", lambda: True)
    abort = chat._refuse_if_daemon_live(_config(expects_daemon=False))
    assert abort is False  # host: warn loudly, keep going
    out = capsys.readouterr().out
    assert "already awake" in out  # the loud warning fired
    assert "Telegram" in out and "service stop" in out  # names the alternatives


def test_live_daemon_in_container_refuses(monkeypatch, capsys):
    monkeypatch.setattr(chat, "is_daemon_live", lambda: True)
    abort = chat._refuse_if_daemon_live(_config(expects_daemon=True))
    assert abort is True  # container: refuse — the daemon owns the store
    assert "already awake" in capsys.readouterr().out
