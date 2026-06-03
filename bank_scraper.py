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

BASE_URL = "https://dcsc.belarusbank.by"
LOGIN_URL = f"{BASE_URL}/auth"

# Селекторы новой платформы (Angular SPA)
SEL_LOGIN_INPUT    = 'input[placeholder="Логин"]'
SEL_PASSWORD_INPUT = 'input[placeholder="Пароль"]'
SEL_SUBMIT_BTN     = 'button[type="submit"]'


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
    # === ИССЛЕДОВАТЕЛЬСКАЯ ВЕРСИЯ под новую платформу dcsc.belarusbank.by ===
    # Логинимся, потом дампим всё что увидим. Реальный парсинг — следующая итерация.

    log.info("Opening new login page: %s", LOGIN_URL)
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_selector(SEL_LOGIN_INPUT, timeout=20_000)
    await asyncio.sleep(2)

    log.info("Filling credentials")
    await page.fill(SEL_LOGIN_INPUT, login)
    await page.fill(SEL_PASSWORD_INPUT, password)
    await page.screenshot(path="/tmp/00_login_filled.png", full_page=True)
    await page.click(SEL_SUBMIT_BTN)

    # ждём редиректа куда угодно НЕ на /auth
    try:
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/auth')",
            timeout=30_000,
        )
    except PlaywrightTimeout:
        html = await page.content()
        body_text = await page.evaluate("() => document.body.innerText")
        log.error("Login did not redirect. URL=%s", page.url)
        log.error("Page text: %s", body_text[:500])
        await page.screenshot(path="/tmp/01_login_failed.png", full_page=True)
        with open("/tmp/01_login_failed.html", "w") as f:
            f.write(html)
        raise RuntimeError("Login failed — bank rejected credentials or new flow detected")

    log.info("Logged in. Current URL: %s", page.url)
    await asyncio.sleep(5)  # больше времени на загрузку SPA
    await page.screenshot(path="/tmp/02_after_login.png", full_page=True)
    with open("/tmp/02_after_login.html", "w") as f:
        f.write(await page.content())

    # Перехватываем все XHR/fetch с request/response целиком
    api_calls: list[dict] = []

    async def capture_response(resp):
        u = resp.url
        if "/ibservices/" in u and resp.status != 304:
            try:
                body = await resp.text()
            except Exception:
                body = "<no body>"
            req_body = ""
            try:
                req_body = resp.request.post_data or ""
            except Exception:
                pass
            api_calls.append({
                "status": resp.status,
                "method": resp.request.method,
                "url": u,
                "req_body": req_body[:500],
                "resp_body": body[:3000],
            })

    page.on("response", lambda r: asyncio.create_task(capture_response(r)))

    # ── ШАГ 1: ИНВЕНТАРИЗАЦИЯ страницы ──
    log.info("=== STEP 1: Page inventory ===")

    # Все видимые текстовые элементы которые выглядят как меню
    menu_inventory = await page.evaluate("""() => {
        const items = [];
        // Ищем все кликабельные элементы
        document.querySelectorAll('a, button, [routerlink], [routerlinkactive], [class*=menu], [class*=nav], [role=menuitem], [role=button], [role=link]').forEach(el => {
            const txt = (el.innerText || el.textContent || '').trim();
            if (txt && txt.length < 80 && el.offsetParent !== null) {  // только видимые
                items.push({
                    tag: el.tagName.toLowerCase(),
                    text: txt.slice(0, 60),
                    href: el.getAttribute('href') || '',
                    routerlink: el.getAttribute('routerlink') || '',
                    class: (el.className || '').toString().slice(0, 80),
                    id: el.id,
                });
            }
        });
        // дедуп по тексту
        const seen = new Set();
        return items.filter(i => {
            const key = i.tag + '|' + i.text;
            if (seen.has(key)) return false;
            seen.add(key); return true;
        });
    }""")
    log.info("Visible interactive items: %d", len(menu_inventory))
    for it in menu_inventory[:50]:
        log.info("  [%s] '%s' routerlink=%s href=%s",
                 it['tag'], it['text'], it.get('routerlink', '')[:40], it.get('href', '')[:40])

    # ── ШАГ 2: Попытка найти и кликнуть пункт меню типа "Счета" ──
    log.info("=== STEP 2: Trying to navigate to Accounts ===")
    candidates = ["Счета", "Счёт", "Accounts", "Выписка", "Документы", "Главная", "Финансы", "Операции"]
    clicked = False
    for label in candidates:
        try:
            # ищем по точному совпадению, видимый
            locator = page.get_by_text(label, exact=True).first
            if await locator.count() > 0:
                log.info("Trying click on label '%s'", label)
                await locator.click(timeout=5000)
                await asyncio.sleep(4)
                log.info("  After click URL: %s", page.url)
                await page.screenshot(path=f"/tmp/03_clicked_{label}.png", full_page=True)
                clicked = True
                break
        except Exception as e:
            log.info("  click '%s' failed: %s", label, str(e)[:80])

    if not clicked:
        log.warning("No menu item could be clicked")

    # Кликаем на счёт с нашим IBAN — если найдём
    iban_target = os.environ.get("BANK_ACCOUNT_IBAN", "").replace(" ", "")
    # IBAN на странице может быть с пробелами или без — ищем оба варианта
    iban_with_spaces = " ".join([iban_target[i:i+4] for i in range(0, len(iban_target), 4)])
    log.info("Looking for our IBAN on page: %s OR %s", iban_target, iban_with_spaces)

    # Попробуем кликнуть на строку с этим IBAN
    try:
        for sel in [f'text="{iban_target}"', f'text="{iban_with_spaces}"',
                    f'text=/30120041040434000000/',]:
            try:
                await page.click(sel, timeout=3000)
                log.info("Clicked on account row using selector: %s", sel)
                await asyncio.sleep(3)
                await page.screenshot(path="/tmp/04_account_detail.png", full_page=True)
                break
            except Exception:
                continue
    except Exception as e:
        log.warning("Could not click account: %s", e)

    log.info("URL after account click: %s", page.url)

    # ── ШАГ 3: пассивное наблюдение API после открытия страницы счетов ──
    await asyncio.sleep(5)
    await page.screenshot(path="/tmp/05_settled.png", full_page=True)
    with open("/tmp/05_settled.html", "w") as f:
        f.write(await page.content())

    # Дамп всех captured API calls (это и есть РЕАЛЬНЫЕ endpoints, которые использует SPA)
    log.info("=== CAPTURED REAL API CALLS (%d) ===", len(api_calls))
    for c in api_calls:
        endpoint = c["url"].split("?")[0].replace(BASE_URL, "")
        log.info("--- %s %s %s ---", c["status"], c["method"], endpoint)
        if c["req_body"]:
            log.info("  REQ:  %s", c["req_body"][:300])
        log.info("  RESP: %s", c["resp_body"][:1500])

    log.warning("EXPLORATION MODE — returning []")
    return []

    # === ЗАГЛУШКА — старое прощупывание (вернёмся когда найдём правильные endpoint'ы) ===
    target_account_id = 0
    candidate_endpoints = [
        ("cardfile/getCardfile", {"accountId": target_account_id}),
        ("cardfile/getCardfileByAccountId", {"accountId": target_account_id}),
        ("cardfile/getCardfileList", {"accountId": target_account_id}),
        ("cardfile/getList", {"accountId": target_account_id}),
        ("statement/getStatement", {"accountId": target_account_id}),
        ("statement/getAccountStatement", {"accountId": target_account_id}),
        ("statement/getStatementList", {"accountId": target_account_id}),
        ("statement/getList", {"accountId": target_account_id}),
        ("account/getStatement", {"accountId": target_account_id}),
        ("account/getCardfile", {"accountId": target_account_id}),
        ("account/getCardfileList", {"accountId": target_account_id}),
        ("account/getOperations", {"accountId": target_account_id}),
        ("operation/getList", {"accountId": target_account_id}),
        ("operation/getOperations", {"accountId": target_account_id}),
        ("operation/getOperationList", {"accountId": target_account_id}),
        # Полные параметры
        ("statement/getStatement",
         {"accountId": target_account_id,
          "dateFrom": "2026-05-21", "dateTo": "2026-05-28"}),
        ("cardfile/getCardfile",
         {"accountId": target_account_id,
          "dateFrom": "2026-05-21", "dateTo": "2026-05-28"}),
    ]

    log.info("=== Probing %d endpoints ===", len(candidate_endpoints))
    for path, body in candidate_endpoints:
        try:
            result = await page.evaluate("""async ({path, body}) => {
                try {
                    const r = await fetch('/ibservices/' + path, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                        credentials: 'include',
                    });
                    const text = await r.text();
                    return r.status + ' :: ' + text.slice(0, 800);
                } catch(e) { return 'EXC: ' + e.message; }
            }""", {"path": path, "body": body})
            # Печатаем только успешные (200) и многообещающие
            status = result.split(" :: ")[0] if " :: " in result else result
            if status == "200":
                log.info("  ✅ %s body=%s", path, body)
                log.info("     %s", result[:500])
            elif status not in ("404", "500"):
                log.info("  ⚠️ %s status=%s body=%s", path, status, body)
                log.info("     %s", result[:300])
        except Exception as e:
            log.info("  EXC %s: %s", path, e)

    # Дамп всех API endpoints с request bodies
    log.info("=== Captured /ibservices/ calls (%d) ===", len(api_calls))
    for c in api_calls:
        endpoint = c["url"].split("?")[0].replace(BASE_URL, "")
        log.info("--- %s %s %s ---", c["status"], c["method"], endpoint)
        if c["req_body"]:
            log.info("  REQ:  %s", c["req_body"][:300])
        log.info("  RESP: %s", c["resp_body"][:1500])

    log.warning("Exploration mode — returning []")
    return []

    # СТАРЫЙ КОД НИЖЕ НЕАКТИВЕН (для нового сайта неприменим)

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
