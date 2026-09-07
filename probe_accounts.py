"""РАЗВЕДКА (временный): зайти на страницу «Счета» и сохранить всё, что банк
отдаёт по счетам и остаткам, в /tmp — чтобы понять, откуда брать остатки для
будущей кнопки «Остатки». Прод не трогает. После анализа удалить вместе с
.github/workflows/probe.yml.
"""

import asyncio
import json
import logging
import os
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("probe")

BASE_URL = "https://dcsc.belarusbank.by"
LOGIN_URL = f"{BASE_URL}/auth"
SEL_LOGIN_INPUT = 'input[placeholder="Логин"]'
SEL_PASSWORD_INPUT = 'input[placeholder="Пароль"]'
SEL_SUBMIT_BTN = 'button[type="submit"]'
_MODAL_CLOSE_LABELS = ["Пропустить", "Продолжить", "Закрыть", "Позже", "ОК", "Понятно", "Ознакомлен"]

captured: list[dict] = []


async def main() -> None:
    login = os.environ["BANK_LOGIN"]
    password = os.environ["BANK_PASSWORD"]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
            timezone_id="Europe/Minsk",
            geolocation={"latitude": 53.9006, "longitude": 27.5590},
            permissions=["geolocation"],
        )
        page = await context.new_page()

        async def on_response(resp):
            u = resp.url
            ct = (resp.headers or {}).get("content-type", "")
            if "ibservices" in u or "account" in u.lower() or "json" in ct:
                try:
                    body = await resp.text()
                    captured.append({"url": u, "status": resp.status, "ct": ct, "body": body})
                except Exception:
                    pass

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        # === ЛОГИН ===
        log.info("Opening login page")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_selector(SEL_LOGIN_INPUT, timeout=20_000)
        await asyncio.sleep(2)
        await page.fill(SEL_LOGIN_INPUT, login)
        await page.fill(SEL_PASSWORD_INPUT, password)
        await page.click(SEL_SUBMIT_BTN)
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/auth')", timeout=30_000)
        log.info("Logged in, URL=%s", page.url)
        await asyncio.sleep(6)

        # закрыть модалку если есть
        for label in _MODAL_CLOSE_LABELS:
            try:
                btn = page.get_by_role("button", name=label, exact=True).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    log.info("Closed modal via %s", label)
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        # === КЛИК «Счета» ===
        try:
            await page.get_by_text("Счета", exact=True).first.click(timeout=10_000)
            log.info("Clicked 'Счета'")
        except Exception as e:
            log.warning("Could not click 'Счета': %s", e)
        await asyncio.sleep(8)

        # дамп страницы счетов
        try:
            await page.screenshot(path="/tmp/accounts_page.png", full_page=True)
            with open("/tmp/accounts_page.html", "w", encoding="utf-8") as f:
                f.write(await page.content())
            log.info("Dumped accounts_page.html/.png")
        except Exception as e:
            log.warning("dump page failed: %s", e)

        # видимый текст страницы (для быстрого взгляда на остатки)
        try:
            txt = await page.evaluate("() => document.body.innerText")
            with open("/tmp/accounts_text.txt", "w", encoding="utf-8") as f:
                f.write(txt)
        except Exception:
            pass

        await context.close()
        await browser.close()

    # === СОХРАНЯЕМ ЗАХВАЧЕННЫЕ ОТВЕТЫ ===
    idx_lines = []
    for i, c in enumerate(captured):
        fn = f"/tmp/probe_{i:02d}.json"
        try:
            with open(fn, "w", encoding="utf-8") as f:
                f.write(c["body"][:500_000])
        except Exception:
            pass
        idx_lines.append(f"{i:02d}  status={c['status']}  size={len(c['body'])}  {c['url']}")
    with open("/tmp/probe_index.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(idx_lines))
    log.info("Captured %d responses. Index:\n%s", len(captured), "\n".join(idx_lines))


if __name__ == "__main__":
    asyncio.run(main())
