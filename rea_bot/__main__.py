import argparse
import asyncio
import json
import logging
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .bot import build_dispatcher
from .config import Settings
from .db import Store
from .refresh import ScheduleRefresh
from .schedule import ReaClient, parse_week
from .snapshots import restore_schedules


async def run(settings):
    if not settings.token:
        raise SystemExit("Заполните BOT_TOKEN в .env (см. .env.example).")
    store = Store(settings.database, settings.admins)
    restore_schedules(store, settings)
    schedule = ReaClient(store, settings)
    refresher = ScheduleRefresh(store, schedule, settings)
    refresh_task = asyncio.create_task(refresher.run())
    try:
        async with Bot(
            settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        ) as bot:
            if not settings.admins:
                logging.getLogger(__name__).warning(
                    "SUPERADMIN_IDS is empty. Use /id, then configure the owner and restart."
                )
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="id", description="Мой Telegram ID"),
                    BotCommand(command="help", description="Как пользоваться"),
                    BotCommand(command="info", description="Краткая инструкция"),
                    BotCommand(command="cancel", description="Отменить ввод"),
                ]
            )
            await build_dispatcher(store, schedule, settings, refresher).start_polling(
                bot, allowed_updates=["message", "callback_query"]
            )
    finally:
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await schedule.close()
        store.close()


async def smoke(settings, course, level, group, monday):
    """Explicit diagnostic only; no full-faculty crawl or Telegram messages."""
    store = Store(":memory:", {1})
    store.register(1, None, "Local diagnostic")
    client = ReaClient(store, settings)
    try:
        courses = await client.navigator()
        if course not in {o["value"] for o in courses["options"]}:
            raise SystemExit(
                "Курс не найден: " + json.dumps(courses["options"], ensure_ascii=False)
            )
        levels = await client.navigator(course)
        if level not in {o["value"] for o in levels["options"]}:
            raise SystemExit(
                "Уровень не найден: " + json.dumps(levels["options"], ensure_ascii=False)
            )
        groups = await client.navigator(course, level)
        print(json.dumps(groups["options"], ensure_ascii=False, indent=2))
        if not group:
            return
        if group not in {o["value"] for o in groups["options"]}:
            raise SystemExit("Группа не найдена в выбранном факультете/курсе/уровне.")
        row = store.catalog_group(1, group, settings.faculty, course, level)
        target = (
            date.fromisoformat(monday)
            if monday
            else datetime.now(ZoneInfo(settings.timezone)).date()
        )
        target -= timedelta(days=target.weekday())
        week = await client.week(1, row["id"], target)
        print(json.dumps(week, ensure_ascii=False, indent=2))
    finally:
        await client.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description="ДЗ РЭУ · Telegram")
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser(
        "check-schedule", help="До 5 последовательных запросов: один курс/уровень/группа"
    )
    check.add_argument("--course", default="1-й курс")
    check.add_argument("--level", default="Бакалавр")
    check.add_argument("--group")
    check.add_argument("--monday", help="Дата нужной недели, YYYY-MM-DD")
    parse = sub.add_parser("parse-schedule", help="Проверить сохранённый HTML без сети")
    parse.add_argument("file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    if args.command == "check-schedule":
        asyncio.run(smoke(settings, args.course, args.level, args.group, args.monday))
    elif args.command == "parse-schedule":
        print(json.dumps(parse_week(args.file.read_text()), ensure_ascii=False, indent=2))
    else:
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
