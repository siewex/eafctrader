"""Клиент публичного API FUTNext (без ключей).

Проверено на FC 27 (сентябрь 2026):
  GET  enhancer-api.futnext.com/players/prices?ids=ID&platform=pc  - текущая минимальная цена (один ID за запрос)
  GET  client-api.futnext.com/players/ID/price-history?platform=pc - почасовая история avg/cheapest
  GET  client-api.futnext.com/players/ID                            - карточка игрока
  POST client-api.futnext.com/players?platform=pc&page=N            - список всех карт, 48 на страницу, по рейтингу вниз
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("futnext")

CLIENT_API = "https://client-api.futnext.com"
ENHANCER_API = "https://enhancer-api.futnext.com"
ASSETS = "https://game-assets.futnext.com"

POSITIONS = {
    0: "GK", 1: "SW", 2: "RWB", 3: "RB", 4: "RCB", 5: "CB", 6: "LCB", 7: "LB", 8: "LWB", 9: "RDM", 10: "CDM",
    11: "LDM", 12: "RM", 13: "RCM", 14: "CM", 15: "LCM", 16: "LM", 17: "RAM", 18: "CAM", 19: "LAM", 20: "RF",
    21: "CF", 22: "LF", 23: "RW", 24: "RS", 25: "ST", 26: "LS", 27: "LW",
}


@dataclass
class PlayerInfo:
    id: int
    name: str
    rating: int
    position: str
    rarity: str
    club: str
    nation: str
    price: int | None  # цена из списка (почасовой снимок), None = не торгуется/нет лотов

    @property
    def tradeable(self) -> bool:
        return bool(self.price)

    @property
    def title(self) -> str:
        return " ".join(x for x in (self.name, self.rarity, str(self.rating), self.position) if x)

    @property
    def headshot_url(self) -> str:
        return f"{ASSETS}/players/{self.id}.png?v=2027"

    @property
    def page_url(self) -> str:
        return f"https://www.futnext.com/players/{self.id}"


@dataclass
class LivePrice:
    player_id: int
    price: int  # 0 = нет лотов
    updated_at: float  # unix seconds

    @property
    def age_minutes(self) -> float:
        return max(0.0, (time.time() - self.updated_at) / 60)


@dataclass
class HistoryPoint:
    ts: float
    average: int
    cheapest: int


class FutnextError(Exception):
    pass


class FutnextClient:
    def __init__(self, platform: str = "pc", concurrency: int = 4, timeout: int = 20):
        self.platform = platform
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=self._timeout,
            headers={"User-Agent": "fut-market-bot/1.0 (telegram price alerts)", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def _request(self, method: str, url: str, *, params=None, json=None, retries: int = 3):
        assert self._session, "нужно открыть клиент через async with"
        last: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._sem:
                    async with self._session.request(method, url, params=params, json=json) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            raise FutnextError(f"HTTP {resp.status} {url}")
                        if resp.status == 404:
                            return None
                        if resp.status >= 400:
                            text = await resp.text()
                            raise FutnextError(f"HTTP {resp.status} {url}: {text[:200]}")
                        return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, FutnextError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise FutnextError(f"{url}: {last}")

    # ---------- парсинг ----------

    @staticmethod
    def _parse_player(raw: dict) -> PlayerInfo:
        d = raw.get("definition") or {}
        name = d.get("commonName") or " ".join(x for x in (d.get("firstName"), d.get("lastName")) if x) or f"#{raw.get('id')}"
        positions = raw.get("positions") or []
        preferred = next((p for p in positions if p.get("isPreferred")), positions[0] if positions else {})
        price_raw = raw.get("price") or {}
        cheapest = price_raw.get("cheapestPrice") if isinstance(price_raw, dict) else None
        return PlayerInfo(
            id=int(raw["id"]),
            name=name,
            rating=int(raw.get("rating") or 0),
            position=POSITIONS.get(preferred.get("id"), "?"),
            rarity=(raw.get("rarity") or {}).get("name") or "",
            club=(raw.get("club") or {}).get("name") or "",
            nation=(raw.get("nation") or {}).get("name") or "",
            price=int(cheapest) if cheapest else None,
        )

    # ---------- публичные методы ----------

    async def list_page(self, page: int) -> tuple[list[PlayerInfo], bool]:
        """Страница списка карт (48 шт., по рейтингу вниз). Возвращает (игроки, есть_ли_следующая)."""
        data = await self._request("POST", f"{CLIENT_API}/players", params={"platform": self.platform, "page": page}, json={})
        players = [self._parse_player(p) for p in (data or {}).get("players", [])]
        return players, bool((data or {}).get("hasNext"))

    async def players_with_min_rating(self, min_rating: int, max_pages: int = 200) -> list[PlayerInfo]:
        """Все карты с рейтингом >= min_rating. Список отсортирован по рейтингу, идём страницами, пока не упадём ниже."""
        out: list[PlayerInfo] = []
        for page in range(1, max_pages + 1):
            players, has_next = await self.list_page(page)
            if not players:
                break
            out.extend(p for p in players if p.rating >= min_rating)
            if players[-1].rating < min_rating or not has_next:
                break
        return out

    async def get_player(self, player_id: int) -> PlayerInfo | None:
        data = await self._request("GET", f"{CLIENT_API}/players/{player_id}", params={"platform": self.platform})
        return self._parse_player(data) if data else None

    async def get_price(self, player_id: int) -> LivePrice | None:
        data = await self._request("GET", f"{ENHANCER_API}/players/prices", params={"ids": player_id, "platform": self.platform})
        if not data:
            return None
        row = data[0] if isinstance(data, list) else data
        prices = row.get("prices") or []
        price = int(prices[0]) if prices and prices[0] else 0
        updated = row.get("updatedAt") or 0
        return LivePrice(player_id=int(row.get("definitionId") or player_id), price=price, updated_at=updated / 1000)

    async def get_history(self, player_id: int) -> list[HistoryPoint]:
        data = await self._request("GET", f"{CLIENT_API}/players/{player_id}/price-history", params={"platform": self.platform})
        points = []
        for p in (data or {}).get("historicalPrices", []):
            if p.get("averagePrice") or p.get("cheapestPrice"):
                points.append(HistoryPoint(ts=p["timeStamp"] / 1000, average=int(p.get("averagePrice") or 0), cheapest=int(p.get("cheapestPrice") or 0)))
        points.sort(key=lambda x: x.ts)
        return points
