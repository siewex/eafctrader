"""SQLite: watchlist, снимки цен, подписчики, отправленные сигналы.

Всё, что связано с ценами, хранится отдельно по платформам: у PC и PS это разные рынки
со своими ценами, поэтому ключ везде (player_id, platform).
"""
import sqlite3
import time
from pathlib import Path

from futnext import PlayerInfo, slugify

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL,
    position TEXT NOT NULL,
    rarity TEXT NOT NULL,
    club TEXT NOT NULL,
    nation TEXT NOT NULL,
    list_price INTEGER,
    manual INTEGER NOT NULL DEFAULT 0,   -- 1 = добавлен вручную через /watch, не удаляется при обновлении watchlist
    updated_at REAL NOT NULL,
    PRIMARY KEY (id, platform)
);
CREATE TABLE IF NOT EXISTS prices (
    player_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    ts REAL NOT NULL,
    price INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_lookup ON prices(player_id, platform, ts);
CREATE TABLE IF NOT EXISTS alerts (
    player_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    ts REAL NOT NULL,
    price INTEGER NOT NULL,
    market INTEGER NOT NULL,
    PRIMARY KEY (player_id, platform)
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'pc',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path, default_platform: str = "pc"):
        self.default_platform = default_platform
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """Переход со схемы без платформы. Подписчиков сохраняем, кэш цен и watchlist пересоберутся сами."""
        def columns(table: str) -> set[str]:
            return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        with self.conn:
            if columns("subscribers") and "platform" not in columns("subscribers"):
                self.conn.execute(f"ALTER TABLE subscribers ADD COLUMN platform TEXT NOT NULL DEFAULT '{self.default_platform}'")
            for table in ("players", "prices", "alerts"):
                cols = columns(table)
                if cols and "platform" not in cols:
                    self.conn.execute(f"DROP TABLE {table}")

    # ---------- meta ----------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ---------- watchlist ----------

    def replace_auto_watchlist(self, players: list[PlayerInfo], platform: str) -> tuple[int, int]:
        """Заменяет автоматическую часть watchlist платформы. Ручные (manual=1) не трогает."""
        now = time.time()
        old = {r["id"] for r in self.conn.execute("SELECT id FROM players WHERE manual=0 AND platform=?", (platform,))}
        new_ids = {p.id for p in players}
        with self.conn:
            self.conn.executemany(
                "INSERT INTO players(id, platform, name, slug, rating, position, rarity, club, nation, list_price, manual, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(id, platform) DO UPDATE SET name=excluded.name, slug=excluded.slug, rating=excluded.rating, "
                "position=excluded.position, rarity=excluded.rarity, club=excluded.club, nation=excluded.nation, "
                "list_price=excluded.list_price, updated_at=excluded.updated_at",
                [(p.id, platform, p.name, p.slug, p.rating, p.position, p.rarity, p.club, p.nation, p.price, now) for p in players],
            )
            removed = old - new_ids
            if removed:
                self.conn.executemany("DELETE FROM players WHERE id=? AND platform=? AND manual=0", [(i, platform) for i in removed])
        return len(new_ids - old), len(removed)

    def upsert_manual(self, p: PlayerInfo, platform: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO players(id, platform, name, slug, rating, position, rarity, club, nation, list_price, manual, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(id, platform) DO UPDATE SET manual=1, name=excluded.name, slug=excluded.slug, "
                "rating=excluded.rating, list_price=excluded.list_price, updated_at=excluded.updated_at",
                (p.id, platform, p.name, p.slug, p.rating, p.position, p.rarity, p.club, p.nation, p.price, time.time()),
            )

    def remove(self, player_id: int, platform: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM players WHERE id=? AND platform=?", (player_id, platform))
        return cur.rowcount > 0

    def all_players(self, platform: str) -> list[PlayerInfo]:
        rows = self.conn.execute("SELECT * FROM players WHERE platform=? ORDER BY rating DESC, name", (platform,))
        return [self._row_to_player(r) for r in rows]

    def get_player(self, player_id: int, platform: str) -> PlayerInfo | None:
        row = self.conn.execute("SELECT * FROM players WHERE id=? AND platform=?", (player_id, platform)).fetchone()
        return self._row_to_player(row) if row else None

    def search(self, query: str, platform: str, limit: int = 5) -> list[PlayerInfo]:
        rows = self.conn.execute(
            "SELECT * FROM players WHERE platform=? AND lower(name) LIKE ? ORDER BY rating DESC LIMIT ?",
            (platform, f"%{query.lower()}%", limit),
        )
        return [self._row_to_player(r) for r in rows]

    def count(self, platform: str | None = None) -> int:
        if platform:
            return self.conn.execute("SELECT COUNT(*) FROM players WHERE platform=?", (platform,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    @staticmethod
    def _row_to_player(r: sqlite3.Row) -> PlayerInfo:
        return PlayerInfo(
            id=r["id"], name=r["name"], slug=r["slug"] or slugify(r["name"].split()[-1]), rating=r["rating"],
            position=r["position"], rarity=r["rarity"], club=r["club"], nation=r["nation"], price=r["list_price"],
        )

    # ---------- цены ----------

    def add_price(self, player_id: int, platform: str, price: int, ts: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO prices(player_id, platform, ts, price) VALUES (?, ?, ?, ?)", (player_id, platform, ts or time.time(), price)
        )

    def recent_prices(self, player_id: int, platform: str, hours: float = 24) -> list[tuple[float, int]]:
        since = time.time() - hours * 3600
        rows = self.conn.execute(
            "SELECT ts, price FROM prices WHERE player_id=? AND platform=? AND ts>=? ORDER BY ts", (player_id, platform, since)
        )
        return [(r["ts"], r["price"]) for r in rows]

    def last_prices(self, player_id: int, platform: str, n: int = 10) -> list[tuple[float, int]]:
        """Последние n замеров (от старых к новым) — наш аналог «последних продаж» Futbin."""
        rows = self.conn.execute(
            "SELECT ts, price FROM prices WHERE player_id=? AND platform=? ORDER BY ts DESC LIMIT ?", (player_id, platform, n)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in reversed(rows)]

    def prune_prices(self, keep_hours: float = 48) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM prices WHERE ts < ?", (time.time() - keep_hours * 3600,))
        return cur.rowcount

    def commit(self) -> None:
        self.conn.commit()

    # ---------- подписчики ----------

    def subscribe(self, chat_id: int, platform: str | None = None) -> bool:
        """Подписывает чат (платформа по умолчанию — из настроек). True, если подписчик новый."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO subscribers(chat_id, platform, ts) VALUES (?, ?, ?)",
                (chat_id, platform or self.default_platform, time.time()),
            )
        return cur.rowcount > 0

    def unsubscribe(self, chat_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

    def set_platform(self, chat_id: int, platform: str) -> None:
        """Меняет платформу, подписывая чат, если он ещё не подписан."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO subscribers(chat_id, platform, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET platform=excluded.platform",
                (chat_id, platform, time.time()),
            )

    def platform_of(self, chat_id: int) -> str:
        row = self.conn.execute("SELECT platform FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        return row["platform"] if row else self.default_platform

    def subscribers(self) -> list[tuple[int, str]]:
        rows = self.conn.execute("SELECT chat_id, platform FROM subscribers ORDER BY ts")
        return [(r["chat_id"], r["platform"]) for r in rows]

    def subscribers_for(self, platform: str) -> list[int]:
        rows = self.conn.execute("SELECT chat_id FROM subscribers WHERE platform=? ORDER BY ts", (platform,))
        return [r["chat_id"] for r in rows]

    # ---------- сигналы ----------

    def last_alert(self, player_id: int, platform: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT ts, price, market FROM alerts WHERE player_id=? AND platform=?", (player_id, platform)
        ).fetchone()

    def record_alert(self, player_id: int, platform: str, price: int, market: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO alerts(player_id, platform, ts, price, market) VALUES (?, ?, ?, ?, ?)",
                (player_id, platform, time.time(), price, market),
            )

    def alerts_since(self, hours: float = 24) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (time.time() - hours * 3600,)).fetchone()[0]
