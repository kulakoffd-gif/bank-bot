"""Telegram Bot API — отправка сообщений и приём команд."""

import os
import logging
import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# Главная клавиатура с кнопками внизу экрана Telegram
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔄 Проверить сейчас"}, {"text": "📋 Последние платежи"}],
        [{"text": "📊 Статус"}, {"text": "❓ Помощь"}],
        [{"text": "⏸ Пауза"}, {"text": "▶️ Возобновить"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Маппинг текста кнопки на команду
BUTTON_TO_COMMAND = {
    "🔄 Проверить сейчас":   "/check",
    "📋 Последние платежи":  "/last",
    "📊 Статус":             "/status",
    "❓ Помощь":             "/help",
    "⏸ Пауза":              "/pause",
    "▶️ Возобновить":       "/resume",
}


def send(text: str, with_keyboard: bool = False) -> None:
    payload: dict = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if with_keyboard:
        payload["reply_markup"] = MAIN_KEYBOARD
    try:
        r = httpx.post(f"{API}/sendMessage", json=payload, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)


def poll_commands(last_update_id: int) -> tuple[list[str], int]:
    """Опросить Telegram. Возвращает (список команд от админа, новый update_id)."""
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

    commands: list[str] = []
    new_last = last_update_id
    for update in updates:
        new_last = max(new_last, update["update_id"])
        msg = update.get("message", {})
        # принимаем команды только от админа
        if msg.get("from", {}).get("id") != CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        # Команда вида /xxx или текст кнопки
        if text.startswith("/"):
            commands.append(text.split()[0].lower().split("@")[0])
        elif text in BUTTON_TO_COMMAND:
            commands.append(BUTTON_TO_COMMAND[text])

    return commands, new_last
