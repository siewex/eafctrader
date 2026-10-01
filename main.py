"""FUT Market Bot: следит за ценами карт EA FC на FUTNext и присылает в Telegram резкие просадки с профитом.

Каждый подписчик выбирает свою платформу (PC или PS) командой /platform — это разные рынки с разными ценами,
поэтому бот опрашивает каждую платформу, на которую кто-то подписан, отдельно.

Запуск: python main.py (настройки в .env, см. .env.example)
"""
import asyncio
import html
import logging
import os
import sys
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from config import settings
from detector import Detector, Signal
from formatter import coins, fmt_time, format_price_card, format_signal
from futnext import FutnextClient, FutnextError, PlayerInfo
from storage import Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("fut-market")

PLATFORMS = ("pc", "ps")
PLATFORM_NAMES = {"pc": "PC", "ps": "PlayStation / Xbox"}
WATCHLIST_HEADROOM = 1.5  # во сколько раз выше бюджета берём карты в watchlist (см. refresh_watchlist)

router = Router()
store: Store
clients: dict[str, FutnextClient] = {}
detectors: dict[str, Detector] = {}
bot: Bot

_send_lock = asyncio.Lock()
_stats: dict[str, dict] = {}


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def stats_for(platform: str) -> dict:
    return _stats.setdefault(platform, {"cycle_started": 0.0, "cycle_seconds": 0.0, "checked": 0, "errors": 0, "signals": 0, "alerts": 0, "cycles": 0})


def platform_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=("✅ " if current == p else "") + PLATFORM_NAMES[p], callback_data=f"platform:{p}")
        for p in PLATFORMS
    ]])


def active_platforms() -> list[str]:
    """Платформы, на которые кто-то подписан. Если подписчиков нет — платформа по умолчанию (для канала)."""
    used = {p for _, p in store.subscribers()}
    used |= {store.platform_of(c) for c in permanent_chats()}
    if settings.channel_id:
        used.add(settings.platform)
    return sorted(used) or [settings.platform]


# ---------- отправка ----------

async def notify_admins(text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            log.debug("не удалось уведомить админа %s", admin_id)


def permanent_chats() -> set[int]:
    """Получатели из настроек: SUBSCRIBER_IDS и (по умолчанию) админы."""
    chats = set(settings.subscriber_ids)
    if settings.subscribe_admins:
        chats |= settings.admin_ids
    return chats


def recipients(platform: str) -> list[int]:
    """Подписчики этой платформы, постоянные получатели из настроек и канал.

    Постоянные берутся из настроек, а не из базы: сигналы дойдут, даже если market.db
    на хостинге сбросился или не записывается.
    """
    chats = store.subscribers_for(platform)
    for chat_id in sorted(permanent_chats()):
        if chat_id not in chats and store.platform_of(chat_id) == platform:
            chats.append(chat_id)
    if settings.channel_id and platform == settings.platform and settings.channel_id not in chats:
        chats.append(settings.channel_id)
    return chats


async def send_to(chat_id: int, sig: Signal, text: str) -> bool:
    for attempt in range(3):
        try:
            try:
                await bot.send_photo(chat_id, photo=sig.player.headshot_url, caption=text)
            except TelegramBadRequest as e:  # картинка недоступна/слишком длинный caption — шлём текстом
                log.warning("send_photo в %s не удался (%s), шлю текстом", chat_id, e)
                await bot.send_message(chat_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True))
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:  # пользователь заблокировал бота — убираем из подписчиков
            if chat_id not in settings.subscriber_ids and store.unsubscribe(chat_id):
                log.info("Подписчик %s заблокировал бота, удалён", chat_id)
            return False
        except Exception:
            log.exception("Не удалось отправить сигнал по %s в %s", sig.player.title, chat_id)
            return False
    return False


async def send_alert(sig: Signal, platform: str, only_to: int | None = None) -> int:
    """Рассылает сигнал подписчикам платформы (или одному чату). Возвращает число успешных отправок."""
    text = format_signal(sig, platform)
    chats = [only_to] if only_to else recipients(platform)
    if not chats:
        log.warning(
            "[%s] СИГНАЛ %s некому отправить: подписчиков нет. Напиши боту /start или задай SUBSCRIBER_IDS",
            platform, sig.player.title,
        )
        return 0
    sent = 0
    async with _send_lock:
        for chat_id in chats:
            if await send_to(chat_id, sig, text):
                sent += 1
            if len(chats) > 1:
                await asyncio.sleep(0.5)  # лимит Telegram ~30 сообщений/сек
    return sent


# ---------- опрос ----------

async def check_player(player: PlayerInfo, platform: str) -> None:
    client, detector, stats = clients[platform], detectors[platform], stats_for(platform)
    try:
        live = await client.get_price(player.id)
    except FutnextError as e:
        stats["errors"] += 1
        log.debug("цена %s (%s): %s", player.id, platform, e)
        return
    stats["checked"] += 1
    if not live or live.price <= 0:
        return
    if not detector.price_usable(live):
        store.add_price(player.id, platform, live.price, live.updated_at)
        return
    # максимум за час считаем ДО записи текущего снимка, иначе он включит саму текущую цену
    try:
        recent_ref = await detector.recent_reference(player.id)
        store.add_price(player.id, platform, live.price, live.updated_at)
        sig = await detector.evaluate(player, live, recent_ref)
    except FutnextError as e:
        stats["errors"] += 1
        log.debug("история %s (%s): %s", player.id, platform, e)
        return
    if not sig:
        return
    stats["signals"] += 1
    if stats["cycles"] < settings.warmup_cycles:  # прогрев: копим свои замеры, чтобы не слать всё подряд после старта
        return
    if not detector.should_alert(sig):
        return
    log.info("СИГНАЛ [%s] %s: %s vs рынок %s, профит %s (-%.1f%%)", platform, player.title, sig.price, sig.market, sig.profit, sig.drop_percent)
    if await send_alert(sig, platform) > 0:
        store.record_alert(player.id, platform, sig.price, sig.market)
        store.set_meta("last_sent", str(time.time()))
        stats["alerts"] += 1


async def poll_platform(platform: str) -> None:
    players = store.all_players(platform)
    if not players:
        log.warning("Watchlist [%s] пуст — жду обновления", platform)
        return
    stats = stats_for(platform)
    stats.update(cycle_started=time.time(), checked=0, errors=0, signals=0, alerts=0)
    await asyncio.gather(*(check_player(p, platform) for p in players))
    store.commit()
    stats["cycle_seconds"] = time.time() - stats["cycle_started"]
    stats["cycles"] += 1
    warmup = " (прогрев, не отправляю)" if stats["cycles"] <= settings.warmup_cycles else ""
    log.info(
        "Круг [%s] %d: %d карт за %.0f с, ошибок %d, сигналов %d, отправлено %d%s",
        platform, stats["cycles"], stats["checked"], stats["cycle_seconds"], stats["errors"], stats["signals"], stats["alerts"], warmup,
    )
    if stats["cycles"] % 20 == 0:
        store.prune_prices()


async def poll_loop() -> None:
    while True:
        started = time.time()
        try:
            for platform in active_platforms():
                await poll_platform(platform)
        except Exception:
            log.exception("Ошибка в круге опроса")
        await asyncio.sleep(max(10, settings.poll_seconds - (time.time() - started)))


async def refresh_watchlist(platform: str) -> tuple[int, int, int]:
    """Пересобирает автоматический watchlist платформы. Возвращает (всего, добавлено, удалено)."""
    players = await clients[platform].players_with_min_rating(settings.min_rating)
    # запас над бюджетом: карта, которая обычно стоит чуть дороже потолка, при обвале может в него попасть
    ceiling = settings.max_price * WATCHLIST_HEADROOM if settings.max_price else 0
    tradeable = [
        p for p in players
        if p.tradeable and (p.price or 0) >= settings.min_price and (not ceiling or p.price <= ceiling)
    ]
    added, removed = store.replace_auto_watchlist(tradeable, platform)
    store.set_meta(f"watchlist_updated:{platform}", str(time.time()))
    log.info("Watchlist [%s] обновлён: %d карт %d+ (в бюджете %d), +%d / -%d", platform, len(players), settings.min_rating, len(tradeable), added, removed)
    return store.count(platform), added, removed


async def watchlist_loop() -> None:
    while True:
        for platform in active_platforms():
            last = float(store.get_meta(f"watchlist_updated:{platform}", "0") or 0)
            if time.time() - last >= settings.watchlist_refresh_hours * 3600 or store.count(platform) == 0:
                try:
                    await refresh_watchlist(platform)
                except Exception:
                    log.exception("Не удалось обновить watchlist [%s]", platform)
        await asyncio.sleep(600)


# ---------- команды ----------

def chat_platform(message: Message) -> str:
    return store.platform_of(message.chat.id)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    new = store.subscribe(message.chat.id)
    platform = chat_platform(message)
    log.info("/start от %s (%s): %s, всего подписчиков %d", message.chat.id, platform, "новый" if new else "уже был", len(store.subscribers()))
    if new:
        who = html.escape(message.from_user.full_name) if message.from_user else "чат"
        await notify_admins(
            f"➕ Новый подписчик: <code>{message.chat.id}</code> ({who}), платформа {platform.upper()}.\n"
            f"Всего: {len(store.subscribers())}. Список для настроек — /subs"
        )
    await message.answer(
        ("✅ Подписал. " if new else "Ты уже подписан. ")
        + "Буду присылать сюда карты, чья цена резко упала ниже рыночной.\n"
        f"Платформа: <b>{PLATFORM_NAMES[platform]}</b> — поменять можно кнопкой ниже или командой /platform.\n"
        "Отписаться — /stop, справка — /help",
        reply_markup=platform_keyboard(platform),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    await message.answer("Отписал. Вернуться — /start" if store.unsubscribe(message.chat.id) else "Ты и не был подписан. Подписаться — /start")


@router.message(Command("platform"))
async def cmd_platform(message: Message) -> None:
    """Переключение платформы: /platform ps или кнопками."""
    arg = (message.text or "").split(maxsplit=1)
    choice = arg[1].strip().lower() if len(arg) > 1 else ""
    if choice in {"ps", "playstation", "xbox", "console", "консоль", "пс"}:
        choice = "ps"
    elif choice in {"pc", "пк"}:
        choice = "pc"
    else:
        choice = ""
    if choice:
        store.set_platform(message.chat.id, choice)
        await message.answer(f"Платформа: <b>{PLATFORM_NAMES[choice]}</b>. Сигналы теперь по ценам этого рынка.", reply_markup=platform_keyboard(choice))
        return
    current = chat_platform(message)
    await message.answer(f"Сейчас: <b>{PLATFORM_NAMES[current]}</b>. Выбери платформу:", reply_markup=platform_keyboard(current))


@router.callback_query(F.data.startswith("platform:"))
async def cb_platform(call: CallbackQuery) -> None:
    choice = call.data.split(":", 1)[1]
    if choice not in PLATFORMS or not call.message:
        await call.answer()
        return
    store.set_platform(call.message.chat.id, choice)
    await call.answer(f"Платформа: {PLATFORM_NAMES[choice]}")
    try:
        await call.message.edit_reply_markup(reply_markup=platform_keyboard(choice))
    except TelegramBadRequest:
        pass
    await call.message.answer(f"Платформа: <b>{PLATFORM_NAMES[choice]}</b>. Сигналы теперь по ценам этого рынка.")


@router.message(Command("subs"))
async def cmd_subs(message: Message) -> None:
    """Список подписчиков и готовая строка SUBSCRIBER_IDS, чтобы восстановить их после сброса базы."""
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    subs = store.subscribers()
    perm = permanent_chats()
    if not subs and not perm:
        await message.answer("Подписчиков нет.")
        return
    lines = [
        f"<code>{chat_id}</code> — {PLATFORM_NAMES[platform]}" + (" · из настроек" if chat_id in perm else "")
        for chat_id, platform in subs
    ]
    known = {c for c, _ in subs}
    lines += [f"<code>{c}</code> — только в настройках" for c in sorted(perm) if c not in known]
    plat_of = {c: p for c, p in subs}
    everyone = ",".join(
        f"{c}:{plat_of.get(c, settings.platform)}" for c in sorted(known | perm)
    )
    await message.answer(
        f"<b>Подписчики ({len(lines)}):</b>\n" + "\n".join(lines)
        + "\n\nЧтобы подписки пережили сброс базы, впиши в настройки хостинга:\n"
        f"<code>SUBSCRIBER_IDS={everyone}</code>"
    )


@router.message(Command("sub"))
async def cmd_sub(message: Message) -> None:
    """/sub <chat_id> [pc|ps] — подписать кого-то вручную."""
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /sub &lt;chat_id&gt; [pc|ps]")
        return
    chat_id = int(parts[1])
    platform = parts[2].lower() if len(parts) > 2 and parts[2].lower() in PLATFORMS else settings.platform
    store.set_platform(chat_id, platform)
    await message.answer(f"Подписал <code>{chat_id}</code> ({PLATFORM_NAMES[platform]}). Всего: {len(store.subscribers())}")


@router.message(Command("unsub"))
async def cmd_unsub(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /unsub &lt;chat_id&gt;")
        return
    chat_id = int(parts[1])
    ok = store.unsubscribe(chat_id)
    note = " Он есть в SUBSCRIBER_IDS и вернётся при перезапуске — убери его оттуда." if chat_id in permanent_chats() else ""
    await message.answer(("Отписал." if ok else "Такого подписчика нет.") + note)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    platform = chat_platform(message)
    text = (
        "Слежу за рынком EA FC (FUTNext) и присылаю карты, "
        "чья текущая цена ниже рыночной на {drop:g}%+ с профитом от {profit} после налога.\n"
        "Твоя платформа: <b>{plat}</b>. Бюджет: до {budget} за карту.\n\n"
        "/platform — переключить PC / PlayStation\n"
        "/start — подписаться на сигналы\n"
        "/stop — отписаться\n"
        "/price &lt;имя&gt; — текущая цена карты из watchlist\n"
        "/id — chat_id и user_id\n"
    ).format(
        plat=PLATFORM_NAMES[platform], drop=settings.drop_percent, profit=coins(settings.min_profit),
        budget=coins(settings.max_price) if settings.max_price else "без ограничений",
    )
    if is_admin(message.from_user.id if message.from_user else None):
        text += (
            "\n<b>Админ:</b>\n"
            "/status — состояние\n"
            "/refresh — пересобрать watchlist\n"
            "/watch &lt;id&gt; — добавить карту вручную (id из ссылки futnext.com/players/...)\n"
            "/unwatch &lt;id&gt; — убрать\n"
            "/test &lt;id&gt; — прислать пример поста сюда\n"
            "/subs — кто подписан (+ строка для SUBSCRIBER_IDS)\n"
            "/sub &lt;chat_id&gt; [pc|ps] — подписать вручную\n"
            "/unsub &lt;chat_id&gt; — отписать\n"
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
    lines = []
    for platform in active_platforms():
        last = float(store.get_meta(f"watchlist_updated:{platform}", "0") or 0)
        st = stats_for(platform)
        lines.append(
            f"<b>{PLATFORM_NAMES[platform]}</b>: {store.count(platform)} карт, "
            f"обновлён {fmt_time(last, with_seconds=False) + ' ' + settings.tz_label if last else 'ещё нет'}\n"
            f"  кругов {st['cycles']}, последний: {st['checked']} карт за {st['cycle_seconds']:.0f} с, "
            f"ошибок {st['errors']}, сигналов {st['signals']}\n"
            f"  подписчиков: {len(store.subscribers_for(platform))}"
        )
    subs = store.subscribers()
    last_sent = float(store.get_meta("last_sent", "0") or 0)
    warming = [p for p in active_platforms() if stats_for(p)["cycles"] < settings.warmup_cycles]
    await message.answer(
        "\n".join(lines) + "\n\n"
        + ("⚠️ <b>Подписчиков нет</b> — сигналы никому не уйдут, напиши /start\n" if not subs and not settings.channel_id else "")
        + ("⏳ Идёт прогрев (" + ", ".join(warming) + ") — сигналы пока не отправляются\n" if warming else "")
        + f"Всего подписчиков: {len(subs)}" + (f", канал {settings.channel_id}" if settings.channel_id else "") + "\n"
        f"Последний отправленный сигнал: {fmt_time(last_sent, with_seconds=False) + ' ' + settings.tz_label if last_sent else 'ещё не было'}\n"
        f"Постов за 24ч: {store.alerts_since(24)}\n"
        f"Фильтры: рейтинг {settings.min_rating}+, цена {coins(settings.min_price)}–{coins(settings.max_price) if settings.max_price else '∞'}\n"
        f"Условия: просадка ≥{settings.drop_percent:g}% к рынку и ≥{settings.fresh_percent:g}% за час, профит ≥{coins(settings.min_profit)}, "
        f"cooldown {settings.alert_cooldown_minutes} мин, опрос каждые {settings.poll_seconds} с, прогрев {settings.warmup_cycles} кругов"
    )


@router.message(Command("refresh"))
async def cmd_refresh(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Собираю watchlist с FUTNext…")
    out = []
    for platform in active_platforms():
        try:
            total, added, removed = await refresh_watchlist(platform)
            out.append(f"{PLATFORM_NAMES[platform]}: {total} карт (+{added} / -{removed})")
        except Exception as e:
            out.append(f"{PLATFORM_NAMES[platform]}: ошибка {html.escape(str(e))}")
    await message.answer("\n".join(out))


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
    platform = chat_platform(message)
    try:
        p = await clients[platform].get_player(pid)
    except FutnextError as e:
        await message.answer(f"FUTNext не ответил: {html.escape(str(e))}")
        return
    if not p:
        await message.answer("Карта не найдена")
        return
    store.upsert_manual(p, platform)
    await message.answer(f"Добавил ({PLATFORM_NAMES[platform]}): {html.escape(p.title)}")


@router.message(Command("unwatch"))
async def cmd_unwatch(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    pid = _arg_id(message)
    if not pid:
        await message.answer("Использование: /unwatch <id>")
        return
    await message.answer("Убрал" if store.remove(pid, chat_platform(message)) else "Такой карты в watchlist нет")


@router.message(Command("price"))
async def cmd_price(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /price <имя игрока>")
        return
    platform = chat_platform(message)
    matches = store.search(parts[1], platform, limit=3)
    if not matches:
        await message.answer("В watchlist таких нет. Админ может добавить через /watch <id>")
        return
    client, detector = clients[platform], detectors[platform]
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
            detector.market_price(points), points, store.last_prices(p.id, platform, 10),
        ))
    await message.answer(
        f"<b>{PLATFORM_NAMES[platform]}</b>\n\n" + "\n\n".join(blocks),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.message(Command("test"))
async def cmd_test(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    platform = chat_platform(message)
    client, detector = clients[platform], detectors[platform]
    pid = _arg_id(message)
    p = store.get_player(pid, platform) if pid else None
    if pid and not p:
        try:
            p = await client.get_player(pid)
        except FutnextError:
            p = None
    if not p:
        players = store.all_players(platform)
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
        single_lot=False, history=points[-6:], snapshots=store.last_prices(p.id, platform, 10),
    )
    if not await send_alert(sig, platform, only_to=message.chat.id):
        await message.answer("Не получилось отправить пример — смотри логи")


# ---------- запуск ----------

def check_db_persistence() -> None:
    """Предупреждает, если база не переживает перезапуск: на некоторых хостингах файл затирается при деплое."""
    try:
        runs = int(store.get_meta("runs", "0") or 0) + 1
        store.set_meta("runs", str(runs))
    except Exception:
        log.exception("База %s не пишется — подписки работать не будут, задай SUBSCRIBER_IDS", settings.db_path)
        return
    if runs == 1:
        log.warning(
            "База %s создана заново: подписки прошлого запуска потеряны. Если так после каждого деплоя — "
            "задай SUBSCRIBER_IDS, тогда /start больше не понадобится", settings.db_path,
        )
    else:
        log.info("База %s на месте (запуск №%d), подписки сохраняются", settings.db_path, runs)


def restore_permanent_subscribers() -> None:
    """Подписывает чаты из SUBSCRIBER_IDS (и админов) — им не нужен /start, и подписка переживает сброс базы."""
    chats = set(settings.subscriber_ids)
    if settings.subscribe_admins:
        chats |= settings.admin_ids
    restored, pinned = [], []
    for chat_id in sorted(chats):
        platform = settings.subscriber_platforms.get(chat_id, "")
        if platform:
            # платформа закреплена в настройках: восстанавливаем её и после сброса базы
            if store.platform_of(chat_id) != platform or store.subscribe(chat_id, platform):
                store.set_platform(chat_id, platform)
                pinned.append(f"{chat_id}:{platform}")
        elif store.subscribe(chat_id):
            restored.append(str(chat_id))
    if restored:
        log.info("Постоянные подписчики восстановлены: %s", ", ".join(restored))
    if pinned:
        log.info("Платформа закреплена из настроек: %s", ", ".join(pinned))


async def main() -> None:
    global store, bot
    problems = settings.validate()
    if problems:
        for p in problems:
            log.error(p)
        sys.exit(1)

    store = Store(settings.db_path, settings.platform)
    check_db_persistence()
    restore_permanent_subscribers()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    async with FutnextClient("pc", settings.concurrency) as pc, FutnextClient("ps", settings.concurrency) as ps:
        clients.update(pc=pc, ps=ps)
        for name, c in clients.items():
            detectors[name] = Detector(settings, store, c)
        me = await bot.get_me()
        await bot.set_my_commands([
            BotCommand(command="start", description="Подписаться на сигналы"),
            BotCommand(command="platform", description="Переключить PC / PlayStation"),
            BotCommand(command="price", description="Текущая цена карты"),
            BotCommand(command="stop", description="Отписаться"),
            BotCommand(command="help", description="Справка"),
        ])
        log.info(
            "Бот @%s запущен, подписчиков %d, активные платформы: %s, канал %s",
            me.username, len(store.subscribers()), ", ".join(active_platforms()), settings.channel_id or "-",
        )
        if not store.subscribers() and not settings.channel_id:
            log.warning("Подписчиков нет — сигналы никому не уйдут. Напиши боту /start или задай SUBSCRIBER_IDS")
        if settings.warmup_cycles:
            log.info("Прогрев: первые %d круг(а) сигналы не отправляются (FUT_WARMUP_CYCLES)", settings.warmup_cycles)
        tasks = [asyncio.create_task(watchlist_loop()), asyncio.create_task(poll_loop())]
        try:
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
        finally:
            for t in tasks:
                t.cancel()
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
