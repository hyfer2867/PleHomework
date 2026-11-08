"""Only scheduled morning refreshes and explicitly confirmed owner imports use the network."""

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class ScheduleRefresh:
    def __init__(self, store, client, settings):
        self.store, self.client, self.settings = store, client, settings
        self.lock = asyncio.Lock()

    async def morning(self, now=None):
        now = now or datetime.now(ZoneInfo(self.settings.timezone))
        if now.hour < self.settings.refresh_hour or not self.settings.admins:
            return
        actor = min(self.settings.admins)
        monday = now.date() - timedelta(days=now.weekday())
        async with self.lock:
            for group in self.store.visible_groups(actor):
                if group["faculty"] != self.settings.faculty:
                    continue
                for start in (monday, monday + timedelta(days=7)):
                    key = f"daily-attempt:{now.date()}:{group['id']}:{start}"
                    if self.store.cache_get(key):
                        continue
                    # Durable attempt marker: restarting cannot repeatedly hit the site on failure.
                    self.store.cache_put(key, {"attempted": True})
                    try:
                        await self.client.week(actor, group["id"], start, force=True)
                    except Exception:
                        logging.getLogger(__name__).exception("Morning schedule refresh failed")
                        return  # Stop this cycle, retaining all previously saved data.

    async def run(self):
        while True:
            try:
                await self.morning()
            except Exception:
                logging.getLogger(__name__).exception("Schedule refresh loop failed")
            await asyncio.sleep(30)
