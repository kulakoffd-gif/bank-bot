"""AmoCRM API client — поиск ответственного менеджера по УНП клиента."""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# Параметры из env
AMO_TOKEN = os.environ.get("AMO_TOKEN", "")
AMO_SUBDOMAIN = os.environ.get("AMO_SUBDOMAIN", "ecoranpro")
AMO_BASE = f"https://{AMO_SUBDOMAIN}.amocrm.ru"

# ID поля «УНП» в карточке компании (получено эмпирически)
AMO_UNP_FIELD_ID = 907879

# Простой in-memory кэш на один запуск
_cache: dict[str, tuple[float, dict | None]] = {}
CACHE_TTL_SEC = 3600  # 1 час


def is_configured() -> bool:
    """AmoCRM настроен и можно делать запросы."""
    return bool(AMO_TOKEN)


def _http_get(path: str, params: dict | None = None) -> dict | None:
    """GET к AmoCRM API. Возвращает JSON или None."""
    if not is_configured():
        return None

    qs = "?" + urllib.parse.urlencode(params) if params else ""
    url = AMO_BASE + path + qs

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {AMO_TOKEN}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 204:
                return None
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return None
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log.error("AmoCRM HTTP %s on %s: %s", e.code, path, body)
        return None
    except Exception as exc:
        log.error("AmoCRM request failed for %s: %s", path, exc)
        return None


def find_company_by_unp(unp: str) -> dict | None:
    """Найти компанию по УНП. Возвращает dict {id, name, responsible_user_id} или None."""
    if not unp:
        return None

    unp_str = str(unp).strip()
    # Проверка кэша
    now = time.time()
    cached = _cache.get(unp_str)
    if cached and (now - cached[0]) < CACHE_TTL_SEC:
        return cached[1]

    data = _http_get("/api/v4/companies", {"query": unp_str})
    result: dict | None = None
    if data:
        companies = data.get("_embedded", {}).get("companies", [])
        for c in companies:
            for cf in (c.get("custom_fields_values") or []):
                if cf.get("field_id") == AMO_UNP_FIELD_ID:
                    for v in (cf.get("values") or []):
                        if str(v.get("value")).strip() == unp_str:
                            result = {
                                "id": c.get("id"),
                                "name": c.get("name", ""),
                                "responsible_user_id": c.get("responsible_user_id"),
                            }
                            break
                    if result:
                        break
            if result:
                break

    _cache[unp_str] = (now, result)
    return result


def get_user_info(user_id: int) -> dict | None:
    """Получить информацию о пользователе AmoCRM (имя, email)."""
    if not user_id:
        return None
    data = _http_get(f"/api/v4/users/{user_id}")
    if not data:
        return None
    return {"id": data.get("id"), "name": data.get("name"), "email": data.get("email")}


def list_users() -> list[dict]:
    """Список всех пользователей AmoCRM (для команды /managers)."""
    data = _http_get("/api/v4/users", {"limit": 50})
    if not data:
        return []
    return data.get("_embedded", {}).get("users", [])
