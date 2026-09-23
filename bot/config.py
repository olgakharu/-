"""Настройки бота из .env и контент из content.json."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Telegram Stars поддерживает подписки только с периодом ровно 30 дней
STARS_PERIOD_SECONDS = 30 * 24 * 3600


def _int_or_none(value: str | None) -> int | None:
    m = re.search(r"-?\d{5,}", value or "")
    return int(m.group()) if m else None


def _clean_url(value: str | None) -> str:
    """Достаёт https-ссылку, отбрасывая мусор (например, служебные символы веб-консоли)."""
    m = re.search(r"https://[^\s\x00-\x1f\x7f]+", value or "")
    return m.group() if m else ""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    payment_mode: str
    provider_token: str
    yookassa_receipt: bool
    channel_id: int | None
    miniapp_url: str
    tz: ZoneInfo
    grace_hours: int
    db_path: Path
    content_path: Path


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Не задан BOT_TOKEN в .env")

    mode = os.getenv("PAYMENT_MODE", "stars").strip().lower()
    if mode not in ("stars", "provider"):
        raise SystemExit("PAYMENT_MODE должен быть stars или provider")
    provider_token = os.getenv("PROVIDER_TOKEN", "").strip()
    if mode == "provider" and not provider_token:
        raise SystemExit("Для PAYMENT_MODE=provider нужен PROVIDER_TOKEN")

    return Config(
        bot_token=token,
        admin_ids=frozenset(int(x) for x in re.findall(r"\d{5,}", os.getenv("ADMIN_IDS", ""))),
        payment_mode=mode,
        provider_token=provider_token,
        yookassa_receipt=os.getenv("YOOKASSA_RECEIPT", "1").strip() == "1",
        channel_id=_int_or_none(os.getenv("CHANNEL_ID")),
        miniapp_url=_clean_url(os.getenv("MINIAPP_URL")),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow").strip()),
        grace_hours=int(os.getenv("GRACE_HOURS", "12")),
        db_path=BASE_DIR / os.getenv("DB_PATH", "bot.db"),
        content_path=BASE_DIR / os.getenv("CONTENT_PATH", "content.json"),
    )


def load_content(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
