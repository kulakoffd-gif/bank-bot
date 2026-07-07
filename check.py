"""Один прогон бота: опрос Telegram → команды → проверка банка → уведомления."""

import asyncio
import hashlib
import logging
import re
import sys
from datetime import datetime, timezone

import amo_client
import bank_scraper
import state
import telegram_io
from bank_scraper import fetch_incoming_transactions, Transaction

# Минимальный интервал между обращениями к банку (минут)
BANK_CHECK_INTERVAL_MIN = 14

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────── ФОРМАТИРОВАНИЕ ───────────────────────────

def format_payment(tx: Transaction, routing_info: str = "") -> str:
    body = (
        "💰 <b>Новое поступление</b>\n\n"
        f"<b>Сумма:</b> {tx.amount} {tx.currency}\n"
        f"<b>Дата:</b> {tx.booking_date}\n"
        f"<b>От:</b> {tx.counterparty}\n"
        f"<b>Назначение:</b> {tx.purpose}"
    )
    if routing_info:
        body += f"\n\n{routing_info}"
    return body


def resolve_routing(tx: Transaction, st: dict) -> tuple[list[int], str, str]:
    """Определяет кому слать уведомление о платеже.

    Returns:
        (manager_chat_ids, admin_copy_note, manager_label)

    - manager_chat_ids — telegram_chat_id ответственного менеджера (список из 0 или 1)
    - admin_copy_note — текст для админа («Копия отправлена: ...»)
    - manager_label — текст для самого менеджера (контекст клиента)
    """
    if not amo_client.is_configured():
        return [], "📬 <i>Копия отправлена:</i> только тебе и в канал (AmoCRM не подключён)", ""

    if not tx.payer_unp:
        return [], "📬 <i>Копия отправлена:</i> только тебе и в канал (УНП плательщика не определён)", ""

    company = amo_client.find_company_by_unp(tx.payer_unp)
    if not company:
        return [], (
            f"📬 <i>Копия отправлена:</i> <b>никому</b> — клиент с УНП "
            f"<code>{tx.payer_unp}</code> отсутствует в AmoCRM"
        ), ""

    amo_user_id = company.get("responsible_user_id")
    company_name = company.get("name", "")

    # Получим имя менеджера для красивого вывода
    manager_name = "?"
    if amo_user_id:
        user_info = amo_client.get_user_info(int(amo_user_id))
        if user_info:
            manager_name = user_info.get("name", "?")

    routing = st.get("manager_routing", {})
    manager_chat_id = routing.get(str(amo_user_id))

    if not manager_chat_id:
        return [], (
            f"📇 <i>Клиент в AmoCRM:</i> {company_name}\n"
            f"📬 <i>Копия отправлена:</i> <b>никому</b> — менеджер "
            f"{manager_name} (AmoCRM id={amo_user_id}) не привязан к Telegram"
        ), ""

    return (
        [int(manager_chat_id)],
        f"📇 <i>Клиент:</i> {company_name}\n"
        f"📬 <i>Копия отправлена:</i> <b>{manager_name}</b>",
        f"📇 <i>Клиент:</i> {company_name}",
    )


def format_status(st: dict) -> str:
    state_icon = "⏸ <b>на паузе</b>" if st["is_paused"] else "▶️ <b>активен</b>"
    last_at = st.get("last_check_at") or "—"
    last_res = st.get("last_check_result") or "—"
    recipients_n = 1 + len(st.get("co_admins", [])) + len(st.get("recipients", []))
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
    "<code>/recipients</code> — список и инструкция\n\n"
    "<b>Менеджеры (AmoCRM роутинг):</b>\n"
    "<code>/managers</code> — список менеджеров AmoCRM и их Telegram-привязки\n"
    "<code>/link_manager &lt;amocrm_id&gt; &lt;telegram_id&gt;</code> — привязать менеджера\n"
    "<code>/unlink_manager &lt;amocrm_id&gt;</code> — снять привязку"
)


def format_managers_help(st: dict) -> str:
    """Список менеджеров AmoCRM с их Telegram-привязками."""
    routing = st.get("manager_routing", {})

    if not amo_client.is_configured():
        return (
            "❌ <b>AmoCRM не настроен</b>\n\n"
            "Не заданы переменные окружения AMO_TOKEN или AMO_SUBDOMAIN."
        )

    users = amo_client.list_users()
    if not users:
        return "❌ Не удалось получить список менеджеров из AmoCRM."

    lines = ["<b>👤 Менеджеры AmoCRM</b>\n"]
    for u in users:
        amo_id = str(u.get("id"))
        name = u.get("name", "?")
        email = u.get("email", "")
        tg = routing.get(amo_id)
        if tg:
            lines.append(f"✅ <b>{name}</b>\n  AmoCRM <code>{amo_id}</code> → Telegram <code>{tg}</code>")
        else:
            lines.append(f"⬜ <b>{name}</b>\n  AmoCRM <code>{amo_id}</code> — не привязан\n  {email}")
    lines.append("")
    lines.append("<b>Как привязать:</b>")
    lines.append("1. Менеджер открывает @userinfobot → /start → получает свой Telegram ID")
    lines.append("2. <b>ОБЯЗАТЕЛЬНО</b> открывает @Banking_incomes_bot → /start")
    lines.append("3. Ты выполняешь: <code>/link_manager AMO_ID TELEGRAM_ID</code>")
    lines.append("   например: <code>/link_manager 11527566 123456789</code>")
    return "\n".join(lines)


# ────────────────────────── ОБРАБОТКА КОМАНД ──────────────────────────

def _parse_int_arg(args: str) -> int | None:
    m = re.search(r"-?\d+", args.strip())
    return int(m.group(0)) if m else None


def _minutes_since(iso_str: str | None) -> float | None:
    """Сколько минут прошло с момента iso_str (UTC). None если нет данных."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 60
    except Exception:
        return None


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

        elif cmd == "/managers":
            telegram_io.send_to_admin(format_managers_help(st), with_keyboard=True)

        elif cmd == "/link_manager":
            # /link_manager 11527566 123456789
            nums = re.findall(r"-?\d+", args)
            if len(nums) < 2:
                telegram_io.send_to_admin(
                    "❌ Формат: <code>/link_manager amocrm_id telegram_id</code>\n"
                    "Пример: <code>/link_manager 11527566 123456789</code>\n\n"
                    "Список менеджеров — /managers",
                    with_keyboard=True,
                )
                continue
            amo_id, tg_id = nums[0], int(nums[1])
            # Проверим что менеджер существует в AmoCRM
            user_info = amo_client.get_user_info(int(amo_id))
            if not user_info:
                telegram_io.send_to_admin(
                    f"⚠️ Менеджер AmoCRM <code>{amo_id}</code> не найден. "
                    f"Список доступных — /managers",
                    with_keyboard=True,
                )
                continue
            # Проверим что бот может писать менеджеру
            ok, err = telegram_io._post(
                tg_id,
                f"🔔 Привет! Тебя только что подключили к боту уведомлений о платежах в "
                f"ООО «ЭкоРан Про». Тебе будут приходить уведомления о новых поступлениях "
                f"от твоих клиентов (по данным AmoCRM).",
                with_keyboard=False,
            )
            if not ok:
                msg_lower = err.lower()
                if "blocked" in msg_lower or "chat not found" in msg_lower or "forbidden" in msg_lower:
                    telegram_io.send_to_admin(
                        f"❌ Не могу написать менеджеру <code>{tg_id}</code>.\n\n"
                        f"<b>Причина:</b> он ещё не нажал Start у нашего бота.\n\n"
                        f"<b>Что делать:</b> {user_info.get('name', 'менеджер')} должен открыть "
                        f"<b>@Banking_incomes_bot</b> в Telegram и нажать <b>Start</b>. "
                        f"После этого повтори команду:\n"
                        f"<code>/link_manager {amo_id} {tg_id}</code>",
                        with_keyboard=True,
                    )
                else:
                    telegram_io.send_to_admin(
                        f"❌ Ошибка отправки: <code>{err}</code>",
                        with_keyboard=True,
                    )
                continue

            st.setdefault("manager_routing", {})[str(amo_id)] = tg_id
            telegram_io.send_to_admin(
                f"✅ Привязал.\n\n"
                f"<b>{user_info.get('name', '?')}</b> (AmoCRM <code>{amo_id}</code>) "
                f"→ Telegram <code>{tg_id}</code>\n\n"
                f"Теперь все платежи от клиентов, за которых он отвечает в AmoCRM, "
                f"будут приходить ему лично.",
                with_keyboard=True,
            )

        elif cmd == "/unlink_manager":
            nums = re.findall(r"-?\d+", args)
            if not nums:
                telegram_io.send_to_admin(
                    "❌ Формат: <code>/unlink_manager amocrm_id</code>\n"
                    "Пример: <code>/unlink_manager 11527566</code>",
                    with_keyboard=True,
                )
                continue
            amo_id = nums[0]
            routing = st.setdefault("manager_routing", {})
            if amo_id not in routing:
                telegram_io.send_to_admin(
                    f"ℹ️ Менеджер <code>{amo_id}</code> и так не привязан.",
                    with_keyboard=True,
                )
                continue
            tg_id = routing.pop(amo_id)
            telegram_io.send_to_admin(
                f"✅ Привязка снята: AmoCRM <code>{amo_id}</code> ↛ Telegram <code>{tg_id}</code>",
                with_keyboard=True,
            )

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

def _surface_bank_notice(st: dict) -> None:
    """Если при входе банк показал окно — переслать его админу (один раз на уникальный текст).

    Скриншот+текст собирает bank_scraper в LAST_NOTICE; здесь дедуп по тексту через
    state["seen_bank_notices"], чтобы одно и то же объявление не слалось каждый прогон.
    """
    notice = getattr(bank_scraper, "LAST_NOTICE", None)
    if not notice:
        return

    text = (notice.get("text") or "").strip()
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] if text else "bank-notice-no-text"

    seen = st.setdefault("seen_bank_notices", [])
    if key in seen:
        log.info("Bank notice already sent earlier (key=%s) — skip", key)
        return

    caption = (
        "🔔 <b>Банк показал окно при входе</b>\n"
        "Бот закрыл его, чтобы продолжить работу. Проверь — вдруг важное 👇"
    )
    if text:
        caption += f"\n\n{text[:900]}"

    sent = False
    shot = notice.get("screenshot")
    if shot:
        sent, _ = telegram_io.send_photo_to_admin(shot, caption)
    if not sent:
        telegram_io.send_to_admin(caption, with_keyboard=True)

    seen.append(key)
    st["seen_bank_notices"] = seen[-50:]
    log.info("Bank notice forwarded to admin (key=%s)", key)


async def do_bank_check(st: dict) -> str:
    try:
        transactions = await fetch_incoming_transactions(days_back=3)
    except Exception as exc:
        log.exception("Bank scrape failed")
        _surface_bank_notice(st)  # окно могло всплыть до сбоя — не теряем его
        telegram_io.send_to_admin(
            f"❌ <b>Ошибка при обращении к банку</b>\n<code>{exc}</code>",
            with_keyboard=True,
        )
        return f"ошибка: {exc}"

    _surface_bank_notice(st)

    # ВАЖНО: дедуплицируем по стабильному ключу (УНП + № документа + дата + сумма),
    # потому что Id в банке меняется между стадиями обработки платежа.
    # Поле seen_transactions хранит композитные ключи (после миграции).

    # Защита от первого запуска С НОВОЙ ЛОГИКОЙ: если все элементы старого формата
    # (просто цифры), считаем что состояние «грязное» и переинициализируем тихо.
    old_format = all(s.isdigit() for s in st["seen_transactions"]) if st["seen_transactions"] else False
    is_first_run = (len(st["seen_transactions"]) == 0) or old_format
    if old_format:
        log.warning("State has OLD-format IDs (digits only). Migrating: marking all current as seen, no notifications.")
        st["seen_transactions"] = []

    new_count = 0
    seen = set(st["seen_transactions"])

    for tx in transactions:
        key = tx.dedup_key
        if not key or key in seen:
            continue
        if is_first_run:
            log.info("Initial run: marking key=%s (id=%s) as seen", key, tx.transaction_id)
        else:
            # Определяем кому слать
            manager_chats, admin_note, manager_note = resolve_routing(tx, st)

            # 1. АДМИНУ + co-admins (полные копии всех платежей с тем же admin_note)
            admins = [telegram_io.ADMIN_CHAT_ID] + list(st.get("co_admins", []))
            for admin_chat in admins:
                telegram_io._post(admin_chat, format_payment(tx, admin_note),
                                  with_keyboard=False)

            # 2. КАНАЛАМ/доп. подписчикам — чистое сообщение (без аннотации)
            for recipient in st.get("recipients", []):
                if recipient in admins:
                    continue
                telegram_io._post(recipient, format_payment(tx), with_keyboard=False)

            # 3. МЕНЕДЖЕРУ (если есть) — с указанием клиента;
            #    скипаем, если он уже получил копию как admin/co-admin
            for manager_chat in manager_chats:
                if manager_chat in admins:
                    continue
                telegram_io._post(manager_chat, format_payment(tx, manager_note),
                                  with_keyboard=False)

            new_count += 1
            log.info("Notified about tx %s, manager_chats=%s", key, manager_chats)
        seen.add(key)

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

    # Дросселирование: банк не дёргаем чаще чем раз в 14 минут
    # (исключение: ручная команда /check всегда форсирует)
    forced = pending.get("force_check", False)
    minutes_since_check = _minutes_since(st.get("last_check_at"))
    too_soon = (minutes_since_check is not None
                and minutes_since_check < BANK_CHECK_INTERVAL_MIN
                and not forced)

    if st["is_paused"] and not forced:
        log.info("Skipping bank check — bot is paused")
        state.mark_check(st, "пропущено (на паузе)")
    elif too_soon:
        log.info("Skipping bank check — last check was %.1f min ago (need ≥%d)",
                 minutes_since_check, BANK_CHECK_INTERVAL_MIN)
        # last_check_at и last_check_result не трогаем — пусть остаются от прошлого
    else:
        log.info("Running bank check (paused=%s, forced=%s, since_last=%s)",
                 st["is_paused"], forced, minutes_since_check)
        result = await do_bank_check(st)
        state.mark_check(st, result)

    if pending.get("show_last"):
        await show_last_payments()

    state.save(st)
    log.info("Done. Result: %s", st["last_check_result"])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
