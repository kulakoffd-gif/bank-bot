"""Один прогон бота: опрос Telegram → команды → проверка банка → уведомления."""

import asyncio
import logging
import re
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
    recipients_n = 1 + len(st.get("recipients", []))
    return (
        "<b>📊 Статус бота</b>\n\n"
        f"Состояние: {state_icon}\n"
        f"Расписание: каждые 15 мин, пн–пт, 08:00–17:00 МСК\n"
        f"Получают уведомления: <b>{recipients_n}</b> чел.\n"
        f"Последняя проверка: <code>{last_at}</code>\n"
        f"Результат: {last_res}"
    )


def format_recipients_help(st: dict) -> str:
    extras = st.get("recipients", [])
    lines = [
        "<b>👥 Получатели уведомлений</b>\n",
        f"• <b>Ты</b> (админ) — <code>{telegram_io.ADMIN_CHAT_ID}</code>",
    ]
    for chat_id in extras:
        lines.append(f"• <code>{chat_id}</code>")
    lines.append("")
    lines.append("<b>Как добавить человека:</b>")
    lines.append("1. Скажи ему открыть бота @userinfobot и нажать Start — он пришлёт ему его <b>ID</b> (число вида 123456789)")
    lines.append("2. <b>Этот человек ОБЯЗАТЕЛЬНО должен открыть нашего бота @Banking_incomes_bot и нажать Start</b> — без этого Telegram запрещает боту ему писать")
    lines.append("3. Пришли мне его ID командой:")
    lines.append("   <code>/add 123456789</code>")
    lines.append("")
    lines.append("<b>Как удалить:</b>")
    lines.append("<code>/remove 123456789</code>")
    lines.append("")
    lines.append("<b>Посмотреть список:</b> /recipients")
    return "\n".join(lines)


WELCOME_TEXT = (
    "👋 <b>Привет!</b>\n\n"
    "Я слежу за поступлениями на твой счёт в Беларусбанке "
    "и пишу сюда (и тем, кого ты добавишь) о каждом новом платеже.\n\n"
    "Проверяю автоматически <b>каждые 15 минут</b> в рабочее время "
    "(пн–пт, 08:00–17:00 МСК).\n\n"
    "<b>Кнопки внизу экрана:</b>\n"
    "🔄 <b>Проверить сейчас</b> — внеплановая проверка\n"
    "📋 <b>Последние платежи</b> — выписка за 30 дней\n"
    "📊 <b>Статус</b> — что сейчас с ботом\n"
    "👥 <b>Получатели</b> — кто получает уведомления + инструкции\n"
    "⏸ <b>Пауза</b> / ▶️ <b>Возобновить</b> — управление автопроверкой\n"
    "❓ <b>Помощь</b> — это сообщение\n\n"
    "<b>Команды для управления получателями:</b>\n"
    "<code>/add 123456789</code> — добавить получателя\n"
    "<code>/remove 123456789</code> — удалить получателя\n"
    "<code>/recipients</code> — список и инструкция"
)


# ────────────────────────── ОБРАБОТКА КОМАНД ──────────────────────────

def _parse_int_arg(args: str) -> int | None:
    m = re.search(r"-?\d+", args.strip())
    return int(m.group(0)) if m else None


def handle_commands(commands: list[tuple[str, str]], st: dict, pending: dict) -> None:
    for cmd, args in commands:
        log.info("Handling command: %s args=%r", cmd, args)

        if cmd in ("/start", "/help", "/menu"):
            telegram_io.send_to_admin(WELCOME_TEXT, with_keyboard=True)

        elif cmd == "/status":
            telegram_io.send_to_admin(format_status(st), with_keyboard=True)

        elif cmd == "/check":
            pending["force_check"] = True
            if st["is_paused"]:
                telegram_io.send_to_admin("🔄 Принято. Бот на паузе, но эту проверку выполню.")
            else:
                telegram_io.send_to_admin("🔄 Принято. Запускаю проверку прямо сейчас.")

        elif cmd == "/last":
            pending["show_last"] = True
            telegram_io.send_to_admin("⏳ Загружаю выписку за последние 30 дней…")

        elif cmd in ("/pause", "/stop"):
            if st["is_paused"]:
                telegram_io.send_to_admin("⏸ Уже на паузе. Возобновить: /resume", with_keyboard=True)
            else:
                st["is_paused"] = True
                telegram_io.send_to_admin(
                    "⏸ <b>Автопроверка приостановлена.</b>\n"
                    "Чтобы возобновить — нажми ▶️ Возобновить или /resume",
                    with_keyboard=True,
                )

        elif cmd == "/resume":
            if not st["is_paused"]:
                telegram_io.send_to_admin("▶️ Бот уже активен. Проверки идут по расписанию.", with_keyboard=True)
            else:
                st["is_paused"] = False
                telegram_io.send_to_admin(
                    "▶️ <b>Автопроверка возобновлена.</b>\n"
                    "Следующая проверка — в ближайшие 15 минут.",
                    with_keyboard=True,
                )

        elif cmd == "/recipients":
            telegram_io.send_to_admin(format_recipients_help(st), with_keyboard=True)

        elif cmd == "/add":
            new_id = _parse_int_arg(args)
            if not new_id:
                telegram_io.send_to_admin(
                    "❌ Нужен Telegram ID числом.\n"
                    "Пример: <code>/add 123456789</code>\n\n"
                    "Чтобы узнать ID — открой /recipients (там инструкция).",
                    with_keyboard=True,
                )
                continue
            if new_id == telegram_io.ADMIN_CHAT_ID:
                telegram_io.send_to_admin("ℹ️ Ты — админ, уже получаешь все уведомления.", with_keyboard=True)
                continue
            if new_id in st["recipients"]:
                telegram_io.send_to_admin(
                    f"ℹ️ Получатель <code>{new_id}</code> уже в списке.\n"
                    f"Посмотреть всех: /recipients",
                    with_keyboard=True,
                )
                continue
            # Пробуем отправить ему приветственное сообщение чтобы проверить, что он /start-овал бота
            ok, err = telegram_io._post(
                new_id,
                "🔔 Привет! Ты теперь подписан на уведомления о поступлениях на счёт ООО «ЭкоРан Про». "
                "О каждом новом входящем платеже я буду тебе писать сюда.",
                with_keyboard=False,
            )
            if not ok:
                if "blocked" in err.lower() or "chat not found" in err.lower() or "deactivated" in err.lower() or "Forbidden" in err:
                    telegram_io.send_to_admin(
                        f"❌ Не могу написать пользователю <code>{new_id}</code>.\n\n"
                        f"<b>Причина:</b> он ещё не нажал Start у нашего бота.\n\n"
                        f"<b>Что делать:</b> попроси его открыть <b>@Banking_incomes_bot</b> в Telegram "
                        f"и нажать <b>Start</b>. После этого повтори команду:\n"
                        f"<code>/add {new_id}</code>",
                        with_keyboard=True,
                    )
                else:
                    telegram_io.send_to_admin(
                        f"❌ Ошибка при отправке <code>{new_id}</code>:\n<code>{err}</code>",
                        with_keyboard=True,
                    )
                continue
            st["recipients"].append(new_id)
            telegram_io.send_to_admin(
                f"✅ Готово. <code>{new_id}</code> добавлен в список получателей.\n"
                f"Ему уже отправлено приветствие.\n"
                f"Всего получателей: <b>{1 + len(st['recipients'])}</b>",
                with_keyboard=True,
            )

        elif cmd == "/remove":
            rem_id = _parse_int_arg(args)
            if not rem_id:
                telegram_io.send_to_admin(
                    "❌ Нужен Telegram ID числом.\n"
                    "Пример: <code>/remove 123456789</code>",
                    with_keyboard=True,
                )
                continue
            if rem_id == telegram_io.ADMIN_CHAT_ID:
                telegram_io.send_to_admin("ℹ️ Тебя самого удалить нельзя — ты админ.", with_keyboard=True)
                continue
            if rem_id not in st["recipients"]:
                telegram_io.send_to_admin(
                    f"ℹ️ <code>{rem_id}</code> и так не в списке. Текущий список: /recipients",
                    with_keyboard=True,
                )
                continue
            st["recipients"].remove(rem_id)
            telegram_io.send_to_admin(
                f"✅ <code>{rem_id}</code> удалён.\n"
                f"Осталось получателей: <b>{1 + len(st['recipients'])}</b>",
                with_keyboard=True,
            )

        else:
            telegram_io.send_to_admin(
                f"Неизвестная команда: <code>{cmd}</code>\nИспользуй /help",
                with_keyboard=True,
            )


# ────────────────────────────── ПРОВЕРКА БАНКА ──────────────────────────

async def do_bank_check(st: dict) -> str:
    try:
        transactions = await fetch_incoming_transactions(days_back=3)
    except Exception as exc:
        log.exception("Bank scrape failed")
        telegram_io.send_to_admin(
            f"❌ <b>Ошибка при обращении к банку</b>\n<code>{exc}</code>",
            with_keyboard=True,
        )
        return f"ошибка: {exc}"

    is_first_run = len(st["seen_transactions"]) == 0
    new_count = 0
    seen = set(st["seen_transactions"])

    for tx in transactions:
        if not tx.transaction_id or tx.transaction_id in seen:
            continue
        if is_first_run:
            log.info("Initial run: marking %s as seen", tx.transaction_id)
        else:
            telegram_io.broadcast(format_payment(tx), st.get("recipients", []))
            new_count += 1
        seen.add(tx.transaction_id)

    st["seen_transactions"] = sorted(seen)

    if is_first_run and transactions:
        telegram_io.send_to_admin(
            "✅ Бот подключился к счёту.\n"
            f"Найдено {len(transactions)} ранее прошедших поступлений — отмечены как «уже учтённые», "
            "уведомлений по ним не будет.\n\n"
            "О каждом <b>новом</b> поступлении пришлю отдельное сообщение."
        )

    return f"новых поступлений: {new_count}"


# ─────────────────────── /last  «Последние платежи» ──────────────────────

async def show_last_payments():
    try:
        recent = await fetch_incoming_transactions(days_back=30)
    except Exception as exc:
        log.exception("/last failed")
        telegram_io.send_to_admin(
            f"❌ <b>Не удалось загрузить выписку</b>\n<code>{exc}</code>",
            with_keyboard=True,
        )
        return

    if not recent:
        telegram_io.send_to_admin("За последние 30 дней поступлений не было.", with_keyboard=True)
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

    full = "\n\n".join(blocks)
    chunk_size = 3800
    for i in range(0, len(full), chunk_size):
        is_last = (i + chunk_size >= len(full))
        telegram_io.send_to_admin(full[i:i + chunk_size], with_keyboard=is_last)


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
