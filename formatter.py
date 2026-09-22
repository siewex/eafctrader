"""Текст поста в Telegram (HTML)."""
import html
from datetime import datetime, timezone

from detector import Signal
from futnext import HistoryPoint, PlayerInfo


def coins(n: int | None) -> str:
    return f"{n:,}" if n is not None else "—"


def ago(minutes: float) -> str:
    m = int(minutes)
    if m < 1:
        return "только что"
    if m < 60:
        return f"{m} мин назад"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин назад"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def history_block(points: list[HistoryPoint], n: int = 10) -> str:
    if not points:
        return ""
    lines = [f"{coins(p.cheapest or p.average):>10}  {_utc(p.ts)[:16]}" for p in reversed(points[-n:])]
    return "Мин. цена по часам (UTC±0:00) ⏱\n<pre>" + "\n".join(lines) + "</pre>"


def snapshots_block(snapshots: list[tuple[float, int]]) -> str:
    """Наши замеры минимального лота каждые пару минут — аналог «последних продаж» Futbin."""
    if len(snapshots) < 2:
        return ""
    lines = [f"{coins(price):>10}  {_utc(ts)}" for ts, price in reversed(snapshots)]
    return "Последние замеры мин. лота (UTC±0:00) ⏱\n<pre>" + "\n".join(lines) + "</pre>"


def format_signal(sig: Signal, platform: str) -> str:
    p = sig.player
    title = html.escape(p.title)
    single_lot = (
        "\n⚠️ <b>Похоже на одиночный лот</b> — остальные лоты стоят около средней за час, "
        "при продаже ориентируйся на неё, а не на эту цену.\n"
        if sig.single_lot else ""
    )
    hour_avg = f"<u>Средняя по лотам за час:</u> {coins(sig.hour_avg)}\n" if sig.hour_avg else ""
    blocks = "\n\n".join(b for b in (snapshots_block(sig.snapshots), history_block(sig.history, 5)) if b)
    return (
        f"<a href=\"{p.page_url}\">{title}</a> · {platform.upper()}\n\n"
        f"<u>Новая цена:</u> <b>{coins(sig.price)}</b> 💰\n"
        f"<u>Профит:</u> <b>{coins(sig.profit)}</b> 📈 <i>(после налога 5%)</i>\n\n"
        f"<u>Изменение:</u> -{sig.drop_percent:.2f}% 🔻 <i>(к рыночной)</i>\n"
        f"<u>За последний час:</u> -{sig.fresh_percent:.1f}% <i>(было {coins(sig.recent_ref)})</i>\n"
        f"<u>Обновлено:</u> {ago(sig.age_minutes)}\n\n"
        f"<u>Рыночная цена:</u> {coins(sig.market)} 💰 <i>(медиана 24ч)</i>\n"
        f"{hour_avg}{single_lot}\n{blocks}"
    )


def format_price_card(p: PlayerInfo, price: int | None, age_minutes: float | None, market: int | None,
                      points: list[HistoryPoint], snapshots: list[tuple[float, int]] = ()) -> str:
    title = html.escape(p.title)
    lines = [f"<a href=\"{p.page_url}\">{title}</a>"]
    current = coins(price) if price else "нет лотов"
    suffix = f" ({ago(age_minutes)})" if age_minutes is not None and price else ""
    lines.append(f"<u>Текущая мин. цена:</u> <b>{current}</b>{suffix}")
    if market:
        lines.append(f"<u>Рыночная (медиана 24ч):</u> {coins(market)}")
        if price:
            diff = (price - market) / market * 100
            lines.append(f"<u>Отклонение:</u> {diff:+.1f}%")
    for block in (snapshots_block(list(snapshots)), history_block(points, 6)):
        if block:
            lines.append("")
            lines.append(block)
    return "\n".join(lines)
