"""Relay (email-api) client — Gmail/Outlook mail over a single REST API.

ratecon talks to Relay (https://relay.identify.uz) as ONE "master" user (a single
`relay_sk_...` key). Each company connects its own mailbox via `/connect/start`
OAuth; all mailboxes live under the master user in Relay. ratecon maps each
`relay_account_id` back to a company in its `email_accounts` table.

Mirrors ~/sales.identify.uz/app/relay.py, adapted to ratecon's os.environ config
and extended with `get_attachment` (the attachment download endpoint added to Relay).
"""

import logging
import os

import httpx

log = logging.getLogger("relay")


class RelayClient:
    def __init__(self) -> None:
        self.base = os.environ.get("RELAY_BASE_URL", "https://relay.identify.uz").rstrip("/")
        self.key = os.environ.get("RELAY_API_KEY", "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.base and self.key)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"}

    async def connect_start(self, provider: str, return_url: str) -> str | None:
        """Headless OAuth: returns the authorize URL to send the user to."""
        if not self.configured:
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                resp = await http.post(
                    f"{self.base}/connect/start",
                    headers=self._headers(),
                    json={"provider": provider, "returnUrl": return_url},
                )
            if resp.status_code >= 400:
                log.warning("Relay connect/start: %s %s", resp.status_code, resp.text[:300])
                return None
            return resp.json().get("url")
        except Exception:
            log.exception("Relay connect/start error")
            return None

    async def list_messages(self, relay_account_id: str, limit: int = 15) -> list[dict]:
        if not self.configured:
            return []
        try:
            async with httpx.AsyncClient(timeout=25) as http:
                resp = await http.get(
                    f"{self.base}/messages",
                    headers=self._headers(),
                    params={"accountId": relay_account_id, "limit": str(limit)},
                )
            if resp.status_code >= 400:
                log.warning("Relay /messages: %s %s", resp.status_code, resp.text[:200])
                return []
            return resp.json().get("messages", [])
        except Exception:
            log.exception("Relay /messages error")
            return []

    async def get_message(self, relay_account_id: str, message_id: str) -> dict | None:
        if not self.configured:
            return None
        try:
            async with httpx.AsyncClient(timeout=25) as http:
                resp = await http.get(
                    f"{self.base}/messages/{message_id}",
                    headers=self._headers(),
                    params={"accountId": relay_account_id},
                )
            if resp.status_code >= 400:
                log.warning("Relay /messages/:id: %s %s", resp.status_code, resp.text[:200])
                return None
            return resp.json().get("message")
        except Exception:
            log.exception("Relay /messages/:id error")
            return None

    async def get_attachment(
        self, relay_account_id: str, message_id: str, attachment_id: str
    ) -> tuple[bytes, str, str] | None:
        """Download one attachment. Returns (bytes, content_type, filename) or None."""
        if not self.configured:
            return None
        try:
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.get(
                    f"{self.base}/messages/{message_id}/attachments/{attachment_id}",
                    headers=self._headers(),
                    params={"accountId": relay_account_id},
                )
            if resp.status_code >= 400:
                log.warning("Relay attachment: %s %s", resp.status_code, resp.text[:200])
                return None
            content_type = resp.headers.get("content-type", "application/octet-stream")
            disposition = resp.headers.get("content-disposition", "")
            filename = "attachment"
            if "filename=" in disposition:
                filename = disposition.split("filename=", 1)[1].strip().strip('"') or filename
            return resp.content, content_type, filename
        except Exception:
            log.exception("Relay attachment error")
            return None

    async def send(
        self,
        relay_account_id: str,
        to: list[str],
        subject: str,
        text: str | None = None,
        html: str | None = None,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> bool:
        if not self.configured:
            return False
        payload: dict = {"accountId": relay_account_id, "to": to, "subject": subject}
        if text:
            payload["text"] = text
        if html:
            payload["html"] = html
        if thread_id:
            payload["threadId"] = thread_id
        if in_reply_to:
            payload["inReplyTo"] = in_reply_to
        if reply_to_message_id:
            payload["replyToMessageId"] = reply_to_message_id
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(
                    f"{self.base}/messages/send",
                    headers=self._headers(),
                    json=payload,
                )
            if resp.status_code >= 400:
                log.error("Relay send: %s %s", resp.status_code, resp.text[:300])
                return False
            return True
        except Exception:
            log.exception("Relay send error")
            return False

    async def disconnect(self, relay_account_id: str) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                resp = await http.delete(
                    f"{self.base}/accounts/{relay_account_id}",
                    headers=self._headers(),
                )
            return resp.status_code < 400
        except Exception:
            log.exception("Relay disconnect error")
            return False


relay = RelayClient()
