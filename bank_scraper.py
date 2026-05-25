"""
Belarusbank corporate Internet Banking scraper.

Logs into icb.asb.by, opens the account statement page, parses incoming payments.

NOTE: selectors below are based on the typical structure of the icb.asb.by web app.
After the first real run we may need to adjust them once we see the actual page —
all selector strings are gathered at the top of this file for easy tweaking.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

LOGIN_URL = "https://icb.asb.by/Login/Index"
ACCOUNTS_URL = "https://icb.asb.by/"
INCOMING_PAYMENTS_URL = "https://icb.asb.by/Accounts/IncomingPayments"

# --- селекторы (правим после первого запуска, если структура страницы другая) ---
SEL_LOGIN_INPUT    = 'input[name="Login"], input[type="text"]'
SEL_PASSWORD_INPUT = 'input[name="Password"], input[type="password"]'
SEL_SUBMIT_BTN     = 'button[type="submit"], input[type="submit"]'
SEL_STATEMENT_ROW  = 'table tr, .statement-row, .operation-row'


@dataclass
class Transaction:
    transaction_id: str       # сырое поле Id из банка (может меняться между стадиями обработки!)
    dedup_key: str            # СТАБИЛЬНЫЙ ключ для дедупликации (не меняется в банке)
    booking_date: str
    amount: str
    currency: str
    counterparty: str
    purpose: str


def _make_dedup_key(r: dict) -> str:
    """Стабильный ключ для дедупликации — НЕ зависит от внутреннего Id банка,
    который меняется между состояниями (предв./проведённый/окончательный).

    Используем стабильные поля из платёжной инструкции:
      - УНП плательщика
      - Номер документа
      - Дата платёжной инструкции
      - Сумма перевода
    Эта комбинация уникальна для каждого реального платежа.
    """
    parts = [
        str(r.get("PayerUnp") or "").strip(),
        str(r.get("NumberPaymentInstructions") or "").strip(),
        str(r.get("DatePaymentInstructions") or "").strip()[:10],  # только дата YYYY-MM-DD
        str(r.get("AmountOfTransfer") or r.get("Debit") or "").strip(),
    ]
    return "|".join(parts)


async def fetch_incoming_transactions(days_back: int = 3) -> list[Transaction]:
    login = os.environ["BANK_LOGIN"]
    password = os.environ["BANK_PASSWORD"]
    iban = os.environ.get("BANK_ACCOUNT_IBAN", "")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        page = await context.new_page()

        try:
            transactions = await _do_scrape(page, login, password, iban, days_back)
        finally:
            await context.close()
            await browser.close()

    return transactions


async def _do_scrape(
    page: Page, login: str, password: str, iban: str, days_back: int
) -> list[Transaction]:
    log.info("Opening login page")
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30_000)

    log.info("Filling credentials")
    await page.fill(SEL_LOGIN_INPUT, login)
    await page.fill(SEL_PASSWORD_INPUT, password)
    await page.click(SEL_SUBMIT_BTN)

    try:
        await page.wait_for_url(re.compile(r"icb\.asb\.by/(?!Login)"), timeout=30_000)
    except PlaywrightTimeout:
        # сохраняем что увидели для диагностики
        html = await page.content()
        log.error("Login did not redirect. Current URL: %s", page.url)
        log.error("Page content (first 500 chars): %s", html[:500])
        raise RuntimeError("Login failed — check BANK_LOGIN / BANK_PASSWORD secrets")

    log.info("Logged in. Going to accounts page")
    await page.goto(ACCOUNTS_URL, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(3)

    # Получаем полный список счетов из памяти Kendo Grid и находим наш по IBAN
    iban_target = os.environ.get("BANK_ACCOUNT_IBAN", "").replace(" ", "")
    log.info("Target IBAN (no spaces): %s", iban_target)

    accounts_list = await page.evaluate("""() => {
        const grids = document.querySelectorAll('[data-role="grid"]');
        for (const el of grids) {
            const grid = $(el).data('kendoGrid');
            if (grid && grid.dataSource) {
                const data = grid.dataSource.data();
                if (data.length > 5) {  // основной грид со счетами
                    return JSON.stringify(data.map(r => ({
                        id: r.ID,
                        iban: r.AccountIban,
                        type: r.TypeAccountID,
                        balance: r.Balance,
                    })));
                }
            }
        }
        return "[]";
    }""")
    accounts = json.loads(accounts_list)
    log.info("Got %d accounts from grid", len(accounts))

    target_id = None
    for acc in accounts:
        if acc["iban"] and acc["iban"].replace(" ", "") == iban_target:
            target_id = acc["id"]
            log.info("Match found! AccountID=%s, IBAN=%s, balance=%s",
                     target_id, acc["iban"], acc["balance"])
            break

    if not target_id:
        log.error("Target IBAN %s NOT found in account list!", iban_target)
        raise RuntimeError(f"IBAN {iban_target} not found in bank")

    # Идём на страницу входящих платежей — нужна для куки / контекста
    log.info("Opening IncomingPayments page (context only)")
    await page.goto(INCOMING_PAYMENTS_URL, wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(2)

    # Расширяем диапазон чтобы первый запуск увидел недавние платежи (потом фильтр по seen_transactions)
    days_back = max(days_back, 7)
    # Формат банка: MM/dd/yyyy HH:mm:ss (US-style)
    date_from = (date.today() - timedelta(days=days_back)).strftime("%m/%d/%Y 00:00:00")
    date_to   = date.today().strftime("%m/%d/%Y 23:59:59")
    log.info("Fetching payments for AccountId=%s, FirstDate=%s, LastDate=%s",
             target_id, date_from, date_to)

    js_fetch_payments = """async ({accountId, firstDate, lastDate}) => {
        const qs = new URLSearchParams();
        qs.append('AccountId', accountId);
        qs.append('FirstDate', firstDate);
        qs.append('LastDate', lastDate);

        // Большая страница + сортировка по дате убыванию (новые первыми)
        const body = new URLSearchParams();
        body.append('page', '1');
        body.append('pageSize', '500');
        body.append('skip', '0');
        body.append('take', '500');
        body.append('sort[0].field', 'DateOperation');
        body.append('sort[0].dir', 'desc');

        const r = await fetch('/Accounts/ReadPayments?' + qs.toString(), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json',
            },
            body: body.toString(),
            credentials: 'include',
        });
        const text = await r.text();
        return r.status + ' :: ' + text;
    }"""
    raw = await page.evaluate(
        js_fetch_payments,
        {"accountId": target_id, "firstDate": date_from, "lastDate": date_to},
    )
    # ответ имеет формат "200 :: {json}" — отрезаем префикс
    if " :: " in raw:
        status_str, json_str = raw.split(" :: ", 1)
        log.info("ReadPayments HTTP %s, body size=%d", status_str, len(json_str))
    else:
        log.error("Unexpected response format: %s", raw[:200])
        return []

    try:
        data = json.loads(json_str)
    except Exception as exc:
        log.error("Failed to parse JSON: %s\nRaw: %s", exc, json_str[:500])
        return []

    records = data.get("Data") or []
    log.info("Total records returned: %d (Total field=%s)", len(records), data.get("Total"))

    # ДИАГНОСТИКА: показываем 5 самых свежих с полными полями
    if records:
        sorted_recs = sorted(records, key=lambda r: r.get("DateOperation", ""), reverse=True)
        log.info("=== 5 newest records FULL DUMP ===")
        for r in sorted_recs[:5]:
            log.info("--- id=%s, dedup_key=%s, date=%s ---",
                     r.get("Id"), _make_dedup_key(r), r.get("DateOperation", "?")[:10])
            log.info("  Title         = %s", (r.get("Title") or "")[:120])
            log.info("  PayerName     = %s", r.get("PayerName"))
            log.info("  AmountOfTransfer = %s, Debit = %s", r.get("AmountOfTransfer"), r.get("Debit"))
        log.info("=== end full dump ===")

    # Фильтруем только входящие — те, где BeneficiaryAccount == наш счёт
    our_iban_compact = iban_target  # уже без пробелов
    incoming = []
    for r in records:
        beneficiary_acc = (r.get("BeneficiaryAccount") or "").replace(" ", "")
        # это входящий если: получатель = мы, отправитель = не мы
        if beneficiary_acc == our_iban_compact:
            tx = Transaction(
                transaction_id=str(r.get("Id") or ""),
                dedup_key=_make_dedup_key(r),
                booking_date=r.get("DateOperation", "")[:10],
                amount=str(r.get("AmountOfTransfer") or r.get("Debit") or "?"),
                currency=r.get("IsoOfTransfer", "BYN"),
                counterparty=(r.get("PayerName") or "").strip() or "Неизвестно",
                purpose=(r.get("DetPay") or "").strip() or "—",
            )
            incoming.append(tx)

    log.info("Filtered to %d INCOMING transactions", len(incoming))
    return incoming


async def _parse_statements_unused(page: Page, days_back: int) -> list[Transaction]:
    """Stub kept for reference, no longer called."""
    # сохраняем HTML страницы в репо для отладки (через Actions artifact)
    html = await page.content()
    debug_path = "/tmp/statements_page.html"
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Statements page HTML saved to %s (size=%d)", debug_path, len(html))

    # TODO: implement real parsing once we see the page structure
    return []
