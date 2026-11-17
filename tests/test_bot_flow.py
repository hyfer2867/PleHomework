"""Real aiogram Dispatcher, intercepted Telegram transport: no messages sent externally."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendRichMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    Message,
    RichBlockDocument,
    RichMessage,
    Update,
    User,
)

from rea_bot.bot import build_dispatcher
from rea_bot.config import FORESIGHT, Settings
from rea_bot.db import Store
from rea_bot.schedule import parse_options, parse_week

FIXTURES = Path(__file__).parent / "fixtures"


class TelegramTransport(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []
        self.alerts = []
        self.messages = {}
        self.fail_edit = None
        self.deleted = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            if method.text:
                self.alerts.append(method)
            return True
        if isinstance(method, DeleteMessage):
            self.deleted.append(method.message_id)
            self.messages.pop((int(method.chat_id), method.message_id), None)
            return True
        if isinstance(method, EditMessageReplyMarkup):
            return True
        if isinstance(method, SendMediaGroup):
            self.sent.append(method)
            return [
                Message(
                    message_id=20000 + index,
                    date=datetime.now(UTC),
                    chat=Chat(id=int(method.chat_id), type="private"),
                    document=Document(
                        file_id=f"group-file-{index}", file_unique_id=f"group-{index}"
                    ),
                )
                for index, _ in enumerate(method.media)
            ]
        assert isinstance(
            method,
            (
                SendMessage,
                EditMessageText,
                EditMessageMedia,
                SendDocument,
                SendPhoto,
                SendRichMessage,
            ),
        )
        if isinstance(method, EditMessageText) and self.fail_edit:
            from aiogram.exceptions import TelegramBadRequest

            error, self.fail_edit = self.fail_edit, None
            raise TelegramBadRequest(method=method, message=error)
        self.sent.append(method)
        mid = (
            method.message_id
            if isinstance(method, (EditMessageText, EditMessageMedia))
            else 10000 + len(self.sent)
        )
        media = method.media if isinstance(method, EditMessageMedia) else None
        document_id = (
            media.media
            if media
            else (method.document if isinstance(method, SendDocument) else None)
        )
        if document_id is not None and not isinstance(document_id, str):
            document_id = f"uploaded-{mid}"
        rich = getattr(method, "rich_message", None)
        rich_result = None
        if rich is not None:
            rich_result = RichMessage(
                blocks=[
                    RichBlockDocument(
                        document=Document(file_id=f"rich-{index}", file_unique_id=f"rich-{index}")
                    )
                    for index, _ in enumerate(rich.media or [])
                ]
            )
        result = Message(
            message_id=mid,
            rich_message=rich_result,
            date=datetime.now(UTC),
            chat=Chat(id=int(method.chat_id), type="private"),
            from_user=User(id=bot.id, is_bot=True, first_name="Bot"),
            text=method.text if isinstance(method, (SendMessage, EditMessageText)) else None,
            caption=media.caption if media else getattr(method, "caption", None),
            document=Document(
                file_id=document_id, file_unique_id="unique", file_name="homework-photo.jpg"
            )
            if document_id
            else None,
            reply_markup=getattr(method, "reply_markup", None),
        )
        self.messages[(int(method.chat_id), mid)] = result
        return result

    async def stream_content(self, *args, **kwargs):
        yield b""


class SavedSchedule:
    def __init__(self, db):
        self.db = db

    async def navigator(self, course=None, level=None):
        name = "Group" if level else ("Type" if course else "Course")
        return {
            "options": parse_options((FIXTURES / f"{name.lower()}.html").read_text(), name),
            "stale": False,
            "fetched": 0,
            "next": name,
        }

    async def week(self, uid, gid, monday):
        self.db.require(uid, gid)
        week = parse_week((FIXTURES / "schedule.html").read_text())
        self.db.sync_lessons(uid, gid, week)
        return {**week, "stale": False, "fetched": datetime.now(UTC).timestamp()}


@pytest.fixture
async def app(monkeypatch):
    from rea_bot.bot import BotUI

    monkeypatch.setattr(BotUI, "today", lambda self: date(2026, 11, 5))
    db = Store(":memory:", {1})
    session = TelegramTransport()
    bot = Bot(
        "123456789:abcdefghijklmnopqrstuvwxyzABCDE",
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = build_dispatcher(db, SavedSchedule(db), Settings(admins=frozenset({1})))
    counter = 0

    async def send(
        uid,
        text=None,
        callback=None,
        chat_type="private",
        document=None,
        message_id=None,
        caption=None,
        photo=None,
        media_group_id=None,
    ):
        nonlocal counter
        counter += 1
        user = User(id=uid, is_bot=False, first_name=f"User {uid}", username=f"person{uid}")
        chat = Chat(id=uid if chat_type == "private" else -100, type=chat_type)
        msg = Message(
            message_id=counter,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
            document=document,
            caption=caption,
            photo=photo,
            media_group_id=media_group_id,
        )
        if callback:
            candidates = [
                m
                for (chat_id, _), m in session.messages.items()
                if chat_id == uid and (m.text or m.caption or m.rich_message)
            ]
            source = next(
                (
                    m
                    for m in reversed(candidates)
                    if m.reply_markup
                    and any(
                        b.callback_data == callback
                        for row in m.reply_markup.inline_keyboard
                        for b in row
                    )
                ),
                None,
            )
            if message_id:
                source = session.messages[(uid, message_id)]
            elif source is None and candidates:
                source = candidates[-1]
            msg = source or msg
            update = Update(
                update_id=counter,
                callback_query=CallbackQuery(
                    id=str(counter),
                    from_user=user,
                    chat_instance="instance",
                    message=msg,
                    data=callback,
                ),
            )
        else:
            update = Update(update_id=counter, message=msg)
        await dp.feed_update(bot, update)

    def click(label, uid):
        for (chat_id, _), message in reversed(session.messages.items()):
            if chat_id != uid or not message.reply_markup:
                continue
            for row in message.reply_markup.inline_keyboard:
                for button in row:
                    if label in button.text:
                        return button.callback_data
        raise AssertionError(f"Button not found: {label}")

    yield db, session, send, click
    await bot.session.close()
    db.close()


async def test_end_to_end_roles_homework_and_attachments(app):
    db, transport, send, click = app
    for uid in (1, 2, 3, 4):
        await send(uid, "/start")
    await send(1, callback=click("Старосты", 1))
    await send(1, callback=click("Назначить", 1))
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("Подтвердить", 1))
    assert db.user(2)["head_allowed"] == 1
    await send(2, "/start")
    await send(2, callback=click("Выбрать свою группу", 2))
    await send(2, callback=click("1-й курс", 2))
    await send(2, callback=click("Бакалавр", 2))
    await send(2, callback=click("15.22Д-ПМ01/26б", 2))
    await send(2, callback=click("Да, моя группа", 2))
    assert db.membership(2)["role"] == "head"
    db.sync_lessons(
        2, db.membership(2)["group_id"], parse_week((FIXTURES / "schedule.html").read_text())
    )
    await send(2, callback=click("Участники и права", 2))
    await send(2, callback=click("Добавить", 2))
    await send(2, "@person3")
    await send(2, callback=click("Подтвердить", 2))
    assert db.membership(3)["role"] == "student"
    await send(2, callback=click("Выдать права ДЗ", 2))
    await send(2, callback=click("User 3", 2))
    await send(2, callback=click("Подтвердить", 2))
    assert db.membership(3)["role"] == "editor"
    await send(3, "/start")
    await send(3, callback=click("Выбрать неделю", 3))
    await send(3, callback=click("Текущая неделя", 3))
    await send(3, callback=click("Пн (02.11)", 3))
    await send(3, callback=click("3 пара ·", 3))
    await send(3, callback=click("Добавить ДЗ", 3))
    await send(
        3,
        document={"file_id": "test-document", "file_unique_id": "unique", "file_name": "task.pdf"},
    )
    confirmation = click("Сохранить ДЗ", 3)
    await send(3, callback=confirmation)
    assert db.one("SELECT count(*) AS n FROM homework")["n"] == 1
    await send(3, callback=confirmation)
    assert db.one("SELECT count(*) AS n FROM homework")["n"] == 1
    hid = db.one("SELECT id FROM homework")["id"]
    attachment = db.token(3, "attachment", hid=hid)
    await send(3, callback=attachment)
    assert transport.sent[-1].rich_message is not None
    assert "Открыть вложение" not in str(transport.sent[-1].reply_markup)
    # Forwarded/stolen callback cannot be used by another student.
    await send(4, callback=attachment)
    assert "Кнопка устарела" in transport.alerts[-1].text
    # A registered outsider with a token issued for their ID still fails DB authorization.
    hid = db.one("SELECT id FROM homework")["id"]
    await send(4, callback=db.token(4, "attachment", hid=hid))
    assert "Нет доступа" in transport.alerts[-1].text
    # Existing button stops working after access is revoked.
    file_button = attachment
    db.set_member(2, 1, 3, "remove")
    await send(3, callback=file_button)
    assert "Нет доступа" in transport.alerts[-1].text


async def test_cancel_invalidates_preview_and_group_chat_is_ignored(app):
    db, transport, send, click = app
    for uid in (1, 2):
        await send(uid, "/start")
    gid = db.catalog_group(1, "G", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.appoint_head(1, 2)
    db.claim(2, gid)
    db.sync_lessons(2, gid, parse_week((FIXTURES / "schedule.html").read_text()))
    lid = db.one("SELECT id FROM lessons")["id"]
    await send(2, callback=db.token(2, "write", lid=lid))
    await send(2, "Solve <equation> & explain")
    token = click("Сохранить ДЗ", 2)
    await send(2, "/cancel")
    await send(2, callback=token)
    assert db.one("SELECT count(*) AS n FROM homework")["n"] == 0
    assert "Кнопка устарела" in transport.alerts[-1].text
    before = len(transport.sent)
    await send(9, "/start", chat_type="group")
    assert len(transport.sent) == before
    assert db.user(9) is None


async def test_demotion_between_draft_and_save(app):
    db, transport, send, click = app
    for uid in (1, 2, 3):
        await send(uid, "/start")
    gid = db.catalog_group(1, "G", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.appoint_head(1, 2)
    db.claim(2, gid)
    db.set_member(2, gid, 3, "add")
    db.set_member(2, gid, 3, "editor")
    db.sync_lessons(2, gid, parse_week((FIXTURES / "schedule.html").read_text()))
    lid = db.one("SELECT id FROM lessons")["id"]
    await send(3, callback=db.token(3, "write", lid=lid))
    await send(3, "draft")
    token = click("Сохранить ДЗ", 3)
    db.set_member(2, gid, 3, "student")
    await send(3, callback=token)
    assert "Нет доступа" in transport.alerts[-1].text
    assert db.one("SELECT count(*) AS n FROM homework")["n"] == 0


def screen_text(method):
    rich = getattr(method, "rich_message", None)
    if rich is not None:
        return rich.html.replace("<br>", "\n")
    return getattr(method, "text", None) or getattr(method, "caption", None) or method.media.caption


def screen_media(method):
    rich = getattr(method, "rich_message", None)
    if rich is not None:
        return [item.media.media for item in rich.media]
    return [method.media.media]


def current_screen(db, transport, uid):
    method = next(
        m
        for m in reversed(transport.sent)
        if m.chat_id == uid
        and isinstance(
            m, (SendMessage, SendRichMessage, EditMessageText, EditMessageMedia, SendDocument)
        )
    )
    if isinstance(method, (EditMessageText, EditMessageMedia)):
        mid = method.message_id
    else:
        mid = next(
            mid
            for (chat, mid), msg in reversed(transport.messages.items())
            if chat == uid and msg.reply_markup == method.reply_markup
        )
    return mid, db.screen_get(uid, mid)


async def prepare_group(app):
    db, _transport, send, _click = app
    await send(1, "/start")
    await send(2, "/start")
    gid = db.catalog_group(1, "G", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.appoint_head(1, 2)
    db.claim(2, gid)
    db.sync_lessons(2, gid, parse_week((FIXTURES / "schedule.html").read_text()))
    await send(2, callback=db.token(2, "home"))
    return gid


async def test_catalog_back_edits_same_message(app):
    db, transport, send, click = app
    await send(1, "/start")
    mid, _ = current_screen(db, transport, 1)
    await send(1, callback=click("Подключить группу", 1))
    await send(1, callback=click("1-й курс", 1))
    await send(1, callback=click("Бакалавр", 1))
    assert current_screen(db, transport, 1)[1]["current"]["level"] == "Бакалавр"
    await send(1, callback=click("⬅️ Назад", 1))
    assert current_screen(db, transport, 1)[1]["current"]["course"] == "1-й курс"
    assert current_screen(db, transport, 1)[1]["current"]["level"] is None
    await send(1, callback=click("⬅️ Назад", 1))
    assert current_screen(db, transport, 1)[1]["current"]["course"] is None
    await send(1, callback=click("⬅️ Назад", 1))
    assert current_screen(db, transport, 1)[1]["current"] == {"action": "home"}
    assert len([m for m in transport.sent if isinstance(m, (SendMessage, SendRichMessage))]) == 1
    assert all(m.message_id == mid for m in transport.sent if isinstance(m, EditMessageText))


async def test_back_preserves_selected_day_and_week(app):
    db, transport, send, click = app
    await prepare_group(app)
    await send(2, callback=click("Выбрать неделю", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Пт (06.11)", 2))
    mid, before = current_screen(db, transport, 2)
    await send(2, callback=click("3 пара ·", 2))
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2) == (mid, before)
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["scope"] == "week"
    assert (
        len(
            [
                m
                for m in transport.sent
                if m.chat_id == 2 and isinstance(m, (SendMessage, SendRichMessage))
            ]
        )
        == 1
    )


async def test_drafts_save_delete_and_homework_pages_use_one_card(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.save_homework(2, lid, "First task")
    db.save_homework(2, lid, "Second task")
    await send(2, callback=click("Выбрать неделю", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Пн (02.11)", 2))
    await send(2, callback=click("3 пара ·", 2))
    assert "First task" in screen_text(transport.sent[-1])
    await send(2, callback=click("ДЗ →", 2))
    assert "Second task" in screen_text(transport.sent[-1])
    assert "First task" not in screen_text(transport.sent[-1])
    mid, _ = current_screen(db, transport, 2)
    await send(2, callback=click("Добавить ДЗ", 2))
    await send(2, "Third task <x> & y")
    assert transport.sent[-1].message_id == mid
    old_confirmation = click("Сохранить ДЗ", 2)
    await send(2, callback=click("⬅️ Назад", 2))
    assert db.session_get(2)["state"] == "write"
    await send(2, callback=old_confirmation)
    assert "Кнопка устарела" in transport.alerts[-1].text
    await send(2, "Third task <x> & y")
    await send(2, callback=click("Сохранить ДЗ", 2))
    assert "✅ ДЗ сохранено" in screen_text(transport.sent[-1])
    assert "Third task &lt;x&gt; &amp; y" in screen_text(transport.sent[-1])
    assert current_screen(db, transport, 2)[1]["current"]["page"] == 2
    assert "Удалить" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("Редактировать", 2))
    await send(2, callback=click("🗑 Удалить ДЗ", 2))
    await send(2, callback=click("⬅️ Назад", 2))
    assert len(db.homework(2, lid)) == 3
    await send(2, callback=click("🗑 Удалить ДЗ", 2))
    await send(2, callback=click("Да, удалить", 2))
    assert "✅ ДЗ удалено" in screen_text(transport.sent[-1])
    assert "Second task" in screen_text(transport.sent[-1])
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["action"] == "week"
    assert (
        len(
            [
                m
                for m in transport.sent
                if m.chat_id == 2 and isinstance(m, (SendMessage, SendRichMessage))
            ]
        )
        == 1
    )


async def test_edit_fallback_only_when_message_unavailable(app):
    db, transport, send, click = app
    await send(1, "/start")
    original, _ = current_screen(db, transport, 1)
    transport.fail_edit = "Bad Request: message to edit not found"
    await send(1, callback=click("Помощь", 1))
    replacement, state = current_screen(db, transport, 1)
    assert replacement != original
    assert state["current"]["action"] == "help"
    await send(1, callback=click("⬅️ Назад", 1))
    assert transport.sent[-1].message_id == replacement
    count = len(transport.sent)
    transport.fail_edit = "Bad Request: message is not modified"
    await send(1, callback=click("Помощь", 1))
    assert len(transport.sent) == count
    # An unrelated API error must not be hidden by creating a duplicate message.
    transport.fail_edit = "Bad Request: can't parse entities"
    await send(1, callback=click("Помощь", 1))
    assert len(transport.sent) == count
    assert "Не удалось" in transport.alerts[-1].text


async def test_back_rechecks_access_and_keeps_screen_on_denial(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    await send(3, "/start")
    await send(3, callback=click("Сегодня", 3))
    mid, before = current_screen(db, transport, 3)
    db.set_member(2, gid, 3, "remove")
    count = len(transport.sent)
    await send(3, callback=click("⬅️ Назад", 3))
    assert len(transport.sent) == count
    assert "Нет доступа" in transport.alerts[-1].text
    assert db.screen_get(3, mid) == before


async def test_each_menu_keeps_its_own_history(app):
    db, transport, send, click = app
    await send(1, "/start")
    first, _ = current_screen(db, transport, 1)
    await send(1, callback=click("Подключить группу", 1))
    await send(1, "/start")
    second, _ = current_screen(db, transport, 1)
    await send(1, callback=click("Помощь", 1))
    await send(1, callback=db.token(1, "back"), message_id=first)
    assert transport.sent[-1].message_id == first
    assert db.screen_get(1, first)["current"]["action"] == "home"
    assert db.screen_get(1, second)["current"]["action"] == "help"


async def test_successful_appointment_does_not_return_to_completed_input(app):
    db, transport, send, click = app
    await send(1, "/start")
    await send(2, "/start")
    await send(1, callback=click("Старосты", 1))
    await send(1, callback=click("Назначить", 1))
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("Подтвердить", 1))
    assert "✅ Права обновлены" in screen_text(transport.sent[-1])
    await send(1, callback=click("⬅️ Назад", 1))
    assert current_screen(db, transport, 1)[1]["current"] == {"action": "home"}
    assert db.session_get(1) is None


async def test_saved_week_picker_preserves_page_and_group_access(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    from datetime import timedelta

    monday = date(2026, 11, 2)
    for i in range(9):
        start = monday + timedelta(days=7 * i)
        db.cache_put(
            f"week:{gid}:{start}",
            {"monday": str(start), "sunday": str(start + timedelta(days=6)), "lessons": []},
        )
    await send(2, callback=click("Выбрать неделю", 2))
    assert "Выберите неделю" in screen_text(transport.sent[-1])
    rows = transport.sent[-1].reply_markup.inline_keyboard
    assert [row[0].text for row in rows[:2]] == [
        "Текущая неделя · 02.11–08.11",
        "Следующая неделя · 09.11–15.11",
    ]
    await send(2, callback=click("→", 2))
    assert current_screen(db, transport, 2)[1]["current"]["page"] == 1
    await send(2, callback=click("28.12", 2))
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"] == {
        "action": "weeks",
        "gid": gid,
        "page": 1,
    }
    db.register(3, "person3", "Outsider")
    await send(3, "/start")
    await send(3, callback=db.token(3, "weeks", gid=gid))
    assert "Нет доступа" in transport.alerts[-1].text


async def test_past_weeks_show_saved_dates_newest_first(app):
    from datetime import timedelta

    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(2, callback=click("Выбрать неделю", 2))
    await send(2, callback=click("Прошедшие недели", 2))
    assert "Пока нет сохранённых прошедших недель" in screen_text(transport.sent[-1])
    for monday in (date(2024, 10, 28), date(2026, 10, 26), date(2026, 11, 2)):
        db.cache_put(
            f"week:{gid}:{monday}",
            {"monday": str(monday), "sunday": str(monday + timedelta(days=6)), "lessons": []},
        )
    await send(2, callback=click("Назад", 2))
    await send(2, callback=click("Прошедшие недели", 2))
    rows = transport.sent[-1].reply_markup.inline_keyboard
    assert [row[0].text for row in rows[:2]] == [
        "26.10–01.11.2026 · пар: 0",
        "28.10–03.11.2024 · пар: 0",
    ]
    assert "02.11–08.11" not in str(rows)
    await send(2, callback=click("28.10–03.11.2024", 2))
    assert "28.10.2024 — 03.11.2024" in screen_text(transport.sent[-1])
    await send(2, callback=click("Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["past"] is True


async def test_weekly_homework_shows_all_days_and_excludes_deleted(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    lessons = db.all("SELECT * FROM lessons WHERE group_id=? ORDER BY day,slot", (gid,))
    first = lessons[0]["id"]
    last = lessons[-1]["id"]
    db.save_homework(2, first, "Monday task")
    db.save_homework(2, first, "Another task &lt;пример&gt;")
    db.save_homework(2, last, "Saturday task")
    removed = db.save_homework(2, first, "Deleted task")
    db.delete_homework(2, removed, 1)
    await send(2, callback=click("Все ДЗ за неделю", 2))
    assert "Monday task" in transport.sent[-1].rich_message.html
    await send(2, callback=click("Неделя →", 2))
    assert "09.11.2026 — 15.11.2026" in transport.sent[-1].rich_message.html
    assert current_screen(db, transport, 2)[1]["current"]["scope"] == "hw"
    await send(2, callback=click("← Неделя", 2))
    assert "Monday task" in transport.sent[-1].rich_message.html
    await send(2, callback=click("Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["action"] == "group"
    await send(2, callback=click("Выбрать неделю", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Все ДЗ за неделю", 2))
    weekly = transport.sent[-1].rich_message.html
    assert "<h3>Пн · 02.11</h3>" in weekly
    assert "<blockquote>Monday task</blockquote>" in weekly
    assert weekly.count("<h3>Пн · 02.11</h3>") == 1
    first_task = weekly.index("<blockquote>Monday task</blockquote>")
    second_task = weekly.index("<blockquote>Another task")
    assert "<p><b>📘 " in weekly[first_task:second_task]
    assert "· · ·" not in weekly
    assert " пара</b>" in weekly
    assert "<hr>" not in weekly[first_task:second_task]
    assert weekly.count("<hr>") == 2
    assert "&amp;lt;пример&amp;gt;" in weekly
    assert "Saturday task" in weekly
    assert "Deleted task" not in weekly
    await send(2, callback=click("Дни недели", 2))
    await send(2, callback=click("Ср (04.11)", 2))
    assert "ещё не загружено" in screen_text(transport.sent[-1])


async def test_pre_registration_bulk_flow_and_start_assigns_group(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(2, callback=click("Участники и права", 2))
    await send(2, callback=click("Добавить", 2))
    await send(2, "@person3\n@person4")
    await send(2, callback=click("Подтвердить", 2))
    assert len(db.pending_members(2, gid)) == 2
    await send(2, callback=click("Ожидают входа", 2))
    await send(2, callback=click("Отменить @person4", 2))
    await send(3, "/start")
    assert db.membership(3)["group_id"] == gid
    assert "Ваша роль" not in screen_text(transport.sent[-1])
    rows = transport.sent[-1].reply_markup.inline_keyboard
    assert [[button.text for button in row] for row in rows] == [
        ["Сегодня", "Завтра"],
        ["📅 Выбрать неделю"],
        ["📝 Все ДЗ за неделю"],
        ["ℹ️ Помощь"],
    ]
    await send(4, "/start")
    assert db.membership(4) is None


async def test_remove_inactive_member_without_username_or_start(app):
    db, _transport, send, click = app
    gid = await prepare_group(app)
    db.register(3, None, "Inactive Student")
    db.set_member(2, gid, 3, "add")
    db.conn.execute("UPDATE users SET seen=0 WHERE id=3")
    await send(2, callback=click("Участники и права", 2))
    await send(2, callback=click("Удалить из группы", 2))
    await send(2, callback=click("Inactive Student", 2))
    assert db.membership(3) is not None
    await send(2, callback=click("⬅️ Назад", 2))
    assert db.membership(3) is not None
    await send(2, callback=click("Inactive Student", 2))
    await send(2, callback=click("Подтвердить", 2))
    assert db.membership(3) is None


async def test_owner_can_remove_head_from_picker(app):
    db, _transport, send, click = app
    gid = await prepare_group(app)
    db.add_prepared_members(2, gid, db.prepare_members(2, gid, ["future_student"]))
    await send(1, callback=db.token(1, "members", gid=gid))
    await send(1, callback=click("Удалить из группы", 1))
    await send(1, callback=click("староста", 1))
    assert db.membership(2) is not None
    await send(1, callback=click("Подтвердить", 1))
    assert db.membership(2) is None
    assert not db.user(2)["head_allowed"]
    assert not db.pending_members(1, gid)


async def test_revoke_head_picker_without_recent_start(app):
    db, _transport, send, click = app
    gid = await prepare_group(app)
    db.conn.execute("UPDATE users SET seen=0 WHERE id=2")
    await send(1, callback=click("Старосты", 1))
    await send(1, callback=click("Снять полномочия", 1))
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("⬅️ Назад", 1))
    assert db.membership(2)["role"] == "head"
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("Подтвердить", 1))
    assert db.membership(2)["group_id"] == gid
    assert db.membership(2)["role"] == "student"
    assert not db.user(2)["head_allowed"]


async def test_rights_pickers_and_empty_notice(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(2, callback=click("Участники и права", 2))
    await send(2, callback=click("Снять права ДЗ", 2))
    assert "Редакторов ДЗ пока нет" in screen_text(transport.sent[-1])
    assert "<blockquote>" in screen_text(transport.sent[-1])
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    db.conn.execute("UPDATE users SET seen=0 WHERE id=3")
    await send(2, callback=db.token(2, "person", operation="editor", gid=gid))
    await send(2, callback=click("Student", 2))
    await send(2, callback=click("Подтвердить", 2))
    assert db.membership(3)["role"] == "editor"
    await send(2, callback=click("Снять права ДЗ", 2))
    await send(2, callback=click("Student", 2))
    await send(2, callback=click("Подтвердить", 2))
    assert db.membership(3)["role"] == "student"
    await send(3, "/start")
    await send(3, callback=db.token(3, "pick_person", operation="editor", gid=gid))
    assert "Нет доступа" in transport.alerts[-1].text


async def test_viewing_any_week_makes_no_schedule_request(app):
    from unittest.mock import AsyncMock

    db, transport, send, click = app
    gid = await prepare_group(app)
    # Navigation must never call SavedSchedule.week (including cache misses and refresh).
    original = SavedSchedule.week
    SavedSchedule.week = AsyncMock(side_effect=AssertionError("Unexpected network request"))
    try:
        await send(2, callback=db.token(2, "week", gid=gid, monday="2026-11-02"))
        await send(2, callback=click("Пн (02.11)", 2))
        assert "Показать сохранённое" not in str(transport.sent[-1].reply_markup)
        assert "К группе" not in str(transport.sent[-1].reply_markup)
        await send(2, callback=db.token(2, "week", gid=gid, monday="2026-12-28"))
        assert "ещё не загружено" in screen_text(transport.sent[-1])
        SavedSchedule.week.assert_not_awaited()
        # Only owner sees and can open the bulk-load confirmation.
        await send(2, callback=db.token(2, "year_confirm"))
        assert "Недостаточно прав" in transport.alerts[-1].text
        await send(1, callback=db.token(1, "year_confirm"))
        assert "Загрузить расписание" in screen_text(transport.sent[-1])
        SavedSchedule.week.assert_not_awaited()
    finally:
        SavedSchedule.week = original


async def test_owner_year_load_requires_confirmation_and_is_one_time(app, monkeypatch):
    from unittest.mock import AsyncMock

    db, transport, send, click = app
    await prepare_group(app)
    load = AsyncMock(return_value={"stale": False})
    monkeypatch.setattr(SavedSchedule, "week", load)
    await send(1, callback=db.token(1, "year_confirm"))
    load.assert_not_awaited()
    confirm = click("Подтвердить загрузку", 1)
    await send(1, callback=click("Главное меню", 1))
    await send(1, callback=confirm)
    load.assert_not_awaited()
    await send(1, callback=db.token(1, "year_confirm"))
    confirm = click("Подтвердить загрузку", 1)
    await send(1, callback=confirm)
    assert load.await_count == 9
    assert all(call.kwargs["force"] for call in load.await_args_list)
    assert "✅ Расписание загружено" in screen_text(transport.sent[-1])
    await send(1, callback=confirm)
    assert load.await_count == 9


async def test_day_navigation_crosses_week_boundary_and_no_duplicate_button(app):
    db, transport, send, click = app
    await prepare_group(app)
    assert str(transport.sent[-1].reply_markup).count("Выбрать неделю") == 1
    await send(2, callback=click("Сегодня", 2))
    assert "05.11.2026" in screen_text(transport.sent[-1])
    assert "Неделя →" not in str(transport.sent[-1].reply_markup)
    for _ in range(4):
        await send(2, callback=click("День →", 2))
    route = current_screen(db, transport, 2)[1]["current"]
    assert route["day"] == "2026-11-09"
    assert route["monday"] == "2026-11-09"
    await send(2, callback=click("← День", 2))
    route = current_screen(db, transport, 2)[1]["current"]
    assert route["day"] == "2026-11-08"
    assert route["monday"] == "2026-11-02"
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["action"] == "group"
    await send(2, callback=click("Завтра", 2))
    assert current_screen(db, transport, 2)[1]["current"]["day"] == "2026-11-06"


async def test_document_is_part_of_navigation_and_back_replaces_media(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.save_homework(2, lid, "Task with first file", "first-doc", "document")
    db.save_homework(2, lid, "Task with second file", "second-doc", "document")
    mid, _ = current_screen(db, transport, 2)
    await send(2, callback=db.token(2, "lesson", lid=lid))
    assert transport.sent[-1].rich_message is not None
    assert transport.sent[-1].message_id == mid
    assert screen_media(transport.sent[-1])[0] == "first-doc"
    assert "Task with first file" in screen_text(transport.sent[-1])
    await send(2, callback=click("ДЗ →", 2))
    assert screen_media(transport.sent[-1])[0] == "second-doc"
    assert transport.sent[-1].message_id == mid
    assert "Открыть вложение" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("⬅️ Назад", 2))
    assert mid not in transport.deleted
    assert isinstance(transport.sent[-1], EditMessageText)
    new_mid, _ = current_screen(db, transport, 2)
    assert new_mid == mid
    assert (2, mid) in transport.messages
    await send(2, callback=click("Сегодня", 2))
    assert transport.sent[-1].message_id == new_mid


async def test_single_photo_is_below_homework_and_sent_as_collage(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.save_homework(2, lid, "Photo homework", "source-photo", "photo")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    rich = transport.sent[-1].rich_message
    assert rich.html.index("Photo homework") < rich.html.index("<tg-collage>")
    assert rich.media[0].media.type == "photo"
    assert rich.media[0].media.media == "source-photo"
    await send(2, callback=click("Прислать файлы в чат", 2))
    sent = transport.sent[-1]
    assert isinstance(sent, SendRichMessage)
    assert "<tg-collage>" in sent.rich_message.html
    assert sent.rich_message.media[0].media.media == "source-photo"


async def test_long_attachment_caption_is_paginated_without_losing_text(app):
    from bs4 import BeautifulSoup

    from rea_bot.bot import BotUI

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    body = "😀<&>" * 700
    db.save_homework(2, lid, body, "doc", "document")
    chunks = BotUI.text_chunks(body)
    assert "".join(chunks) == body
    assert all(len(part.encode("utf-16-le")) // 2 <= 600 for part in chunks)
    await send(2, callback=db.token(2, "lesson", lid=lid))
    mid = transport.sent[-1].message_id
    for index in range(len(chunks)):
        method = transport.sent[-1]
        caption = BeautifulSoup(screen_text(method), "html.parser").get_text()
        assert len(caption.encode("utf-16-le")) // 2 <= 1024
        assert chunks[index] in caption
        assert method.message_id == mid
        if index + 1 < len(chunks):
            await send(2, callback=click("Текст →", 2))


async def test_document_with_caption_is_one_homework(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    await send(2, callback=db.token(2, "write", lid=lid))
    await send(
        2,
        document={"file_id": "task-file", "file_unique_id": "unique"},
        caption="Решить задачи из файла <1–5>",
    )
    await send(2, callback=click("Сохранить ДЗ", 2))
    tasks = db.homework(2, lid)
    assert len(tasks) == 1
    assert tasks[0]["body"] == "Решить задачи из файла <1–5>"
    assert tasks[0]["file_id"] == "task-file"
    assert "<blockquote>" in screen_text(transport.sent[-1])


async def test_separate_text_and_file_accumulate_before_save(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    await send(2, callback=db.token(2, "write", lid=lid))
    await send(2, "Выполнить задания")
    first_confirm = click("Сохранить ДЗ", 2)
    await send(2, document={"file_id": "task-file", "file_unique_id": "unique"})
    await send(2, "Только нечётные номера")
    await send(2, callback=first_confirm)
    assert "Кнопка устарела" in transport.alerts[-1].text
    assert not db.homework(2, lid)
    await send(2, callback=click("Сохранить ДЗ", 2))
    tasks = db.homework(2, lid)
    assert len(tasks) == 1
    assert tasks[0]["body"] == "Выполнить задания\n\nТолько нечётные номера"
    assert tasks[0]["file_id"] == "task-file"


async def test_attach_file_to_existing_homework_preserves_body_and_id(app):
    db, _transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Существующее описание")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    await send(2, callback=click("Редактировать", 2))
    await send(2, callback=click("Редактировать файлы", 2))
    await send(2, callback=click("Добавить файл", 2))
    await send(
        2, document={"file_id": "added-file", "file_unique_id": "unique"}, caption="Дополнение"
    )
    await send(2, callback=click("Сохранить ДЗ", 2))
    tasks = db.homework(2, lid)
    assert len(tasks) == 1
    assert tasks[0]["id"] == hid
    assert tasks[0]["body"] == "Существующее описание\n\nДополнение"
    assert tasks[0]["file_id"] == "added-file"
    assert tasks[0]["revision"] == 2


@pytest.mark.parametrize("replace", [False, True], ids=["append", "replace"])
async def test_edit_description_previews_changes_and_keeps_files(app, replace):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Старое описание", "old-file", "document")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    assert "Редактировать файлы" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("Редактировать", 2))
    text_actions = transport.sent[-1].reply_markup.inline_keyboard[0]
    assert [button.text for button in text_actions] == ["➕ Дописать текст", "🔄 Заменить текст"]
    mode = "Заменить текст" if replace else "Дописать текст"
    await send(2, callback=click(mode, 2))
    await send(2, callback=click("Назад", 2))
    assert "Редактировать ДЗ" in screen_text(transport.sent[-1])
    await send(2, callback=click(mode, 2))
    prompt = transport.sent[-1]
    assert "Предпросмотр ДЗ · файлов: 1" in screen_text(prompt)
    assert screen_media(prompt)[0] == "old-file"
    assert ("<blockquote>Старое описание</blockquote>" in screen_text(prompt)) is not replace
    assert "Сохранить ДЗ" not in str(prompt.reply_markup)
    document = {"file_id": "new-file", "file_unique_id": "new", "file_name": "Задачи.pdf"}
    if replace:
        await send(2, document=document)
        pending = transport.sent[-1]
        assert "Старое описание" not in pending.rich_message.html
        assert "Сохранить ДЗ" not in str(pending.reply_markup)
        assert [item.media.media for item in pending.rich_message.media] == ["old-file", "new-file"]
        await send(2, "Новый текст")
    else:
        await send(2, document=document, caption="Новый текст")

    expected = "Новый текст" if replace else "Старое описание\n\nНовый текст"
    expected_html = expected.replace("\n", "<br>")
    preview = transport.sent[-1]
    assert preview.message_id == prompt.message_id
    assert "Предпросмотр ДЗ · файлов: 2" in preview.rich_message.html
    assert f"<blockquote>{expected_html}</blockquote>" in preview.rich_message.html
    assert [item.media.media for item in preview.rich_message.media] == ["old-file", "new-file"]
    assert db.homework_item(2, hid)["body"] == "Старое описание"

    await send(2, callback=click("Сохранить ДЗ", 2))
    tasks = db.homework(2, lid)
    assert [(task["id"], task["body"]) for task in tasks] == [(hid, expected)]
    assert [file["file_id"] for file in homework_attachments(tasks[0])] == [
        "old-file",
        "new-file",
    ]


async def test_photo_then_description_is_one_homework(app, monkeypatch):
    from io import BytesIO
    from unittest.mock import AsyncMock

    db, transport, send, click = app
    await prepare_group(app)
    monkeypatch.setattr(Bot, "download", AsyncMock(return_value=BytesIO(b"photo-bytes")))
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    await send(2, callback=db.token(2, "write", lid=lid))
    await send(
        2,
        photo=[
            {"file_id": "photo-source", "file_unique_id": "unique", "width": 100, "height": 100}
        ],
    )
    await send(2, "Описание к фотографии")
    await send(2, callback=click("Сохранить ДЗ", 2))
    tasks = db.homework(2, lid)
    assert len(tasks) == 1
    assert tasks[0]["body"] == "Описание к фотографии"
    assert tasks[0]["file_kind"] == "photo"
    assert "<blockquote>Описание к фотографии</blockquote>" in transport.sent[-1].rich_message.html


async def test_homework_ids_only_visible_to_owner_everywhere(app):
    db, transport, send, _click = app
    gid = await prepare_group(app)
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Task text", "doc", "document")
    for uid in (1, 2, 3):
        await send(uid, callback=db.token(uid, "lesson", lid=lid))
        caption = screen_text(transport.sent[-1])
        assert (f"#{hid}" in caption) == (uid == 1)
        await send(uid, callback=db.token(uid, "week", gid=gid, monday="2026-11-02", scope="hw"))
        assert f"#{hid}" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=db.token(2, "delete", hid=hid))
    assert f"#{hid}" not in screen_text(transport.sent[-1])
    await send(1, callback=db.token(1, "delete", hid=hid))
    assert f"#{hid}" in screen_text(transport.sent[-1])


async def test_send_attached_file_preserves_navigation_and_rechecks_access(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Download task", "task-doc", "document")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    mid, state = current_screen(db, transport, 2)
    button = click("Прислать файлы в чат", 2)
    await send(2, callback=button)
    assert isinstance(transport.sent[-1], SendDocument)
    assert transport.sent[-1].document == "task-doc"
    assert db.screen_get(2, mid) == state
    await send(3, "/start")
    await send(3, callback=db.token(3, "send_attachment", hid=hid))
    assert "Нет доступа" in transport.alerts[-1].text


async def test_compact_day_and_identical_headers_for_attachment_and_text(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.conn.execute(
        "UPDATE lessons SET details=? WHERE id=?", ("6 корпус -\n    528\n    , пл. Основная", lid)
    )
    db.save_homework(2, lid, "First task", "doc", "document")
    db.save_homework(2, lid, "Second task")
    await send(
        2, callback=db.token(2, "week", gid=1, monday="2026-11-02", scope="day", day="2026-11-02")
    )
    assert "📝 — есть ДЗ" in screen_text(transport.sent[-1])
    assert "▫️ — ДЗ не добавлено" in screen_text(transport.sent[-1])
    assert "Математический анализ" not in screen_text(transport.sent[-1])
    assert "Математический анализ" in str(transport.sent[-1].reply_markup)
    await send(2, callback=db.token(2, "lesson", lid=lid))
    first = screen_text(transport.sent[-1]).split("<b>ДЗ")[0]
    assert "6 корпус — 528, пл. Основная" in first
    await send(2, callback=click("ДЗ →", 2))
    second = screen_text(transport.sent[-1]).split("<b>ДЗ")[0]
    assert first == second


async def test_student_info_is_short_and_hides_role_management(app):
    db, transport, send, _click = app
    gid = await prepare_group(app)
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    await send(3, "/info")
    text = screen_text(transport.sent[-1])
    assert "Как найти ДЗ" in text
    assert "«Выбрать неделю», затем выберите неделю, день и пару" in text
    assert "назнач" not in text.lower()
    assert "старост" not in text.lower()
    assert "права" not in text.lower()
    assert len(text) < 650
    await send(3, "/help")
    assert screen_text(transport.sent[-1]) == text


async def test_multiple_attachments_are_saved_and_opened_as_one_homework(app):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    await send(2, callback=db.token(2, "write", lid=lid))
    await send(2, "Решить задания из двух файлов")
    await send(2, document={"file_id": "first-file", "file_unique_id": "first"})
    await send(2, document={"file_id": "second-file", "file_unique_id": "second"})
    preview = transport.sent[-1].rich_message
    assert "файлов: 2" in preview.html
    assert [item.media.media for item in preview.media] == ["first-file", "second-file"]
    await send(2, callback=click("Сохранить ДЗ", 2))

    tasks = db.homework(2, lid)
    assert len(tasks) == 1
    assert [file["file_id"] for file in homework_attachments(tasks[0])] == [
        "first-file",
        "second-file",
    ]
    card = transport.sent[-1]
    assert card.rich_message is not None
    assert "Решить задания из двух файлов" in card.rich_message.html
    assert [item.media.media for item in card.rich_message.media] == ["first-file", "second-file"]
    assert "Файл →" not in str(card.reply_markup)
    assert "← Файл" not in str(card.reply_markup)
    await send(2, callback=click("Прислать файлы в чат", 2))
    assert isinstance(transport.sent[-1], SendMediaGroup)
    assert [item.media for item in transport.sent[-1].media] == ["first-file", "second-file"]


async def test_add_file_to_legacy_homework_keeps_original_file(app):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Существующее ДЗ", "old-file", "document")
    db.conn.execute("UPDATE homework SET attachments=NULL WHERE id=?", (hid,))
    await send(2, callback=db.token(2, "lesson", lid=lid))
    assert screen_media(transport.sent[-1])[0] == "old-file"
    await send(2, callback=click("Редактировать", 2))
    await send(2, callback=click("Редактировать файлы", 2))
    await send(2, callback=click("Добавить файл", 2))
    await send(2, document={"file_id": "added-file", "file_unique_id": "new"})
    await send(2, callback=click("Сохранить ДЗ", 2))
    homework = db.homework_item(2, hid)
    assert homework["body"] == "Существующее ДЗ"
    assert [file["file_id"] for file in homework_attachments(homework)] == [
        "old-file",
        "added-file",
    ]


async def test_file_editor_shows_names_and_removes_only_selected_attachment(app):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Описание", "old-file", "document")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    await send(2, callback=click("Редактировать", 2))
    await send(2, callback=click("Редактировать файлы", 2))
    assert "Файл 1" in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("Добавить файл", 2))
    await send(
        2,
        document={"file_id": "new-file", "file_unique_id": "new", "file_name": "Практика.pdf"},
    )
    await send(2, callback=click("Сохранить ДЗ", 2))
    await send(2, callback=click("Практика.pdf", 2))
    assert screen_media(transport.sent[-1])[0] == "new-file"
    await send(2, callback=click("Удалить файл", 2))
    confirmation = click("Да, удалить", 2)
    await send(2, callback=confirmation)

    saved = db.homework_item(2, hid)
    assert saved["body"] == "Описание"
    assert [file["file_id"] for file in homework_attachments(saved)] == ["old-file"]
    assert "Файл 1" in str(transport.sent[-1].reply_markup)
    assert "Практика.pdf" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=confirmation)
    assert homework_attachments(db.homework_item(2, hid)) == homework_attachments(saved)


@pytest.mark.parametrize("manager", [1, 2], ids=["superadmin", "head"])
async def test_subject_materials_can_be_viewed_and_reused_on_another_day(app, manager):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(3, "/start")
    db.set_member(2, gid, 3, "add")
    lessons = db.all(
        "SELECT id FROM lessons WHERE subject=? ORDER BY day,slot",
        ("Математический анализ",),
    )
    source, target = lessons[0]["id"], lessons[-1]["id"]
    assert db.lesson(2, source)["day"] != db.lesson(2, target)["day"]
    file = {"file_id": "practice-file", "file_kind": "document", "name": "Практика.pdf"}
    source_hw = db.save_homework(2, source, "Исходное ДЗ", attachments=[file])
    target_hw = db.save_homework(2, target, "Новое ДЗ", "existing-file", "document")

    await send(manager, callback=db.token(manager, "lesson", lid=source))
    await send(manager, callback=click("Редактировать", manager))
    await send(manager, callback=click("Редактировать файлы", manager))
    assert "Из материалов" not in str(transport.sent[-1].reply_markup)
    await send(manager, callback=click("Практика.pdf", manager))
    await send(manager, callback=click("Добавить в материалы", manager))
    assert "Файл добавлен в материалы" in screen_text(transport.sent[-1])
    await send(manager, callback=click("Добавить в материалы", manager))
    assert "Файл уже есть в материалах" in screen_text(transport.sent[-1])
    assert len(db.subject_materials(3, target)) == 1
    db.delete_homework(2, source_hw, 1)

    await send(3, callback=db.token(3, "lesson", lid=target))
    await send(3, callback=click("📚 Материалы курса", 3))
    assert "Материалы курса · Математический анализ" in screen_text(transport.sent[-1])
    assert "Добавить к ДЗ" not in str(transport.sent[-1].reply_markup)
    message_id, screen = current_screen(db, transport, 3)
    await send(3, callback=click("Практика.pdf", 3))
    assert isinstance(transport.sent[-1], SendDocument)
    assert transport.sent[-1].document == file["file_id"]
    assert db.screen_get(3, message_id) == screen
    assert transport.messages[3, message_id].rich_message is not None

    await send(manager, callback=db.token(manager, "lesson", lid=target))
    await send(manager, callback=click("📚 Материалы курса", manager))
    rows = transport.sent[-1].reply_markup.inline_keyboard
    assert [[button.text for button in row] for row in rows[:3]] == [
        ["📄 Практика.pdf"],
        ["📎 Добавить к ДЗ", "📤 Все в чат"],
        ["➕ Загрузить", "🗑 Удалить"],
    ]
    await send(manager, callback=click("Добавить к ДЗ", manager))
    await send(manager, callback=click("▫️ Практика.pdf", manager))
    assert "Файл прикреплён к ДЗ" in screen_text(transport.sent[-1])
    assert "✅ Практика.pdf" in str(transport.sent[-1].reply_markup)
    saved = db.homework_item(2, target_hw)
    assert saved["body"] == "Новое ДЗ"
    assert homework_attachments(saved) == [
        {"file_id": "existing-file", "file_kind": "document"},
        file,
    ]
    detach = click("✅ Практика.pdf", manager)
    await send(manager, callback=detach)
    assert "Файл откреплён от ДЗ" in screen_text(transport.sent[-1])
    assert homework_attachments(db.homework_item(2, target_hw)) == homework_attachments(saved)[:1]
    assert len(db.subject_materials(3, target)) == 1
    await send(manager, callback=click("▫️ Практика.pdf", manager))
    assert "✅ Практика.pdf" in str(transport.sent[-1].reply_markup)
    await send(manager, callback=detach)
    assert "Список файлов изменился" in transport.alerts[-1].text
    assert homework_attachments(db.homework_item(2, target_hw)) == homework_attachments(saved)
    await send(manager, callback=click("Назад", manager))
    assert "Материалы курса · Математический анализ" in screen_text(transport.sent[-1])


async def test_material_cannot_be_detached_if_it_would_leave_empty_homework(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "", "file", "document")
    db.save_subject_material(2, hid, 0, 1)
    await send(2, callback=db.token(2, "materials", hid=hid))
    await send(2, callback=click("✅ Файл 1", 2))
    assert "единственный файл в ДЗ" in transport.alerts[-1].text
    assert db.homework_item(2, hid)["file_id"] == "file"


async def test_subject_materials_recheck_role_group_and_subject(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(3, "/start")
    db.set_member(2, gid, 3, "add")
    db.set_member(2, gid, 3, "editor")
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "ДЗ", "file", "document")
    db.save_subject_material(2, hid, 0, 1)
    material_id = db.subject_materials(2, lid)[0]["id"]

    await send(3, callback=db.token(3, "lesson", lid=lid))
    await send(3, callback=click("Редактировать", 3))
    await send(3, callback=click("Редактировать файлы", 3))
    assert "Из материалов" not in str(transport.sent[-1].reply_markup)
    await send(3, callback=click("Файл 1", 3))
    assert "Добавить в материалы" not in str(transport.sent[-1].reply_markup)
    await send(3, callback=db.token(3, "save_material", hid=hid, index=0, revision=1))
    assert "Нет доступа" in transport.alerts[-1].text
    await send(
        3, callback=db.token(3, "attach_material", hid=hid, revision=1, material_id=material_id)
    )
    assert "Нет доступа" in transport.alerts[-1].text
    await send(3, callback=db.token(3, "materials", lid=lid, homework_id=hid))
    assert "Добавить к ДЗ" not in str(transport.sent[-1].reply_markup)

    different_subject = db.one(
        "SELECT id FROM lessons WHERE subject<>?", (db.lesson(2, lid)["subject"],)
    )["id"]
    await send(2, callback=db.token(2, "materials", lid=different_subject))
    assert "Материалов пока нет" in screen_text(transport.sent[-1])
    await send(
        2, callback=db.token(2, "send_materials", lid=different_subject, material_id=material_id)
    )
    assert "не найден" in transport.alerts[-1].text

    other_group = db.catalog_group(1, "Другие", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.sync_lessons(1, other_group, parse_week((FIXTURES / "schedule.html").read_text()))
    other_lesson = db.one("SELECT id FROM lessons WHERE group_id=? ORDER BY id", (other_group,))
    await send(1, callback=db.token(1, "materials", lid=other_lesson["id"]))
    assert "Материалов пока нет" in screen_text(transport.sent[-1])
    await send(3, callback=db.token(3, "materials", lid=other_lesson["id"]))
    assert "Нет доступа" in transport.alerts[-1].text
    await send(3, callback=db.token(3, "send_materials", lid=other_lesson["id"]))
    assert "Нет доступа" in transport.alerts[-1].text


async def test_send_all_subject_materials_includes_other_pages(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(3, "/start")
    db.set_member(2, gid, 3, "add")
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    for index in range(11):
        hid = db.save_homework(2, lid, "", f"material-{index}", "document")
        db.save_subject_material(2, hid, 0, 1)
    await send(3, callback=db.token(3, "materials", lid=lid))
    await send(3, callback=click("Все в чат", 3))
    album, single = transport.sent[-2:]
    assert isinstance(album, SendMediaGroup)
    assert [item.media for item in album.media] == [f"material-{i}" for i in range(10, 0, -1)]
    assert isinstance(single, SendDocument)
    assert single.document == "material-0"


async def test_manage_course_materials_directly_from_the_list(app):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    gid = await prepare_group(app)
    await send(3, "/start")
    db.set_member(2, gid, 3, "add")
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    await send(2, callback=db.token(2, "lesson", lid=lid))
    await send(2, callback=click("Материалы курса", 2))
    assert "Материалов пока нет" in screen_text(transport.sent[-1])
    await send(2, callback=click("➕ Загрузить", 2))
    document = {
        "file_id": "manual",
        "file_unique_id": "manual",
        "file_name": "Методичка.pdf",
    }
    await send(2, document=document)
    await send(2, document=document)
    assert "уже есть в материалах" in screen_text(transport.sent[-1])
    await send(
        2,
        document={"file_id": "tasks", "file_unique_id": "tasks", "file_name": "Задачи.pdf"},
    )
    await send(2, callback=click("⬅️ Назад", 2))
    assert "Материалы курса" in screen_text(transport.sent[-1])
    assert {file["name"] for file in db.subject_materials(3, lid)} == {
        "Задачи.pdf",
        "Методичка.pdf",
    }
    material_id = next(
        file["id"] for file in db.subject_materials(3, lid) if file["file_id"] == "manual"
    )
    hid = db.save_homework(2, lid, "Задание")
    db.set_subject_material_attachment(2, hid, 1, material_id)

    await send(3, callback=db.token(3, "materials", lid=lid))
    assert "➕ Загрузить" not in str(transport.sent[-1].reply_markup)
    assert "🗑 Удалить" not in str(transport.sent[-1].reply_markup)
    await send(3, callback=db.token(3, "upload_materials", lid=lid))
    assert "Нет доступа" in transport.alerts[-1].text
    await send(3, callback=db.token(3, "delete_material", lid=lid, material_id=material_id))
    assert "Нет доступа" in transport.alerts[-1].text

    await send(2, callback=click("🗑 Удалить", 2))
    await send(2, callback=click("Методичка.pdf", 2))
    assert len(db.subject_materials(3, lid)) == 2
    await send(2, callback=click("Удалить из материалов", 2))
    assert "Файл удалён из материалов" in screen_text(transport.sent[-1])
    assert [file["file_id"] for file in db.subject_materials(3, lid)] == ["tasks"]
    assert homework_attachments(db.homework_item(3, hid))[0]["file_id"] == "manual"


async def test_subject_materials_can_be_selected_before_saving_new_homework(app):
    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    material = {"file_id": "handout", "file_kind": "document", "name": "Практика.pdf"}
    source = db.save_homework(2, lid, "Исходное ДЗ", attachments=[material])
    db.save_subject_material(2, source, 0, 1)
    db.delete_homework(2, source, 1)

    await send(2, callback=db.token(2, "lesson", lid=lid))
    await send(2, callback=click("Добавить ДЗ", 2))
    await send(2, callback=click("Из материалов", 2))
    await send(2, callback=click("▫️ Практика.pdf", 2))
    assert "✅ Практика.pdf" in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("К черновику", 2))
    assert screen_media(transport.sent[-1])[0] == "handout"
    first_save = click("Сохранить ДЗ", 2)
    assert not db.homework(2, lid)

    await send(2, callback=click("Из материалов", 2))
    await send(2, callback=click("✅ Практика.pdf", 2))
    await send(2, callback=click("К черновику", 2))
    assert "Сохранить ДЗ" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=first_save)
    assert "Кнопка устарела" in transport.alerts[-1].text
    await send(2, "Решить упражнения")
    await send(2, document={"file_id": "extra", "file_unique_id": "extra"})
    await send(2, callback=click("Из материалов", 2))
    await send(2, callback=click("▫️ Практика.pdf", 2))
    await send(2, callback=click("К черновику", 2))
    preview = transport.sent[-1].rich_message
    assert "Решить упражнения" in preview.html
    assert [item.media.media for item in preview.media] == ["extra", "handout"]
    assert not db.homework(2, lid)

    await send(2, callback=click("Сохранить ДЗ", 2))
    [saved] = db.homework(2, lid)
    assert saved["body"] == "Решить упражнения"
    assert homework_attachments(saved) == [{"file_id": "extra", "file_kind": "document"}, material]


@pytest.mark.parametrize("replace", [False, True])
async def test_photo_album_updates_one_draft_with_all_photos(app, replace):
    import asyncio

    from rea_bot.db import homework_attachments

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = None
    if replace:
        hid = db.save_homework(2, lid, "Старое описание", "old-file", "document")
    await send(
        2,
        callback=db.token(
            2, "write", lid=lid, hid=hid, description_mode="replace" if replace else None
        ),
    )
    before = len(transport.sent)
    for index in range(3):
        db.conn.execute(
            "INSERT INTO document_cache VALUES(?,?)", (f"photo-{index}", f"document-{index}")
        )

    async def photo(index):
        await send(
            2,
            photo=[
                {
                    "file_id": f"photo-{index}",
                    "file_unique_id": f"unique-{index}",
                    "width": 100,
                    "height": 100,
                }
            ],
            caption="Задания из альбома" if index == 1 else None,
            media_group_id="album-1",
        )

    await asyncio.gather(*(photo(index) for index in range(3)))
    assert len(transport.sent) == before + 1
    preview = transport.sent[-1].rich_message
    assert "Задания из альбома" in preview.html
    assert "Старое описание" not in preview.html
    expected_files = (["old-file"] if replace else []) + [
        "photo-0",
        "photo-1",
        "photo-2",
    ]
    assert [item.media.media for item in preview.media] == expected_files
    assert len(db.homework(2, lid)) == int(replace)
    await send(2, callback=click("Сохранить ДЗ", 2))
    [saved] = db.homework(2, lid)
    assert saved["body"] == "Задания из альбома"
    assert [file["file_id"] for file in homework_attachments(saved)] == (
        (["old-file"] if replace else []) + ["photo-0", "photo-1", "photo-2"]
    )


async def test_oversized_album_keeps_draft_unchanged(app):
    import asyncio

    db, transport, send, _click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(
        2,
        lid,
        "Описание",
        attachments=[{"file_id": f"existing-{i}", "file_kind": "document"} for i in range(9)],
    )
    await send(2, callback=db.token(2, "write", lid=lid, hid=hid, attach=True))
    before = db.session_get(2)
    await asyncio.gather(
        *(
            send(
                2,
                document={"file_id": f"new-{i}", "file_unique_id": f"unique-{i}"},
                media_group_id="too-many",
            )
            for i in range(2)
        )
    )
    assert db.session_get(2) == before
    assert "до 10 файлов" in screen_text(transport.sent[-1])
    assert db.homework_item(2, hid)["revision"] == 1
