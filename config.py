"""Настройки. Всё берётся из переменных окружения (или файла .env рядом с main.py)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:  # dotenv не обязателен, если переменные заданы в панели хостинга
    pass

BASE_DIR = Path(__file__).parent


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    return {int(x) for x in _str(name).replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: _str("BOT_TOKEN"))
    channel_id: int = field(default_factory=lambda: _int("CHANNEL_ID", 0))
    admin_ids: set[int] = field(default_factory=lambda: _ids("ADMIN_IDS"))

    platform: str = field(default_factory=lambda: _str("FUT_PLATFORM", "pc").lower())
    min_rating: int = field(default_factory=lambda: _int("FUT_MIN_RATING", 84))
    min_price: int = field(default_factory=lambda: _int("FUT_MIN_PRICE", 5000))
    watchlist_refresh_hours: float = field(default_factory=lambda: _float("FUT_WATCHLIST_REFRESH_HOURS", 12))

    drop_percent: float = field(default_factory=lambda: _float("FUT_DROP_PERCENT", 8))
    min_profit: int = field(default_factory=lambda: _int("FUT_MIN_PROFIT", 1000))
    fresh_percent: float = field(default_factory=lambda: _float("FUT_FRESH_PERCENT", 5))
    single_lot_gap: float = field(default_factory=lambda: _float("FUT_SINGLE_LOT_GAP", 10))
    skip_single_lot: bool = field(default_factory=lambda: _str("FUT_SKIP_SINGLE_LOT", "0") in {"1", "true", "yes"})
    alert_cooldown_minutes: int = field(default_factory=lambda: _int("FUT_ALERT_COOLDOWN_MINUTES", 180))
    max_price_age_minutes: int = field(default_factory=lambda: _int("FUT_MAX_PRICE_AGE_MINUTES", 30))

    tz_offset: float = field(default_factory=lambda: _float("FUT_TZ_OFFSET", 3))
    tz_label: str = field(default_factory=lambda: _str("FUT_TZ_LABEL", "МСК"))

    poll_seconds: int = field(default_factory=lambda: _int("FUT_POLL_SECONDS", 120))
    concurrency: int = field(default_factory=lambda: max(1, min(_int("FUT_CONCURRENCY", 4), 8)))
    db_path: Path = field(default_factory=lambda: BASE_DIR / _str("DB_PATH", "market.db"))

    def validate(self) -> list[str]:
        problems = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан")
        if self.platform not in {"pc", "ps"}:
            problems.append("FUT_PLATFORM должен быть pc или ps")
        return problems


settings = Settings()
