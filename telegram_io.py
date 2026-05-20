"""Telegram Bot API — отправка сообщений и приём команд."""

import os
import logging
import httpx

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# Главный (админский) chat_id — кто может давать команды
ADMIN_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

# Все получатели уведомлений (включая админа). Пустой → только админ.
_extra = os.environ.get("TELEGRAM_EXTRA_RECIPIENTS", "").strip()
EXTRA_RECIPIENTS = [int(x.strip()) for x in _extra.split(",") if x.strip()] if _extra else []
ALL_RECIPIENTS = list({ADMIN_CHAT_ID, *EXTRA_RECIPIENTS})

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# Клавиатура с кнопками внизу экрана Telegram (только для админа)
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🔄 Проверить сейчас"}, {"text": "📋 Последние платежи"}],
        [{"text": "📊 Статус"}, {"text": "❓ Помощь"}],
        [{"text": "⏸ Пауза"}, {"text": "▶️ Возобновить"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BUTTON_TO_COMMAND = {
    "🔄 Проверить сейчас":   "/check",
    "📋 Последние платежи":  "/last",
    "📊 Статус":             "/status",
    "❓ Помощь":             "/help",
    "⏸ Пауза":              "/pause",
    "▶️ Возобновить":       "/resume",
}


def _post(chat_id: int, text: str, with_keyboard: bool) -> None:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if with_keyboard:
        payload["reply_markup"] = MAIN_KEYBOARD
    try:
        r = httpx.post(f"{API}/sendMessage", json=payload, timeout=15)
        if r.status_code == 403:
            log.warning(
                "Recipient %s blocked the bot or didn't start chat. "
                "He needs to open @<bot_username> and press Start.", chat_id,
            )
        elif r.status_code >= 400:
            log.error("Telegram %s for chat %s: %s", r.status_code, chat_id, r.text[:200])
    except Exception as exc:
        log.error("Failed to send to %s: %s", chat_id, exc)


def send_to_admin(text: str, with_keyboard: bool = False) -> None:
    """Отправить сообщение ТОЛЬКО админу (ответы на команды, статус, ошибки)."""
    _post(ADMIN_CHAT_ID, text, with_keyboard=with_keyboard)


def broadcast(text: str) -> None:
    """Разослать сообщение ВСЕМ получателям (уведомления о платежах)."""
    for chat_id in ALL_RECIPIENTS:
        # клавиатура только админу
        _post(chat_id, text, with_keyboard=(chat_id == ADMIN_CHAT_ID))


# Обратная совместимость для уже существующего кода
def send(text: str, with_keyboard: bool = False) -> None:
    """Алиас: отправить админу. Для broadcast уведомлений используй broadcast()."""
    send_to_admin(text, with_keyboard=with_keyboard)


def poll_commands(last_update_id: int) -> tuple[list[str], int]:
    """Опросить Telegram. Возвращает (список команд от АДМИНА, новый update_id)."""
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
        # Команды принимаются ТОЛЬКО от админа
        if msg.get("from", {}).get("id") != ADMIN_CHAT_ID:
            continue
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            commands.append(text.split()[0].lower().split("@")[0])
        elif text in BUTTON_TO_COMMAND:
            commands.append(BUTTON_TO_COMMAND[text])

    return commands, new_last
