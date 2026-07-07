"""Смерджить локальный state.json с актуальным remote main.

Используется в commit-step workflow для разрешения race condition,
когда два прогона одновременно пытаются обновить state.json.

Стратегия слияния (по полю):
- seen_transactions: UNION (никогда не теряем дедуп-ключи)
- last_telegram_update_id: MAX (offset монотонно растёт)
- last_check_at, last_check_result: LOCAL (свежий результат прогона)
- recipients, manager_routing, is_paused: REMOTE (управляются командами
  пользователя; если в параллельном прогоне их изменили, эти изменения
  важнее, чем стейл-копия в нашем checkout'е)
- остальные неизвестные поля: REMOTE (безопасный default)
"""

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: merge_state.py <local_state.json>", file=sys.stderr)
        return 2

    local_path = sys.argv[1]

    with open("state.json", encoding="utf-8") as f:
        remote = json.load(f)
    with open(local_path, encoding="utf-8") as f:
        local = json.load(f)

    merged = dict(remote)

    merged["seen_transactions"] = sorted(
        set(remote.get("seen_transactions", [])) | set(local.get("seen_transactions", []))
    )
    # UNION, чтобы уже отправленное окно банка не ушло повторно после гонки
    merged["seen_bank_notices"] = sorted(
        set(remote.get("seen_bank_notices", [])) | set(local.get("seen_bank_notices", []))
    )
    merged["last_telegram_update_id"] = max(
        remote.get("last_telegram_update_id", 0) or 0,
        local.get("last_telegram_update_id", 0) or 0,
    )

    for k in ("last_check_at", "last_check_result"):
        if local.get(k):
            merged[k] = local[k]

    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
