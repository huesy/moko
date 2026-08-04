"""Telegram channel — long-poll the Bot API over httpx (no SDK).

Inbound: `getUpdates` (long poll) with a persisted offset so we don't
reprocess on restart. Outbound: `sendMessage` to the companion's chat.
Access control (who is heard) lives in the gateway; this just plumbs
messages in and out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from pathlib import Path

import httpx

from ..config.paths import ensure_home, moko_home
from ..log import get_logger
from .base import Attachment, Channel, Inbound

_log = get_logger("telegram")
_API = "https://api.telegram.org"


def _kind_for_mime(mime: str | None, default: str) -> str:
    """Map a MIME top-level type to Moko's media `kind`, falling back to `default`."""
    if not mime:
        return default
    return {"image": "image", "audio": "audio", "video": "video"}.get(mime.split("/")[0], default)


def _attachment_spec(msg: dict) -> dict | None:
    """Pull the one downloadable attachment off a Telegram message, or None if it
    carries none we handle (a sticker, location, contact…). Telegram sends a photo
    as several sizes — take the largest."""
    if msg.get("photo"):
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return {"file_id": largest["file_id"], "kind": "image",
                "mime": "image/jpeg", "filename": None}
    if "document" in msg:
        d = msg["document"]
        return {"file_id": d["file_id"], "kind": _kind_for_mime(d.get("mime_type"), "document"),
                "mime": d.get("mime_type"), "filename": d.get("file_name")}
    if "voice" in msg:
        v = msg["voice"]
        return {"file_id": v["file_id"], "kind": "audio",
                "mime": v.get("mime_type", "audio/ogg"), "filename": None}
    if "audio" in msg:
        a = msg["audio"]
        return {"file_id": a["file_id"], "kind": "audio",
                "mime": a.get("mime_type"), "filename": a.get("file_name")}
    if "video" in msg:
        vv = msg["video"]
        return {"file_id": vv["file_id"], "kind": "video",
                "mime": vv.get("mime_type"), "filename": vv.get("file_name")}
    return None


async def _get_file_path(client: httpx.AsyncClient, token: str, file_id: str) -> str | None:
    resp = await client.get(f"{_base(token)}/getFile", params={"file_id": file_id}, timeout=20)
    resp.raise_for_status()
    return ((resp.json() or {}).get("result") or {}).get("file_path")


async def _download_file(client: httpx.AsyncClient, token: str, file_path: str) -> bytes:
    # The bot file endpoint is a different host path: /file/bot<token>/<file_path>.
    resp = await client.get(f"{_API}/file/bot{token}/{file_path}", timeout=60)
    resp.raise_for_status()
    return resp.content

# Inline-button `callback_data` → the verdict word the gateway already
# understands, so a button press and a typed reply travel the same approval path.
_CALLBACK_VERDICT = {"approve": "yes", "always": "yes always", "deny": "no"}

# Unambiguous alphabet for the pairing code — no 0/O/1/I/L to misread or mistype.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def make_pairing_code(length: int = 6) -> str:
    """A short, random, one-time code the companion must echo to pair. ~31^6 ≈
    900M combinations — infeasible to guess inside the pairing window."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _base(token: str) -> str:
    return f"{_API}/bot{token}"


# `timeout` is the Telegram long-poll param, not an asyncio timeout → ASYNC109 is a false positive.
async def _get_updates(client: httpx.AsyncClient, token: str, offset: int, timeout: int) -> list:  # noqa: ASYNC109
    resp = await client.get(
        f"{_base(token)}/getUpdates",
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


_CHUNK_LIMIT = 4000  # headroom under Telegram's 4096-char sendMessage hard cap
_SEND_ATTEMPTS = 3


def _split_message(text: str, limit: int = _CHUNK_LIMIT) -> list[str]:
    """Split a long reply into Telegram-sized pieces on line boundaries (hard-
    slicing any single over-long line as a last resort), so a reply over 4096
    chars is delivered in order instead of silently 400-ing into dead air."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single line longer than a whole message
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


async def _send_message(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> dict:
    """POST one message, retrying transient failures: a 429 waits its
    `retry_after`, a 5xx/network blip backs off — so a momentary hiccup doesn't
    drop the companion's reply into silence. A 4xx (other than 429) raises at once
    (retrying won't fix a bad request)."""
    body: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    for i in range(_SEND_ATTEMPTS):
        last = i == _SEND_ATTEMPTS - 1
        try:
            resp = await client.post(f"{_base(token)}/sendMessage", json=body, timeout=20)
        except httpx.HTTPError:
            if last:
                raise
            await asyncio.sleep(0.5 * (2**i))
            continue
        if resp.status_code == 429:
            if last:
                resp.raise_for_status()
            retry_after = (resp.json().get("parameters") or {}).get("retry_after", 1)
            await asyncio.sleep(min(float(retry_after), 30))
            continue
        if resp.status_code >= 500 and not last:
            await asyncio.sleep(0.5 * (2**i))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable: send loop exhausted without return or raise")


_CAPTION_LIMIT = 1024  # Telegram caps media captions at 1024 chars (vs 4096 for text)


async def _upload_file(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    data: bytes,
    *,
    filename: str,
    caption: str | None,
    kind: str,
) -> dict:
    """Upload a file via the endpoint that gives it a native bubble — a photo previews
    inline, a voice note / video / audio play in place — falling back to a plain
    document for anything else. Telegram derives the MIME from the bytes/filename."""
    # sendAudio (not sendVoice) plays an mp3 bubble; sendVoice is opus-only.
    endpoint, field = {
        "image": ("sendPhoto", "photo"),
        "audio": ("sendAudio", "audio"),
        "video": ("sendVideo", "video"),
    }.get(kind, ("sendDocument", "document"))
    body: dict = {"chat_id": chat_id}
    if caption:
        body["caption"] = caption[:_CAPTION_LIMIT]
    resp = await client.post(
        f"{_base(token)}/{endpoint}",
        data=body,
        files={field: (filename, data)},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


async def _answer_callback(
    client: httpx.AsyncClient, token: str, callback_id: str, text: str
) -> None:
    resp = await client.post(
        f"{_base(token)}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()


async def _clear_keyboard(client: httpx.AsyncClient, token: str, chat_id, message_id) -> None:
    """Drop the inline buttons off a message (after a press) so it can't be
    tapped twice."""
    resp = await client.post(
        f"{_base(token)}/editMessageReplyMarkup",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        },
        timeout=10,
    )
    resp.raise_for_status()


async def _send_chat_action(
    client: httpx.AsyncClient, token: str, chat_id: str, action: str = "typing"
) -> None:
    resp = await client.post(
        f"{_base(token)}/sendChatAction",
        json={"chat_id": chat_id, "action": action},
        timeout=10,
    )
    resp.raise_for_status()


async def wait_for_first_message(
    client: httpx.AsyncClient,
    token: str,
    expected_code: str,
    *,
    poll_timeout: int = 30,
    max_rounds: int = 120,
) -> dict | None:
    """Block until the companion proves themselves with the one-time code, then
    return their {user_id, chat_id, name, offset}. None if it never comes.

    Pairing binds Moko to *one* person, so it must not bind to whoever messages
    first — the bot is discoverable by username, and a stranger (or a stale
    pending message from before pairing) could otherwise hijack it. Only a
    message whose text matches `expected_code` (case-insensitively) binds;
    everything else is ignored. `expected_code` is required so this can never
    fail open."""
    want = expected_code.strip().upper()
    offset = 0
    for _ in range(max_rounds):
        updates = await _get_updates(client, token, offset, poll_timeout)
        chosen = None
        for update in updates:
            offset = int(update["update_id"]) + 1
            msg = update.get("message") or {}
            text = msg.get("text")
            if not (text and "from" in msg and "chat" in msg):
                continue
            if text.strip().upper() != want:
                # Not the code — a stranger or a stale message. Skip it (the
                # offset still advances so we don't see it again).
                _log.info("pairing: ignored a non-matching message from %s", msg["from"].get("id"))
                continue
            chosen = msg
        if chosen is not None:
            frm = chosen["from"]
            return {
                "user_id": str(frm["id"]),
                "chat_id": str(chosen["chat"]["id"]),
                "name": frm.get("first_name", "there"),
                "offset": offset,  # so the daemon resumes AFTER the code message
            }
    return None


def _default_state_path() -> Path:
    """Where the daemon persists the Telegram poll offset it resumes from."""
    return moko_home() / "telegram_state.json"


def _read_offset(path: Path) -> int:
    """The saved poll offset (0 when unset or unreadable)."""
    try:
        return int(json.loads(path.read_text())["offset"])
    except (FileNotFoundError, ValueError, KeyError):
        return 0


def _write_offset(path: Path, offset: int) -> None:
    ensure_home()
    path.write_text(json.dumps({"offset": int(offset)}))


def save_offset(offset: int, *, state_path: Path | None = None) -> None:
    """Persist the poll offset the daemon will resume from. Pairing calls this
    after binding so the daemon starts *after* what pairing already consumed —
    otherwise it re-delivers the pairing code as a normal message and replies."""
    _write_offset(Path(state_path) if state_path else _default_state_path(), offset)


class TelegramChannel(Channel):
    name = "telegram"
    can_send_files = True

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient,
        allowed_sender_id: str | None = None,
        state_path: Path | None = None,
        poll_timeout: int = 30,
        error_backoff: float = 5.0,
    ) -> None:
        self._token = token
        self.chat_id = str(chat_id)
        self._client = client
        # Access control lives here: only this sender is heard (None = anyone).
        self._allowed_sender_id = str(allowed_sender_id) if allowed_sender_id else None
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff
        self._state_path = Path(state_path) if state_path else _default_state_path()
        self._offset = _read_offset(self._state_path)

    def _save_offset(self) -> None:
        _write_offset(self._state_path, self._offset)

    async def _poll_once(self, emit) -> None:
        updates = await _get_updates(self._client, self._token, self._offset, self._poll_timeout)
        for update in updates:
            self._offset = int(update["update_id"]) + 1
            if "callback_query" in update:  # an inline-button press (e.g. an approval)
                await self._handle_callback(update["callback_query"], emit)
                continue
            msg = update.get("message") or {}
            if "from" not in msg or "chat" not in msg:
                continue
            # A message can be text, an attachment with a caption, or a bare
            # attachment. Treat the caption as the text when there's no separate text.
            text = msg.get("text") or msg.get("caption") or ""
            spec = _attachment_spec(msg)
            if not text and spec is None:
                continue  # nothing we handle (sticker, location, contact…)
            sender_id = str(msg["from"]["id"])
            if self._allowed_sender_id is not None and sender_id != self._allowed_sender_id:
                _log.info("ignored telegram message from %s", sender_id)
                continue
            attachments = []
            if spec is not None:
                att = await self._fetch_attachment(spec, caption=msg.get("caption"))
                if att is not None:
                    attachments.append(att)
            await emit(
                Inbound(
                    text=text,
                    sender_id=sender_id,
                    chat_id=str(msg["chat"]["id"]),
                    channel=self.name,
                    attachments=attachments,
                )
            )
        if updates:
            self._save_offset()

    async def _fetch_attachment(self, spec: dict, *, caption: str | None) -> Attachment | None:
        """Download one attachment's bytes (getFile → the file endpoint). A failed
        download must not lose the message — we return None and the text still goes
        through. The bytes are persisted later, by the store (one writer)."""
        try:
            file_path = await _get_file_path(self._client, self._token, spec["file_id"])
            if not file_path:
                return None
            data = await _download_file(self._client, self._token, file_path)
        except httpx.HTTPError:
            _log.warning("couldn't download a %s attachment", spec["kind"], exc_info=True)
            return None
        return Attachment(
            kind=spec["kind"], data=data, mime=spec["mime"],
            filename=spec["filename"], caption=caption, ref=spec["file_id"],
        )

    async def _handle_callback(self, cb: dict, emit) -> None:
        """An inline-button press. Map it to the same verdict word a typed reply
        would carry and emit it as an ordinary inbound, so the gateway resolves
        it through exactly one approval path. Then dismiss the spinner and strip
        the buttons so it can't be tapped twice."""
        sender_id = str((cb.get("from") or {}).get("id", ""))
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        # Reject an unauthorized presser FIRST, before any outbound API call — so a
        # stranger (e.g. if the bot is ever in a group) can't strip the companion's
        # approval buttons or burn API calls before being dropped. Verdict gating was
        # already correct; this also gates the side effects.
        if self._allowed_sender_id is not None and sender_id != self._allowed_sender_id:
            _log.info("ignored telegram callback from %s", sender_id)
            return
        with contextlib.suppress(httpx.HTTPError):
            await _answer_callback(self._client, self._token, cb.get("id", ""), "Got it")
        with contextlib.suppress(httpx.HTTPError):
            await _clear_keyboard(self._client, self._token, chat.get("id"), msg.get("message_id"))
        word = _CALLBACK_VERDICT.get(cb.get("data", ""))
        if word is None or "id" not in chat:
            return
        await emit(
            Inbound(text=word, sender_id=sender_id, chat_id=str(chat["id"]), channel=self.name)
        )

    async def run(self, emit) -> None:
        while True:
            try:
                await self._poll_once(emit)
            except Exception:
                # Resilience: one bad poll (network blip, odd update) must
                # not take the daemon down — log it and back off.
                _log.warning("telegram poll failed; backing off", exc_info=True)
                await asyncio.sleep(self._error_backoff)

    async def send(self, text: str) -> None:
        # Chunk anything over Telegram's 4096-char cap; without this a long reply
        # 400s and the companion gets nothing (dead air).
        for chunk in _split_message(text):
            await _send_message(self._client, self._token, self.chat_id, chunk)

    async def send_file(
        self, data: bytes, *, filename: str, caption: str | None = None, kind: str = "document"
    ) -> None:
        await _upload_file(
            self._client, self._token, self.chat_id, data,
            filename=filename, caption=caption, kind=kind,
        )

    async def request_approval(self, description: str, *, always_ok: bool = True) -> str | None:
        """Ask with native inline buttons; the press comes back via getUpdates as
        a callback_query (see `_handle_callback`). The "Always" button only shows
        when the action's class can be trusted (`always_ok`) — escalations get just
        Approve/Deny. Returns the prompt's message id so it can be cleared on
        resolution. Falls back to a text prompt if the keyboard send fails, so
        approval is never lost."""
        buttons = [{"text": "✅ Approve", "callback_data": "approve"}]
        if always_ok:
            buttons.append({"text": "👍 Always", "callback_data": "always"})
        buttons.append({"text": "❌ Deny", "callback_data": "deny"})
        try:
            data = await _send_message(
                self._client, self._token, self.chat_id,
                f"🔐 Approve: {description}", reply_markup={"inline_keyboard": [buttons]},
            )
            mid = (data.get("result") or {}).get("message_id")
            return str(mid) if mid is not None else None
        except httpx.HTTPError:
            return await super().request_approval(description, always_ok=always_ok)  # text fallback

    async def clear_approval(self, handle: str) -> None:
        """Strip the inline buttons off a resolved prompt so it can't be tapped
        later. Best-effort — a clear that races a button press (which already
        cleared it) just no-ops."""
        with contextlib.suppress(httpx.HTTPError, ValueError):
            await _clear_keyboard(self._client, self._token, self.chat_id, int(handle))

    async def typing(self) -> None:
        """Show "typing…" in the chat. Best-effort: a failure here must never
        sink the turn, so swallow errors (the reply itself still goes via
        `send`). Telegram clears the indicator after ~5s or when a message
        arrives, so the gateway re-sends it while a long turn runs."""
        try:
            await _send_chat_action(self._client, self._token, self.chat_id)
        except Exception:
            _log.debug("telegram typing action failed", exc_info=True)
