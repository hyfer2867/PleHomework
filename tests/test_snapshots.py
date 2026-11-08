import time
from pathlib import Path

from rea_bot.config import FORESIGHT, Settings
from rea_bot.db import Store
from rea_bot.schedule import parse_week
from rea_bot.snapshots import restore_schedules


def test_snapshot_is_portable_preserves_timestamp_and_newer_data():
    store = Store(":memory:", {1})
    week = parse_week((Path(__file__).parent / "fixtures/schedule.html").read_text())
    snapshot = {
        "groups": [
            {
                "group": {
                    "name": "Pilot",
                    "faculty": FORESIGHT,
                    "course": "1-й курс",
                    "level": "Бакалавр",
                },
                "weeks": [{"value": week, "fetched": time.time() - 3600}],
            }
        ]
    }
    settings = Settings(admins=frozenset({1}))
    assert restore_schedules(store, settings, snapshot) == 1
    group = store.visible_groups(1)[0]
    key = f"week:{group['id']}:{week['monday']}"
    assert store.cache_get(key)["fetched"] == snapshot["groups"][0]["weeks"][0]["fetched"]
    assert len(store.week_lessons(1, group["id"], week["monday"], week["sunday"])) == 13
    assert restore_schedules(store, settings, snapshot) == 0
    store.cache_put(key, week)
    assert restore_schedules(store, settings, snapshot) == 0
    store.close()
