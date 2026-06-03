"""
Belarusbank corporate Internet Banking scraper — новая платформа dcsc.belarusbank.by.

Логинимся через Playwright, потом дёргаем внутренний REST endpoint
/ibservices/account/getAccountStatement напрямую через fetch().
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

BASE_URL = "https://dcsc.belarusbank.by"
LOGIN_URL = f"{BASE_URL}/auth"

# Селекторы (Angular SPA)
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
    purpose: str


def _make_dedup_key(r: dict) -> str:
    """Стабильный ключ дедупликации — не зависит от внутреннего Id банка."""
    parts = [
        str(r.get("payerUnp") or "").strip(),
        str(r.get("documentNumber") or r.get("numberDocument") or "").strip(),
        str(r.get("documentDate") or r.get("acceptDate") or "").strip()[:10],
        str(r.get("amount") or r.get("debit") or r.get("credit") or "").strip(),
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


async def _do_scrape(page, login, password, iban, days_back):
    days_back = max(days_back, 3)

    log.info("Opening login page: %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector(SEL_LOGIN_INPUT, timeout=20_000)
    await asyncio.sleep(2)

    log.info("Filling credentials")
    await page.fill(SEL_LOGIN_INPUT, login)
    await page.fill(SEL_PASSWORD_INPUT, password)
    await page.click(SEL_SUBMIT_BTN)

    try:
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/auth')",
            timeout=30_000,
        )
    except PlaywrightTimeout:
        raise RuntimeError("Login failed — bank rejected credentials")

    log.info("Logged in. URL: %s", page.url)
    await asyncio.sleep(5)

    # Прямой вызов /ibservices/account/getAccountStatement
    # AccountId известен — 18067 (получаем из ENV если нужна гибкость)
    account_id = int(os.environ.get("BANK_ACCOUNT_ID", "18067"))

    date_from = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = date.today().strftime("%Y-%m-%d")

    log.info("Calling getAccountStatement for accountId=%s, %s..%s",
             account_id, date_from, date_to)

    js_call = """async ({accountId, dateFrom, dateTo}) => {
        const body = {
            sort: {columnName: "acceptDate", columnValue: "asc"},
            sortList: [{columnName: "acceptDate", columnValue: "asc"}],
            filterLike: [],
            numberOfPage: 1,
            itemsPerPage: 500,
            dateFrom: dateFrom,
            dateTo: dateTo,
            customerAccountId: accountId,
            isWithRevaluation: true,
            correspondentAccountFilter: {},
        };
        const r = await fetch('/ibservices/account/getAccountStatement', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
            credentials: 'include',
        });
        const text = await r.text();
        return {status: r.status, body: text};
    }"""

    result = await page.evaluate(js_call, {
        "accountId": account_id, "dateFrom": date_from, "dateTo": date_to,
    })
    log.info("getAccountStatement HTTP %s, body size=%d",
             result["status"], len(result["body"]))

    if result["status"] != 200:
        log.error("Bad response: %s", result["body"][:500])
        return []

    data = json.loads(result["body"])
    if data.get("errorInfo", {}).get("error") != "0":
        log.error("Bank error: %s", data.get("errorInfo"))
        return []

    # Дамп для отладки структуры (первые 3000 знаков уже хватит)
    log.info("Response summary: items=%s, opening=%s, closing=%s",
             data.get("items"), data.get("openingBalance"), data.get("closingBalance"))

    # Ключевые поля могут лежать в разных полях response — анализируем
    log.info("Top-level keys: %s", sorted(data.keys()))

    # Ищем массив операций — называется по-разному в разных банках
    operations = None
    for key in ("operations", "operationList", "items_data", "rows", "list", "gridRows", "statementList"):
        if isinstance(data.get(key), list):
            operations = data[key]
            log.info("Operations found under key '%s', count=%d", key, len(operations))
            break

    if operations is None:
        # Попробуем найти любой ключ со списком dict-объектов
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                log.info("Possible operations list under '%s' (size=%d)", k, len(v))
                if operations is None:
                    operations = v
                    break

    if not operations:
        # Дампим всё чтобы увидеть структуру
        log.warning("No operations array found! Full response (first 3000 chars):")
        log.warning(json.dumps(data, ensure_ascii=False, indent=2)[:3000])
        return []

    # Дампим первую операцию для понимания структуры
    if operations:
        log.info("=== First operation (full structure) ===")
        log.info(json.dumps(operations[0], ensure_ascii=False, indent=2)[:1500])

    # Парсим входящие
    iban_target = iban.replace(" ", "")
    incoming = []
    for op in operations:
        # Пока считаем что приходящие — у которых credit > 0
        credit = op.get("credit") or op.get("creditAmount") or op.get("incomingAmount")
        if credit and float(str(credit).replace(",", ".") or 0) > 0:
            tx = Transaction(
                transaction_id=str(op.get("id") or op.get("operationId") or ""),
                dedup_key=_make_dedup_key(op),
                booking_date=str(op.get("acceptDate") or op.get("operationDate") or "")[:10],
                amount=str(credit),
                currency=op.get("currency") or "BYN",
                counterparty=str(op.get("payerName") or op.get("correspondentName") or "Неизвестно"),
                purpose=str(op.get("paymentPurpose") or op.get("purpose") or "—"),
            )
            incoming.append(tx)

    log.info("Filtered to %d incoming transactions", len(incoming))
    return incoming
