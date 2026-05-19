"""Telegram Bot API — send messages and poll commands."""

import os
import logging
import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send(text: str) -> None:
    try:
        r = httpx.post(
            f"{API}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)


def poll_commands(last_update_id: int) -> tuple[list[str], int]:
    """Get new commands sent to the bot. Returns (commands, new_last_update_id)."""
    try:
        r = httpx.get(
            f"{API}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 0, "allowed_updates": '["message"]'},
            timeout=15,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as exc:
        log.error("Failed to poll Telegram: %s", exc)
        return [], last_update_id

    commands: list[str] = []
    new_last = last_update_id
    for update in updates:
        new_last = max(new_last, update["update_id"])
        msg = update.get("message", {})
        # принимаем команды только от админа
        if msg.get("from", {}).get("id") != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            commands.append(text.split()[0].lower().split("@")[0])

    return commands, new_last
