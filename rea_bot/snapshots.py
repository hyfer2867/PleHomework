"""Restore bundled public schedules without shipping accounts, tokens or homework."""

import json
from importlib.resources import files


def restore_schedules(store, settings, snapshot=None):
    if not settings.admins:
        return 0
    if snapshot is None:
        resource = files("rea_bot").joinpath("data/schedule-2026.json")
        if not resource.is_file():
            return 0
        snapshot = json.loads(resource.read_text(encoding="utf-8"))
    actor = min(settings.admins)
    restored = 0
    for entry in snapshot["groups"]:
        group = entry["group"]
        if group["faculty"] != settings.faculty:
            continue
        saved = store.one("SELECT * FROM groups WHERE name=?", (group["name"],))
        if saved is None:
            saved = store.catalog_group(
                actor, group["name"], group["faculty"], group["course"], group["level"]
            )
        for cached in entry["weeks"]:
            week = cached["value"]
            key = f"week:{saved['id']}:{week['monday']}"
            existing = store.cache_get(key)
            if existing and existing["fetched"] >= cached["fetched"]:
                continue
            store.sync_lessons(actor, saved["id"], week)
            store.cache_put(key, week)
            store.conn.execute("UPDATE cache SET fetched=? WHERE key=?", (cached["fetched"], key))
            restored += 1
    return restored
