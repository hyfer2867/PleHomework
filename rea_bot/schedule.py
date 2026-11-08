"""Lazy adapter for rasp.rea.ru, based on its Navigator and ScheduleCard contracts."""

import asyncio
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from .db import UserError

BASE_URL = "https://rasp.rea.ru"


class ScheduleError(UserError):
    pass


def clean_details(text):
    text = " ".join(text.split())
    text = re.sub(r"\s*,\s*", ", ", text)
    return re.sub(r"\s+-\s+", " — ", text)


def parse_options(html, name):
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", attrs={"name": name})
    if select is None:
        raise ScheduleError("Сайт изменил формат списка. Повторите позже.")
    return [
        {"value": x.get("value", x.get_text(strip=True)), "label": x.get_text(" ", strip=True)}
        for x in select.find_all("option")
        if x.get("value", "") not in ("", "na", "-1")
    ]


def parse_week(html):
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.select("table"):
        header = table.select_one(".dayh")
        if header:
            match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", header.get_text())
            if not match:
                raise ScheduleError("Не удалось прочитать дату в расписании.")
            tables.append((datetime.strptime(match[1], "%d.%m.%Y").date(), table))
    if not tables:
        # A login form, empty response or changed markup must never erase saved lessons.
        raise ScheduleError(
            "Сайт не вернул таблицу расписания. Сохранённые пары и ДЗ оставлены без изменений."
        )
    monday = min(day for day, _ in tables)
    monday -= timedelta(days=monday.weekday())
    if any(not monday <= day <= monday + timedelta(days=6) for day, _ in tables):
        raise ScheduleError("В ответе сайта смешаны разные недели.")
    # The site renders Monday–Saturday even if no classes. Partial markup is unsafe to sync.
    expected_days = {monday + timedelta(days=n) for n in range(6)}
    if not expected_days.issubset({day for day, _ in tables}):
        raise ScheduleError("Сайт вернул неполную неделю. Сохранённое расписание не изменено.")
    lessons = {}
    for day, table in tables:
        rows = table.select("tr.slot")
        if not rows and "Занятия отсутствуют" not in table.get_text(" ", strip=True):
            raise ScheduleError("Неизвестная разметка дня расписания.")
        for row in rows:
            tasks = row.select("a.task")
            if not tasks:
                if row.get_text(" ", strip=True) == "Занятия отсутствуют":
                    continue
                if "load-empty" not in row.get("class", []):
                    raise ScheduleError("Не удалось прочитать занятие. Обновление отменено.")
                continue
            caption = row.select_one(".pcap")
            slot = re.search(r"\d+", caption.get_text()) if caption else None
            cells = row.find_all("td", recursive=False)
            times = re.findall(r"\b\d{1,2}:\d{2}\b", cells[0].get_text(" ")) if cells else []
            if not slot or len(times) < 2:
                raise ScheduleError("Не удалось прочитать номер или время пары.")
            for task in tasks:
                strings = list(task.stripped_strings)
                if not strings:
                    raise ScheduleError("У занятия отсутствует название.")
                subject = strings[0]
                kind = task.find("i")
                kind = kind.get_text(" ", strip=True) if kind else ""
                details = clean_details(" ".join(strings[2:]) if kind else " ".join(strings[1:]))
                identity = [
                    day.isoformat(),
                    int(slot[0]),
                    subject,
                    kind,
                    task.get("data-elementid", ""),
                ]
                key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                lessons[key] = {
                    "source_key": key,
                    "day": day.isoformat(),
                    "slot": int(slot[0]),
                    "start": times[0],
                    "finish": times[1],
                    "subject": subject,
                    "kind": kind,
                    "details": details,
                }
    week_node = soup.select_one("#weekNum")
    raw_week = week_node.get("value", "") if week_node else ""
    return {
        "monday": monday.isoformat(),
        "sunday": (monday + timedelta(days=6)).isoformat(),
        "number": int(raw_week) if raw_week.isdigit() else None,
        "lessons": list(lessons.values()),
    }


class ReaClient:
    def __init__(self, store, settings, transport=None):
        self.store, self.settings = store, settings
        self.http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=20,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
                "X-Requested-With": "XMLHttpRequest",
            },
            transport=transport,
        )
        self.lock = asyncio.Lock()
        self.ready = False
        self.last_request = 0.0
        self.cooldown_until = 0.0

    async def close(self):
        await self.http.aclose()

    async def cached(self, key, ttl, method, path, parser, **kwargs):
        async with self.lock:
            cached = self.store.cache_get(key)
            if cached and time.time() - cached["fetched"] < ttl:
                return cached["value"], False, cached["fetched"]
            if time.monotonic() < self.cooldown_until:
                if cached:
                    return cached["value"], True, cached["fetched"]
                raise ScheduleError("Сайт расписания временно недоступен. Повторите через минуту.")
            await asyncio.sleep(
                max(0, self.settings.request_interval - (time.monotonic() - self.last_request))
            )
            try:
                if not self.ready:
                    # The site issues a session cookie on the landing page. Calling the
                    # AJAX endpoints without that session currently returns HTTP 403.
                    self.last_request = time.monotonic()
                    home = await self.http.get("/")
                    home.raise_for_status()
                    self.ready = True
                    await asyncio.sleep(self.settings.request_interval)
                self.last_request = time.monotonic()
                response = await self.http.request(method, path, **kwargs)
                response.raise_for_status()
                value = parser(response.text)
            except (httpx.HTTPError, ScheduleError, ValueError) as exc:
                self.cooldown_until = time.monotonic() + 60
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (
                    401,
                    403,
                ):
                    self.ready = False
                if cached:
                    return cached["value"], True, cached["fetched"]
                code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                detail = f" (HTTP {code})" if code else ""
                raise ScheduleError(
                    f"Не удалось загрузить расписание РЭУ{detail}. Попробуйте позже. ДЗ сохранены."
                ) from exc
            self.store.cache_put(key, value)
            return value, False, self.store.cache_get(key)["fetched"]

    async def navigator(self, course=None, level=None):
        state = {
            "Faculty": self.settings.faculty,
            "Course": course or "na",
            "Type": level or "na",
            "Group": "na",
            "Cathedra": "na",
            "Teacher": "na",
        }
        node, output = (
            ("Type", "Group")
            if level
            else (("Course", "Type") if course else ("Faculty", "Course"))
        )
        state.update(ChangedNode=node, ChangedValue=state[node])
        key = "nav:" + json.dumps(state, sort_keys=True, ensure_ascii=False)
        # Same multipart request as FormData in the public site's main.js.
        options, stale, fetched = await self.cached(
            key,
            self.settings.catalog_ttl,
            "POST",
            "/Schedule/Navigator",
            lambda text: parse_options(text, output),
            files={k: (None, v) for k, v in state.items()},
        )
        return {"options": options, "stale": stale, "fetched": fetched, "next": output}

    async def week(self, uid, gid, monday, force=False):
        self.store.require(uid, gid)
        group = self.store.group(gid)
        if group["faculty"] != self.settings.faculty:
            raise UserError("Пилот работает только с разрешённым факультетом.")
        # Academic week 1 is the Monday-based week containing September 1.
        # Validate returned dates, so a changed site convention cannot silently misattach homework.
        academic_year = monday.year if monday.month >= 9 else monday.year - 1
        sep = date(academic_year, 9, 1)
        origin = sep - timedelta(days=sep.weekday())
        week_number = (monday - origin).days // 7 + 1
        key = f"week:{gid}:{monday.isoformat()}"

        def parser(text):
            value = parse_week(text)
            if value["monday"] != monday.isoformat():
                raise ScheduleError("Сайт вернул другую неделю. Обновление отменено.")
            return value

        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        ttl = (
            max(self.settings.schedule_ttl, 86400)
            if monday > today + timedelta(days=7)
            else self.settings.schedule_ttl
        )
        value, stale, fetched = await self.cached(
            key,
            0 if force else ttl,
            "GET",
            "/Schedule/ScheduleCard",
            parser,
            params={"selection": group["name"].lower(), "weekNum": week_number, "catfilter": 0},
        )
        self.store.sync_lessons(uid, gid, value)
        return {**value, "stale": stale, "fetched": fetched}
