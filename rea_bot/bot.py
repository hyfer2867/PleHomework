import asyncio
import logging
import re
import weakref
from datetime import date, datetime, timedelta
from html import escape, unescape
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Message,
)

from .db import MAX_ATTACHMENTS, UserError, attachment_name, homework_attachments
from .navigation import Screen, attachment_message
from .refresh import ScheduleRefresh
from .schedule import ScheduleError, clean_details

ROLES = {
    "superadmin": "суперадмин",
    "head": "староста",
    "editor": "редактор ДЗ",
    "student": "студент",
}
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


class HomeworkAlbums(BaseMiddleware):
    def __init__(self, store):
        self.store = store
        self.pending = {}

    async def __call__(self, handler, message, data):
        state = self.store.session_get(message.from_user.id) if message.from_user else None
        if (
            not message.media_group_id
            or message.chat.type != "private"
            or not state
            or state["state"] not in ("write", "preview")
        ):
            return await handler(message, data)
        key = (message.chat.id, message.media_group_id)
        if key in self.pending:
            self.pending[key].append(message)
            return
        album = self.pending[key] = [message]
        try:
            while True:
                count = len(album)
                await asyncio.sleep(0.5)
                if count == len(album):
                    break
            current = self.store.session_get(message.from_user.id)
            identity = ("screen_id", "lid", "hid", "revision", "draft_id")
            if (
                not current
                or current["state"] not in ("write", "preview")
                or any(current.get(field) != state.get(field) for field in identity)
            ):
                return
            data["album"] = sorted(album, key=lambda item: item.message_id)
            return await handler(message, data)
        finally:
            self.pending.pop(key, None)


class PrivateAccess(BaseMiddleware):
    def __init__(self, store):
        self.store = store
        self.locks = weakref.WeakValueDictionary()

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом.", show_alert=True)
            return
        user = event.from_user
        if not user or user.is_bot or message.chat.id != user.id:
            return
        lock = self.locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            self.store.register(user.id, user.username, user.full_name)
            try:
                return await handler(event, data)
            except UserError as exc:
                if isinstance(event, CallbackQuery):
                    await event.answer(str(exc)[:200], show_alert=True)
                else:
                    await message.answer(escape(str(exc)))
            except Exception:
                logging.getLogger(__name__).exception("Failed to process a bot action")
                text = "Не удалось выполнить действие. Откройте /start и повторите. Если ошибка повторится, попробуйте позже."
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                else:
                    await message.answer(text)


class BotUI:
    def __init__(self, store, schedule, settings, refresher=None):
        self.refresher = refresher or ScheduleRefresh(store, schedule, settings)
        self.db, self.schedule, self.settings = store, schedule, settings
        self.tz = ZoneInfo(settings.timezone)

    def today(self):
        return datetime.now(self.tz).date()

    def button(self, uid, label, action, **payload):
        return InlineKeyboardButton(text=label, callback_data=self.db.token(uid, action, **payload))

    def keyboard(self, uid, rows, home=True):
        result = [
            [self.button(uid, label, action, **payload) for label, action, payload in row]
            for row in rows
        ]
        if home:
            result.append([self.button(uid, "🏠 Главное меню", "home")])
        return InlineKeyboardMarkup(inline_keyboard=result)

    async def home(self, msg, uid):
        self.db.session_clear(uid)
        member = self.db.membership(uid)
        if member and not self.db.is_admin(uid):
            await self.group(msg, uid, member["group_id"])
            return
        msg.route("home")
        rows = []
        if self.db.is_admin(uid):
            text = "<b>📚 PleHomework</b>\n\nВыберите группу или откройте список старост."
            rows.append([("📚 Группы", "groups", {}), ("👥 Старосты", "heads", {})])
            rows.append([("🗓 Загрузить до конца года", "year_confirm", {})])
        else:
            text = (
                "<b>📚 PleHomework</b>\n\n"
                "Отправьте старосте свой @username или ID, чтобы получить доступ к группе.\n\n"
                f"Ваш ID · <code>{uid}</code>"
            )
        if self.db.can_catalog(uid):
            label = "➕ Подключить группу" if self.db.is_admin(uid) else "🎓 Выбрать свою группу"
            rows.append([(label, "catalog", {})])
        rows.append([("ℹ️ Помощь", "help", {})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows, home=False))

    async def group(self, msg, uid, gid):
        self.db.require(uid, gid)
        group = self.db.group(gid)
        role = self.db.role(uid, gid)
        is_home = role != "superadmin"
        msg.route("group", gid=gid, root=is_home)
        rows = [
            [
                ("Сегодня", "week", {"gid": gid, "scope": "today"}),
                ("Завтра", "week", {"gid": gid, "scope": "tomorrow"}),
            ],
            [("📅 Выбрать неделю", "weeks", {"gid": gid})],
            [("📝 Все ДЗ за неделю", "week", {"gid": gid, "scope": "hw"})],
        ]
        if role in ("head", "superadmin"):
            rows.append([("👥 Участники и права", "members", {"gid": gid})])
        if is_home:
            rows.append([("ℹ️ Помощь", "help", {})])
        await msg.answer(
            f"<b>📚 {escape(group['name'])}</b>\n"
            f"<i>{escape(group['course'])} · {escape(group['level'])}</i>\n"
            + (f"Ваша роль: <b>{ROLES[role]}</b>.\n" if role != "student" else "")
            + "\nВыберите день или неделю, затем пару.",
            reply_markup=self.keyboard(uid, rows, home=not is_home),
        )

    async def groups(self, msg, uid, page=0):
        self.db.require_admin(uid)
        groups = self.db.visible_groups(uid)
        page = max(0, min(page, max(0, (len(groups) - 1) // 8)))
        msg.route("groups", page=page)
        rows = [[(g["name"], "group", {"gid": g["id"]})] for g in groups[page * 8 : page * 8 + 8]]
        if page:
            rows.append([("←", "groups", {"page": page - 1})])
        if len(groups) > (page + 1) * 8:
            rows.append([("→", "groups", {"page": page + 1})])
        rows.append([("➕ Подключить группу", "catalog", {})])
        await msg.answer(
            "<b>Подключённые группы</b>" if groups else "Пока нет подключённых групп.",
            reply_markup=self.keyboard(uid, rows),
        )

    async def catalog(self, msg, uid, course=None, level=None, page=0):
        self.db.require_catalog(uid)
        result = await self.schedule.navigator(course, level)
        options = result["options"]
        page = max(0, min(page, max(0, (len(options) - 1) // 8)))
        msg.route("catalog", course=course, level=level, page=page)
        rows = []
        for option in options[page * 8 : page * 8 + 8]:
            value = option["value"]
            if not course:
                action, payload = "catalog", {"course": value}
            elif not level:
                action, payload = "catalog", {"course": course, "level": value}
            else:
                action, payload = "choose_group", {"course": course, "level": level, "name": value}
            rows.append([(option["label"], action, payload)])
        if page:
            rows.append([("←", "catalog", {"course": course, "level": level, "page": page - 1})])
        if len(options) > (page + 1) * 8:
            rows.append([("→", "catalog", {"course": course, "level": level, "page": page + 1})])
        name = "группу" if level else ("уровень обучения" if course else "курс")
        suffix = "\n⚠️ Сайт недоступен, показан сохранённый список." if result["stale"] else ""
        await msg.answer(
            f"<b>Форсайт</b>\nВыберите {name}."
            + ("\nСписок пуст." if not options else "")
            + suffix,
            reply_markup=self.keyboard(uid, rows),
        )

    async def choose_group(self, msg, uid, name, course, level):
        self.db.require_catalog(uid)
        # Revalidate using cached official catalogue before making the membership concrete.
        result = await self.schedule.navigator(course, level)
        if name not in {o["value"] for o in result["options"]}:
            raise UserError("Группа больше не найдена в списке. Начните выбор заново.")
        group = self.db.catalog_group(uid, name, self.settings.faculty, course, level)
        if self.db.is_admin(uid):
            await self.group(msg, uid, group["id"])
        else:
            msg.route("choose_group", name=name, course=course, level=level)
            await msg.answer(
                f"Закрепить за вами группу <b>{escape(name)}</b>?\n"
                "Вы сможете добавлять одногруппников, назначать редакторов и публиковать ДЗ.",
                reply_markup=self.keyboard(
                    uid, [[("✅ Да, моя группа", "claim", {"gid": group["id"]})]]
                ),
            )

    async def year_confirm(self, msg, uid):
        self.db.require_admin(uid)
        today = self.today()
        start = today - timedelta(days=today.weekday())
        end = date(today.year, 12, 31)
        groups = [g for g in self.db.visible_groups(uid) if g["faculty"] == self.settings.faculty]
        count = ((end - start).days // 7 + 1) * len(groups)
        msg.route("year_confirm")
        await msg.answer(
            f"<b>Загрузить расписание до 31.12.{today.year}?</b>\nГрупп: {len(groups)}. Запросов недель: {count}.\nЗагрузка будет последовательной, с паузами. Сохранённые ДЗ останутся на месте.",
            reply_markup=self.keyboard(uid, [[("✅ Подтвердить загрузку", "year_load", {})]]),
        )

    async def year_load(self, msg, uid):
        self.db.require_admin(uid)
        today = self.today()
        start = today - timedelta(days=today.weekday())
        end = date(today.year, 12, 31)
        groups = [g for g in self.db.visible_groups(uid) if g["faculty"] == self.settings.faculty]
        total = ((end - start).days // 7 + 1) * len(groups)
        done = 0
        msg.pending_history = []
        async with self.refresher.lock:
            for group in groups:
                monday = start
                while monday <= end:
                    self.db.require_admin(uid)
                    await msg.answer(
                        f"Загрузка расписания: {done}/{total} недель.\n{escape(group['name'])} · {monday:%d.%m.%Y}"
                    )
                    try:
                        result = await self.schedule.week(uid, group["id"], monday, force=True)
                        if result["stale"]:
                            raise ScheduleError("Сайт недоступен, возвращена старая копия.")
                    except UserError as exc:
                        msg.notice = f"Загрузка остановлена: {done}/{total}. " + escape(str(exc))
                        await self.home(msg, uid)
                        return
                    done += 1
                    monday += timedelta(days=7)
        msg.notice = f"✅ Расписание загружено до 31.12.{today.year}. Недель: {done}."
        await self.home(msg, uid)

    async def weeks(self, msg, uid, gid, page=0, past=False):
        saved_weeks = self.db.saved_weeks(uid, gid)
        today = self.today()
        current = today - timedelta(days=today.weekday())
        upcoming = current + timedelta(days=7)
        if past:
            weeks = [week for week in reversed(saved_weeks) if week["monday"] < current.isoformat()]
        else:
            weeks = [week for week in saved_weeks if week["monday"] > upcoming.isoformat()]
        page_size = 8 if past else 6
        page = max(0, min(page, max(0, (len(weeks) - 1) // page_size)))
        msg.route("weeks", gid=gid, page=page, **({"past": True} if past else {}))
        rows = []
        if not past:
            for name, start in (("Текущая неделя", current), ("Следующая неделя", upcoming)):
                end = start + timedelta(days=6)
                label = f"{name} · {start:%d.%m}–{end:%d.%m}"
                rows.append([(label, "week", {"gid": gid, "monday": start.isoformat()})])
        for week in weeks[page * page_size : (page + 1) * page_size]:
            start, end = date.fromisoformat(week["monday"]), date.fromisoformat(week["sunday"])
            count = len(week["lessons"])
            label = f"{start:%d.%m}–{end:%d.%m.%Y} · пар: {count}"
            rows.append([(label, "week", {"gid": gid, "monday": week["monday"]})])
        destination = {"gid": gid, "past": past}
        paging = []
        if page:
            paging.append(("←", "weeks", {**destination, "page": page - 1}))
        if len(weeks) > (page + 1) * page_size:
            paging.append(("→", "weeks", {**destination, "page": page + 1}))
        if paging:
            rows.append(paging)
        if past:
            text = "<b>Прошедшие недели</b>"
            if not weeks:
                text += "\nПока нет сохранённых прошедших недель."
        else:
            text = "<b>Выберите неделю</b>"
            rows.append([("🗂 Прошедшие недели", "weeks", {"gid": gid, "past": True})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    def adjacent_weeks(self, gid, monday, today, scope):
        buttons = []
        for label, offset in (("← Неделя", -7), ("Неделя →", 7)):
            start = monday + timedelta(days=offset)
            if abs((start - today).days) <= 370 or self.db.cache_get(f"week:{gid}:{start}"):
                buttons.append(
                    (label, "week", {"gid": gid, "monday": start.isoformat(), "scope": scope})
                )
        return buttons

    async def week(self, msg, uid, gid, monday=None, scope="week", page=0, day=None):
        self.db.require(uid, gid)
        today = self.today()
        target = today + timedelta(days=1) if scope == "tomorrow" else today
        if scope == "day":
            target = date.fromisoformat(day)
        if scope in ("today", "tomorrow"):
            scope, day = "day", target.isoformat()
        monday = date.fromisoformat(monday) if monday else target - timedelta(days=target.weekday())
        saved = self.db.cache_get(f"week:{gid}:{monday.isoformat()}")
        if monday.weekday() != 0 or (not saved and abs((monday - today).days) > 370):
            raise UserError(
                "Выберите сохранённую неделю или неделю в пределах года от текущей даты."
            )
        sunday = monday + timedelta(days=6)
        if scope == "day" and not monday <= target <= sunday:
            raise UserError("День не относится к выбранной неделе.")
        if saved:
            updated = datetime.fromtimestamp(saved["fetched"], self.tz).strftime("%d.%m.%Y %H:%M")
            notice = f"Сохранённое расписание. Обновлено: {updated}."
        else:
            notice = (
                "Расписание на эту неделю ещё не загружено. Ближайшие две недели проверяются утром."
            )
        lessons = self.db.week_lessons(uid, gid, monday.isoformat(), sunday.isoformat())
        if scope == "week":
            msg.route("week", gid=gid, monday=monday.isoformat(), scope=scope, page=0)
            rows = []
            for offset in range(7):
                selected = monday + timedelta(days=offset)
                daily = [item for item in lessons if item["day"] == selected.isoformat()]
                count = sum(item["hw_count"] for item in daily)
                label = f"{DAYS[offset]} ({selected:%d.%m}) · пар: {len(daily)} · ДЗ: {count}"
                rows.append(
                    [
                        (
                            label,
                            "week",
                            {
                                "gid": gid,
                                "monday": monday.isoformat(),
                                "scope": "day",
                                "day": selected.isoformat(),
                            },
                        )
                    ]
                )
            rows.append(
                [
                    (
                        "📝 Все ДЗ за неделю",
                        "week",
                        {"gid": gid, "monday": monday.isoformat(), "scope": "hw"},
                    )
                ]
            )
            adjacent = self.adjacent_weeks(gid, monday, today, scope)
            if adjacent:
                rows.append(adjacent)
            await msg.answer(
                f"<b>📅 {escape(self.db.group(gid)['name'])}</b>\n"
                f"<b>{monday:%d.%m.%Y} — {sunday:%d.%m.%Y}</b>\n\n"
                "Выберите день недели.\n\n"
                f"<i>{escape(notice)}</i>",
                reply_markup=self.keyboard(uid, rows),
            )
            return
        if scope == "hw":
            tasks = [
                (lesson, task) for lesson in lessons for task in self.db.homework(uid, lesson["id"])
            ]
            page = max(0, min(page, max(0, (len(tasks) - 1) // 5)))
            msg.route("week", gid=gid, monday=monday.isoformat(), scope=scope, page=page)
            text = (
                "<h2>📝 Все ДЗ за неделю</h2>"
                f"<p><b>{monday:%d.%m.%Y} — {sunday:%d.%m.%Y}</b><br>"
                f"{escape(self.db.group(gid)['name'])} · Всего заданий: <b>{len(tasks)}</b></p>"
            )
            rows = []
            previous_day = None
            for lesson, task in tasks[page * 5 : page * 5 + 5]:
                day = date.fromisoformat(lesson["day"])
                label = f"{DAYS[day.weekday()]} · {day:%d.%m} · {lesson['slot']} пара · {lesson['subject'][:70]}"
                body = task["body"] or "Задание во вложении."
                if day != previous_day:
                    text += f"<hr><h3>{DAYS[day.weekday()]} · {day:%d.%m}</h3>"
                    previous_day = day
                files = homework_attachments(task)
                text += (
                    f"<p><b>📘 {escape(lesson['subject'][:100])} · {lesson['slot']} пара</b>"
                    + (" ⚠️ Пара изменилась" if not lesson["active"] else "")
                    + "</p><blockquote>"
                    + escape(body[:350])
                    + ("…" if len(body) > 350 else "")
                    + "</blockquote>"
                    + (f"<p><i>📎 Вложений: {len(files)}</i></p><br>" if files else "")
                )
                task_page = next(
                    i
                    for i, item in enumerate(self.db.homework(uid, lesson["id"]))
                    if item["id"] == task["id"]
                )
                rows.append(
                    [
                        (
                            label[:60],
                            "lesson",
                            {"lid": lesson["id"], "page": task_page},
                        )
                    ]
                )
            if not tasks:
                text += (
                    "<blockquote>"
                    + ("ДЗ за эту неделю пока не добавлено." if saved or lessons else notice)
                    + "</blockquote>"
                )
            if len(tasks) > 5:
                text += f"<footer>Страница {page + 1} из {(len(tasks) + 4) // 5}</footer>"
            pages = []
            if page:
                pages.append(
                    (
                        "← ДЗ",
                        "week",
                        {"gid": gid, "monday": monday.isoformat(), "scope": "hw", "page": page - 1},
                    )
                )
            if len(tasks) > (page + 1) * 5:
                pages.append(
                    (
                        "ДЗ →",
                        "week",
                        {"gid": gid, "monday": monday.isoformat(), "scope": "hw", "page": page + 1},
                    )
                )
            if pages:
                rows.append(pages)
            adjacent = self.adjacent_weeks(gid, monday, today, scope)
            if adjacent:
                rows.append(adjacent)
            rows.append([("📅 Дни недели", "week", {"gid": gid, "monday": monday.isoformat()})])
            await msg.answer(text, reply_markup=self.keyboard(uid, rows), rich=True)
            return
        if scope in ("today", "tomorrow", "day"):
            lessons = [x for x in lessons if x["day"] == target.isoformat()]
        if scope == "hw":
            lessons = [x for x in lessons if x["hw_count"]]
        page = max(0, min(page, max(0, (len(lessons) - 1) // 8)))
        msg.route(
            "week",
            gid=gid,
            monday=monday.isoformat(),
            scope=scope,
            page=page,
            **({"day": target.isoformat()} if scope == "day" else {}),
        )
        text = (
            f"<b>{DAYS[target.weekday()]} · {target:%d.%m.%Y}</b>\n"
            f"<i>{escape(self.db.group(gid)['name'])}</i>\n\n"
            "📝 — есть ДЗ\n▫️ — ДЗ не добавлено"
        )
        if any(not item["active"] for item in lessons):
            text += "\n⚠️ — пара изменилась в расписании"
        rows = []
        for lesson in lessons[page * 8 : page * 8 + 8]:
            prefix = "⚠️" if not lesson["active"] else ("📝" if lesson["hw_count"] else "▫️")
            rows.append(
                [
                    (
                        f"{prefix} {lesson['slot']} пара · {lesson['subject']}",
                        "lesson",
                        {"lid": lesson["id"]},
                    )
                ]
            )
        if not lessons:
            text += (
                "\nНет добавленных ДЗ за этот период."
                if scope == "hw"
                else (
                    "\nРасписание на этот день ещё не загружено."
                    if not saved
                    else "\nСайт не указал занятий на этот период. Расписание может быть дополнено."
                )
            )
        base = {
            "gid": gid,
            "monday": monday.isoformat(),
            "scope": scope,
            **({"day": target.isoformat()} if scope == "day" else {}),
        }
        paging = []
        if page:
            paging.append(("← Пары", "week", {**base, "page": page - 1}))
        if len(lessons) > (page + 1) * 8:
            paging.append(("Пары →", "week", {**base, "page": page + 1}))
        if paging:
            rows.append(paging)
        rows.append(
            [
                (
                    "← День",
                    "week",
                    {"gid": gid, "scope": "day", "day": (target - timedelta(days=1)).isoformat()},
                ),
                (
                    "День →",
                    "week",
                    {"gid": gid, "scope": "day", "day": (target + timedelta(days=1)).isoformat()},
                ),
            ]
        )
        rows.append([("📅 Вся неделя", "week", {"gid": gid, "monday": monday.isoformat()})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    def lesson_title(self, lesson):
        day = date.fromisoformat(lesson["day"])
        details = clean_details(lesson["details"])[:100]
        return (
            f"<h3>📘 {escape(lesson['subject'][:80])}</h3>"
            f"<p><b>{DAYS[day.weekday()]} · {day:%d.%m.%Y} · {lesson['slot']} пара</b><br>"
            f"<i>{escape(lesson['start'])}–{escape(lesson['finish'])} · "
            f"{escape(' '.join(lesson['kind'].split())[:50])}</i></p>"
            + (f"<p>📍 {escape(details)}</p>" if details else "")
        )

    @staticmethod
    def text_chunks(text, limit=600):
        chunks = []
        current = ""
        size = 0
        for char in text:
            length = len(char.encode("utf-16-le")) // 2
            if size + length > limit:
                chunks.append(current)
                current = ""
                size = 0
            current += char
            size += length
        chunks.append(current)
        return chunks

    async def document_input(self, msg, file_id, file_kind):
        if file_kind != "photo":
            return file_id
        cached = self.db.one("SELECT document_id FROM document_cache WHERE source_id=?", (file_id,))
        if cached:
            return cached["document_id"]
        stream = await msg.bot.download(file_id)
        return BufferedInputFile(stream.getvalue(), filename="homework-photo.jpg")

    def remember_document(self, source_id, document):
        if document is not None:
            self.db.conn.execute(
                "INSERT OR REPLACE INTO document_cache VALUES(?,?)",
                (source_id, document.file_id),
            )

    async def show_file(self, msg, uid, text, rows, file_id, file_kind):
        document = await self.document_input(msg, file_id, file_kind)
        result = await msg.answer(text, reply_markup=self.keyboard(uid, rows), documents=[document])
        if file_kind == "photo":
            rich = getattr(result, "rich_message", None)
            if rich and rich.blocks and rich.blocks[-1].type == "document":
                self.remember_document(file_id, rich.blocks[-1].document)
        return result

    async def send_files_to_chat(self, msg, files):
        media_group_limit = 10
        for start in range(0, len(files), media_group_limit):
            batch = files[start : start + media_group_limit]
            photos = [file["file_id"] for file in batch if file["file_kind"] == "photo"]
            documents = [file["file_id"] for file in batch if file["file_kind"] == "document"]
            if photos:
                await msg.bot.send_rich_message(
                    chat_id=msg.chat.id, rich_message=attachment_message("", documents, photos)
                )
                continue
            if len(documents) == 1:
                await msg.answer_document(documents[0])
            else:
                await msg.answer_media_group(
                    media=[InputMediaDocument(media=document) for document in documents]
                )

    async def show_homework(self, msg, uid, text, rows, files):
        if not files:
            await msg.answer(text, reply_markup=self.keyboard(uid, rows))
            return
        if len(files) == 1 and files[0]["file_kind"] == "document":
            file = files[0]
            await self.show_file(msg, uid, text, rows, file["file_id"], file["file_kind"])
            return
        documents = [file["file_id"] for file in files if file["file_kind"] == "document"]
        photos = [file["file_id"] for file in files if file["file_kind"] == "photo"]
        await msg.answer(
            text, reply_markup=self.keyboard(uid, rows), documents=documents, photos=photos
        )

    async def lesson(self, msg, uid, lid, page=0, text_page=0):
        lesson = self.db.lesson(uid, lid)
        tasks = self.db.homework(uid, lid)
        page = max(0, min(page, len(tasks) - 1))
        msg.route("lesson", lid=lid, page=page, text_page=text_page)
        role = self.db.role(uid, lesson["group_id"])
        writable = role in ("superadmin", "head", "editor")
        text = self.lesson_title(lesson)
        if not lesson["active"]:
            text += "\n⚠️ Пара исчезла или изменилась в расписании. ДЗ сохранено; старосте нужно проверить привязку."
        rows = []
        files = []
        homework_actions = []
        pages = []
        materials_target = {"lid": lid}
        if tasks:
            hw = tasks[page]
            if lesson["active"] and role in ("head", "superadmin"):
                materials_target["homework_id"] = hw["id"]
            files = homework_attachments(hw)
            updated = datetime.fromtimestamp(hw["updated"], self.tz).strftime("%d.%m %H:%M")
            identifier = f" · #{hw['id']}" if self.db.is_admin(uid) else ""
            text += f"\n\n<b>ДЗ {page + 1} из {len(tasks)}{identifier}</b>\n<i>Обновлено {updated}</i>\n\n"
            # One shared header for text-only homework and attachments; reserve space
            # for Telegram's caption limit, page indicator and save confirmation.
            header_units = len(unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2
            limit = 6000 if files else 2400
            if len(files) == 1 and files[0]["file_kind"] == "document":
                limit = max(1, min(600, 1024 - header_units - 100))
            chunks = self.text_chunks(hw["body"] or "Задание во вложении.", limit)
            text_page = max(0, min(text_page, len(chunks) - 1))
            msg.route("lesson", lid=lid, page=page, text_page=text_page)
            text += "<blockquote>" + escape(chunks[text_page]) + "</blockquote>"
            if len(chunks) > 1:
                text += f"\n\nТекст: {text_page + 1}/{len(chunks)}"
                controls = []
                if text_page:
                    controls.append(
                        (
                            "← Текст",
                            "lesson",
                            {
                                "lid": lid,
                                "page": page,
                                "text_page": text_page - 1,
                            },
                        )
                    )
                if text_page + 1 < len(chunks):
                    controls.append(
                        (
                            "Текст →",
                            "lesson",
                            {
                                "lid": lid,
                                "page": page,
                                "text_page": text_page + 1,
                            },
                        )
                    )
                rows.append(controls)
            if files:
                rows.append(
                    [
                        (
                            "📎 Прислать файлы в чат",
                            "send_attachment",
                            {"hid": hw["id"], "revision": hw["revision"]},
                        ),
                    ]
                )
            if writable:
                homework_actions.append(("✏️ Редактировать", "edit_homework", {"hid": hw["id"]}))
            if page:
                pages.append(("← ДЗ", "lesson", {"lid": lid, "page": page - 1}))
            if page + 1 < len(tasks):
                pages.append(("ДЗ →", "lesson", {"lid": lid, "page": page + 1}))
        else:
            text += "\n\nДЗ пока не добавлено."
        if writable and lesson["active"]:
            homework_actions.append(("➕ Добавить ДЗ", "write", {"lid": lid}))
        if homework_actions:
            rows.append(homework_actions)
        if pages:
            rows.append(pages)
        rows.insert(1, [("📚 Материалы курса", "materials", materials_target)])
        await self.show_homework(msg, uid, text, rows, files)

    def material_draft(self, msg, uid, lid, draft_id):
        draft = self.db.session_get(uid)
        if not draft or (draft.get("draft_id"), draft.get("lid"), draft.get("screen_id")) != (
            draft_id,
            lid,
            msg.message_id,
        ):
            raise UserError("Черновик устарел. Откройте добавление ДЗ заново.")
        self.db.lesson(uid, lid, "manage")
        return draft

    async def materials(
        self, msg, uid, lid=None, hid=None, page=0, draft_id=None, homework_id=None, remove=False
    ):
        route = "materials"
        draft = None
        if draft_id:
            draft = self.material_draft(msg, uid, lid, draft_id)
            target = {"lid": lid, "draft_id": draft_id}
            route = "draft_materials"
        elif hid is None:
            target = {"lid": lid}
            if homework_id is not None:
                target["homework_id"] = homework_id
            if remove:
                target["remove"] = True
        else:
            homework = self.db.homework_item(uid, hid, "manage")
            lid = homework["lesson_id"]
            target = {"hid": hid}
        lesson = self.db.lesson(uid, lid, "manage" if remove else "read")
        can_manage = self.db.role(uid, lesson["group_id"]) in ("head", "superadmin")
        files = self.db.subject_materials(uid, lid)
        title = "Материалы курса"
        attached = set()
        if remove:
            title = "Удалить материал"
        elif draft or hid is not None:
            title = "Файл из материалов"
            attached = {file["file_id"] for file in homework_attachments(draft or homework)}
        page_size = 8
        page = max(0, min(page, (len(files) - 1) // page_size))
        msg.route(route, **target, page=page)
        rows = []
        for file in files[page * page_size : (page + 1) * page_size]:
            selection = {**target, "material_id": file["id"]}
            if remove:
                action = "confirm_material_delete"
                selection = {"lid": lid, "material_id": file["id"]}
                icon = "📄"
            elif not draft and hid is None:
                action = "send_materials"
                icon = "📄"
            else:
                is_attached = file["file_id"] in attached
                icon = "✅" if is_attached else "▫️"
                selection["page"] = page
                if draft:
                    action = "draft_material"
                    selection["detach"] = is_attached
                else:
                    action = "detach_material" if is_attached else "attach_material"
                    selection["revision"] = homework["revision"]
            rows.append([(f"{icon} {file['name'][:60]}", action, selection)])
        paging = []
        if page:
            paging.append(("←", route, {**target, "page": page - 1}))
        if (page + 1) * page_size < len(files):
            paging.append(("→", route, {**target, "page": page + 1}))
        if paging:
            rows.append(paging)
        text = f"<b>{title} · {escape(lesson['subject'][:100])}</b>"
        if files:
            text += f"\n<i>Файлов: {len(files)}</i>"
        if not files:
            text += "\n\nМатериалов пока нет."
        elif remove:
            text += "\n\nВыберите файл."
        elif hid is None and not draft:
            file_actions = []
            if (
                homework_id is not None
                and lesson["active"]
                and can_manage
                and any(task["id"] == homework_id for task in self.db.homework(uid, lid))
            ):
                file_actions.append(("📎 Добавить к ДЗ", "materials", {"hid": homework_id}))
            file_actions.append(("📤 Все в чат", "send_materials", {"lid": lid}))
            rows.append(file_actions)
        else:
            text += "\n\n✅ В ДЗ — нажмите, чтобы открепить\n▫️ Нажмите, чтобы прикрепить"
            if len(attached) >= MAX_ATTACHMENTS:
                text += f"\nДостигнут лимит: {MAX_ATTACHMENTS} файлов на ДЗ."
        if can_manage and not (draft or hid is not None or remove):
            manage_actions = [("➕ Загрузить", "upload_materials", {"lid": lid})]
            if files:
                manage_actions.append(("🗑 Удалить", "materials", {**target, "remove": True}))
            rows.append(manage_actions)
        if draft:
            self.db.session_clear(uid)
            self.db.session_set(uid, draft)
            await msg.answer(
                text, reply_markup=self.keyboard(uid, rows), back_label="⬅️ К черновику"
            )
            msg.bind_input()
        else:
            await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def upload_materials(self, msg, uid, lid):
        lesson = self.db.lesson(uid, lid, "manage")
        msg.route("upload_materials", lid=lid)
        self.db.session_set(uid, {"state": "upload_materials", "lid": lid})
        await msg.answer(
            f"<b>Загрузить файлы · {escape(lesson['subject'][:100])}</b>\n\n"
            "Пришлите документы или фото. Каждый файл сразу появится в материалах. "
            "Для фото можно указать название в подписи. Кнопка «Назад» вернёт вас к списку.",
            reply_markup=self.keyboard(uid, []),
        )
        msg.bind_input()

    async def confirm_material_delete(self, msg, uid, lid, material_id):
        material = self.db.subject_material(uid, lid, material_id, "manage")
        msg.route("confirm_material_delete", lid=lid, material_id=material_id)
        rows = [
            [
                (
                    "🗑 Удалить из материалов",
                    "delete_material",
                    {"lid": lid, "material_id": material_id},
                )
            ]
        ]
        await msg.answer(
            "<b>Удалить материал?</b>\n\n"
            f"<blockquote>{escape(material['name'][:200])}</blockquote>\n"
            "<i>Вложения в ДЗ сохранятся.</i>",
            reply_markup=self.keyboard(uid, rows),
        )

    async def change_draft_material(self, msg, uid, lid, draft_id, material_id, detach, page):
        draft = self.material_draft(msg, uid, lid, draft_id)
        if not msg.current or msg.current["action"] != "draft_materials":
            raise UserError("Откройте «Из материалов» в черновике.")
        material = self.db.subject_material(uid, lid, material_id)
        files = homework_attachments(draft)
        other_files = [file for file in files if file["file_id"] != material["file_id"]]
        if detach:
            files = other_files
        elif len(other_files) == len(files):
            if len(files) >= MAX_ATTACHMENTS:
                raise UserError(f"К одному ДЗ можно прикрепить до {MAX_ATTACHMENTS} файлов.")
            files.append({key: material[key] for key in ("file_id", "file_kind", "name")})
        draft.update(attachments=files, state="preview" if files or draft["body"] else "write")
        self.db.session_set(uid, draft)
        await self.materials(msg, uid, lid=lid, draft_id=draft_id, page=page)

    async def edit_homework(self, msg, uid, hid):
        homework = self.db.homework_item(uid, hid, "write")
        lesson = self.db.lesson(uid, homework["lesson_id"])
        msg.route("edit_homework", hid=hid)
        rows = []
        if lesson["active"]:
            target = {"lid": lesson["id"], "hid": hid}
            rows = [
                [
                    ("➕ Дописать текст", "write", {**target, "description_mode": "append"}),
                    ("🔄 Заменить текст", "write", {**target, "description_mode": "replace"}),
                ],
                [("📎 Редактировать файлы", "edit_files", {"hid": hid})],
            ]
        rows.append([("🗑 Удалить ДЗ", "delete", {"hid": hid})])
        await msg.answer("<b>Редактировать ДЗ</b>", reply_markup=self.keyboard(uid, rows))

    async def edit_files(self, msg, uid, hid):
        homework = self.db.homework_item(uid, hid, "write")
        files = homework_attachments(homework)
        msg.route("edit_files", hid=hid)
        rows = [
            [
                (
                    f"📄 {attachment_name(file, index)[:60]}",
                    "edit_file",
                    {"hid": hid, "index": index, "revision": homework["revision"]},
                )
            ]
            for index, file in enumerate(files)
        ]
        if len(files) < MAX_ATTACHMENTS:
            rows.append(
                [
                    (
                        "➕ Добавить файл",
                        "write",
                        {"lid": homework["lesson_id"], "hid": hid, "attach": True},
                    )
                ]
            )
        text = f"<b>Редактировать файлы</b>\n<i>Вложений: {len(files)} из {MAX_ATTACHMENTS}</i>"
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    def file_for_edit(self, uid, hid, index, revision):
        homework, files = self.db.homework_files_for_edit(uid, hid, revision)
        if not 0 <= index < len(files):
            raise UserError("Список файлов изменился. Откройте его заново.")
        return homework, files

    async def edit_file(self, msg, uid, hid, index, revision):
        homework, files = self.file_for_edit(uid, hid, index, revision)
        file = files[index]
        msg.route("edit_file", hid=hid, index=index, revision=revision)
        rows = [
            [
                (
                    "🗑 Удалить файл",
                    "confirm_file_delete",
                    {"hid": hid, "index": index, "revision": revision},
                )
            ]
        ]
        lesson = self.db.lesson(uid, homework["lesson_id"])
        if self.db.role(uid, lesson["group_id"]) in ("head", "superadmin"):
            rows.insert(
                0,
                [
                    (
                        "📎 Добавить в материалы",
                        "save_material",
                        {"hid": hid, "index": index, "revision": revision},
                    )
                ],
            )
        await self.show_file(
            msg,
            uid,
            f"<b>{escape(attachment_name(file, index)[:200])}</b>",
            rows,
            file["file_id"],
            file["file_kind"],
        )

    async def confirm_file_delete(self, msg, uid, hid, index, revision):
        homework, files = self.file_for_edit(uid, hid, index, revision)
        msg.route("confirm_file_delete", hid=hid, index=index, revision=revision)
        text = (
            "<b>Удалить файл?</b>\n\n"
            f"<blockquote>{escape(attachment_name(files[index], index)[:200])}</blockquote>"
        )
        if len(files) == 1 and not homework["body"].strip():
            text += "\n<b>Это единственный файл в ДЗ без текста.</b> При подтверждении задание будет удалено."
        rows = [
            [("Да, удалить", "delete_file", {"hid": hid, "index": index, "revision": revision})]
        ]
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def members(self, msg, uid, gid, page=0):
        members = self.db.members(uid, gid)
        page = max(0, min(page, max(0, (len(members) - 1) // 8)))
        msg.route("members", gid=gid, page=page)
        text = f"<b>Участники · {escape(self.db.group(gid)['name'])}</b>\n"
        if not members:
            text += "\n<blockquote>ℹ️ В группе пока нет участников.</blockquote>\n"
        for user in members[page * 8 : page * 8 + 8]:
            username = "@" + user["username"] if user["username"] else "без username"
            text += (
                f"\n<b>{escape(user['name'])}</b> · {escape(username)}\n"
                f"<code>{user['id']}</code> · <i>{ROLES[user['role']]}</i>\n"
            )
        rows = [
            [
                ("➕ Добавить", "person", {"operation": "add", "gid": gid}),
                ("✍️ Выдать права ДЗ", "person", {"operation": "editor", "gid": gid}),
            ],
            [
                ("Снять права ДЗ", "person", {"operation": "student", "gid": gid}),
                ("Удалить из группы", "person", {"operation": "remove", "gid": gid}),
            ],
        ]
        if page:
            rows.append([("←", "members", {"gid": gid, "page": page - 1})])
        if len(members) > (page + 1) * 8:
            rows.append([("→", "members", {"gid": gid, "page": page + 1})])
        rows.append([("⏳ Ожидают входа", "pending", {"gid": gid})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def remove_members(self, msg, uid, gid, page=0):
        people = [
            person
            for person in self.db.members(uid, gid)
            if person["id"] != uid
            and person["id"] not in self.db.admins
            and (person["role"] != "head" or self.db.is_admin(uid))
        ]
        page = max(0, min(page, max(0, (len(people) - 1) // 8)))
        msg.route("remove_members", gid=gid, page=page)
        rows = []
        for person in people[page * 8 : page * 8 + 8]:
            label = person["name"][:45] + (
                " · @" + person["username"] if person["username"] else ""
            )
            if person["role"] == "head":
                label += " · староста"
            rows.append([(label, "remove_member", {"gid": gid, "target": person["id"]})])
        if page:
            rows.append([("←", "remove_members", {"gid": gid, "page": page - 1})])
        if len(people) > (page + 1) * 8:
            rows.append([("→", "remove_members", {"gid": gid, "page": page + 1})])
        await msg.answer(
            "<b>Удалить участника</b>\nВыберите человека из списка. Повторный вход в бота ему не нужен."
            + (
                "\n<blockquote>ℹ️ Нет участников, доступных для удаления.</blockquote>"
                if not people
                else ""
            ),
            reply_markup=self.keyboard(uid, rows),
        )

    async def remove_member(self, msg, uid, gid, target):
        person = self.db.resolve_group_member(uid, gid, str(target))
        if person["role"] == "head" and not self.db.is_admin(uid):
            raise UserError("Недостаточно прав для снятия полномочий старосты.")
        msg.route("remove_member", gid=gid, target=target)
        await msg.answer(
            "<b>Удалить из группы?</b>\n\n"
            f"<b>{escape(person['name'])}</b>\nID: <code>{target}</code>\n\n"
            "<b>Доступ к группе будет закрыт.</b>"
            + (
                "\nПолномочия старосты и созданные им ожидающие приглашения будут отменены."
                if person["role"] == "head"
                else ""
            ),
            reply_markup=self.keyboard(
                uid,
                [
                    [
                        (
                            "✅ Подтвердить",
                            "person_confirm",
                            {"operation": "remove", "gid": gid, "target": target},
                        )
                    ]
                ],
            ),
        )

    async def pending(self, msg, uid, gid, page=0):
        people = self.db.pending_members(uid, gid)
        page = max(0, min(page, max(0, (len(people) - 1) // 8)))
        msg.route("pending", gid=gid, page=page)
        text = (
            "<b>Ожидают первого входа</b>\n"
            + escape(self.db.group(gid)["name"])
            + "\n<i>Привязки действуют 30 дней.</i>\nНажмите имя, чтобы отменить ожидание.\n"
        )
        rows = []
        for person in people[page * 8 : page * 8 + 8]:
            name = person["username"]
            rows.append([("Отменить @" + name, "pending_cancel", {"gid": gid, "username": name})])
        if not people:
            text += "\n<blockquote>ℹ️ Ожидающих пользователей нет.</blockquote>"
        if page:
            rows.append([("←", "pending", {"gid": gid, "page": page - 1})])
        if len(people) > (page + 1) * 8:
            rows.append([("→", "pending", {"gid": gid, "page": page + 1})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def heads(self, msg, uid, page=0):
        users = self.db.heads(uid)
        page = max(0, min(page, max(0, (len(users) - 1) // 8)))
        msg.route("heads", page=page)
        text = "<b>Назначенные старосты</b>\n"
        if not users:
            text += "\n<blockquote>ℹ️ Назначенных старост пока нет.</blockquote>\n"
        for user in users[page * 8 : page * 8 + 8]:
            text += (
                f"\n<b>{escape(user['name'])}</b> · <code>{user['id']}</code>\n"
                f"<i>{escape(user['group_name'] or 'Группа ещё не выбрана')}</i>\n"
            )
        rows = [
            [
                ("Назначить", "person", {"operation": "appoint"}),
                ("Снять полномочия", "person", {"operation": "revoke"}),
            ]
        ]
        if page:
            rows.append([("←", "heads", {"page": page - 1})])
        if len(users) > (page + 1) * 8:
            rows.append([("→", "heads", {"page": page + 1})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def revoke_heads(self, msg, uid, page=0):
        people = self.db.heads(uid)
        page = max(0, min(page, max(0, (len(people) - 1) // 8)))
        msg.route("revoke_heads", page=page)
        rows = []
        for person in people[page * 8 : page * 8 + 8]:
            label = person["name"][:35] + (
                " · @" + person["username"] if person["username"] else ""
            )
            rows.append([(label, "revoke_head", {"target": person["id"]})])
        if page:
            rows.append([("←", "revoke_heads", {"page": page - 1})])
        if len(people) > (page + 1) * 8:
            rows.append([("→", "revoke_heads", {"page": page + 1})])
        await msg.answer(
            "<b>Снять полномочия старосты</b>\nВыберите человека из списка."
            + ("\n<blockquote>ℹ️ Назначенных старост пока нет.</blockquote>" if not people else ""),
            reply_markup=self.keyboard(uid, rows),
        )

    async def revoke_head(self, msg, uid, target):
        people = self.db.heads(uid)
        person = next((item for item in people if item["id"] == target), None)
        if person is None:
            raise UserError("Полномочия этого старосты уже сняты. Обновите список.")
        msg.route("revoke_head", target=target)
        await msg.answer(
            "<b>Снять полномочия старосты?</b>\n\n"
            f"<b>{escape(person['name'])}</b>\nID: <code>{target}</code>\n\n"
            + (
                "Он останется студентом своей группы."
                if person["group_name"]
                else "Выбор группы для него станет недоступен."
            ),
            reply_markup=self.keyboard(
                uid,
                [[("✅ Подтвердить", "person_confirm", {"operation": "revoke", "target": target})]],
            ),
        )

    def check_person_action(self, uid, operation, gid=None):
        if operation in ("appoint", "revoke"):
            self.db.require_admin(uid)
        elif operation in ("add", "editor", "student", "remove") and gid:
            self.db.require(uid, gid, "manage")
        else:
            raise UserError("Недопустимое действие.")

    async def person(self, msg, uid, operation, gid=None):
        self.check_person_action(uid, operation, gid)
        if operation in ("appoint", "editor", "student"):
            await self.pick_person(msg, uid, operation, gid)
            return
        if operation == "revoke":
            await self.revoke_heads(msg, uid)
            return
        if operation == "remove":
            await self.remove_members(msg, uid, gid)
            return
        msg.route("person", operation=operation, gid=gid)
        self.db.session_set(uid, {"state": "person", "operation": operation, "gid": gid})
        await msg.answer(
            "<b>Добавить участников</b>\n\n"
            + (
                "Отправьте @username одногруппников списком: через пробел, запятую или с новой строки (до 100). Можно добавить людей до первого запуска бота. Либо отправьте один числовой ID."
                if operation == "add"
                else "Отправьте @username или числовой ID зарегистрированного пользователя."
            )
            + "\n\n<i>Кнопка «Назад» или /cancel — отменить.</i>",
            reply_markup=self.keyboard(uid, []),
        )
        msg.bind_input()

    def eligible_people(self, uid, operation, gid=None):
        self.check_person_action(uid, operation, gid)
        if operation == "appoint":
            return [
                person
                for person in self.db.all(
                    "SELECT id,name,username FROM users WHERE head_allowed=0 ORDER BY name,id"
                )
                if person["id"] not in self.db.admins
            ]
        if operation not in ("editor", "student"):
            raise UserError("Недопустимое действие.")
        source_role = "student" if operation == "editor" else "editor"
        return [
            person
            for person in self.db.members(uid, gid)
            if person["role"] == source_role
            and person["id"] != uid
            and person["id"] not in self.db.admins
        ]

    async def pick_person(self, msg, uid, operation, gid=None, page=0):
        people = self.eligible_people(uid, operation, gid)
        page = max(0, min(page, max(0, (len(people) - 1) // 8)))
        msg.route("pick_person", operation=operation, gid=gid, page=page)
        titles = {
            "appoint": "Назначить старосту",
            "editor": "Выдать права ДЗ",
            "student": "Снять права ДЗ",
        }
        rows = []
        for person in people[page * 8 : page * 8 + 8]:
            label = person["name"][:40] + (
                " · @" + person["username"] if person["username"] else ""
            )
            rows.append(
                [
                    (
                        label,
                        "pick_confirm",
                        {"operation": operation, "gid": gid, "target": person["id"]},
                    )
                ]
            )
        if page:
            rows.append(
                [("←", "pick_person", {"operation": operation, "gid": gid, "page": page - 1})]
            )
        if len(people) > (page + 1) * 8:
            rows.append(
                [("→", "pick_person", {"operation": operation, "gid": gid, "page": page + 1})]
            )
        text = f"<b>{titles[operation]}</b>\nВыберите человека из списка."
        if not people:
            empty = {
                "appoint": "Нет пользователей, доступных для назначения старостой.",
                "editor": "Нет студентов, которым можно выдать права ДЗ.",
                "student": "Редакторов ДЗ пока нет. Снимать права не у кого.",
            }
            text += "\n<blockquote>ℹ️ " + empty[operation] + "</blockquote>"
        if operation == "appoint":
            text += "\nВ списке отображаются пользователи, которые уже открывали бота."
        await msg.answer(text, reply_markup=self.keyboard(uid, rows))

    async def pick_confirm(self, msg, uid, operation, target, gid=None):
        person = next(
            (p for p in self.eligible_people(uid, operation, gid) if p["id"] == target), None
        )
        if person is None:
            raise UserError("Права или состав участников изменились. Обновите список.")
        msg.route("pick_confirm", operation=operation, gid=gid, target=target)
        titles = {
            "appoint": "Назначить старостой",
            "editor": "Выдать права редактора ДЗ",
            "student": "Снять права редактора ДЗ",
        }
        await msg.answer(
            f"<b>{titles[operation]}?</b>\n{escape(person['name'])}\nID: <code>{target}</code>",
            reply_markup=self.keyboard(
                uid,
                [
                    [
                        (
                            "✅ Подтвердить",
                            "person_confirm",
                            {"operation": operation, "gid": gid, "target": target},
                        )
                    ]
                ],
            ),
        )

    async def person_confirm(self, msg, uid, operation, target, gid=None):
        self.check_person_action(uid, operation, gid)
        if operation == "appoint":
            self.db.appoint_head(uid, target)
        elif operation == "revoke":
            self.db.revoke_head(uid, target)
        else:
            self.db.set_member(uid, gid, target, operation)
        self.db.session_clear(uid)
        msg.notice = "✅ Права обновлены."
        route = msg.return_to("members", gid=gid) if gid else msg.return_to("heads")
        await self.render(msg, uid, route)

    async def write(self, msg, uid, lid, hid=None, attach=False, description_mode=None):
        lesson = self.db.lesson(uid, lid, "write")
        hw = self.db.homework_item(uid, hid, "write") if hid else None
        if hw and hw["lesson_id"] != lid:
            raise UserError("Задание относится к другой паре.")
        if attach and hw is None:
            raise UserError("Сначала выберите существующее ДЗ.")
        if hw and not attach and description_mode is None:
            await self.edit_homework(msg, uid, hid)
            return
        msg.route("write", lid=lid, hid=hid, attach=attach, description_mode=description_mode)
        draft = {
            "state": "write",
            "lid": lid,
            "hid": hid,
            "revision": hw["revision"] if hw else None,
            "body": hw["body"] if hw and description_mode != "replace" else "",
            "attachments": homework_attachments(hw) if hw else [],
            "replace_description": description_mode == "replace",
        }
        if hw is None and self.db.role(uid, lesson["group_id"]) in ("head", "superadmin"):
            draft["draft_id"] = uuid4().hex
        self.db.session_set(uid, draft)
        if description_mode:
            await self.homework_preview(msg, uid, lesson, draft)
            return
        if attach:
            prompt = (
                "Пришлите файл или фото. Если добавите подпись, она дополнит описание."
                "\nОтправляйте текст и файлы по очереди. Они попадут в один черновик.\n"
                "\nКогда всё готово, нажмите <b>«Сохранить ДЗ»</b>.\n"
                f"<i>До 3000 символов и {MAX_ATTACHMENTS} файлов на задание.</i>\n"
                "Кнопка «Назад» или /cancel — отменить."
            )
        else:
            prompt = (
                "<b>📝 Пришлите задание</b>\n\n"
                "✏️ Текст\n"
                "📎 Файлы\n"
                "🖼 Фото или альбом\n\n"
                "Можно отправить несколько сообщений — всё соберётся <b>в одно ДЗ</b>. "
                "Подписи к вложениям станут частью описания.\n\n"
                "Готово? Нажмите <b>«Сохранить ДЗ»</b>.\n\n"
                f"<i>3000 символов · {MAX_ATTACHMENTS} файлов</i>\n"
                "<i>«Назад» или /cancel — отменить.</i>"
            )
        await msg.answer(
            prompt,
            reply_markup=self.keyboard(uid, self.draft_material_buttons(draft)),
        )
        msg.bind_input()

    @staticmethod
    def draft_material_buttons(draft):
        if not draft.get("draft_id"):
            return []
        target = {"lid": draft["lid"], "draft_id": draft["draft_id"]}
        return [[("📚 Из материалов", "draft_materials", target)]]

    async def homework_preview(self, msg, uid, lesson, draft):
        body, files = draft["body"], draft["attachments"]
        awaiting_description = draft["replace_description"]
        rows = self.draft_material_buttons(draft)
        if draft["state"] == "preview" and not awaiting_description:
            # The button stores the immutable draft shown in this preview.
            confirm = {k: draft[k] for k in ("lid", "hid", "revision", "body", "attachments")}
            rows.insert(0, [("✅ Сохранить ДЗ", "save", confirm)])
        heading = (
            f"<b>Предпросмотр ДЗ · файлов: {len(files)}</b>" if files else "<b>Предпросмотр ДЗ</b>"
        )
        title = escape(lesson["subject"][:80]) if files else self.lesson_title(lesson)
        text = f"{heading}\n{title}"
        if body and not awaiting_description:
            limit = 6000 if files else 1000
            if len(files) == 1 and files[0]["file_kind"] == "document":
                limit = 600
            chunks = self.text_chunks(body, limit)
            text += f"\n\n<blockquote>{escape(chunks[0])}</blockquote>"
            if len(chunks) > 1:
                text += "\n… Полный текст будет сохранён."
        if awaiting_description:
            text += "\n\nПришлите новое описание текстом или подписью к файлу."
        elif draft["state"] == "write":
            text += "\n\nПришлите текст или файл, чтобы дополнить ДЗ."
        else:
            text += "\n\nМожно дослать текст или файл, затем сохранить ДЗ."
        await self.show_homework(msg, uid, text, rows, files)
        msg.bind_input()

    async def help(self, msg, uid):
        msg.route("help")
        member = self.db.membership(uid)
        if not self.db.is_admin(uid) and (not member or member["role"] != "head"):
            await msg.answer(
                "<b>Как найти ДЗ</b>\n\n"
                "В меню группы нажмите «Выбрать неделю», затем выберите неделю, день и пару.\n\n"
                "📝 — есть ДЗ\n▫️ — ДЗ не добавлено\n\n"
                "«Сегодня» и «Завтра» открывают день сразу. «Все ДЗ за неделю» показывает задания на текущую неделю.\n"
                "Переключайтесь стрелками, возвращайтесь кнопкой «Назад». Вложения можно получить кнопкой «Прислать файлы в чат».\n\n"
                "/start — главное меню",
                reply_markup=self.keyboard(uid, []),
            )
            return
        await msg.answer(
            "<b>Как пользоваться</b>\n\n"
            "1. Каждый студент нажимает /start.\n"
            "2. Владелец назначает старосту. Староста выбирает свою группу в «Форсайте».\n"
            "3. Староста добавляет одногруппников по @username или ID и при необходимости выдаёт права ДЗ.\n"
            "4. Откройте день → пару → домашнее задание. Староста и редакторы видят кнопку «Добавить ДЗ».\n\n"
            "Студенты видят только свою группу.\n"
            f"Расписание текущей и следующей недели проверяется ежедневно после {self.settings.refresh_hour:02d}:00. Просмотр использует сохранённые данные.\n\n"
            "/start — меню\n/id — ваш Telegram ID\n/cancel — отменить ввод\n/help — помощь",
            reply_markup=self.keyboard(uid, []),
        )

    async def message(self, msg: Message, album=None):
        uid = msg.from_user.id
        state = self.db.session_get(uid)
        msg = Screen(msg, self.db, uid, message_id=state.get("screen_id") if state else None)
        text = msg.text or ""
        command = text.split()[0].split("@")[0] if text.startswith("/") else ""
        if command == "/start":
            self.db.activate_pending(uid)
            await self.home(msg, uid)
            return
        if command == "/cancel":
            self.db.session_clear(uid)
            # Leave the whole input flow, including its preview.
            while msg.history and msg.history[-1]["action"] in ("write", "preview", "person"):
                msg.history.pop()
            await self.render(msg, uid, msg.back())
            return
        if command == "/id":
            await msg.message.answer(f"Ваш Telegram ID: <code>{uid}</code>")
            return
        if command in ("/help", "/info"):
            self.db.session_clear(uid)
            await self.help(msg, uid)
            return
        if command:
            raise UserError("Неизвестная команда. /start — меню, /cancel — отмена.")
        state = self.db.session_get(uid)
        if not state:
            await self.home(msg, uid)
            return
        if state["state"] == "upload_materials":
            if msg.document:
                file = {
                    "file_id": msg.document.file_id,
                    "file_kind": "document",
                    "name": msg.document.file_name or f"Файл {msg.message_id}",
                }
            elif msg.photo:
                caption = " ".join((msg.caption or "").split())
                file = {
                    "file_id": msg.photo[-1].file_id,
                    "file_kind": "photo",
                    "name": caption[:120] or f"Фото {msg.message_id}.jpg",
                }
            else:
                raise UserError("Пришлите документ или фото.")
            saved = self.db.upload_subject_material(uid, state["lid"], file)
            msg.notice = (
                f"✅ Добавлено: {escape(file['name'][:120])}."
                if saved
                else "Этот файл уже есть в материалах."
            )
            await self.upload_materials(msg, uid, state["lid"])
            return
        if state["state"] == "person":
            self.check_person_action(uid, state["operation"], state["gid"])
            if state["operation"] == "add" and not text.strip().isdecimal():
                names = self.db.parse_usernames(text)
                plan = self.db.prepare_members(uid, state["gid"], names)
                self.db.session_clear(uid)
                msg.route("person_preview")
                self.db.session_set(uid, {"state": "person_preview", "screen_id": msg.message_id})
                text = (
                    "<b>Добавить в группу?</b>\n"
                    + escape(self.db.group(state["gid"])["name"])
                    + "\n\n"
                    + escape(", ".join("@" + name for name in names))
                    + "\n\nУже зарегистрированные будут добавлены сразу. Остальные получат группу после /start. Ожидание действует 30 дней."
                )
                await msg.answer(
                    text,
                    reply_markup=self.keyboard(
                        uid,
                        [[("✅ Подтвердить", "bulk_confirm", {"gid": state["gid"], "plan": plan})]],
                    ),
                )
                msg.bind_input()
                return
            target = (
                self.db.resolve_group_member(uid, state["gid"], text)
                if state["operation"] == "remove"
                else self.db.resolve(text)
            )
            titles = {
                "add": "Добавить в группу",
                "editor": "Выдать право добавлять и изменять ДЗ",
                "student": "Снять права редактора ДЗ",
                "remove": "Удалить из группы",
                "appoint": "Назначить старостой",
                "revoke": "Снять полномочия старосты",
            }
            payload = {"operation": state["operation"], "gid": state["gid"], "target": target["id"]}
            self.db.session_clear(uid)
            msg.route("person_preview")
            self.db.session_set(uid, {"state": "person_preview", "screen_id": msg.message_id})
            await msg.answer(
                f"<b>{titles[state['operation']]}?</b>\n{escape(target['name'])}\n"
                f"@{escape(target['username'] or '—')} · ID <code>{target['id']}</code>\n\nПроверьте ID: username может меняться.",
                reply_markup=self.keyboard(uid, [[("✅ Подтвердить", "person_confirm", payload)]]),
            )
            msg.bind_input()
            return
        if state["state"] in ("write", "preview"):
            lesson = self.db.lesson(uid, state["lid"], "write")
            body = state.get("body", "")
            files = homework_attachments(state)
            replace_description = state.get("replace_description", False)
            for item in album or [msg]:
                file_kind = "photo" if item.photo else ("document" if item.document else None)
                file_id = (
                    item.photo[-1].file_id
                    if item.photo
                    else (item.document.file_id if item.document else None)
                )
                incoming = (item.text or item.caption or "").strip()
                if not (incoming or file_id):
                    raise UserError("Нужен текст, фото или документ.")
                if incoming:
                    body = f"{body}\n\n{incoming}" if body and not replace_description else incoming
                    replace_description = False
                if file_id:
                    file = {"file_id": file_id, "file_kind": file_kind}
                    if item.document and item.document.file_name:
                        file["name"] = item.document.file_name
                    if not any(existing["file_id"] == file_id for existing in files):
                        files = [*files, file]
            if len(body) > 3000:
                raise UserError(
                    "Общий текст ДЗ должен быть не длиннее 3000 символов. Черновик сохранён без последнего сообщения."
                )
            if len(files) > MAX_ATTACHMENTS:
                raise UserError(
                    f"К одному ДЗ можно прикрепить до {MAX_ATTACHMENTS} файлов. "
                    "Черновик сохранён без последних вложений."
                )
            self.db.session_clear(uid)
            payload = {
                **state,
                "state": "preview",
                "body": body,
                "attachments": files,
                "replace_description": replace_description,
            }
            self.db.session_set(uid, payload)
            if msg.current and msg.current["action"] == "draft_materials":
                msg.back()
            if not (msg.current and msg.current.get("description_mode")):
                msg.route("preview")
            await self.homework_preview(msg, uid, lesson, payload)
            return
        await msg.message.answer(
            "Подтвердите сохранение кнопкой под предпросмотром или отправьте /cancel."
        )

    async def delete(self, msg, uid, hid):
        hw = self.db.homework_item(uid, hid, "write")
        msg.route("delete", hid=hid)
        await msg.answer(
            ("Удалить ДЗ" + (f" #{hw['id']}" if self.db.is_admin(uid) else "") + "?"),
            reply_markup=self.keyboard(
                uid,
                [
                    [
                        (
                            "Да, удалить",
                            "delete_confirm",
                            {"hid": hw["id"], "revision": hw["revision"]},
                        )
                    ]
                ],
            ),
        )

    async def render(self, msg, uid, route):
        payload = dict(route)
        action = payload.pop("action")
        views = {
            "home": self.home,
            "year_confirm": self.year_confirm,
            "group": self.group,
            "groups": self.groups,
            "catalog": self.catalog,
            "choose_group": self.choose_group,
            "week": self.week,
            "weeks": self.weeks,
            "lesson": self.lesson,
            "materials": self.materials,
            "upload_materials": self.upload_materials,
            "confirm_material_delete": self.confirm_material_delete,
            "edit_homework": self.edit_homework,
            "edit_description": self.edit_homework,  # Route saved before the menus were merged.
            "edit_files": self.edit_files,
            "edit_file": self.edit_file,
            "confirm_file_delete": self.confirm_file_delete,
            "members": self.members,
            "pending": self.pending,
            "remove_members": self.remove_members,
            "remove_member": self.remove_member,
            "heads": self.heads,
            "revoke_heads": self.revoke_heads,
            "revoke_head": self.revoke_head,
            "person": self.person,
            "pick_person": self.pick_person,
            "pick_confirm": self.pick_confirm,
            "write": self.write,
            "help": self.help,
            "delete": self.delete,
        }
        if action not in views:
            raise UserError("Кнопка устарела. Откройте /start.")
        if action == "lesson":
            payload.pop("file_page", None)  # Cards sent before all files were shown together.
        await views[action](msg, uid, **payload)

    async def callback(self, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        payload = self.db.token_get(uid, query.data or "")
        action = payload.pop("action")
        if action == "open_material":  # Buttons from previously displayed lists.
            action = "send_materials"
        msg = Screen(query.message, self.db, uid, message_id=query.message.message_id)
        if action == "back" and msg.current and msg.current["action"] == "draft_materials":
            lid, draft_id = msg.current["lid"], msg.current["draft_id"]
            draft = self.material_draft(msg, uid, lid, draft_id)
            msg.back()
            msg.route("preview")
            await self.homework_preview(msg, uid, self.db.lesson(uid, lid), draft)
            return
        if action not in (
            "save",
            "person_confirm",
            "bulk_confirm",
            "attachment",
            "send_attachment",
            "send_materials",
            "draft_materials",
            "draft_material",
        ):
            self.db.session_clear(uid)
        if action == "draft_materials":
            await self.materials(msg, uid, **payload)
        elif action == "draft_material":
            await self.change_draft_material(msg, uid, **payload)
            self.db.token_delete(query.data)
        elif action == "back":
            await self.render(msg, uid, msg.back())
        elif action == "person_confirm":
            await self.person_confirm(msg, uid, **payload)
            self.db.token_delete(query.data)
        elif action == "bulk_confirm":
            added, waiting = self.db.add_prepared_members(uid, payload["gid"], payload["plan"])
            self.db.token_delete(query.data)
            self.db.session_clear(uid)
            msg.notice = f"✅ Добавлено: {added}. Ожидают входа: {waiting}."
            await self.render(msg, uid, msg.return_to("members", gid=payload["gid"]))
        elif action == "pending_cancel":
            self.db.cancel_pending(uid, **payload)
            msg.notice = "Ожидание отменено."
            await self.render(msg, uid, msg.return_to("pending", gid=payload["gid"]))
        elif action == "year_load":
            self.db.require_admin(uid)
            self.db.token_delete(query.data)
            await self.year_load(msg, uid)
        elif action == "claim":
            self.db.claim(uid, payload["gid"])
            self.db.token_delete(query.data)
            await self.group(msg, uid, payload["gid"])
        elif action == "save":
            hid = self.db.save_homework(uid, **payload, token=query.data)
            self.db.session_clear(uid)
            msg.notice = "✅ ДЗ сохранено."
            if any(
                route["action"] == "edit_files" and route["hid"] == hid for route in msg.history
            ):
                await self.render(msg, uid, msg.return_to("edit_files", hid=hid))
                return
            tasks = self.db.homework(uid, payload["lid"])
            page = next(i for i, task in enumerate(tasks) if task["id"] == hid)
            route = msg.return_to("lesson", lid=payload["lid"])
            await self.render(msg, uid, {**route, "page": page})
        elif action == "save_material":
            saved = self.db.save_subject_material(uid, **payload)
            msg.notice = "✅ Файл добавлен в материалы." if saved else "Файл уже есть в материалах."
            await self.edit_file(msg, uid, **payload)
        elif action == "delete_material":
            self.db.delete_subject_material(uid, **payload)
            self.db.token_delete(query.data)
            msg.notice = "✅ Файл удалён из материалов."
            await self.render(msg, uid, msg.return_to("materials", lid=payload["lid"], remove=True))
        elif action in ("attach_material", "detach_material"):
            page = payload.pop("page", 0)
            detach = action == "detach_material"
            self.db.set_subject_material_attachment(uid, **payload, detach=detach)
            msg.notice = "Файл откреплён от ДЗ." if detach else "✅ Файл прикреплён к ДЗ."
            await self.materials(msg, uid, hid=payload["hid"], page=page)
        elif action == "delete_file":
            hid, index, revision = payload["hid"], payload["index"], payload["revision"]
            homework, files = self.file_for_edit(uid, hid, index, revision)
            remaining = files[:index] + files[index + 1 :]
            if not remaining and not homework["body"].strip():
                self.db.delete_homework(uid, hid, revision)
                msg.notice = "✅ ДЗ удалено."
                await self.render(msg, uid, msg.return_to("lesson", lid=homework["lesson_id"]))
            else:
                self.db.save_homework(
                    uid,
                    homework["lesson_id"],
                    homework["body"],
                    hid=hid,
                    revision=revision,
                    attachments=remaining,
                )
                msg.notice = "✅ Файл удалён."
                await self.render(msg, uid, msg.return_to("edit_files", hid=hid))
        elif action == "send_attachment":
            hw = self.db.homework_item(uid, payload["hid"])
            files = homework_attachments(hw)
            if payload.get("revision", hw["revision"]) != hw["revision"] or not files:
                raise UserError("Вложения изменились. Откройте задание заново.")
            await self.send_files_to_chat(msg, files)
        elif action == "send_materials":
            lid, material_id = payload["lid"], payload.get("material_id")
            files = (
                self.db.subject_materials(uid, lid)
                if material_id is None
                else [self.db.subject_material(uid, lid, material_id)]
            )
            if not files:
                raise UserError("Материалов пока нет.")
            await self.send_files_to_chat(msg, files)
        elif action == "attachment":
            hw = self.db.homework_item(uid, payload["hid"])
            tasks = self.db.homework(uid, hw["lesson_id"])
            page = next(i for i, item in enumerate(tasks) if item["id"] == hw["id"])
            await self.lesson(msg, uid, hw["lesson_id"], page=page)
        elif action == "delete_confirm":
            hw = self.db.homework_item(uid, payload["hid"], "write")
            self.db.delete_homework(uid, **payload)
            self.db.token_delete(query.data)
            msg.notice = "✅ ДЗ удалено."
            await self.render(msg, uid, msg.return_to("lesson", lid=hw["lesson_id"]))
        else:
            await self.render(msg, uid, {"action": action, **payload})


def build_dispatcher(store, schedule, settings, refresher=None):
    ui = BotUI(store, schedule, settings, refresher)
    router = Router()
    middleware = PrivateAccess(store)
    router.message.outer_middleware(HomeworkAlbums(store))
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    router.message.register(ui.message)
    router.callback_query.register(ui.callback)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
