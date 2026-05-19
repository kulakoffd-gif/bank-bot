"""Entry point — one run of: poll Telegram → handle commands → check bank → notify."""

import asyncio
import logging
import sys
from datetime import datetime, timezone

import state
import telegram_io
from bank_scraper import fetch_incoming_transactions, Transaction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)


def format_payment(tx: Transaction) -> str:
    return (
        "💰 <b>Новое поступление</b>\n\n"
        f"<b>Сумма:</b> {tx.amount} {tx.currency}\n"
        f"<b>Дата:</b> {tx.booking_date}\n"
        f"<b>Контрагент:</b> {tx.counterparty}\n"
        f"<b>Назначение:</b> {tx.purpose}"
    )


def format_status(st: dict) -> str:
    paused = "⏸ на паузе" if st["is_paused"] else "▶️ активен"
    last_at = st.get("last_check_at") or "ещё не запускалась"
    last_res = st.get("last_check_result") or "—"
    return (
        f"<b>Статус бота</b>\n\n"
        f"Состояние: {paused}\n"
        f"Расписание: каждые 15 мин, пн–пт, 8:00–17:00 МСК\n"
        f"Последняя проверка: {last_at}\n"
        f"Результат: {last_res}"
    )


def handle_commands(commands: list[str], st: dict, pending: dict) -> None:
    """Process commands and either mutate state or set flags for actions during this run."""
    for cmd in commands:
        log.info("Handling command: %s", cmd)
        if cmd in ("/pause", "/stop"):
            st["is_paused"] = True
            telegram_io.send("⏸ Автопроверка приостановлена.\nКоманда: /resume — возобновить")
        elif cmd in ("/resume", "/start"):
            st["is_paused"] = False
            telegram_io.send("▶️ Автопроверка возобновлена.")
        elif cmd == "/check":
            pending["force_check"] = True
            telegram_io.send("🔄 Принято — проверю в этом запуске.")
        elif cmd == "/status":
            telegram_io.send(format_status(st))
        elif cmd == "/last":
            pending["show_last"] = True
        elif cmd in ("/help", "/menu"):
            telegram_io.send(
                "<b>Команды бота:</b>\n\n"
                "/status — статус и расписание\n"
                "/check — проверить прямо сейчас\n"
                "/last — последние поступления\n"
                "/pause — поставить на паузу\n"
                "/resume — возобновить\n"
                "/help — эта справка"
            )
        else:
            telegram_io.send(f"Неизвестная команда: {cmd}\nИспользуй /help")


async def do_bank_check(st: dict) -> str:
    try:
        transactions = await fetch_incoming_transactions(days_back=3)
    except Exception as exc:
        log.exception("Bank scrape failed")
        msg = f"❌ Ошибка при обращении к банку:\n<code>{exc}</code>"
        telegram_io.send(msg)
        return f"ошибка: {exc}"

    new_count = 0
    seen = set(st["seen_transactions"])
    for tx in transactions:
        if not tx.transaction_id or tx.transaction_id in seen:
            continue
        telegram_io.send(format_payment(tx))
        seen.add(tx.transaction_id)
        new_count += 1

    st["seen_transactions"] = sorted(seen)
    return f"новых поступлений: {new_count}"


async def main() -> int:
    st = state.load()
    pending: dict = {}

    log.info("Polling Telegram for new commands (offset=%d)", st["last_telegram_update_id"])
    commands, new_offset = telegram_io.poll_commands(st["last_telegram_update_id"])
    if commands:
        log.info("Got %d commands: %s", len(commands), commands)
    st["last_telegram_update_id"] = new_offset

    handle_commands(commands, st, pending)

    should_check = not st["is_paused"] or pending.get("force_check")
    if should_check:
        log.info("Running bank check (paused=%s, forced=%s)", st["is_paused"], pending.get("force_check"))
        result = await do_bank_check(st)
        state.mark_check(st, result)
    else:
        log.info("Skipping bank check — bot is paused")
        state.mark_check(st, "пропущено (на паузе)")

    if pending.get("show_last"):
        if st["seen_transactions"]:
            last_ids = st["seen_transactions"][-10:]
            telegram_io.send(
                f"Последние ID учтённых поступлений ({len(last_ids)}):\n"
                + "\n".join(f"• <code>{i}</code>" for i in last_ids)
            )
        else:
            telegram_io.send("Учтённых поступлений пока нет.")

    state.save(st)
    log.info("Done. Result: %s", st["last_check_result"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
