"""Бот-перекуп: мониторинг Авито 24/7, оценка выгоды, уведомления в Telegram.

Запуск:  python bot.py
"""
import asyncio
import html
import logging
import random
import time
from pathlib import Path

import yaml
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

import db
from analyzer import analyze
from parsers import PARSERS, ParserError

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flip_bot")

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_config()
TG = CFG["telegram"]

if not TG.get("token") or "ВСТАВЬ" in str(TG["token"]):
    raise SystemExit("⚠️  Впиши токен от @BotFather в config.yaml (telegram.token)")

bot = Bot(TG["token"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Состояние в рантайме
STATE = {
    "paused": False,
    "last_scan": None,
    "errors": {},          # platform -> подряд ошибок
    "alerted": {},         # platform -> уже предупредили
}


def allowed_chat_id() -> int:
    saved = db.get_setting("chat_id")
    return int(saved) if saved else int(TG.get("chat_id") or 0)


def is_owner(message: Message) -> bool:
    cid = allowed_chat_id()
    return cid == 0 or message.chat.id == cid


def fmt_money(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽"


# ============================== СКАНЕР ==============================

async def scan_once() -> dict:
    """Один цикл: все фильтры → новые лоты → анализ → уведомления."""
    global CFG
    CFG = load_config()  # подхватываем правки конфига на лету
    summary = {"scanned": 0, "new": 0, "sent": 0, "errors": []}

    for flt in CFG.get("filters", []):
        if not flt.get("enabled", True):
            continue
        platform = flt.get("platform", "avito")
        parser = PARSERS.get(platform)
        if not parser:
            continue
        try:
            listings = await parser.fetch_listings(
                flt["query"],
                location_id=CFG.get("location_id"),
                category_id=flt.get("category_id"),
                count=30,
            )
            STATE["errors"][platform] = 0
            STATE["alerted"][platform] = False
        except ParserError as e:
            STATE["errors"][platform] = STATE["errors"].get(platform, 0) + 1
            summary["errors"].append(str(e))
            log.warning("%s (подряд: %s)", e, STATE["errors"][platform])
            await maybe_alert_parser_down(platform, str(e))
            continue

        summary["scanned"] += len(listings)
        scan_prices = [(l.price, l.title) for l in listings]
        max_price = flt.get("max_price")

        for l in listings:
            l.filter_name = flt["name"]
            if db.is_seen(l.uid):
                continue
            if max_price and l.price > max_price:
                continue
            v = analyze(l, scan_prices, CFG)
            db.save_seen(l.uid, platform, flt["name"], l.title, l.price,
                         v.market_price, v.margin_pct, v.risk, v.profit,
                         l.url, notified=int(v.worth_it))
            summary["new"] += 1
            if v.worth_it:
                await notify_deal(v)
                summary["sent"] += 1

        await asyncio.sleep(random.uniform(4, 9))  # пауза между фильтрами

    STATE["last_scan"] = time.time()
    db.cleanup()
    return summary


async def maybe_alert_parser_down(platform: str, err: str):
    if STATE["errors"].get(platform, 0) >= 4 and not STATE["alerted"].get(platform):
        STATE["alerted"][platform] = True
        cid = allowed_chat_id()
        if cid:
            await bot.send_message(
                cid,
                f"🚨 <b>Парсер {platform} не работает</b> (4+ ошибок подряд)\n"
                f"Последняя ошибка: {html.escape(err)}\n\n"
                "Скорее всего Авито изменил API или ограничил запросы. "
                "Проверь лог и при необходимости увеличь интервал в config.yaml.")


async def scanner_loop():
    await asyncio.sleep(5)  # даём боту подняться
    while True:
        if not STATE["paused"]:
            try:
                s = await scan_once()
                log.info("Скан: %s лотов, новых %s, отправлено %s",
                         s["scanned"], s["new"], s["sent"])
            except Exception as e:
                log.exception("Ошибка цикла сканирования: %s", e)
        interval = max(5, int(CFG["settings"]["check_interval_minutes"])) * 60
        await asyncio.sleep(interval + random.uniform(0, 60))


# ========================== УВЕДОМЛЕНИЯ ==========================

def deal_card(v) -> str:
    l = v.listing
    lines = [
        f"🔥 <b>{html.escape(l.title)}</b>",
        f"💵 Цена: <b>{fmt_money(l.price)}</b>",
        "",
        f"📈 Рынок: ~{fmt_money(v.market_price)} (по {v.samples} лотам)",
        f"💰 Прибыль: <b>~{fmt_money(v.profit)}</b> ({v.margin_pct:+.0f}%)",
        f"⚠️ Риск: <b>{v.risk}/10</b>",
    ]
    if v.red_flags:
        lines.append(f"🚩 Флаги: {', '.join(v.red_flags)}")
    if v.reasons:
        lines.append(f"📝 {'; '.join(v.reasons)}")
    seller = " · ".join(x for x in [l.seller_name, l.seller_since] if x)
    if seller:
        lines.append(f"👤 {html.escape(seller)}")
    lines.append(f"🏷 {html.escape(l.filter_name)} · {l.platform}")
    return "\n".join(lines)


def deal_keyboard(v) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Открыть", url=v.listing.url),
        InlineKeyboardButton(text="✅ Беру", callback_data=f"take:{v.listing.uid}"),
        InlineKeyboardButton(text="❌ Мимо", callback_data=f"skip:{v.listing.uid}"),
    ]])


async def notify_deal(v):
    cid = allowed_chat_id()
    if not cid:
        log.warning("chat_id неизвестен — напиши боту /start")
        return
    text = deal_card(v)
    try:
        if v.listing.image:
            await bot.send_photo(cid, v.listing.image, caption=text[:1024],
                                 reply_markup=deal_keyboard(v))
        else:
            await bot.send_message(cid, text, reply_markup=deal_keyboard(v))
    except Exception as e:
        log.warning("Не смог отправить карточку: %s", e)
        await bot.send_message(cid, text, reply_markup=deal_keyboard(v))


# =========================== КОМАНДЫ ===========================

@dp.message(Command("start"))
async def cmd_start(m: Message):
    if allowed_chat_id() == 0:
        db.set_setting("chat_id", str(m.chat.id))
    await m.answer(
        "🤖 <b>Бот-перекуп запущен</b>\n\n"
        "Мониторю Авито и пришлю карточку, когда найдётся лот "
        "с маржой выше порога и приемлемым риском.\n\n"
        "Команды:\n"
        "/status — состояние сканера\n"
        "/filters — активные фильтры\n"
        "/stats — статистика\n"
        "/test — сканировать прямо сейчас\n"
        "/pause, /resume — пауза/старт")


@dp.message(Command("status"))
async def cmd_status(m: Message):
    if not is_owner(m):
        return
    last = (time.strftime("%H:%M:%S", time.localtime(STATE["last_scan"]))
            if STATE["last_scan"] else "ещё не было")
    errs = ", ".join(f"{p}: {n}" for p, n in STATE["errors"].items() if n) or "нет"
    await m.answer(
        f"📊 Статус: {'⏸ пауза' if STATE['paused'] else '▶️ работает'}\n"
        f"Последний скан: {last}\n"
        f"Интервал: {CFG['settings']['check_interval_minutes']} мин\n"
        f"Ошибки парсеров: {errs}")


@dp.message(Command("filters"))
async def cmd_filters(m: Message):
    if not is_owner(m):
        return
    lines = []
    for f in CFG.get("filters", []):
        mark = "✅" if f.get("enabled", True) else "⛔️"
        lines.append(f"{mark} <b>{f['name']}</b> — до {fmt_money(f['max_price'])} "
                     f"({f.get('platform', 'avito')})")
    await m.answer("Фильтры (правь config.yaml, подхватится само):\n" + "\n".join(lines))


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_owner(m):
        return
    s = db.stats()
    await m.answer(
        f"📈 Всего обработано: {s['total']}\n"
        f"🔥 Уведомлений отправлено: {s['notified']}\n"
        f"✅ Отмечено «Беру»: {s['taken']}\n"
        f"❌ Отмечено «Мимо»: {s['skipped']}\n"
        f"💰 Потенциальная прибыль по взятым: {fmt_money(s['potential_profit'])}")


@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if is_owner(m):
        STATE["paused"] = True
        await m.answer("⏸ Сканер на паузе. /resume — продолжить.")


@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if is_owner(m):
        STATE["paused"] = False
        await m.answer("▶️ Сканер работает.")


@dp.message(Command("test"))
async def cmd_test(m: Message):
    if not is_owner(m):
        return
    await m.answer("🔍 Сканирую вне очереди...")
    s = await scan_once()
    await m.answer(f"Готово: {s['scanned']} лотов, новых {s['new']}, "
                   f"подходящих {s['sent']}.\n"
                   + ("\n".join(s["errors"]) if s["errors"] else "Ошибок нет."))


@dp.callback_query(F.data.startswith(("take:", "skip:")))
async def on_decision(cb: CallbackQuery):
    action, uid = cb.data.split(":", 1)
    db.set_decision(uid, action)
    mark = "✅ <b>ВЗЯТО</b> — удачной перепродажи!" if action == "take" else "❌ пропущено"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer()
    await cb.message.reply(mark)


# ============================ ЗАПУСК ============================

async def main():
    log.info("Бот-перекуп стартует...")
    asyncio.create_task(scanner_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
