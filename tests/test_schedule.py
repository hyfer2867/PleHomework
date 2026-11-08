import asyncio
import copy
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from rea_bot.config import Settings
from rea_bot.db import UserError
from rea_bot.schedule import ReaClient, ScheduleError, parse_options, parse_week

FIXTURES = Path(__file__).parent / "fixtures"


def html():
    return (FIXTURES / "schedule.html").read_text()


def test_real_schedule_fixture():
    week = parse_week(html())
    assert week["monday"] == "2026-11-02"
    assert week["sunday"] == "2026-11-08"
    assert len(week["lessons"]) == 13
    assert week["lessons"][0]["subject"] == "Математический анализ"
    assert week["lessons"][0]["start"] == "11:50"
    assert not [l for l in week["lessons"] if l["day"] == "2026-11-04"]
    assert len({l["source_key"] for l in week["lessons"]}) == 13


def test_real_navigator_fixtures():
    assert len(parse_options((FIXTURES / "course.html").read_text(), "Course")) == 4
    assert (
        parse_options((FIXTURES / "group.html").read_text(), "Group")[0]["value"]
        == "15.22Д-ПМ01/26б"
    )


@pytest.mark.parametrize(
    "bad", ["<html>Login required</html>", "", '<table><th class="dayh">02.11.2026</th></table>']
)
def test_partial_and_login_responses_rejected(bad):
    with pytest.raises(ScheduleError):
        parse_week(bad)


def test_room_change_keeps_identity():
    old = parse_week(html())
    soup = BeautifulSoup(html(), "html.parser")
    for node in soup.select_one("a.task").find_all(string=True):
        if "227" in node:
            node.replace_with(node.replace("227", "999"))
    changed = parse_week(str(soup))
    assert [x["source_key"] for x in old["lessons"]] == [
        x["source_key"] for x in changed["lessons"]
    ]


def test_removed_lesson_preserves_homework(store):
    week = parse_week(html())
    lid = store.one("SELECT id FROM lessons WHERE group_id=1 ORDER BY id")["id"]
    store.save_homework(2, lid, "Keep me")
    updated = copy.deepcopy(week)
    updated["lessons"] = updated["lessons"][1:]
    store.sync_lessons(2, 1, updated)
    assert store.lesson(4, lid)["active"] == 0
    assert store.homework(4, lid)[0]["body"] == "Keep me"
    assert any(x["id"] == lid for x in store.week_lessons(4, 1, week["monday"], week["sunday"]))
    with pytest.raises(UserError):
        store.save_homework(2, lid, "new")
    store.sync_lessons(2, 1, week)
    assert store.lesson(4, lid)["active"] == 1


async def test_serialization_cache_and_session(store):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(
                200, text="<html>home</html>", headers={"set-cookie": "session=public; Path=/"}
            )
        assert "session=public" in request.headers["cookie"]
        await asyncio.sleep(0.01)
        return httpx.Response(200, text=html())

    client = ReaClient(store, Settings(request_interval=0), httpx.MockTransport(respond))
    try:
        results = await asyncio.gather(*(client.week(4, 1, date(2026, 11, 2)) for _ in range(5)))
        assert calls == ["/", "/Schedule/ScheduleCard"]
        assert all(not r["stale"] for r in results)
    finally:
        await client.close()


async def test_stale_cache_and_cooldown(store):
    failed = False
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(200, text="home")
        return httpx.Response(503) if failed else httpx.Response(200, text=html())

    client = ReaClient(store, Settings(request_interval=0), httpx.MockTransport(respond))
    try:
        await client.week(4, 1, date(2026, 11, 2))
        store.conn.execute("UPDATE cache SET fetched=?", (time.time() - 9999,))
        failed = True
        result = await client.week(4, 1, date(2026, 11, 2))
        assert result["stale"]
        await client.week(4, 1, date(2026, 11, 2))
        assert calls == ["/", "/Schedule/ScheduleCard", "/Schedule/ScheduleCard"]
        assert len(store.week_lessons(4, 1, "2026-11-02", "2026-11-08")) == 13
    finally:
        await client.close()


async def test_unauthorized_request_never_reaches_network(store):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(500)

    client = ReaClient(store, Settings(request_interval=0), httpx.MockTransport(respond))
    try:
        with pytest.raises(UserError):
            await client.week(5, 1, date(2026, 11, 2))
        assert not calls
    finally:
        await client.close()


async def test_wrong_week_rejected_without_erasing_saved_data(store):
    def respond(request):
        return httpx.Response(200, text="home" if request.url.path == "/" else html())

    client = ReaClient(store, Settings(request_interval=0), httpx.MockTransport(respond))
    try:
        with pytest.raises(ScheduleError):
            await client.week(4, 1, date(2026, 11, 9))
        assert store.cache_get("week:1:2026-11-09") is None
        assert len(store.week_lessons(4, 1, "2026-11-02", "2026-11-08")) == 13
    finally:
        await client.close()
