"""
Belarusbank corporate Internet Banking scraper.

Logs into icb.asb.by, opens the account statement page, parses incoming payments.

NOTE: selectors below are based on the typical structure of the icb.asb.by web app.
After the first real run we may need to adjust them once we see the actual page —
all selector strings are gathered at the top of this file for easy tweaking.
"""

import asyncio
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
    await page.screenshot(path="/tmp/01_accounts_default.png", full_page=True)

    # Получаем JSON-список типов счетов через тот же endpoint что использует фронт
    log.info("Querying account types list via /Accounts/GetTypeAccounts")
    try:
        # выполним AJAX-запрос через JS в браузере (использует текущую сессию)
        types_json = await page.evaluate("""async () => {
            const r = await fetch('/Accounts/GetTypeAccounts', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: ''
            });
            return await r.text();
        }""")
        with open("/tmp/account_types.json", "w", encoding="utf-8") as f:
            f.write(types_json)
        log.info("Account types saved (size=%d)", len(types_json))
    except Exception as e:
        log.warning("GetTypeAccounts failed: %s", e)

    # Запрашиваем СПИСОК САМИХ СЧЕТОВ через AJAX endpoint
    log.info("Querying full accounts list via JS fetch")
    try:
        accounts_json = await page.evaluate("""async () => {
            // typical Kendo grid read endpoint - try common patterns
            const r = await fetch('/Accounts/GetAccountBalances', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'},
                body: ''
            });
            return r.status + ' :: ' + await r.text();
        }""")
        with open("/tmp/accounts_data.txt", "w", encoding="utf-8") as f:
            f.write(accounts_json)
        log.info("Accounts data: status+body saved (size=%d)", len(accounts_json))
    except Exception as e:
        log.warning("GetAccountBalances failed: %s", e)

    iban = os.environ.get("BANK_ACCOUNT_IBAN", "")
    log.info("Target IBAN: %s", iban)

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
