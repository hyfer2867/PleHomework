import asyncio
import logging
import re
import weakref
from datetime import date, datetime, timedelta
from html import escape, unescape
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .db import UserError
from .navigation import Screen
from .refresh import ScheduleRefresh
from .schedule import ScheduleError, clean_details

ROLES = {
    "superadmin": "",
    "head": "староста",
    "editor": "редактор ДЗ",
    "student": "студент",
}
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


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
        msg.route("home")
        self.db.session_clear(uid)
        member = self.db.membership(uid)
        rows = []
        if self.db.is_admin(uid):
            text = "<b>ДЗ · РЭУ</b>\nВыберите группу или назначьте старосту."
            rows.extend(
                [
                    [("📚 Группы", "groups", {}), ("👥 Старосты", "heads", {})],
                    [("➕ Назначить старосту", "person", {"operation": "appoint"})],
                ]
            )
            rows.append([("🗓 Загрузить до конца года", "year_confirm", {})])
        elif member:
            text = f"<b>ДЗ · {escape(member['name'])}</b>\nВаша роль: {ROLES[member['role']]}"
            rows.append([("📚 Моя группа", "group", {"gid": member["group_id"]})])
        else:
            text = (
                "<b>ДЗ · РЭУ</b>\nВы зарегистрированы. Отправьте старосте свой @username "
                f"или ID <code>{uid}</code>, чтобы он добавил вас в группу."
            )
        if self.db.can_catalog(uid):
            rows.append([("🎓 Выбрать группу · Форсайт", "catalog", {})])
        rows.append([("ℹ️ Помощь", "help", {})])
        await msg.answer(text, reply_markup=self.keyboard(uid, rows, home=False))

    async def group(self, msg, uid, gid):
        self.db.require(uid, gid)
        msg.route("group", gid=gid)
        group = self.db.group(gid)
        role = self.db.role(uid, gid)
        rows = [
            [
                ("Сегодня", "week", {"gid": gid, "scope": "today"}),
                ("Завтра", "week", {"gid": gid, "scope": "tomorrow"}),
            ],
            [
                ("📅 Расписание и ДЗ", "weeks", {"gid": gid}),
                ("📝 Только ДЗ", "week", {"gid": gid, "scope": "hw"}),
            ],
        ]
        if role in ("head", "superadmin"):
            rows.append([("👥 Участники и права", "members", {"gid": gid})])
        await msg.answer(
            f"<b>{escape(group['name'])}</b>\n{escape(group['course'])} · {escape(group['level'])}\n"
            + (f"Ваша роль: {ROLES[role]}.\n" if ROLES[role] else "")
            + "Выберите день или неделю, затем пару.",
            reply_markup=self.keyboard(uid, rows),
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
        rows.append([("➕ Подключить группу · Форсайт", "catalog", {})])
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

    async def weeks(self, msg, uid, gid, page=0):
        weeks = self.db.saved_weeks(uid, gid)
        page = max(0, min(page, max(0, (len(weeks) - 1) // 8)))
        msg.route("weeks", gid=gid, page=page)
        rows = []
        for week in weeks[page * 8 : page * 8 + 8]:
            start, end = date.fromisoformat(week["monday"]), date.fromisoformat(week["sunday"])
            count = len(week["lessons"])
            label = f"{start:%d.%m}–{end:%d.%m.%Y} · пар: {count}"
            rows.append([(label, "week", {"gid": gid, "monday": week["monday"]})])
        paging = []
        if page:
            paging.append(("←", "weeks", {"gid": gid, "page": page - 1}))
        if len(weeks) > (page + 1) * 8:
            paging.append(("→", "weeks", {"gid": gid, "page": page + 1}))
        if paging:
            rows.append(paging)
        rows.append([("Текущая неделя", "week", {"gid": gid})])
        await msg.answer(
            "<b>Сохранённые недели</b>\nВыберите период. Данные соответствуют последней загрузке с сайта."
            + ("\nПока нет сохранённых недель." if not weeks else ""),
            reply_markup=self.keyboard(uid, rows),
        )

    async def week(self, msg, uid, gid, monday=None, scope="week", page=0, day=None):
        self.db.require(uid, gid)
        today = self.today()
        target = today + timedelta(days=1) if scope == "tomorrow" else today
        if scope == "day":
            target = date.fromisoformat(day)
        if scope in ("today", "tomorrow"):
            scope, day = "day", target.isoformat()
        monday = date.fromisoformat(monday) if monday else target - timedelta(days=target.weekday())
        if monday.weekday() != 0 or abs((monday - today).days) > 370:
            raise UserError("Выберите неделю в пределах года от текущей даты.")
        sunday = monday + timedelta(days=6)
        if scope == "day" and not monday <= target <= sunday:
            raise UserError("День не относится к выбранной неделе.")
        saved = self.db.cache_get(f"week:{gid}:{monday.isoformat()}")
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
            rows.append(
                [
                    (
                        "← Неделя",
                        "week",
                        {"gid": gid, "monday": (monday - timedelta(days=7)).isoformat()},
                    ),
                    (
                        "Неделя →",
                        "week",
                        {"gid": gid, "monday": (monday + timedelta(days=7)).isoformat()},
                    ),
                ]
            )
            await msg.answer(
                f"<b>{escape(self.db.group(gid)['name'])}</b>\n{monday:%d.%m.%Y} — {sunday:%d.%m.%Y}\n{escape(notice)}\n\nВыберите день недели.",
                reply_markup=self.keyboard(uid, rows),
            )
            return
        if scope == "hw":
            tasks = [
                (lesson, task) for lesson in lessons for task in self.db.homework(uid, lesson["id"])
            ]
            page = max(0, min(page, max(0, (len(tasks) - 1) // 5)))
            msg.route("week", gid=gid, monday=monday.isoformat(), scope=scope, page=page)
            text = f"<b>Все ДЗ за неделю</b>\n{monday:%d.%m.%Y} — {sunday:%d.%m.%Y}\nВсего заданий: {len(tasks)}\n"
            rows = []
            for lesson, task in tasks[page * 5 : page * 5 + 5]:
                label = f"{date.fromisoformat(lesson['day']):%d.%m} · {lesson['slot']} пара · {lesson['subject'][:70]}"
                body = task["body"] or "Задание во вложении."
                text += (
                    "\n<b>"
                    + escape(label)
                    + "</b>"
                    + (" ⚠️ Пара изменилась" if not lesson["active"] else "")
                    + "\n"
                    + escape(body[:350])
                    + ("…" if len(body) > 350 else "")
                    + (" 📎" if task["file_id"] else "")
                    + "\n"
                )
                task_page = next(
                    i
                    for i, item in enumerate(self.db.homework(uid, lesson["id"]))
                    if item["id"] == task["id"]
                )
                rows.append(
                    [
                        (
                            (
                                "ДЗ"
                                + (f" #{task['id']}" if self.db.is_admin(uid) else "")
                                + f" · {label[:48]}"
                            ),
                            "lesson",
                            {"lid": lesson["id"], "page": task_page},
                        )
                    ]
                )
            if not tasks:
                text += "\nДЗ за эту неделю пока не добавлено."
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
            rows.append([("📅 Дни недели", "week", {"gid": gid, "monday": monday.isoformat()})])
            await msg.answer(text, reply_markup=self.keyboard(uid, rows))
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
        text = f"<b>{escape(self.db.group(gid)['name'])}</b>\n<b>{DAYS[target.weekday()]} · {target:%d.%m.%Y}</b>\n\n📝 — есть ДЗ\n▫️ — ДЗ не добавлено"
        if any(not item["active"] for item in lessons):
            text += "\n⚠️ — пара изменилась в расписании"
        rows = []
        for lesson in lessons[page * 8 : page * 8 + 8]:
            day = date.fromisoformat(lesson["day"])
            prefix = "⚠️" if not lesson["active"] else ("📝" if lesson["hw_count"] else "▫️")
            rows.append(
                [
                    (
                        f"{prefix} {day:%d.%m} · {lesson['slot']} · {lesson['subject'][:38]}",
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
        return (
            f"<b>{escape(lesson['subject'][:80])}</b>\n{day:%d.%m.%Y} · {lesson['slot']} пара · "
            f"{escape(lesson['start'])}–{escape(lesson['finish'])}\n{escape(' '.join(lesson['kind'].split())[:50])}\n{escape(clean_details(lesson['details'])[:100])}"
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

    async def show_file(self, msg, uid, text, rows, file_id, file_kind):
        source = None
        if file_kind == "photo":
            source = file_id
            cached = self.db.one(
                "SELECT document_id FROM document_cache WHERE source_id=?", (source,)
            )
            if cached:
                file_id = cached["document_id"]
            else:
                stream = await msg.bot.download(file_id)
                file_id = BufferedInputFile(stream.getvalue(), filename="homework-photo.jpg")
        result = await msg.answer(text, reply_markup=self.keyboard(uid, rows), document=file_id)
        if source and getattr(result, "document", None):
            self.db.conn.execute(
                "INSERT OR REPLACE INTO document_cache VALUES(?,?)",
                (source, result.document.file_id),
            )
        return result

    async def lesson(self, msg, uid, lid, page=0, text_page=0):
        lesson = self.db.lesson(uid, lid)
        tasks = self.db.homework(uid, lid)
        page = max(0, min(page, len(tasks) - 1))
        msg.route("lesson", lid=lid, page=page, text_page=text_page)
        writable = self.db.role(uid, lesson["group_id"]) in ("superadmin", "head", "editor")
        text = self.lesson_title(lesson)
        if not lesson["active"]:
            text += "\n⚠️ Пара исчезла или изменилась в расписании. ДЗ сохранено; старосте нужно проверить привязку."
        rows = []
        if tasks:
            hw = tasks[page]
            updated = datetime.fromtimestamp(hw["updated"], self.tz).strftime("%d.%m %H:%M")
            identifier = f" · #{hw['id']}" if self.db.is_admin(uid) else ""
            text += f"\n\n<b>ДЗ {page + 1} из {len(tasks)}{identifier}</b> · {updated}\n\n"
            # One shared header for text-only homework and attachments; reserve space
            # for Telegram's caption limit, page indicator and save confirmation.
            header_units = len(unescape(re.sub(r"<[^>]+>", "", text)).encode("utf-16-le")) // 2
            limit = max(1, min(600, 1024 - header_units - 100)) if hw["file_id"] else 2400
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
                            {"lid": lid, "page": page, "text_page": text_page - 1},
                        )
                    )
                if text_page + 1 < len(chunks):
                    controls.append(
                        (
                            "Текст →",
                            "lesson",
                            {"lid": lid, "page": page, "text_page": text_page + 1},
                        )
                    )
                rows.append(controls)
            if hw["file_id"]:
                rows.append([("📎 Прислать вложенный файл", "send_attachment", {"hid": hw["id"]})])
            if writable:
                edits = (
                    [("✏️ Изменить", "write", {"lid": lid, "hid": hw["id"]})]
                    if lesson["active"]
                    else []
                )
                if lesson["active"] and not hw["file_id"]:
                    edits.append(
                        (
                            "📎 Прикрепить файл",
                            "write",
                            {"lid": lid, "hid": hw["id"], "attach": True},
                        )
                    )
                rows.append(edits + [("🗑 Удалить", "delete", {"hid": hw["id"]})])
            pages = []
            if page:
                pages.append(("← ДЗ", "lesson", {"lid": lid, "page": page - 1}))
            if page + 1 < len(tasks):
                pages.append(("ДЗ →", "lesson", {"lid": lid, "page": page + 1}))
            if pages:
                rows.append(pages)
        else:
            text += "\n\nДЗ пока не добавлено."
        if writable and lesson["active"]:
            rows.append([("➕ Добавить ДЗ", "write", {"lid": lid})])
        if tasks and tasks[page]["file_id"]:
            await self.show_file(
                msg, uid, text, rows, tasks[page]["file_id"], tasks[page]["file_kind"]
            )
        else:
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
            text += f"\n{escape(user['name'])} · {escape(username)}\n<code>{user['id']}</code> · {ROLES[user['role']]}\n"
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
            f"Удалить из группы <b>{escape(person['name'])}</b>?\nID: <code>{target}</code>\nДоступ к группе будет закрыт."
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
            + "\nПривязки действуют 30 дней. Нажмите имя, чтобы отменить ожидание.\n"
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
            text += f"\n{escape(user['name'])} · <code>{user['id']}</code>\n{escape(user['group_name'] or 'Группа ещё не выбрана')}\n"
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
            f"Снять полномочия старосты у <b>{escape(person['name'])}</b>?\nID: <code>{target}</code>\n"
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
            (
                "Отправьте @username одногруппников списком: через пробел, запятую или с новой строки (до 100). Можно добавить людей до первого запуска бота. Либо отправьте один числовой ID."
                if operation == "add"
                else "Отправьте @username или числовой ID зарегистрированного пользователя."
            )
            + "\n\nКнопка «Назад» или /cancel — отменить.",
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

    async def write(self, msg, uid, lid, hid=None, attach=False):
        self.db.lesson(uid, lid, "write")
        hw = self.db.homework_item(uid, hid, "write") if hid else None
        if hw and hw["lesson_id"] != lid:
            raise UserError("Задание относится к другой паре.")
        if attach and hw is None:
            raise UserError("Сначала выберите существующее ДЗ.")
        msg.route("write", lid=lid, hid=hid, attach=attach)
        self.db.session_set(
            uid,
            {
                "state": "write",
                "lid": lid,
                "hid": hid,
                "revision": hw["revision"] if hw else None,
                "body": hw["body"] if hw else "",
                "file_id": hw["file_id"] if hw else None,
                "file_kind": hw["file_kind"] if hw else None,
                "attach": attach,
            },
        )
        await msg.answer(
            (
                "Пришлите файл или фото к этому заданию — текст сохранится.\n"
                if attach
                else "Отправьте текст и файл/фото с подписью одним сообщением или по отдельности. Я объединю их в одно ДЗ.\n"
            )
            + (
                "При редактировании первый новый текст заменяет старый, вложение сохраняется, если не прислано новое.\n"
                if hw and not attach
                else ""
            )
            + "До сохранения можно досылать текст — он дополнит черновик. Новое вложение заменит предыдущее.\n"
            + "Когда всё готово, нажмите «Сохранить ДЗ». До 3000 символов и один файл на задание. Альбомы не поддерживаются.\nКнопка «Назад» или /cancel — отменить.",
            reply_markup=self.keyboard(uid, []),
        )
        msg.bind_input()

    async def help(self, msg, uid):
        msg.route("help")
        member = self.db.membership(uid)
        if not self.db.is_admin(uid) and (not member or member["role"] != "head"):
            await msg.answer(
                "<b>Как найти ДЗ</b>\n\n"
                "Откройте «Моя группа» → «Расписание и ДЗ» → неделю → день → пару.\n\n"
                "📝 — есть ДЗ\n▫️ — ДЗ не добавлено\n\n"
                "«Сегодня» и «Завтра» открывают день сразу. «Только ДЗ» показывает задания на неделю.\n"
                "Переключайтесь стрелками, возвращайтесь кнопкой «Назад». Вложение можно получить через «Прислать вложенный файл».\n\n"
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

    async def message(self, msg: Message):
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
            while msg.history and msg.history[-1]["action"] in ("write", "person"):
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
            self.db.lesson(uid, state["lid"], "write")
            if msg.media_group_id:
                raise UserError("Отправьте один файл без альбома. /cancel — отменить.")
            file_kind = "photo" if msg.photo else ("document" if msg.document else None)
            file_id = (
                msg.photo[-1].file_id
                if msg.photo
                else (msg.document.file_id if msg.document else None)
            )
            incoming = (msg.text or msg.caption or "").strip()
            if not (incoming or file_id):
                raise UserError("Нужен текст, фото или документ.")
            previous = state.get("body", "")
            if incoming:
                body = (
                    (previous + "\n\n" + incoming).strip()
                    if previous and (state["state"] == "preview" or state.get("attach"))
                    else incoming
                )
            else:
                body = previous
            if len(body) > 3000:
                raise UserError(
                    "Общий текст ДЗ должен быть не длиннее 3000 символов. Черновик сохранён без последнего сообщения."
                )
            file_id = file_id or state.get("file_id")
            file_kind = file_kind or state.get("file_kind")
            self.db.session_clear(uid)
            payload = {
                **state,
                "state": "preview",
                "body": body,
                "file_id": file_id,
                "file_kind": file_kind,
            }
            self.db.session_set(uid, payload)
            # Token stores the full immutable draft; don't accidentally publish a newer draft from an older button.
            confirm = {
                k: payload[k] for k in ("lid", "hid", "revision", "body", "file_id", "file_kind")
            }
            lesson = self.db.lesson(uid, state["lid"])
            msg.route("preview")
            rows = [[("✅ Сохранить ДЗ", "save", confirm)]]
            if file_id:
                caption = (
                    "<b>Предпросмотр ДЗ</b>\n"
                    + escape(lesson["subject"][:80])
                    + "\n\n"
                    + "<blockquote>"
                    + escape(self.text_chunks(body or "Задание во вложении.", 600)[0])
                    + "</blockquote>"
                )
                if len(self.text_chunks(body or "", 600)) > 1:
                    caption += "\n… Полный текст сохранится и будет доступен по страницам."
                caption += "\n\nМожно дослать текст или файл, затем сохранить ДЗ."
                await self.show_file(msg, uid, caption, rows, file_id, file_kind)
            else:
                await msg.answer(
                    "<b>Предпросмотр ДЗ</b>\n"
                    + self.lesson_title(lesson)
                    + "\n\n"
                    + "<blockquote>"
                    + escape(body[:1000])
                    + "</blockquote>"
                    + ("\n… Полный текст будет сохранён." if len(body) > 1000 else "")
                    + "\n\nМожно дослать файл, фото или дополнительный текст. Затем нажмите «Сохранить ДЗ».",
                    reply_markup=self.keyboard(uid, rows),
                )
            msg.bind_input()
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
        await views[action](msg, uid, **payload)

    async def callback(self, query: CallbackQuery):
        await query.answer()
        uid = query.from_user.id
        payload = self.db.token_get(uid, query.data or "")
        action = payload.pop("action")
        msg = Screen(query.message, self.db, uid, message_id=query.message.message_id)
        if action not in (
            "save",
            "person_confirm",
            "bulk_confirm",
            "attachment",
            "send_attachment",
        ):
            self.db.session_clear(uid)
        if action == "back":
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
            # After claiming, the catalogue is no longer available to this head.
            msg.current, msg.history = None, []
            await self.group(msg, uid, payload["gid"])
        elif action == "save":
            hid = self.db.save_homework(uid, **payload, token=query.data)
            self.db.session_clear(uid)
            tasks = self.db.homework(uid, payload["lid"])
            page = next(i for i, task in enumerate(tasks) if task["id"] == hid)
            msg.notice = "✅ ДЗ сохранено."
            route = msg.return_to("lesson", lid=payload["lid"])
            await self.render(msg, uid, {**route, "page": page})
        elif action == "send_attachment":
            hw = self.db.homework_item(uid, payload["hid"])
            if not hw["file_id"]:
                raise UserError("У этого задания нет вложения.")
            file_id = hw["file_id"]
            if hw["file_kind"] == "photo":
                cached = self.db.one(
                    "SELECT document_id FROM document_cache WHERE source_id=?", (file_id,)
                )
                if cached:
                    file_id = cached["document_id"]
                else:
                    stream = await msg.bot.download(file_id)
                    file_id = BufferedInputFile(stream.getvalue(), filename="homework-photo.jpg")
            sent = await msg.answer_document(file_id)
            if hw["file_kind"] == "photo" and getattr(sent, "document", None):
                self.db.conn.execute(
                    "INSERT OR REPLACE INTO document_cache VALUES(?,?)",
                    (hw["file_id"], sent.document.file_id),
                )
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
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    router.message.register(ui.message)
    router.callback_query.register(ui.callback)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
