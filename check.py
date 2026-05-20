"""Один прогон бота: опрос Telegram → команды → проверка банка → уведомления."""

import asyncio
import logging
import sys

import state
import telegram_io
from bank_scraper import fetch_incoming_transactions, Transaction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────── ФОРМАТИРОВАНИЕ ───────────────────────────

def format_payment(tx: Transaction) -> str:
    return (
        "💰 <b>Новое поступление</b>\n\n"
        f"<b>Сумма:</b> {tx.amount} {tx.currency}\n"
        f"<b>Дата:</b> {tx.booking_date}\n"
        f"<b>От:</b> {tx.counterparty}\n"
        f"<b>Назначение:</b> {tx.purpose}"
    )


def format_status(st: dict) -> str:
    state_icon = "⏸ <b>на паузе</b>" if st["is_paused"] else "▶️ <b>активен</b>"
    last_at = st.get("last_check_at") or "—"
    last_res = st.get("last_check_result") or "—"
    return (
        "<b>📊 Статус бота</b>\n\n"
        f"Состояние: {state_icon}\n"
        f"Расписание: каждые 15 мин, пн–пт, 08:00–17:00 МСК\n"
        f"Последняя проверка: <code>{last_at}</code>\n"
        f"Результат: {last_res}"
    )


WELCOME_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я слежу за поступлениями на твой счёт в Беларусбанке "
    "и пишу сюда о каждом новом платеже.\n\n"
    "Проверяю автоматически <b>каждые 15 минут</b> в рабочее время "
    "(пн–пт, 08:00–17:00 МСК).\n\n"
    "Используй кнопки внизу экрана или команды:\n\n"
    "🔄 <b>Проверить сейчас</b> — внеплановая проверка\n"
    "📋 <b>Последние платежи</b> — выписка за 30 дней\n"
    "📊 <b>Статус</b> — что сейчас с ботом\n"
    "⏸ <b>Пауза</b> — приостановить автопроверку\n"
    "▶️ <b>Возобновить</b> — снова включить\n"
    "❓ <b>Помощь</b> — это сообщение"
)


# ────────────────────────── ОБРАБОТКА КОМАНД ──────────────────────────

def handle_commands(commands: list[str], st: dict, pending: dict) -> None:
    for cmd in commands:
        log.info("Handling command: %s", cmd)
        if cmd in ("/start", "/help", "/menu"):
            telegram_io.send(WELCOME_TEXT, with_keyboard=True)

        elif cmd == "/status":
            telegram_io.send(format_status(st), with_keyboard=True)

        elif cmd == "/check":
            pending["force_check"] = True
            if st["is_paused"]:
                telegram_io.send("🔄 Принято. Бот на паузе, но эту проверку выполню.")
            else:
                telegram_io.send("🔄 Принято. Запускаю проверку прямо сейчас.")

        elif cmd == "/last":
            pending["show_last"] = True
            telegram_io.send("⏳ Загружаю выписку за последние 30 дней…")

        elif cmd in ("/pause", "/stop"):
            if st["is_paused"]:
                telegram_io.send("⏸ Уже на паузе. Возобновить: /resume")
            else:
                st["is_paused"] = True
                telegram_io.send(
                    "⏸ <b>Автопроверка приостановлена.</b>\n"
                    "Чтобы возобновить — нажми ▶️ Возобновить или отправь /resume",
                    with_keyboard=True,
                )

        elif cmd == "/resume":
            if not st["is_paused"]:
                telegram_io.send("▶️ Бот уже активен. Проверки идут по расписанию.")
            else:
                st["is_paused"] = False
                telegram_io.send(
                    "▶️ <b>Автопроверка возобновлена.</b>\n"
                    "Следующая проверка — в ближайшие 15 минут.",
                    with_keyboard=True,
                )

        else:
            telegram_io.send(
                f"Неизвестная команда: <code>{cmd}</code>\n"
                f"Используй /help",
                with_keyboard=True,
            )


# ────────────────────────────── ПРОВЕРКА БАНКА ──────────────────────────

async def do_bank_check(st: dict) -> str:
    try:
        transactions = await fetch_incoming_transactions(days_back=3)
    except Exception as exc:
        log.exception("Bank scrape failed")
        telegram_io.send(
            f"❌ <b>Ошибка при обращении к банку</b>\n<code>{exc}</code>",
            with_keyboard=True,
        )
        return f"ошибка: {exc}"

    # На первом запуске не спамим — просто помечаем все старые как «уже видели»
    is_first_run = len(st["seen_transactions"]) == 0
    new_count = 0
    seen = set(st["seen_transactions"])

    for tx in transactions:
        if not tx.transaction_id or tx.transaction_id in seen:
            continue
        if is_first_run:
            log.info("Initial run: marking %s as seen", tx.transaction_id)
        else:
            telegram_io.broadcast(format_payment(tx))
            new_count += 1
        seen.add(tx.transaction_id)

    st["seen_transactions"] = sorted(seen)

    if is_first_run and transactions:
        telegram_io.send(
            "✅ Бот подключился к счёту.\n"
            f"Найдено {len(transactions)} ранее прошедших поступлений — отмечены как «уже учтённые», "
            "уведомлений по ним не будет.\n\n"
            "О каждом <b>новом</b> поступлении пришлю отдельное сообщение."
        )

    return f"новых поступлений: {new_count}"


# ──────────────────────────── /last  «Последние платежи» ────────────────────────

async def show_last_payments():
    try:
        recent = await fetch_incoming_transactions(days_back=30)
    except Exception as exc:
        log.exception("/last failed")
        telegram_io.send(
            f"❌ <b>Не удалось загрузить выписку</b>\n<code>{exc}</code>",
            with_keyboard=True,
        )
        return

    if not recent:
        telegram_io.send("За последние 30 дней поступлений не было.", with_keyboard=True)
        return

    recent = sorted(recent, key=lambda t: t.booking_date, reverse=True)[:10]
    header = f"<b>📋 Последние {len(recent)} поступлений (за 30 дней)</b>\n"
    blocks = [header]
    for t in recent:
        blocks.append(
            f"📅 <b>{t.booking_date}</b>   💰 <b>{t.amount} {t.currency}</b>\n"
            f"<b>От:</b> {t.counterparty}\n"
            f"<i>{t.purpose[:200]}</i>"
        )

    # Telegram лимит 4096 символов на сообщение — нарезаем
    full = "\n\n".join(blocks)
    chunk_size = 3800
    for i in range(0, len(full), chunk_size):
        is_last = (i + chunk_size >= len(full))
        telegram_io.send(full[i:i + chunk_size], with_keyboard=is_last)


# ────────────────────────────────── MAIN ──────────────────────────────────

async def main() -> int:
    st = state.load()
    pending: dict = {}

    log.info("Polling Telegram (offset=%d)", st["last_telegram_update_id"])
    commands, new_offset = telegram_io.poll_commands(st["last_telegram_update_id"])
    if commands:
        log.info("Commands received: %s", commands)
    st["last_telegram_update_id"] = new_offset

    handle_commands(commands, st, pending)

    should_check = not st["is_paused"] or pending.get("force_check")
    if should_check:
        log.info("Running bank check (paused=%s, forced=%s)",
                 st["is_paused"], pending.get("force_check"))
        result = await do_bank_check(st)
        state.mark_check(st, result)
    else:
        log.info("Skipping bank check — bot is paused")
        state.mark_check(st, "пропущено (на паузе)")

    if pending.get("show_last"):
        await show_last_payments()

    state.save(st)
    log.info("Done. Result: %s", st["last_check_result"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
