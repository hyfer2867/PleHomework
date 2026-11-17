import json
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import GetMe, GetUpdates

from rea_bot.healthcheck import PollingHealth, check


async def test_health_requires_successful_poll_and_readable_database(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    database = tmp_path / "bot.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE users(id INTEGER)")
    connection.close()
    health = PollingHealth(heartbeat)
    request = AsyncMock(return_value=[])
    await health(request, None, GetMe())
    assert not heartbeat.exists()
    request.side_effect = RuntimeError("network failure")
    with pytest.raises(RuntimeError):
        await health(request, None, GetUpdates())
    assert not heartbeat.exists()
    request.side_effect = None
    await health(request, None, GetUpdates())
    check(heartbeat, database)
    with pytest.raises(sqlite3.Error):
        check(heartbeat, tmp_path / "missing.sqlite3")
    health.clear()
    with pytest.raises(FileNotFoundError):
        check(heartbeat, database)


def test_health_rejects_stale_heartbeat(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(json.dumps({"pid": os.getpid(), "updated": time.monotonic() - 91}))
    with pytest.raises(ValueError, match="expired"):
        check(heartbeat, tmp_path / "database")
