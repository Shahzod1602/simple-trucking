"""Real Telegram (MTProto) client — the "My Telegram" tab.

Each dispatcher connects their own Telegram account (QR or phone+code). The app
runs several uvicorn workers, so we keep NO long-lived MTProto connections: every
operation builds a Telethon client from the stored (encrypted) StringSession,
connects, does its work, and disconnects. Sessions are a full account login, so
they are encrypted at rest with Fernet(TG_SESSION_SECRET).

Mirrors relay.py in spirit (a thin client other modules call).
"""

import contextlib
import logging
import os

log = logging.getLogger("tg")

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
    from cryptography.fernet import Fernet
    _HAS_LIBS = True
except Exception:  # libs not installed yet (e.g. before a rebuild)
    _HAS_LIBS = False


def _api_id() -> int:
    try:
        return int(os.environ.get("TG_API_ID", "") or 0)
    except ValueError:
        return 0


class TgAccount:
    @property
    def api_id(self) -> int:
        return _api_id()

    @property
    def api_hash(self) -> str:
        return os.environ.get("TG_API_HASH", "").strip()

    @property
    def _secret(self) -> str:
        return os.environ.get("TG_SESSION_SECRET", "").strip()

    @property
    def configured(self) -> bool:
        return bool(_HAS_LIBS and self.api_id and self.api_hash and self._secret)

    # ── session encryption ────────────────────────────────────────────────
    def enc(self, raw: str) -> str:
        return Fernet(self._secret.encode()).encrypt(raw.encode()).decode()

    def dec(self, enc: str) -> str:
        return Fernet(self._secret.encode()).decrypt(enc.encode()).decode()

    def _new_client(self) -> "TelegramClient":
        return TelegramClient(StringSession(), self.api_id, self.api_hash)

    @contextlib.asynccontextmanager
    async def _client(self, session_enc: str):
        client = TelegramClient(StringSession(self.dec(session_enc)), self.api_id, self.api_hash)
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()

    @staticmethod
    def _me_dict(me) -> dict:
        if not me:
            return {}
        name = " ".join(p for p in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if p)
        return {
            "tg_user_id": getattr(me, "id", None),
            "name": name or (getattr(me, "username", None) or "Telegram user"),
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
        }

    # ── Phone login ───────────────────────────────────────────────────────
    async def send_code(self, phone: str) -> dict:
        client = self._new_client()
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            return {"session": self.enc(client.session.save()), "phone_code_hash": sent.phone_code_hash}
        finally:
            await client.disconnect()

    async def sign_in_code(self, session_enc: str, phone: str, code: str, phone_code_hash: str) -> dict:
        client = TelegramClient(StringSession(self.dec(session_enc)), self.api_id, self.api_hash)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                return {"status": "2fa", "session": self.enc(client.session.save())}
            me = await client.get_me()
            return {"status": "ok", "session": self.enc(client.session.save()), "me": self._me_dict(me)}
        finally:
            await client.disconnect()

    async def sign_in_password(self, session_enc: str, password: str) -> dict:
        client = TelegramClient(StringSession(self.dec(session_enc)), self.api_id, self.api_hash)
        await client.connect()
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            return {"status": "ok", "session": self.enc(client.session.save()), "me": self._me_dict(me)}
        finally:
            await client.disconnect()

    # ── QR login ──────────────────────────────────────────────────────────
    async def qr_start(self) -> dict:
        client = self._new_client()
        await client.connect()
        try:
            qr = await client.qr_login()
            return {"session": self.enc(client.session.save()), "qr_url": qr.url}
        finally:
            await client.disconnect()

    async def qr_check(self, session_enc: str) -> dict:
        """After the user scans+accepts, the session's auth key becomes authorized."""
        client = TelegramClient(StringSession(self.dec(session_enc)), self.api_id, self.api_hash)
        await client.connect()
        try:
            me = await client.get_me()
            if me:
                return {"status": "ok", "session": self.enc(client.session.save()), "me": self._me_dict(me)}
            return {"status": "pending"}
        except Exception:
            return {"status": "pending"}
        finally:
            await client.disconnect()

    # ── Operations (authorized session) ───────────────────────────────────
    async def whoami(self, session_enc: str) -> dict | None:
        async with self._client(session_enc) as client:
            me = await client.get_me()
            return self._me_dict(me) if me else None

    async def list_dialogs(self, session_enc: str, limit: int = 40) -> list[dict]:
        out = []
        async with self._client(session_enc) as client:
            async for d in client.iter_dialogs(limit=limit):
                kind = "user" if d.is_user else ("group" if d.is_group else "channel")
                msg = d.message
                out.append({
                    "chat_id": d.id,
                    "title": d.name or "",
                    "kind": kind,
                    "unread": d.unread_count,
                    "last_text": (getattr(msg, "message", "") or "") if msg else "",
                    "date": d.date.isoformat() if d.date else None,
                })
        return out

    async def get_history(self, session_enc: str, chat_id: int, limit: int = 30) -> list[dict]:
        out = []
        async with self._client(session_enc) as client:
            # Warm the entity cache so a bare chat_id resolves (access_hash).
            await client.get_dialogs(limit=200)
            entity = await client.get_entity(chat_id)
            async for m in client.iter_messages(entity, limit=limit):
                sender = getattr(m, "sender", None)
                sname = ""
                if sender is not None:
                    sname = " ".join(p for p in [getattr(sender, "first_name", None), getattr(sender, "last_name", None)] if p) or (getattr(sender, "title", None) or "")
                out.append({
                    "id": m.id,
                    "text": m.message or "",
                    "out": bool(m.out),
                    "date": m.date.isoformat() if m.date else None,
                    "sender": sname,
                })
        return list(reversed(out))  # chronological

    async def send(self, session_enc: str, chat_id: int, text: str) -> bool:
        async with self._client(session_enc) as client:
            await client.get_dialogs(limit=200)
            entity = await client.get_entity(chat_id)
            await client.send_message(entity, text)
            return True

    async def logout(self, session_enc: str) -> bool:
        try:
            async with self._client(session_enc) as client:
                await client.log_out()
            return True
        except Exception:
            log.exception("Telegram logout error")
            return False


tg = TgAccount()
