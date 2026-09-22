"""Логика сигнала.

Условия (все сразу):
  1. текущая минимальная цена (лот) свежая и не ниже FUT_MIN_PRICE;
  2. она ниже рыночной (медиана минимальных цен за 24ч, без часов, когда карты не было в продаже) на FUT_DROP_PERCENT+;
  3. профит после налога EA 5% от рыночной >= FUT_MIN_PROFIT;
  4. спад свежий: цена ниже максимума за последний час (по нашим снимкам, на старте — по истории FUTNext) на FUT_FRESH_PERCENT+.
     Без этого бот постил бы карты, которые просто давно стоят дёшево.

Метка "одиночный лот": в истории FUTNext averagePrice — средняя по замерам минимального лота за час, cheapestPrice — самый
низкий замер. В норме они совпадают (разрыв ~1%). Если текущая цена ниже средней за час на FUT_SINGLE_LOT_GAP+, значит кто-то
один выставил дёшево, а остальные лоты стоят как раньше — и на эту цену не стоит ориентироваться при продаже.
"""
import logging
import statistics
import time
from dataclasses import dataclass, field

from config import Settings
from futnext import FutnextClient, HistoryPoint, LivePrice, PlayerInfo
from storage import Store

log = logging.getLogger("detector")

EA_TAX = 0.05
HISTORY_TTL = 3600  # история почасовая, чаще раза в час тянуть смысла нет
EXTINCT_PRICE = 14_990_000  # ~потолок цены EA: такие точки значат "лотов не было", а не реальную цену


@dataclass
class Signal:
    player: PlayerInfo
    price: int          # текущая минимальная цена
    market: int         # рыночная (медиана минимальных за 24ч)
    profit: int         # market*(1-tax) - price
    drop_percent: float  # насколько ниже рыночной
    recent_ref: int     # максимум цены за последний час
    fresh_percent: float  # насколько ниже recent_ref
    age_minutes: float
    hour_avg: int | None = None       # средняя по лотам за текущий час (FUTNext)
    single_lot: bool = False          # похоже на одиночный слив, а не движение рынка
    history: list[HistoryPoint] = field(default_factory=list)
    snapshots: list[tuple[float, int]] = field(default_factory=list)  # наши последние замеры (ts, price)


def valid_points(points: list[HistoryPoint]) -> list[HistoryPoint]:
    return [p for p in points if 0 < p.cheapest < EXTINCT_PRICE]


class Detector:
    def __init__(self, settings: Settings, store: Store, client: FutnextClient):
        self.s = settings
        self.store = store
        self.client = client
        self._history: dict[int, tuple[float, list[HistoryPoint]]] = {}  # player_id -> (fetched_at, points)

    async def history(self, player_id: int) -> list[HistoryPoint]:
        cached = self._history.get(player_id)
        if cached and time.time() - cached[0] < HISTORY_TTL:
            return cached[1]
        points = await self.client.get_history(player_id)
        self._history[player_id] = (time.time(), points)
        return points

    @staticmethod
    def market_price(points: list[HistoryPoint], hours: float = 24) -> int | None:
        since = time.time() - hours * 3600
        values = [p.cheapest for p in valid_points(points) if p.ts >= since]
        if len(values) < 3:
            return None
        return int(statistics.median(values))

    @staticmethod
    def profit_after_tax(market: int, price: int) -> int:
        return int(market * (1 - EA_TAX)) - price

    async def recent_reference(self, player_id: int, hours: float = 1) -> int | None:
        """Максимум цены за последний час: по нашим снимкам, а если их ещё нет — по последним точкам истории FUTNext."""
        snaps = [price for _, price in self.store.recent_prices(player_id, hours)]
        if snaps:
            return max(snaps)
        points = await self.history(player_id)
        since = time.time() - 2 * 3600
        recent = [p.cheapest for p in valid_points(points) if p.ts >= since]
        return max(recent) if recent else None

    @staticmethod
    def hour_average(points: list[HistoryPoint]) -> int | None:
        """Средняя по лотам за текущий (последний) час истории."""
        recent = [p for p in valid_points(points) if p.ts >= time.time() - 2 * 3600 and p.average]
        return recent[-1].average if recent else None

    def price_usable(self, live: LivePrice) -> bool:
        return live.price >= self.s.min_price and live.age_minutes <= self.s.max_price_age_minutes

    async def evaluate(self, player: PlayerInfo, live: LivePrice, recent_ref: int | None = None) -> Signal | None:
        """Signal, если условия выполнены, иначе None. Cooldown не учитывает — это should_alert().

        recent_ref передаётся вызывающим ДО записи текущего снимка (иначе максимум за час включит саму текущую цену).
        """
        if not self.price_usable(live):
            return None
        points = await self.history(player.id)
        market = self.market_price(points)
        if not market or market <= live.price:
            return None
        drop = (market - live.price) / market * 100
        profit = self.profit_after_tax(market, live.price)
        if drop < self.s.drop_percent or profit < self.s.min_profit:
            return None
        if recent_ref is None:
            recent_ref = await self.recent_reference(player.id)
        if not recent_ref:
            return None
        fresh = (recent_ref - live.price) / recent_ref * 100
        if fresh < self.s.fresh_percent:
            return None
        hour_avg = self.hour_average(points)
        single_lot = bool(hour_avg) and (hour_avg - live.price) / hour_avg * 100 >= self.s.single_lot_gap
        if single_lot and self.s.skip_single_lot:
            return None
        return Signal(
            player=player, price=live.price, market=market, profit=profit, drop_percent=drop,
            recent_ref=recent_ref, fresh_percent=fresh, age_minutes=live.age_minutes,
            hour_avg=hour_avg, single_lot=single_lot, history=points[-6:],
            snapshots=self.store.last_prices(player.id, 8),
        )

    def should_alert(self, sig: Signal) -> bool:
        """Антиспам: одна карта не чаще раза в cooldown, если только цена не упала ещё на 5%+ от прошлого сигнала."""
        last = self.store.last_alert(sig.player.id)
        if not last:
            return True
        if time.time() - last["ts"] >= self.s.alert_cooldown_minutes * 60:
            return True
        return sig.price <= last["price"] * 0.95
