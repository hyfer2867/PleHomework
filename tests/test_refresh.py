from datetime import datetime
from zoneinfo import ZoneInfo

from rea_bot.config import Settings
from rea_bot.refresh import ScheduleRefresh


class Recorder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def week(self, uid, gid, monday, force=False):
        self.calls.append((gid, str(monday), force))
        if self.fail:
            raise RuntimeError("Offline")
        return {"stale": False}


async def test_morning_only_two_weeks_once_per_day_even_after_restart(store):
    settings = Settings(admins=frozenset({1}))
    client = Recorder()
    now = datetime(2026, 11, 8, 6, 59, tzinfo=ZoneInfo("Europe/Moscow"))
    await ScheduleRefresh(store, client, settings).morning(now)
    assert not client.calls
    now = now.replace(hour=7, minute=0)
    await ScheduleRefresh(store, client, settings).morning(now)
    assert len(client.calls) == 4
    assert {item[1] for item in client.calls} == {"2026-11-02", "2026-11-09"}
    await ScheduleRefresh(store, client, settings).morning(now.replace(hour=20))
    assert len(client.calls) == 4
    await ScheduleRefresh(store, client, settings).morning(now.replace(day=9))
    assert len(client.calls) == 8


async def test_failed_week_is_not_retried_same_day(store):
    settings = Settings(admins=frozenset({1}))
    client = Recorder(fail=True)
    now = datetime(2026, 11, 8, 7, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    updater = ScheduleRefresh(store, client, settings)
    await updater.morning(now)
    await updater.morning(now)
    assert len(client.calls) == 2
    assert len(set(client.calls)) == 2
