"""Telegram Bot API — отправка сообщений и приём команд."""

import os
import logging
import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# Клавиатура с кнопками внизу экрана Telegram (только для админа)
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔄 Проверить сейчас"}, {"text": "📋 Последние платежи"}],
        [{"text": "📊 Статус"}, {"text": "👥 Получатели"}],
        [{"text": "⏸ Пауза"}, {"text": "▶️ Возобновить"}],
        [{"text": "❓ Помощь"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BUTTON_TO_COMMAND = {
    "🔄 Проверить сейчас":   "/check",
    "📋 Последние платежи":  "/last",
    "📊 Статус":             "/status",
    "👥 Получатели":         "/recipients",
    "⏸ Пауза":              "/pause",
    "▶️ Возобновить":       "/resume",
    "❓ Помощь":             "/help",
}


def _post(chat_id: int, text: str, with_keyboard: bool) -> tuple[bool, str]:
    """Возвращает (успех, текст ошибки если есть)."""
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if with_keyboard:
        payload["reply_markup"] = MAIN_KEYBOARD
    try:
        r = httpx.post(f"{API}/sendMessage", json=payload, timeout=15)
        if r.status_code == 200:
            return True, ""
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"description": r.text}
        log.warning("Telegram HTTP %s for chat %s: %s", r.status_code, chat_id, body)
        return False, body.get("description", f"HTTP {r.status_code}")
    except Exception as exc:
        log.error("Failed to send to %s: %s", chat_id, exc)
        return False, str(exc)


def send_to_admin(text: str, with_keyboard: bool = False) -> None:
    """Отправить сообщение ТОЛЬКО админу."""
    _post(ADMIN_CHAT_ID, text, with_keyboard=with_keyboard)


def broadcast(text: str, extra_recipients: list[int]) -> dict:
    """Разослать сообщение админу + всем в списке. Возвращает статус по каждому."""
    results: dict[int, tuple[bool, str]] = {}
    all_targets = list({ADMIN_CHAT_ID, *extra_recipients})
    for chat_id in all_targets:
        with_kb = (chat_id == ADMIN_CHAT_ID)
        ok, err = _post(chat_id, text, with_keyboard=with_kb)
        results[chat_id] = (ok, err)
    return results


# Алиас для обратной совместимости
def send(text: str, with_keyboard: bool = False) -> None:
    send_to_admin(text, with_keyboard=with_keyboard)


def poll_commands(last_update_id: int) -> tuple[list[tuple[str, str]], int]:
    """Опросить Telegram. Возвращает (список (команда, аргумент), новый update_id)."""
    try:
        r = httpx.get(
            f"{API}/getUpdates",
            params={
                "offset": last_update_id + 1,
                "timeout": 0,
                "allowed_updates": '["message"]',
            },
            timeout=15,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
    except Exception as exc:
        log.error("Failed to poll Telegram: %s", exc)
        return [], last_update_id

    commands: list[tuple[str, str]] = []
    new_last = last_update_id
    for update in updates:
        new_last = max(new_last, update["update_id"])
        msg = update.get("message", {})
        # Команды принимаются ТОЛЬКО от админа
        if msg.get("from", {}).get("id") != ADMIN_CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower().split("@")[0]
            args = parts[1] if len(parts) > 1 else ""
            commands.append((cmd, args))
        elif text in BUTTON_TO_COMMAND:
            commands.append((BUTTON_TO_COMMAND[text], ""))

    return commands, new_last
