import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent

FORESIGHT = 'Специальный факультет талантливой молодежи "Форсайт" (высшая школа)'


@dataclass(frozen=True)
class Settings:
    token: str = ""
    admins: frozenset[int] = frozenset()
    database: str = "data/bot.sqlite3"
    timezone: str = "Europe/Moscow"
    faculty: str = FORESIGHT
    schedule_ttl: int = 1800
    catalog_ttl: int = 86400
    request_interval: float = 8
    refresh_hour: int = 7

    @classmethod
    def from_env(cls):
        env_path = (
            Path(os.environ["REA_ENV_FILE"]).expanduser().resolve()
            if os.environ.get("REA_ENV_FILE")
            else PROJECT_DIR / ".env"
        )
        load_dotenv(env_path, override=False)
        database = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")).expanduser()
        if not database.is_absolute():
            database = env_path.parent / database
        result = cls(
            token=os.getenv("BOT_TOKEN", "").strip(),
            admins=frozenset(
                int(x.strip()) for x in os.getenv("SUPERADMIN_IDS", "").split(",") if x.strip()
            ),
            database=str(database),
            timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
            faculty=os.getenv("ALLOWED_FACULTY", FORESIGHT),
            schedule_ttl=int(os.getenv("SCHEDULE_TTL_SECONDS", "1800")),
            catalog_ttl=int(os.getenv("CATALOG_TTL_SECONDS", "86400")),
            request_interval=float(os.getenv("REA_REQUEST_INTERVAL_SECONDS", "8")),
            refresh_hour=int(os.getenv("SCHEDULE_REFRESH_HOUR", "7")),
        )
        if not 0 <= result.refresh_hour <= 23:
            raise ValueError("SCHEDULE_REFRESH_HOUR должен быть от 0 до 23")
        ZoneInfo(result.timezone)
        if result.schedule_ttl < 60 or result.catalog_ttl < 60 or result.request_interval < 1:
            raise ValueError("TTL должен быть >= 60 секунд, интервал запросов >= 1 секунды")
        if any(uid <= 0 for uid in result.admins):
            raise ValueError("SUPERADMIN_IDS: нужны положительные числовые Telegram ID")
        return result
