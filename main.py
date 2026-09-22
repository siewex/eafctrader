"""FUT Market Bot: следит за ценами карт EA FC (ПК) на FUTNext и постит в Telegram резкие просадки с профитом.

Запуск: python main.py (настройки в .env, см. .env.example)
"""
import asyncio
import html
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, LinkPreviewOptions, Message

from config import settings
from detector import Detector, Signal
from formatter import coins, format_price_card, format_signal
from futnext import FutnextClient, FutnextError, PlayerInfo
from storage import Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("fut-market")

router = Router()
store: Store
client: FutnextClient
detector: Detector
bot: Bot

_send_lock = asyncio.Lock()
_stats = {"cycle_started": 0.0, "cycle_seconds": 0.0, "checked": 0, "errors": 0, "signals": 0, "alerts": 0, "cycles": 0}


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


# ---------- отправка в канал ----------

async def send_alert(sig: Signal) -> bool:
    text = format_signal(sig, settings.platform)
    async with _send_lock:
        for attempt in range(3):
            try:
                try:
                    await bot.send_photo(settings.channel_id, photo=sig.player.headshot_url, caption=text)
                except TelegramBadRequest as e:  # картинка недоступна/слишком длинный caption — шлём текстом
                    log.warning("send_photo не удался (%s), шлю текстом", e)
                    await bot.send_message(settings.channel_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
                await asyncio.sleep(3)  # лимит Telegram на посты в канал
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                log.exception("Не удалось отправить сигнал по %s", sig.player.title)
                return False
    return False


# ---------- опрос ----------

async def check_player(player: PlayerInfo) -> None:
    try:
        live = await client.get_price(player.id)
    except FutnextError as e:
        _stats["errors"] += 1
        log.debug("цена %s: %s", player.id, e)
        return
    _stats["checked"] += 1
    if not live:
        return
    if live.price <= 0:
        return
    if not detector.price_usable(live):
        store.add_price(player.id, live.price, live.updated_at)
        return
    # максимум за час считаем ДО записи текущего снимка, иначе он включит саму текущую цену
    try:
        recent_ref = await detector.recent_reference(player.id)
    except FutnextError as e:
        _stats["errors"] += 1
        log.debug("история %s: %s", player.id, e)
        return
    store.add_price(player.id, live.price, live.updated_at)
    try:
        sig = await detector.evaluate(player, live, recent_ref)
    except FutnextError as e:
        _stats["errors"] += 1
        log.debug("история %s: %s", player.id, e)
        return
    if not sig:
        return
    _stats["signals"] += 1
    if not detector.should_alert(sig):
        return
    log.info("СИГНАЛ %s: %s vs рынок %s, профит %s (-%.1f%%)", player.title, sig.price, sig.market, sig.profit, sig.drop_percent)
    if await send_alert(sig):
        store.record_alert(player.id, sig.price, sig.market)
        _stats["alerts"] += 1


async def poll_cycle() -> None:
    players = store.all_players()
    if not players:
        log.warning("Watchlist пуст — жду обновления")
        return
    _stats.update(cycle_started=time.time(), checked=0, errors=0, signals=0, alerts=0)
    await asyncio.gather(*(check_player(p) for p in players))
    store.commit()
    _stats["cycle_seconds"] = time.time() - _stats["cycle_started"]
    _stats["cycles"] += 1
    log.info(
        "Круг %d: %d карт за %.0f с, ошибок %d, сигналов %d, отправлено %d",
        _stats["cycles"], _stats["checked"], _stats["cycle_seconds"], _stats["errors"], _stats["signals"], _stats["alerts"],
    )
    if _stats["cycles"] % 20 == 0:
        store.prune_prices()


async def poll_loop() -> None:
    while True:
        try:
            await poll_cycle()
        except Exception:
            log.exception("Ошибка в круге опроса")
        elapsed = time.time() - _stats["cycle_started"] if _stats["cycle_started"] else 0
        await asyncio.sleep(max(10, settings.poll_seconds - elapsed))


async def refresh_watchlist() -> tuple[int, int, int]:
    """Пересобирает автоматический watchlist. Возвращает (всего, добавлено, удалено)."""
    players = await client.players_with_min_rating(settings.min_rating)
    tradeable = [p for p in players if p.tradeable and (p.price or 0) >= settings.min_price]
    added, removed = store.replace_auto_watchlist(tradeable)
    store.set_meta("watchlist_updated", str(time.time()))
    log.info("Watchlist обновлён: %d карт %d+ (торгуемых %d), +%d / -%d", len(players), settings.min_rating, len(tradeable), added, removed)
    return store.count(), added, removed


async def watchlist_loop() -> None:
    while True:
        last = float(store.get_meta("watchlist_updated", "0") or 0)
        due = time.time() - last >= settings.watchlist_refresh_hours * 3600 or store.count() == 0
        if due:
            try:
                await refresh_watchlist()
            except Exception:
                log.exception("Не удалось обновить watchlist")
                await asyncio.sleep(600)
                continue
        await asyncio.sleep(600)


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "Слежу за рынком EA FC (FUTNext, платформа <b>{plat}</b>) и постю в канал карты, "
        "чья текущая цена ниже рыночной на {drop:g}%+ с профитом от {profit} после налога.\n\n"
        "/price &lt;имя&gt; — текущая цена карты из watchlist\n"
        "/id — chat_id и user_id\n"
    ).format(plat=settings.platform.upper(), drop=settings.drop_percent, profit=coins(settings.min_profit))
    if is_admin(message.from_user.id if message.from_user else None):
        text += (
            "\n<b>Админ:</b>\n"
            "/status — состояние\n"
            "/refresh — пересобрать watchlist\n"
            "/watch &lt;id&gt; — добавить карту вручную (id из ссылки futnext.com/players/...)\n"
            "/unwatch &lt;id&gt; — убрать\n"
            "/test &lt;id&gt; — отправить пример поста в канал\n"
        )
    await message.answer(text)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    uid = message.from_user.id if message.from_user else "?"
    await message.answer(f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{uid}</code>")


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    last = float(store.get_meta("watchlist_updated", "0") or 0)
    text = (
        f"Watchlist: <b>{store.count()}</b> карт (рейтинг {settings.min_rating}+, цена от {coins(settings.min_price)})\n"
        f"Обновлён: {time.strftime('%d.%m %H:%M', time.localtime(last)) if last else 'ещё нет'}\n"
        f"Кругов опроса: {_stats['cycles']}, последний: {_stats['checked']} карт за {_stats['cycle_seconds']:.0f} с, "
        f"ошибок {_stats['errors']}, сигналов {_stats['signals']}\n"
        f"Постов за 24ч: {store.alerts_since(24)}\n"
        f"Условия: просадка ≥{settings.drop_percent:g}% к рынку и ≥{settings.fresh_percent:g}% за час, профит ≥{coins(settings.min_profit)}, "
        f"cooldown {settings.alert_cooldown_minutes} мин, опрос каждые {settings.poll_seconds} с"
    )
    await message.answer(text)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Собираю watchlist с FUTNext…")
    try:
        total, added, removed = await refresh_watchlist()
    except Exception as e:
        await message.answer(f"Ошибка: {html.escape(str(e))}")
        return
    await message.answer(f"Готово: {total} карт (+{added} / -{removed})")


def _arg_id(message: Message) -> int | None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().rstrip("/").split("/")[-1].split("?")[0]
    return int(raw) if raw.isdigit() else None


@router.message(Command("watch"))
async def cmd_watch(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    pid = _arg_id(message)
    if not pid:
        await message.answer("Использование: /watch <id или ссылка futnext.com/players/…/id>")
        return
    try:
        p = await client.get_player(pid)
    except FutnextError as e:
        await message.answer(f"FUTNext не ответил: {html.escape(str(e))}")
        return
    if not p:
        await message.answer("Карта не найдена")
        return
    store.upsert_manual(p)
    await message.answer(f"Добавил: {html.escape(p.title)}")


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    pid = _arg_id(message)
    if not pid:
        await message.answer("Использование: /unwatch <id>")
        return
    await message.answer("Убрал" if store.remove(pid) else "Такой карты в watchlist нет")


@router.message(Command("price"))
async def cmd_price(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /price <имя игрока>")
        return
    matches = store.search(parts[1], limit=3)
    if not matches:
        await message.answer("В watchlist таких нет. Админ может добавить через /watch <id>")
        return
    blocks = []
    for p in matches:
        try:
            live = await client.get_price(p.id)
            points = await detector.history(p.id)
        except FutnextError as e:
            blocks.append(f"{html.escape(p.title)}: ошибка {html.escape(str(e))}")
            continue
        blocks.append(format_price_card(
            p, live.price if live else None, live.age_minutes if live else None,
            detector.market_price(points), points, store.last_prices(p.id, 10),
        ))
    await message.answer("\n\n".join(blocks), link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    pid = _arg_id(message)
    p = store.get_player(pid) if pid else None
    if pid and not p:
        try:
            p = await client.get_player(pid)
        except FutnextError:
            p = None
    if not p:
        players = store.all_players()
        p = players[0] if players else None
    if not p:
        await message.answer("Нет карт для примера — сначала /refresh или укажи id")
        return
    live = await client.get_price(p.id)
    points = await detector.history(p.id)
    price = live.price if live and live.price else (p.price or 10000)
    market = detector.market_price(points) or int(price * 1.12)
    recent_ref = await detector.recent_reference(p.id) or market
    sig = Signal(
        player=p, price=price, market=market, profit=detector.profit_after_tax(market, price),
        drop_percent=max(0.0, (market - price) / market * 100), recent_ref=recent_ref,
        fresh_percent=max(0.0, (recent_ref - price) / recent_ref * 100),
        age_minutes=live.age_minutes if live else 0, hour_avg=detector.hour_average(points),
        single_lot=False, history=points[-6:], snapshots=store.last_prices(p.id, 10),
    )
    ok = await send_alert(sig)
    await message.answer("Отправил пример в канал" if ok else "Не получилось отправить — проверь CHANNEL_ID и права бота в канале")


# ---------- запуск ----------

async def main() -> None:
    global store, client, detector, bot
    problems = settings.validate()
    if problems:
        for p in problems:
            log.error(p)
        sys.exit(1)

    store = Store(settings.db_path)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    async with FutnextClient(settings.platform, settings.concurrency) as c:
        client = c
        detector = Detector(settings, store, client)
        me = await bot.get_me()
        await bot.set_my_commands([
            BotCommand(command="price", description="Текущая цена карты"),
            BotCommand(command="status", description="Состояние бота (админ)"),
            BotCommand(command="help", description="Справка"),
        ])
        log.info("Бот @%s запущен, платформа %s, канал %s", me.username, settings.platform, settings.channel_id)
        tasks = [asyncio.create_task(watchlist_loop()), asyncio.create_task(poll_loop())]
        try:
            await dp.start_polling(bot, allowed_updates=["message"])
        finally:
            for t in tasks:
                t.cancel()
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
