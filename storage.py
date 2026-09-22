"""SQLite: watchlist, снимки цен по каждому опросу, отправленные сигналы."""
import sqlite3
import time
from pathlib import Path

from futnext import PlayerInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rating INTEGER NOT NULL,
    position TEXT NOT NULL,
    rarity TEXT NOT NULL,
    club TEXT NOT NULL,
    nation TEXT NOT NULL,
    list_price INTEGER,
    manual INTEGER NOT NULL DEFAULT 0,   -- 1 = добавлен вручную через /watch, не удаляется при обновлении watchlist
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    player_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    price INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_player_ts ON prices(player_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
    player_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    price INTEGER NOT NULL,
    market INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- meta ----------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ---------- watchlist ----------

    def replace_auto_watchlist(self, players: list[PlayerInfo]) -> tuple[int, int]:
        """Заменяет автоматическую часть watchlist. Ручные (manual=1) не трогает. Возвращает (добавлено, удалено)."""
        now = time.time()
        old = {r["id"] for r in self.conn.execute("SELECT id FROM players WHERE manual=0")}
        new_ids = {p.id for p in players}
        with self.conn:
            self.conn.executemany(
                "INSERT INTO players(id, name, rating, position, rarity, club, nation, list_price, manual, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, rating=excluded.rating, position=excluded.position, "
                "rarity=excluded.rarity, club=excluded.club, nation=excluded.nation, list_price=excluded.list_price, updated_at=excluded.updated_at",
                [(p.id, p.name, p.rating, p.position, p.rarity, p.club, p.nation, p.price, now) for p in players],
            )
            removed = old - new_ids
            if removed:
                self.conn.executemany("DELETE FROM players WHERE id=? AND manual=0", [(i,) for i in removed])
        return len(new_ids - old), len(removed)

    def upsert_manual(self, p: PlayerInfo) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO players(id, name, rating, position, rarity, club, nation, list_price, manual, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(id) DO UPDATE SET manual=1, name=excluded.name, rating=excluded.rating, list_price=excluded.list_price, updated_at=excluded.updated_at",
                (p.id, p.name, p.rating, p.position, p.rarity, p.club, p.nation, p.price, time.time()),
            )

    def remove(self, player_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM players WHERE id=?", (player_id,))
        return cur.rowcount > 0

    def all_players(self) -> list[PlayerInfo]:
        return [self._row_to_player(r) for r in self.conn.execute("SELECT * FROM players ORDER BY rating DESC, name")]

    def get_player(self, player_id: int) -> PlayerInfo | None:
        row = self.conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
        return self._row_to_player(row) if row else None

    def search(self, query: str, limit: int = 5) -> list[PlayerInfo]:
        rows = self.conn.execute(
            "SELECT * FROM players WHERE lower(name) LIKE ? ORDER BY rating DESC LIMIT ?", (f"%{query.lower()}%", limit)
        )
        return [self._row_to_player(r) for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]

    @staticmethod
    def _row_to_player(r: sqlite3.Row) -> PlayerInfo:
        return PlayerInfo(
            id=r["id"], name=r["name"], rating=r["rating"], position=r["position"], rarity=r["rarity"],
            club=r["club"], nation=r["nation"], price=r["list_price"],
        )

    # ---------- цены ----------

    def add_price(self, player_id: int, price: int, ts: float | None = None) -> None:
        self.conn.execute("INSERT INTO prices(player_id, ts, price) VALUES (?, ?, ?)", (player_id, ts or time.time(), price))

    def last_price(self, player_id: int) -> int | None:
        row = self.conn.execute("SELECT price FROM prices WHERE player_id=? ORDER BY ts DESC LIMIT 1", (player_id,)).fetchone()
        return row["price"] if row else None

    def recent_prices(self, player_id: int, hours: float = 24) -> list[tuple[float, int]]:
        since = time.time() - hours * 3600
        return [
            (r["ts"], r["price"])
            for r in self.conn.execute("SELECT ts, price FROM prices WHERE player_id=? AND ts>=? ORDER BY ts", (player_id, since))
        ]

    def last_prices(self, player_id: int, n: int = 10) -> list[tuple[float, int]]:
        """Последние n замеров (от старых к новым) — наш аналог \"последних продаж\" Futbin."""
        rows = self.conn.execute(
            "SELECT ts, price FROM prices WHERE player_id=? ORDER BY ts DESC LIMIT ?", (player_id, n)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in reversed(rows)]

    def prune_prices(self, keep_hours: float = 48) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM prices WHERE ts < ?", (time.time() - keep_hours * 3600,))
        return cur.rowcount

    def commit(self) -> None:
        self.conn.commit()

    # ---------- подписчики ----------

    def subscribe(self, chat_id: int) -> bool:
        """True, если подписчик новый."""
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO subscribers(chat_id, ts) VALUES (?, ?)", (chat_id, time.time()))
        return cur.rowcount > 0

    def unsubscribe(self, chat_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        return cur.rowcount > 0

    def subscribers(self) -> list[int]:
        return [r["chat_id"] for r in self.conn.execute("SELECT chat_id FROM subscribers ORDER BY ts")]

    # ---------- сигналы ----------

    def last_alert(self, player_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT ts, price, market FROM alerts WHERE player_id=?", (player_id,)).fetchone()

    def record_alert(self, player_id: int, price: int, market: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO alerts(player_id, ts, price, market) VALUES (?, ?, ?, ?)", (player_id, time.time(), price, market)
            )

    def alerts_since(self, hours: float = 24) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (time.time() - hours * 3600,)).fetchone()[0]
