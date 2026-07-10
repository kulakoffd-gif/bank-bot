"""
Belarusbank corporate IB scraper — dcsc.belarusbank.by.

Полный UI-flow: логин → Счета → клик по строке счёта → клик Сформировать.
Перехватываем ответ /ibservices/account/getAccountStatement.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

BASE_URL = "https://dcsc.belarusbank.by"
LOGIN_URL = f"{BASE_URL}/auth"

SEL_LOGIN_INPUT    = 'input[placeholder="Логин"]'
SEL_PASSWORD_INPUT = 'input[placeholder="Пароль"]'
SEL_SUBMIT_BTN     = 'button[type="submit"]'


@dataclass
class Transaction:
    transaction_id: str
    dedup_key: str
    booking_date: str
    amount: str
    currency: str
    counterparty: str
    payer_unp: str  # УНП плательщика — для матчинга с AmoCRM
    purpose: str


# Заполняется _handle_post_login_modal, читается check.py:
#   None — окна не было; иначе {"text": str, "screenshot": "/tmp/bank_notice.png"|""}
LAST_NOTICE: dict | None = None

# Кнопки, которыми безопасно ЗАКРЫТЬ окно банка (не «Прочитать»/«Изменить» —
# они могут увести на другой экран).
_MODAL_CLOSE_LABELS = ["Пропустить", "Продолжить", "Закрыть", "Позже", "ОК", "Понятно", "Ознакомлен"]


async def _handle_post_login_modal(page) -> dict | None:
    """Если после входа появилось окно банка — снять скриншот+текст и закрыть его.

    Возвращает {"text", "screenshot"} если окно было, иначе None.
    Сам скриншот/текст НЕ отправляет — это делает check.py (там дедуп по тексту,
    чтобы не слать одно и то же объявление каждый прогон).
    """
    # Ищем текст окна: находим видимую кнопку-«закрыть» и поднимаемся к контейнеру-модалке.
    try:
        info = await page.evaluate(
            """(labels) => {
                const btns = [...document.querySelectorAll('button, [role=button]')];
                let target = null;
                for (const b of btns) {
                    const t = (b.innerText || '').trim();
                    if (labels.includes(t) && b.offsetParent !== null) { target = b; break; }
                }
                if (!target) return null;
                let el = target;
                for (let i = 0; i < 8 && el; i++) {
                    const cls = (el.className || '') + '';
                    const role = el.getAttribute && el.getAttribute('role');
                    if (role === 'dialog' || /modal|dialog|popup/i.test(cls)) break;
                    el = el.parentElement;
                }
                const box = el || target.closest('div');
                return { text: (box ? box.innerText : '').trim().slice(0, 1500) };
            }""",
            _MODAL_CLOSE_LABELS,
        )
    except Exception as e:
        log.warning("Modal detection failed: %s", e)
        return None

    if not info:
        return None  # окна нет — обычный вход

    notice = {"text": info.get("text", ""), "screenshot": "/tmp/bank_notice.png"}
    log.info("Post-login modal detected (%d chars of text) — screenshotting", len(notice["text"]))
    try:
        await page.screenshot(path=notice["screenshot"], full_page=True)
    except Exception as e:
        log.warning("Could not screenshot modal: %s", e)
        notice["screenshot"] = ""

    # Закрываем окно, чтобы продолжить к «Счета».
    for label in _MODAL_CLOSE_LABELS:
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                log.info("Closed post-login modal via '%s'", label)
                await asyncio.sleep(2)
                break
        except Exception:
            continue
    return notice


async def _dump_debug(page, tag: str) -> None:
    """Сохранить скриншот + HTML страницы в /tmp для диагностики.

    Файлы /tmp/<tag>.png и /tmp/<tag>.html заливаются как артефакты GitHub Actions
    (шаг Upload debug HTML), чтобы можно было увидеть, как выглядит страница банка
    в момент падения (например, новый экран запроса местоположения).
    """
    try:
        await page.screenshot(path=f"/tmp/{tag}.png", full_page=True)
    except Exception as e:
        log.warning("Could not screenshot %s: %s", tag, e)
    try:
        html = await page.content()
        with open(f"/tmp/{tag}.html", "w", encoding="utf-8") as f:
            f.write(html)
        log.warning("Saved debug %s.html (url=%s, %d chars)", tag, page.url, len(html))
    except Exception as e:
        log.warning("Could not dump HTML %s: %s", tag, e)


def _make_dedup_key(r: dict) -> str:
    """Стабильный ключ дедупликации.

    Для входящего платежа на нашем счёте:
      - correspondentUNP = УНП отправителя (контрагент)
      - documentNumber   = номер платёжки
      - documentDate     = дата платёжки
      - amount           = сумма
    """
    parts = [
        str(r.get("correspondentUNP") or r.get("payerUnp") or "").strip(),
        str(r.get("documentNumber") or "").strip(),
        str(r.get("documentDate") or r.get("docDate") or r.get("acceptDate") or "").strip()[:10],
        str(r.get("amount") or "").strip(),
    ]
    return "|".join(parts)


async def fetch_incoming_transactions(days_back: int = 3) -> list[Transaction]:
    login = os.environ["BANK_LOGIN"]
    password = os.environ["BANK_PASSWORD"]
    iban = os.environ.get("BANK_ACCOUNT_IBAN", "")

    # Банк периодически «залипает» на входе (форма отправлена, но страница не
    # уходит) — разовый сбой на стороне банка. Делаем несколько попыток с чистой
    # сессией, прежде чем сдаваться, чтобы такие разовые сбои не доходили до админа.
    global LAST_NOTICE
    LAST_NOTICE = None
    attempts = int(os.environ.get("BANK_LOGIN_ATTEMPTS", "3"))
    last_exc: Exception | None = None

    async with async_playwright() as pw:
        for attempt in range(1, attempts + 1):
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            # Банк с 2026-06-30 требует местоположение при входе — выдаём браузеру
            # разрешение на геолокацию и координаты Минска, чтобы сайт не блокировал
            # форму логина в ожидании доступа к геопозиции.
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ru-RU",
                timezone_id="Europe/Minsk",
                geolocation={"latitude": 53.9006, "longitude": 27.5590},  # Минск
                permissions=["geolocation"],
            )
            page = await context.new_page()
            try:
                result = await _do_scrape(page, login, password, iban, days_back)
                if attempt > 1:
                    log.info("Scrape succeeded on attempt %d/%d", attempt, attempts)
                return result
            except Exception as exc:
                last_exc = exc
                log.warning("Scrape attempt %d/%d failed: %s", attempt, attempts, exc)
            finally:
                await context.close()
                await browser.close()
            if attempt < attempts:
                await asyncio.sleep(5)

    # Все попытки исчерпаны — пробрасываем последнюю ошибку (её увидит check.py и
    # уведомит админа).
    raise last_exc if last_exc else RuntimeError("Bank scrape failed (no attempts run)")


async def _do_scrape(page, login, password, iban, days_back):
    days_back = max(days_back, 3)

    # === ЛОГИН ===
    log.info("Opening login page")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_selector(SEL_LOGIN_INPUT, timeout=20_000)
    except PlaywrightTimeout:
        # Поле логина не появилось — вероятно, новый экран банка (запрос
        # местоположения / согласие). Сохраняем страницу для диагностики.
        log.error("Login input not found — dumping page for diagnosis")
        await _dump_debug(page, "login_fail")
        raise
    await asyncio.sleep(2)
    await page.fill(SEL_LOGIN_INPUT, login)
    await page.fill(SEL_PASSWORD_INPUT, password)
    await page.click(SEL_SUBMIT_BTN)

    try:
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/auth')", timeout=30_000)
    except PlaywrightTimeout:
        # Форма отправлена, но остались на /auth. Возможно, новый экран банка
        # (подтверждение местоположения / доп. проверка) или вход отклонён.
        # Сохраняем страницу, чтобы увидеть, что именно показывает банк.
        log.error("Still on /auth after submit — dumping page for diagnosis")
        await _dump_debug(page, "login_submit_fail")
        raise RuntimeError("Login failed — did not leave /auth (возможно, новый экран банка после ввода)")

    log.info("Logged in, URL=%s", page.url)
    await asyncio.sleep(5)

    # === МОДАЛКА ПОСЛЕ ВХОДА: заснять → сообщить → закрыть ===
    # Банк периодически показывает окно (объявления о тарифах, «Пароль истекает»
    # и т.п.), которое перекрывает меню и ломает клик по «Счета». Прежде чем
    # закрыть — снимаем скриншот и текст, чтобы админ не пропустил важное.
    # Результат кладём в модульную LAST_NOTICE, дедуп/отправку делает check.py.
    global LAST_NOTICE
    LAST_NOTICE = await _handle_post_login_modal(page)

    # === ПЕРЕХВАТЧИК ОТВЕТА getAccountStatement ===
    statement_responses: list[dict] = []

    async def on_response(resp):
        u = resp.url
        if "/ibservices/account/getAccountStatement" in u and resp.status == 200:
            try:
                body = await resp.text()
                statement_responses.append({"url": u, "body": body})
                log.info("Captured getAccountStatement response, size=%d", len(body))
            except Exception as e:
                log.warning("Could not read response: %s", e)

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    # === КЛИК НА «Счета» ===
    log.info("Clicking 'Счета'")
    try:
        await page.get_by_text("Счета", exact=True).first.click(timeout=10_000)
    except Exception as e:
        # Не смогли нажать «Счета» — вероятно, новый экран/модалка банка после входа.
        # Сохраняем страницу, чтобы увидеть, что показывает банк.
        log.error("Could not click 'Счета' — dumping page for diagnosis")
        await _dump_debug(page, "scheta_fail")
        raise RuntimeError(f"Could not click 'Счета': {e}")
    await asyncio.sleep(4)

    # === КЛИК НА СТРОКУ С НАШИМ IBAN ===
    iban_target = iban.replace(" ", "")
    iban_with_spaces = " ".join([iban_target[i:i+4] for i in range(0, len(iban_target), 4)])

    log.info("Clicking account row with IBAN %s", iban_with_spaces[:18] + "…")
    try:
        await page.get_by_text(iban_with_spaces).first.click(timeout=10_000)
        await asyncio.sleep(3)
    except Exception as e:
        await _dump_debug(page, "iban_fail")
        raise RuntimeError(f"Could not click IBAN row: {e}")

    # === КЛИК НА «Сформировать» в этой же строке ===
    log.info("Clicking 'Сформировать' for our account")
    # До этого мы видели что в строке нашего счёта есть кнопка «Выписка»
    # После клика по строке счёт открывается, а под ним — кнопка «Сформировать»

    # Сначала нажмём «Выписка» по строке счёта
    clicked_vypiska = await page.evaluate("""(iban) => {
        const rows = document.querySelectorAll('tr, [role=row], [class*=row]');
        for (const row of rows) {
            const txt = (row.innerText || '').replace(/\\s/g, '');
            if (txt.includes(iban.replace(/\\s/g, ''))) {
                const btn = row.querySelector('button');
                if (btn) { btn.scrollIntoView(); btn.click(); return true; }
            }
        }
        return false;
    }""", iban_with_spaces)
    log.info("  Выписка clicked: %s", clicked_vypiska)
    await asyncio.sleep(7)
    log.info("  URL: %s", page.url)

    # Установим даты dateFrom и dateTo через URL — затем нажмём Сформировать
    date_from = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = date.today().strftime("%Y-%m-%d")
    account_id = int(os.environ.get("BANK_ACCOUNT_ID", "18067"))
    client_id = int(os.environ.get("BANK_CLIENT_ID", "80182"))

    statement_url = f"{BASE_URL}/work-place/account-statement?accountId={account_id}&clientId={client_id}&dateFrom={date_from}&dateTo={date_to}"
    log.info("Going to statement URL with dates: %s", statement_url)
    await page.goto(statement_url, wait_until="domcontentloaded", timeout=30_000)
    await asyncio.sleep(7)

    # === КЛИК «Сформировать» ===
    clicked = await page.evaluate("""(label) => {
        const btns = document.querySelectorAll('button, [role=button], a, [type=submit]');
        for (const b of btns) {
            if ((b.innerText || '').trim() === label && b.offsetParent !== null) {
                b.scrollIntoView(); b.click(); return true;
            }
        }
        return false;
    }""", "Сформировать")
    log.info("  Сформировать clicked: %s", clicked)
    await asyncio.sleep(15)  # ждём загрузку выписки

    # === ОБРАБОТКА ПЕРЕХВАЧЕННЫХ ОТВЕТОВ ===
    if not statement_responses:
        log.warning("No getAccountStatement responses captured!")
        # Запасной вариант — пробуем дёрнуть API из контекста страницы
        return []

    # Берём ответ с самым большим body (там вероятно operations)
    best = max(statement_responses, key=lambda r: len(r["body"]))
    log.info("Using response, size=%d, total captured=%d",
             len(best["body"]), len(statement_responses))

    data = json.loads(best["body"])
    if data.get("errorInfo", {}).get("error") != "0":
        log.error("Bank error: %s", data.get("errorInfo"))
        return []

    log.info("Top-level keys: %s", sorted(data.keys()))
    log.info("items=%s, opening=%s, closing=%s",
             data.get("items"), data.get("openingBalance"), data.get("closingBalance"))

    # Ищем массив операций
    operations = None
    for key in ("operations", "operationList", "items_data", "rows", "list",
                "gridRows", "statementList", "statementRows", "statement",
                "accountStatementList", "accountStatement"):
        if isinstance(data.get(key), list):
            operations = data[key]
            log.info("Operations found under '%s', count=%d", key, len(operations))
            break

    if operations is None:
        # Любой непустой список dict
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                log.info("Possible operations list under '%s' (size=%d), first keys=%s",
                         k, len(v), sorted(v[0].keys())[:10])
                if operations is None:
                    operations = v
                    break

    if not operations:
        log.warning("No operations found. Full response keys: %s", sorted(data.keys()))
        log.warning("First 3000 chars of body: %s", best["body"][:3000])
        return []

    log.info("=== Keys of first op: %s ===", sorted(operations[0].keys()))
    log.info("Full first op: %s", json.dumps(operations[0], ensure_ascii=False, indent=2)[:3000])

    # Покажем direction-related поля для 3 первых ops чтобы определить как фильтровать
    log.info("=== Direction-related fields for first 3 ops ===")
    for i, op in enumerate(operations[:3]):
        log.info(
            "op[%d] amount=%s amountDebit=%s amountNatural=%s chargesType=%s typeDoc=%s "
            "correspName=%s correspUNP=%s benefAccount=%s",
            i,
            op.get("amount"), op.get("amountDebit"), op.get("amountNatural"),
            op.get("chargesType"), op.get("typeDoc"),
            (op.get("correspondentName") or "")[:30], op.get("correspondentUNP"),
            op.get("beneficiarAccount"),
        )

    # Парсим: для каждой операции пробуем угадать направление
    incoming = []
    for op in operations:
        # Гипотеза 1: amountDebit > 0 → исходящий, иначе входящий
        amt_debit = float(str(op.get("amountDebit") or 0).replace(",", ".") or 0)
        amt = float(str(op.get("amount") or 0).replace(",", ".") or 0)
        is_incoming_v1 = amt_debit == 0 and amt > 0

        # Гипотеза 2: chargesType
        charges = (op.get("chargesType") or "").lower()
        is_incoming_v2 = charges in ("credit", "income", "incoming", "in")

        if is_incoming_v1 or is_incoming_v2:
            tx = Transaction(
                transaction_id=str(op.get("bmsgid") or ""),
                dedup_key=_make_dedup_key(op),
                booking_date=str(op.get("acceptDate") or op.get("documentDate") or "")[:10],
                amount=str(amt),
                currency=op.get("currencyIso") or op.get("currency") or "BYN",
                counterparty=str(op.get("correspondentName") or "Неизвестно"),
                payer_unp=str(op.get("correspondentUNP") or "").strip(),
                purpose=str(op.get("paymentPurpose") or "—"),
            )
            incoming.append(tx)

    log.info("Filtered %d incoming transactions", len(incoming))
    for tx in incoming[:5]:
        log.info("  %s | %s %s | %s",
                 tx.booking_date, tx.amount, tx.currency, tx.counterparty[:40])
    return incoming
