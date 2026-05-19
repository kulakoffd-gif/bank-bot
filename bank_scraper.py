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

    # Переключаем фильтр на "Текущий (расчётный)" — TypeAccountID=1
    log.info("Saving filter TypeAccountID=1 (Текущий расчётный)")
    try:
        save_result = await page.evaluate("""async () => {
            const r = await fetch('/Accounts/SaveGridTypeFilter', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'},
                body: 'typeId=1'
            });
            return r.status + ' :: ' + await r.text();
        }""")
        log.info("SaveGridTypeFilter result: %s", save_result[:200])
    except Exception as e:
        log.warning("SaveGridTypeFilter failed: %s", e)

    # Перезагружаем страницу и делаем скриншот
    await page.reload(wait_until="networkidle", timeout=30_000)
    await asyncio.sleep(4)
    await page.screenshot(path="/tmp/01_checking_accounts.png", full_page=True)
    with open("/tmp/01_checking_accounts.html", "w", encoding="utf-8") as f:
        f.write(await page.content())

    # Парсим IBAN'ы из DOM прямо из браузера
    log.info("Extracting IBAN list from rendered grid")
    try:
        rows_data = await page.evaluate("""() => {
            const rows = document.querySelectorAll('tr[role="row"]');
            const out = [];
            for (const r of rows) {
                const cells = r.querySelectorAll('td');
                if (cells.length === 0) continue;
                const cellTexts = Array.from(cells).map(c => c.innerText.trim());
                out.push(cellTexts);
            }
            return JSON.stringify(out);
        }""")
        with open("/tmp/grid_rows.json", "w", encoding="utf-8") as f:
            f.write(rows_data)
        log.info("Grid rows extracted (size=%d)", len(rows_data))
    except Exception as e:
        log.warning("Row extraction failed: %s", e)

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
