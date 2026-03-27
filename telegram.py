import os
import httpx

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    return token


def send_message(chat_id: int, text: str) -> dict:
    url = TELEGRAM_API.format(token=_token(), method="sendMessage")
    resp = httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_me() -> dict:
    url = TELEGRAM_API.format(token=_token(), method="getMe")
    resp = httpx.get(url, timeout=5)
    resp.raise_for_status()
    return resp.json().get("result", {})


def get_updates(offset: int = 0) -> list[dict]:
    url = TELEGRAM_API.format(token=_token(), method="getUpdates")
    resp = httpx.get(url, params={"offset": offset, "timeout": 2}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result", [])
