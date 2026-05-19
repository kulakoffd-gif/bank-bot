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
    transaction_id: str
    booking_date: str
    amount: str
    currency: str
    counterparty: str
    purpose: str


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

    # Запрос платежей напрямую через AJAX endpoint с правильными параметрами
    date_from = (date.today() - timedelta(days=days_back)).strftime("%d.%m.%Y")
    date_to   = date.today().strftime("%d.%m.%Y")
    log.info("Fetching payments for accountId=%s, period %s — %s", target_id, date_from, date_to)

    js_fetch_payments = """async (accountId, dateFrom, dateTo) => {
        // Стандартные параметры Kendo Grid + AccountId + диапазон дат
        const params = new URLSearchParams();
        params.append('AccountId', accountId);
        params.append('dateFrom', dateFrom);
        params.append('dateTo', dateTo);
        params.append('DateFrom', dateFrom);
        params.append('DateTo', dateTo);
        params.append('page', '1');
        params.append('pageSize', '100');
        params.append('skip', '0');
        params.append('take', '100');

        const r = await fetch('/Accounts/ReadPayments', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/json',
            },
            body: params.toString(),
            credentials: 'include',
        });
        const text = await r.text();
        return r.status + ' :: ' + text;
    }"""
    raw = await page.evaluate(js_fetch_payments, target_id, date_from, date_to)
    with open("/tmp/read_payments_response.txt", "w", encoding="utf-8") as f:
        f.write(raw)
    log.info("ReadPayments response (size=%d): %s", len(raw), raw[:300])

    # на этом этапе нужны точные селекторы конкретного интерфейса банка.
    # пока возвращаем заглушку, чтобы воркфлоу не падал — допишем после
    # первого реального теста, когда увидим HTML страницы выписки.
    transactions = await _parse_statements(page, days_back)
    log.info("Parsed %d incoming transactions", len(transactions))
    return transactions


async def _parse_statements(page: Page, days_back: int) -> list[Transaction]:
    """Stub — to be completed after seeing the real statements page HTML."""
    # сохраняем HTML страницы в репо для отладки (через Actions artifact)
    html = await page.content()
    debug_path = "/tmp/statements_page.html"
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Statements page HTML saved to %s (size=%d)", debug_path, len(html))

    # TODO: implement real parsing once we see the page structure
    return []
