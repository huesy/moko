"""The risky parts of the custom terminal framework are pure — test them hard."""

from moko.tui.editor import Editor, wrapped_rows
from moko.tui.keys import (
    BACKSPACE,
    DELETE,
    ENTER,
    EOF,
    INTERRUPT,
    LEFT,
    NEWLINE,
    RIGHT,
    TEXT,
    UP,
    Key,
    KeyDecoder,
)


def kinds(data: bytes) -> list[str]:
    return [k.kind for k in KeyDecoder().feed(data)]


# ── key decoding ──────────────────────────────────────────────────────
def test_plain_text():
    assert KeyDecoder().feed(b"hi") == [Key(TEXT, "hi")]


def test_enter_and_backspace_and_signals():
    assert kinds(b"\r") == [ENTER]  # CR = Enter key (ICRNL off)
    assert kinds(b"\n") == [NEWLINE]  # LF = Ctrl+J, the universal newline key
    assert kinds(b"\x7f") == [BACKSPACE]
    assert kinds(b"\x03") == [INTERRUPT]
    assert kinds(b"\x04") == [EOF]


def test_arrows_and_edits():
    assert kinds(b"\x1b[D") == [LEFT]
    assert kinds(b"\x1b[C") == [RIGHT]
    assert kinds(b"\x1b[A") == [UP]
    assert kinds(b"\x1b[3~") == [DELETE]


def test_kitty_shift_enter_with_numlock():
    # The bug that sank the static approach: Shift+Enter with NumLock on is
    # \x1b[13;130u (130 = 1 + shift + numlock). Lock bits masked → NEWLINE.
    assert kinds(b"\x1b[13;130u") == [NEWLINE]


def test_kitty_plain_enter_and_ctrl_with_locks():
    assert kinds(b"\x1b[13u") == [ENTER]  # code 13, no mods
    assert kinds(b"\x1b[99;133u") == [INTERRUPT]  # Ctrl+C + NumLock
    assert kinds(b"\x1b[100;5u") == [EOF]  # Ctrl+D


def test_kitty_key_release_ignored():
    assert kinds(b"\x1b[97;1:3u") == []  # event type 3 = release


def test_split_escape_sequence_buffers():
    d = KeyDecoder()
    assert d.feed(b"\x1b[13") == []  # final byte not yet arrived
    assert [k.kind for k in d.feed(b";2u")] == [NEWLINE]


def test_split_utf8_buffers():
    d = KeyDecoder()
    raw = "é".encode()  # two bytes
    assert d.feed(raw[:1]) == []  # truncated multi-byte → wait
    assert d.feed(raw[1:]) == [Key(TEXT, "é")]


# ── edit buffer ───────────────────────────────────────────────────────
def test_insert_backspace_cursor():
    ed = Editor()
    ed.insert("abc")
    assert (ed.text, ed.cursor) == ("abc", 3)
    ed.backspace()
    assert (ed.text, ed.cursor) == ("ab", 2)


def test_cursor_moves_and_midline_insert():
    ed = Editor()
    ed.insert("ac")
    ed.left()
    ed.insert("b")  # insert between a and c
    assert ed.text == "abc"


def test_multiline_geometry():
    ed = Editor()
    ed.insert("ab")
    ed.newline()
    ed.insert("c")
    assert ed.lines == ["ab", "c"]
    assert ed.cursor_line_col == (1, 1)
    ed.home()
    assert ed.cursor_line_col == (1, 0)


def test_backslash_continuation():
    ed = Editor()
    ed.insert("foo\\")
    assert ed.wants_continue
    ed.continue_line()
    assert ed.text == "foo\n" and ed.cursor == 4 and not ed.wants_continue


def test_wrapped_rows():
    assert wrapped_rows(10, 5) == 1
    assert wrapped_rows(10, 10) == 1
    assert wrapped_rows(10, 11) == 2
    assert wrapped_rows(10, 0) == 1  # an empty line still occupies a row
    assert wrapped_rows(10, 5, 15) == 1 + 2
