"""Смерджить локальный state.json с актуальным remote main.

Используется в commit-step workflow для разрешения race condition,
когда два прогона одновременно пытаются обновить state.json.

Стратегия:
- seen_transactions: объединение (union) — никаких потерь дедуп-ключей
- last_telegram_update_id: максимум (старшее offset побеждает)
- остальные поля (is_paused, recipients, manager_routing, last_check_*):
  берутся из нашего (свежего) прогона
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

    merged = dict(local)

    merged["seen_transactions"] = sorted(
        set(remote.get("seen_transactions", [])) | set(local.get("seen_transactions", []))
    )
    merged["last_telegram_update_id"] = max(
        remote.get("last_telegram_update_id", 0) or 0,
        local.get("last_telegram_update_id", 0) or 0,
    )

    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
