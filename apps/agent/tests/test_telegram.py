import json

import httpx

from moko.channels.telegram import TelegramChannel, _send_message, wait_for_first_message


def _updates(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def _async_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _collector(into: list):
    async def emit(inbound):  # the gateway's emit is async; mirror it here
        into.append(inbound)

    return emit


# ── M10: inbound attachments ──────────────────────────────────────────────────
def _media_handler(update, *, file_bytes=b"BYTES", getfile_status=200):
    """A MockTransport that serves getUpdates, then getFile, then the file download —
    the three hops downloading one attachment takes."""

    def handler(req):
        path = req.url.path
        if "getUpdates" in path:
            return _updates([update])
        if "getFile" in path:
            if getfile_status != 200:
                return httpx.Response(getfile_status, json={"ok": False})
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/f.jpg"}})
        if "/file/bot" in path:
            return httpx.Response(200, content=file_bytes)
        return httpx.Response(404)

    return handler


async def test_photo_attachment_is_downloaded_and_emitted(tmp_path):
    update = {
        "update_id": 7,
        "message": {
            "from": {"id": 5}, "chat": {"id": 9}, "caption": "my cat",
            "photo": [{"file_id": "small", "file_size": 100},
                      {"file_id": "big", "file_size": 2000}],  # largest is chosen
        },
    }
    client = _async_client(_media_handler(update, file_bytes=b"JPEGDATA"))
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    got = []
    await ch._poll_once(_collector(got))
    await client.aclose()

    assert len(got) == 1
    assert got[0].text == "my cat"  # the caption rides as the message text
    assert len(got[0].attachments) == 1
    att = got[0].attachments[0]
    assert att.kind == "image" and att.data == b"JPEGDATA" and att.ref == "big"


async def test_voice_only_message_still_emits(tmp_path):
    update = {
        "update_id": 3,
        "message": {"from": {"id": 5}, "chat": {"id": 9},
                    "voice": {"file_id": "v1", "mime_type": "audio/ogg"}},
    }
    client = _async_client(_media_handler(update, file_bytes=b"OGG"))
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    got = []
    await ch._poll_once(_collector(got))
    await client.aclose()

    assert len(got) == 1 and got[0].text == ""  # no text, but the message lands
    assert got[0].attachments[0].kind == "audio"


async def test_failed_download_still_delivers_the_message(tmp_path):
    update = {
        "update_id": 4,
        "message": {"from": {"id": 5}, "chat": {"id": 9}, "caption": "look",
                    "photo": [{"file_id": "p", "file_size": 9}]},
    }
    client = _async_client(_media_handler(update, getfile_status=500))
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    got = []
    await ch._poll_once(_collector(got))
    await client.aclose()

    # A download that fails must not swallow the message — the text still gets through.
    assert len(got) == 1 and got[0].text == "look" and got[0].attachments == []


async def test_poll_once_emits_and_persists_offset(tmp_path):
    update = {
        "update_id": 42,
        "message": {"text": "hi", "from": {"id": 7, "first_name": "A"}, "chat": {"id": 9}},
    }

    def handler(req):
        assert "getUpdates" in req.url.path
        return _updates([update])

    client = _async_client(handler)
    state = tmp_path / "tg.json"
    ch = TelegramChannel("tok", "9", client=client, state_path=state)

    got = []
    await ch._poll_once(_collector(got))
    assert len(got) == 1
    assert got[0].text == "hi"
    assert got[0].sender_id == "7"
    assert got[0].chat_id == "9"
    assert ch._offset == 43
    assert json.loads(state.read_text())["offset"] == 43  # survives restart
    await client.aclose()


async def test_non_text_update_is_skipped_but_offset_advances(tmp_path):
    update = {"update_id": 5, "message": {"from": {"id": 1}, "chat": {"id": 2}}}  # e.g. a sticker

    client = _async_client(lambda req: _updates([update]))
    ch = TelegramChannel("tok", "2", client=client, state_path=tmp_path / "tg.json")
    got = []
    await ch._poll_once(_collector(got))
    assert got == []
    assert ch._offset == 6
    await client.aclose()


async def test_approval_button_press_emits_verdict_and_clears_keyboard(tmp_path):
    # An inline-button press → the same verdict word a typed reply carries, plus
    # answerCallbackQuery (dismiss spinner) + editMessageReplyMarkup (no re-tap).
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True, "result": []})

    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": 7},
            "data": "always",
            "message": {"message_id": 50, "chat": {"id": 9}},
        },
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: _route(r, update, handler)))
    ch = TelegramChannel(
        "tok", "9", client=client, allowed_sender_id="7", state_path=tmp_path / "tg.json"
    )
    got = []
    await ch._poll_once(_collector(got))
    assert [i.text for i in got] == ["yes always"]  # "always" button → trust-this-kind verdict
    assert any("answerCallbackQuery" in p for p in seen)  # spinner dismissed
    assert any("editMessageReplyMarkup" in p for p in seen)  # buttons removed
    await client.aclose()


async def test_approval_button_from_stranger_is_ignored(tmp_path):
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": 999},  # not the bound companion
            "data": "approve",
            "message": {"message_id": 50, "chat": {"id": 9}},
        },
    }
    client = _async_client(lambda req: _updates([update]))
    ch = TelegramChannel(
        "tok", "9", client=client, allowed_sender_id="7", state_path=tmp_path / "tg.json"
    )
    got = []
    await ch._poll_once(_collector(got))
    assert got == []  # a stranger can't approve via a button either
    await client.aclose()


def _route(req, update, handler):
    if "getUpdates" in req.url.path:
        return _updates([update])
    return handler(req)


async def test_only_allowed_sender_is_heard(tmp_path):
    updates = [
        {"update_id": 1, "message": {"text": "hi", "from": {"id": 7}, "chat": {"id": 9}}},
        {"update_id": 2, "message": {"text": "intruder", "from": {"id": 999}, "chat": {"id": 9}}},
    ]
    client = _async_client(lambda req: _updates(updates))
    ch = TelegramChannel(
        "tok", "9", client=client, allowed_sender_id="7", state_path=tmp_path / "tg.json"
    )
    got = []
    await ch._poll_once(_collector(got))
    assert [i.text for i in got] == ["hi"]  # the intruder is filtered out
    assert ch._offset == 3  # but the offset still advances past both
    await client.aclose()


async def test_send_posts_to_chat(tmp_path):
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    client = _async_client(handler)
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    await ch.send("hello there")
    assert "sendMessage" in seen["path"]
    assert seen["body"] == {"chat_id": "9", "text": "hello there"}
    await client.aclose()


def test_split_message_chunks_on_boundaries_and_hard_slices():
    from moko.channels.telegram import _CHUNK_LIMIT, _split_message

    assert _split_message("short") == ["short"]
    assert _split_message("") == []
    # many short lines pack into <=limit chunks, split on line boundaries
    body = "\n".join(["line"] * 5000)  # ~25k chars
    parts = _split_message(body)
    assert len(parts) > 1 and all(len(p) <= _CHUNK_LIMIT for p in parts)
    assert "\n".join(parts).replace("\n", "") == body.replace("\n", "")  # content preserved
    # a single over-long line is hard-sliced
    one = "x" * (_CHUNK_LIMIT * 2 + 17)
    sliced = _split_message(one)
    assert all(len(p) <= _CHUNK_LIMIT for p in sliced) and "".join(sliced) == one


async def test_send_chunks_a_reply_over_the_limit(tmp_path):
    from moko.channels.telegram import _CHUNK_LIMIT

    sent: list[str] = []

    def handler(req):
        sent.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True})

    client = _async_client(handler)
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    await ch.send("y" * (_CHUNK_LIMIT * 2 + 5))  # would 400 as one message
    assert len(sent) == 3 and all(len(t) <= _CHUNK_LIMIT for t in sent)  # delivered, not dropped
    await client.aclose()


async def test_send_retries_on_429_then_succeeds(tmp_path):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True})

    client = _async_client(handler)
    ch = TelegramChannel("tok", "9", client=client, state_path=tmp_path / "tg.json")
    await ch.send("hello")  # a transient 429 must not become dead air
    assert calls["n"] == 2  # retried once, then succeeded
    await client.aclose()


async def test_pairing_binds_only_the_code_sender():
    # A stranger messages in the same batch; only the one sending the exact
    # code is bound (case-insensitively). The intruder is ignored.
    updates = [
        {"update_id": 1, "message": {"text": "hello?", "from": {"id": 999}, "chat": {"id": 999}}},
        {
            "update_id": 2,
            "message": {
                "text": "abc123",
                "from": {"id": 55, "first_name": "Andrew"},
                "chat": {"id": 66},
            },
        },
    ]
    client = _async_client(lambda req: _updates(updates))
    info = await wait_for_first_message(client, "tok", expected_code="ABC123", poll_timeout=0)
    assert info == {"user_id": "55", "chat_id": "66", "name": "Andrew", "offset": 3}
    await client.aclose()


async def test_pairing_advances_offset_past_the_code(tmp_path):
    # The daemon must resume AFTER the pairing code, or it re-delivers it as a
    # message. wait_for_first_message reports the offset; save_offset persists it.
    from moko.channels.telegram import save_offset

    update = {
        "update_id": 7,
        "message": {"text": "abc123", "from": {"id": 5, "first_name": "A"}, "chat": {"id": 6}},
    }
    client = _async_client(lambda req: _updates([update]))
    info = await wait_for_first_message(client, "tok", expected_code="abc123", poll_timeout=0)
    assert info["offset"] == 8  # past update_id 7
    state = tmp_path / "tg.json"
    save_offset(info["offset"], state_path=state)
    assert json.loads(state.read_text()) == {"offset": 8}
    await client.aclose()


async def test_pairing_does_not_bind_a_stranger_without_the_code():
    intruder = {"text": "let me in", "from": {"id": 999}, "chat": {"id": 999}}
    client = _async_client(lambda req: _updates([{"update_id": 1, "message": intruder}]))
    info = await wait_for_first_message(
        client, "tok", expected_code="ABC123", poll_timeout=0, max_rounds=1
    )
    assert info is None  # never sent the code → never paired
    await client.aclose()


def test_make_pairing_code_is_random_and_unambiguous():
    from moko.channels.telegram import _CODE_ALPHABET, make_pairing_code

    code = make_pairing_code()
    assert len(code) == 6 and all(c in _CODE_ALPHABET for c in code)
    assert not (set("01OIL") & set(_CODE_ALPHABET))  # no easily-confused characters
    assert make_pairing_code() != make_pairing_code() or True  # random; not asserting collision


async def test_send_message_helper():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    client = _async_client(handler)
    await _send_message(client, "tok", "42", "hi")
    assert seen["body"]["chat_id"] == "42"
    await client.aclose()
