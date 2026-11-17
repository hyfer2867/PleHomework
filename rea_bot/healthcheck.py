"""Docker health based on successful polling and a readable database."""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from aiogram.methods import GetUpdates

from .config import Settings


class PollingHealth:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.updated = 0

    def clear(self):
        if self.path:
            self.path.unlink(missing_ok=True)

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        now = time.monotonic()
        if self.path and isinstance(method, GetUpdates) and now - self.updated >= 10:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": os.getpid(), "updated": now}))
            temporary.replace(self.path)
            self.updated = now
        return result


def check(path, database, max_age=90):
    heartbeat = json.loads(Path(path).read_text())
    age = time.monotonic() - heartbeat["updated"]
    if not 0 <= age <= max_age:
        raise ValueError("Polling heartbeat expired")
    pid = heartbeat["pid"]
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError("Invalid polling process")
    os.kill(pid, 0)
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        connection.execute("SELECT id FROM users LIMIT 1").fetchone()
    finally:
        connection.close()


def main():
    try:
        check(
            os.getenv("HEALTHCHECK_FILE", "/tmp/rea-homework-bot.health"),
            Settings.from_env().database,
        )
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print("unhealthy: polling heartbeat or database unavailable")
        return 1
    print("healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
