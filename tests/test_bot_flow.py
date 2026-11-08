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
    SendMessage,
    SendPhoto,
)
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

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
        assert isinstance(
            method, (SendMessage, EditMessageText, EditMessageMedia, SendDocument, SendPhoto)
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
        result = Message(
            message_id=mid,
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
        )
        if callback:
            candidates = [
                m
                for (chat_id, _), m in session.messages.items()
                if chat_id == uid and (m.text or m.caption)
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
    await send(1, callback=click("Назначить старосту", 1))
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("Подтвердить", 1))
    assert db.user(2)["head_allowed"] == 1
    await send(2, "/start")
    await send(2, callback=click("Выбрать группу", 2))
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
    await send(3, callback=click("Моя группа", 3))
    await send(3, callback=click("Расписание и ДЗ", 3))
    await send(3, callback=click("Текущая неделя", 3))
    await send(3, callback=click("Пн (02.11)", 3))
    await send(3, callback=click("02.11 · 3", 3))
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
    assert isinstance(transport.sent[-1], EditMessageMedia)
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


def current_screen(db, transport, uid):
    method = next(
        m
        for m in reversed(transport.sent)
        if m.chat_id == uid
        and isinstance(m, (SendMessage, EditMessageText, EditMessageMedia, SendDocument))
    )
    if isinstance(method, (EditMessageText, EditMessageMedia)):
        mid = method.message_id
    else:
        mid = next(
            mid
            for (chat, mid), msg in reversed(transport.messages.items())
            if chat == uid and msg.text == method.text
        )
    return mid, db.screen_get(uid, mid)


async def prepare_group(app):
    db, _transport, send, click = app
    await send(1, "/start")
    await send(2, "/start")
    gid = db.catalog_group(1, "G", FORESIGHT, "1-й курс", "Бакалавр")["id"]
    db.appoint_head(1, 2)
    db.claim(2, gid)
    db.sync_lessons(2, gid, parse_week((FIXTURES / "schedule.html").read_text()))
    await send(2, callback=db.token(2, "home"))
    await send(2, callback=click("Моя группа", 2))
    return gid


async def test_catalog_back_edits_same_message(app):
    db, transport, send, click = app
    await send(1, "/start")
    mid, _ = current_screen(db, transport, 1)
    await send(1, callback=click("Выбрать группу", 1))
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
    assert len([m for m in transport.sent if isinstance(m, SendMessage)]) == 1
    assert all(m.message_id == mid for m in transport.sent if isinstance(m, EditMessageText))


async def test_back_preserves_selected_day_and_week(app):
    db, transport, send, click = app
    await prepare_group(app)
    await send(2, callback=click("Расписание и ДЗ", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Пт (06.11)", 2))
    mid, before = current_screen(db, transport, 2)
    await send(2, callback=click("06.11 · 3", 2))
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2) == (mid, before)
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["scope"] == "week"
    assert len([m for m in transport.sent if m.chat_id == 2 and isinstance(m, SendMessage)]) == 1


async def test_drafts_save_delete_and_homework_pages_use_one_card(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.save_homework(2, lid, "First task")
    db.save_homework(2, lid, "Second task")
    await send(2, callback=click("Расписание и ДЗ", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Пн (02.11)", 2))
    await send(2, callback=click("02.11 · 3", 2))
    assert "First task" in transport.sent[-1].text
    await send(2, callback=click("ДЗ →", 2))
    assert "Second task" in transport.sent[-1].text
    assert "First task" not in transport.sent[-1].text
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
    assert "✅ ДЗ сохранено" in transport.sent[-1].text
    assert "Third task &lt;x&gt; &amp; y" in transport.sent[-1].text
    assert current_screen(db, transport, 2)[1]["current"]["page"] == 2
    await send(2, callback=click("🗑 Удалить", 2))
    await send(2, callback=click("⬅️ Назад", 2))
    assert len(db.homework(2, lid)) == 3
    await send(2, callback=click("🗑 Удалить", 2))
    await send(2, callback=click("Да, удалить", 2))
    assert "✅ ДЗ удалено" in transport.sent[-1].text
    assert "Second task" in transport.sent[-1].text
    await send(2, callback=click("⬅️ Назад", 2))
    assert current_screen(db, transport, 2)[1]["current"]["action"] == "week"
    assert len([m for m in transport.sent if m.chat_id == 2 and isinstance(m, SendMessage)]) == 1


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
    await send(3, callback=click("Моя группа", 3))
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
    await send(1, callback=click("Выбрать группу", 1))
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
    await send(1, callback=click("Назначить старосту", 1))
    await send(1, callback=click("User 2", 1))
    await send(1, callback=click("Подтвердить", 1))
    assert "✅ Права обновлены" in transport.sent[-1].text
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
    await send(2, callback=click("Расписание и ДЗ", 2))
    assert "Сохранённые недели" in transport.sent[-1].text
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


async def test_weekly_homework_shows_all_days_and_excludes_deleted(app):
    db, transport, send, click = app
    gid = await prepare_group(app)
    lessons = db.all("SELECT * FROM lessons WHERE group_id=? ORDER BY day,slot", (gid,))
    first = lessons[0]["id"]
    last = lessons[-1]["id"]
    db.save_homework(2, first, "Monday task")
    db.save_homework(2, last, "Saturday task")
    removed = db.save_homework(2, first, "Deleted task")
    db.delete_homework(2, removed, 1)
    await send(2, callback=click("Расписание и ДЗ", 2))
    await send(2, callback=click("Текущая неделя", 2))
    await send(2, callback=click("Все ДЗ за неделю", 2))
    assert "Monday task" in transport.sent[-1].text
    assert "Saturday task" in transport.sent[-1].text
    assert "Deleted task" not in transport.sent[-1].text
    await send(2, callback=click("Дни недели", 2))
    await send(2, callback=click("Ср (04.11)", 2))
    assert "ещё не загружено" in transport.sent[-1].text


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
    assert "Моя группа" in str(transport.sent[-1].reply_markup)
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
    assert "Редакторов ДЗ пока нет" in transport.sent[-1].text
    assert "<blockquote>" in transport.sent[-1].text
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
        assert "ещё не загружено" in transport.sent[-1].text
        SavedSchedule.week.assert_not_awaited()
        # Only owner sees and can open the bulk-load confirmation.
        await send(2, callback=db.token(2, "year_confirm"))
        assert "Недостаточно прав" in transport.alerts[-1].text
        await send(1, callback=db.token(1, "year_confirm"))
        assert "Загрузить расписание" in transport.sent[-1].text
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
    assert "✅ Расписание загружено" in transport.sent[-1].text
    await send(1, callback=confirm)
    assert load.await_count == 9


async def test_day_navigation_crosses_week_boundary_and_no_duplicate_button(app):
    db, transport, send, click = app
    await prepare_group(app)
    assert "Выбрать неделю" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("Сегодня", 2))
    assert "05.11.2026" in transport.sent[-1].text
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
    assert isinstance(transport.sent[-1], EditMessageMedia)
    assert transport.sent[-1].message_id == mid
    assert transport.sent[-1].media.media == "first-doc"
    assert "Task with first file" in transport.sent[-1].media.caption
    await send(2, callback=click("ДЗ →", 2))
    assert transport.sent[-1].media.media == "second-doc"
    assert transport.sent[-1].message_id == mid
    assert "Открыть вложение" not in str(transport.sent[-1].reply_markup)
    await send(2, callback=click("⬅️ Назад", 2))
    assert mid in transport.deleted
    assert isinstance(transport.sent[-1], SendMessage)
    new_mid, _ = current_screen(db, transport, 2)
    assert new_mid != mid
    assert (2, mid) not in transport.messages
    await send(2, callback=click("Сегодня", 2))
    assert transport.sent[-1].message_id == new_mid


async def test_photo_is_uploaded_as_jpg_document_and_reused(app, monkeypatch):
    from io import BytesIO
    from unittest.mock import AsyncMock

    from aiogram.types import BufferedInputFile

    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    db.save_homework(2, lid, "Photo homework", "source-photo", "photo")
    download = AsyncMock(return_value=BytesIO(b"original-telegram-photo-bytes"))
    monkeypatch.setattr(Bot, "download", download)
    await send(2, callback=db.token(2, "lesson", lid=lid))
    uploaded = transport.sent[-1].media.media
    assert isinstance(uploaded, BufferedInputFile)
    assert uploaded.filename == "homework-photo.jpg"
    assert uploaded.data == b"original-telegram-photo-bytes"
    cached = db.one("SELECT document_id FROM document_cache WHERE source_id=?", ("source-photo",))[
        "document_id"
    ]
    await send(2, callback=click("Главное меню", 2))
    await send(2, callback=db.token(2, "lesson", lid=lid))
    assert transport.sent[-1].media.media == cached
    assert download.await_count == 1


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
        caption = BeautifulSoup(method.media.caption, "html.parser").get_text()
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
    assert "<blockquote>" in transport.sent[-1].media.caption


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
    await send(2, callback=click("Прикрепить файл", 2))
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
    assert "<blockquote>Описание к фотографии</blockquote>" in transport.sent[-1].media.caption


async def test_homework_ids_only_visible_to_owner_everywhere(app):
    db, transport, send, _click = app
    gid = await prepare_group(app)
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Task text", "doc", "document")
    for uid in (1, 2, 3):
        await send(uid, callback=db.token(uid, "lesson", lid=lid))
        caption = transport.sent[-1].media.caption
        assert (f"#{hid}" in caption) == (uid == 1)
        await send(uid, callback=db.token(uid, "week", gid=gid, monday="2026-11-02", scope="hw"))
        assert (f"#{hid}" in str(transport.sent[-1].reply_markup)) == (uid == 1)
    await send(2, callback=db.token(2, "delete", hid=hid))
    assert f"#{hid}" not in transport.sent[-1].text
    await send(1, callback=db.token(1, "delete", hid=hid))
    assert f"#{hid}" in transport.sent[-1].text


async def test_send_attached_file_preserves_navigation_and_rechecks_access(app):
    db, transport, send, click = app
    await prepare_group(app)
    lid = db.one("SELECT id FROM lessons ORDER BY id")["id"]
    hid = db.save_homework(2, lid, "Download task", "task-doc", "document")
    await send(2, callback=db.token(2, "lesson", lid=lid))
    mid, state = current_screen(db, transport, 2)
    button = click("Прислать вложенный файл", 2)
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
    assert "📝 — есть ДЗ" in transport.sent[-1].text
    assert "▫️ — ДЗ не добавлено" in transport.sent[-1].text
    assert "Математический анализ" not in transport.sent[-1].text
    assert "Математический анализ" in str(transport.sent[-1].reply_markup)
    await send(2, callback=db.token(2, "lesson", lid=lid))
    first = transport.sent[-1].media.caption.split("<b>ДЗ")[0]
    assert "6 корпус — 528, пл. Основная" in first
    await send(2, callback=click("ДЗ →", 2))
    second = transport.sent[-1].text.split("<b>ДЗ")[0]
    assert first == second


async def test_student_info_is_short_and_hides_role_management(app):
    db, transport, send, _click = app
    gid = await prepare_group(app)
    db.register(3, "person3", "Student")
    db.set_member(2, gid, 3, "add")
    await send(3, "/info")
    text = transport.sent[-1].text
    assert "Как найти ДЗ" in text
    assert "неделю → день → пару" in text
    assert "назнач" not in text.lower()
    assert "старост" not in text.lower()
    assert "права" not in text.lower()
    assert len(text) < 650
    await send(3, "/help")
    assert transport.sent[-1].text == text
