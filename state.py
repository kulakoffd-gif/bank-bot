"""State persistence — read/write state.json committed back to repo."""

import json
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
MAX_SEEN_HISTORY = 1000  # keep last N transaction IDs to prevent unbounded growth


def load() -> dict:
    if not STATE_PATH.exists():
        return _default()
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        return _default()
    # миграция: гарантируем что все поля присутствуют
    for k, v in _default().items():
        if k not in data:
            data[k] = v
    return data


def save(state: dict) -> None:
    seen = state.get("seen_transactions", [])
    if len(seen) > MAX_SEEN_HISTORY:
        state["seen_transactions"] = seen[-MAX_SEEN_HISTORY:]
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def mark_check(state: dict, result: str) -> None:
    state["last_check_at"] = datetime.now(timezone.utc).isoformat()
    state["last_check_result"] = result


def _default() -> dict:
    return {
        "seen_transactions": [],
        "last_telegram_update_id": 0,
        "is_paused": False,
        "last_check_at": None,
        "last_check_result": None,
        "recipients": [],  # доп. получатели уведомлений (кроме админа)
    }
