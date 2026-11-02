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
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
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
    """Один цикл поиска с постоянной защитой от HTTP-ограничений."""
    global CFG
    CFG = load_config()

    summary = {
        "scanned": 0,
        "new": 0,
        "sent": 0,
        "errors": [],
    }

    # Проверяем блокировку до любого сетевого обращения.
    blocked_until, blocked_reason = db.get_scan_block()

    if blocked_until > int(time.time()):
        summary["blocked_until"] = blocked_until
        summary["blocked_reason"] = blocked_reason
        return summary

    # Срок старой блокировки закончился.
    if blocked_until:
        db.clear_scan_block()

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
            STATE["errors"][platform] = (
                STATE["errors"].get(platform, 0) + 1
            )
            summary["errors"].append(str(e))

            log.warning(
                "%s (подряд: %s)",
                e,
                STATE["errors"][platform],
            )

            # При ограничении останавливаем ВЕСЬ цикл.
            if e.status_code in (403, 429, 439):
                retry_after = e.retry_after

                if retry_after is None:
                    try:
                        minutes = int(
                            CFG.get("settings", {}).get(
                                "blocked_cooldown_minutes", 1440
                            )
                        )
                    except (TypeError, ValueError):
                        minutes = 1440

                    cooldown = max(60, minutes * 60)
                else:
                    cooldown = max(1, int(retry_after))

                until = int(time.time()) + cooldown
                reason = str(e)

                db.set_scan_block(until, reason)

                summary["blocked_until"] = until
                summary["blocked_reason"] = reason

                log.error(
                    "Сканирование остановлено до %s UTC: %s",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(until)),
                    reason,
                )

                # Отправляем одно уведомление, а не повторяем его.
                if not db.scan_block_notified():
                    db.mark_scan_block_notified()
                    cid = allowed_chat_id()

                    if cid:
                        message = (
                            "⛔ <b>Авито ограничило доступ</b>\n\n"
                            f"Причина: {html.escape(reason)}\n"
                            "Сканирование всех фильтров остановлено.\n"
                            f"Следующая проверка не раньше: "
                            f"{time.strftime('%d.%m.%Y %H:%M:%S', time.gmtime(until))} UTC.\n\n"
                            "Перезапуск бота и команда /test не отменяют эту паузу."
                        )
                        try:
                            await bot.send_message(cid, message)
                        except Exception as notify_error:
                            log.warning(
                                "Не удалось отправить уведомление о блокировке: %s",
                                notify_error,
                            )

                return summary

            await maybe_alert_parser_down(platform, str(e))
            continue

        summary["scanned"] += len(listings)

        # Рыночная выборка строится по всем полученным объявлениям.
        scan_prices = [(l.price, l.title) for l in listings]
        max_price = flt.get("max_price")

        for listing in listings:
            listing.filter_name = flt["name"]

            if db.is_seen(listing.uid):
                continue

            if max_price and listing.price > max_price:
                continue

            verdict = analyze(listing, scan_prices, CFG)

            db.save_seen(
                listing.uid,
                platform,
                flt["name"],
                listing.title,
                listing.price,
                verdict.market_price,
                verdict.margin_pct,
                verdict.risk,
                verdict.profit,
                listing.url,
                notified=int(verdict.worth_it),
            )

            summary["new"] += 1

            if verdict.worth_it:
                await notify_deal(verdict)
                summary["sent"] += 1

        await asyncio.sleep(random.uniform(4, 9))

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
    await asyncio.sleep(5)

    while True:
        blocked_until, _ = db.get_scan_block()
        now = int(time.time())

        # Во время блокировки не запускаем даже пустые циклы сканирования.
        if blocked_until > now:
            await asyncio.sleep(min(blocked_until - now, 3600))
            continue

        if not STATE["paused"]:
            try:
                result = await scan_once()

                if not result.get("blocked_until"):
                    log.info(
                        "Скан: %s лотов, новых %s, отправлено %s",
                        result["scanned"],
                        result["new"],
                        result["sent"],
                    )

            except Exception as e:
                log.exception("Ошибка цикла сканирования: %s", e)

        interval = max(
            5,
            int(CFG["settings"]["check_interval_minutes"]),
        ) * 60

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

    if v.listing.image:
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"Referer": "https://www.avito.ru/"}
            ) as client:
                response = await client.get(v.listing.image)
                response.raise_for_status()

                content_type = response.headers.get(
                    "content-type", ""
                ).split(";")[0].lower()

                if not content_type.startswith("image/"):
                    raise ValueError(
                        f"Ссылка вернула не изображение: {content_type}"
                    )

                data = response.content

                if len(data) > 9 * 1024 * 1024:
                    raise ValueError("Изображение превышает 9 МБ")

            extension = {
                "image/jpeg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
            }.get(content_type, "jpg")

            photo = BufferedInputFile(
                data,
                filename=f"avito.{extension}"
            )

            await bot.send_photo(
                cid,
                photo,
                caption=text[:1024],
                reply_markup=deal_keyboard(v)
            )
            return

        except Exception as e:
            log.warning("Не удалось скачать или отправить фото: %s", e)

    await bot.send_message(
        cid,
        text,
        reply_markup=deal_keyboard(v)
    )


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

    last = (
        time.strftime("%H:%M:%S", time.localtime(STATE["last_scan"]))
        if STATE["last_scan"]
        else "ещё не было"
    )

    errs = (
        ", ".join(f"{p}: {n}" for p, n in STATE["errors"].items() if n)
        or "нет"
    )

    blocked_until, blocked_reason = db.get_scan_block()

    if blocked_until > int(time.time()):
        block_status = (
            "⛔ Источник ограничен до: "
            f"{time.strftime('%d.%m.%Y %H:%M:%S', time.gmtime(blocked_until))} UTC\n"
            f"Причина: {html.escape(blocked_reason)}\n"
        )
    else:
        block_status = "Ограничение источника: нет\n"

    await m.answer(
        f"📊 Статус: {'⏸ пауза' if STATE['paused'] else '▶️ работает'}\n"
        f"Последний скан: {last}\n"
        f"Интервал: {CFG['settings']['check_interval_minutes']} мин\n"
        f"{block_status}"
        f"Ошибки парсеров: {errs}"
    )



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
    if not is_owner(m):
        return

    blocked_until, _ = db.get_scan_block()

    if blocked_until > int(time.time()):
        await m.answer(
            "⛔ Ручная пауза снята, но действует ограничение Авито.\n"
            "Новые запросы не будут отправляться до окончания защитной паузы."
        )
        STATE["paused"] = False
        return

    STATE["paused"] = False
    await m.answer("▶️ Сканер работает.")



@dp.message(Command("test"))
async def cmd_test(m: Message):
    if not is_owner(m):
        return

    blocked_until, blocked_reason = db.get_scan_block()

    if blocked_until > int(time.time()):
        await m.answer(
            "⛔ Тестовый запуск запрещён защитой.\n"
            f"Ограничение действует до "
            f"{time.strftime('%d.%m.%Y %H:%M:%S', time.gmtime(blocked_until))} UTC.\n"
            f"Причина: {html.escape(blocked_reason)}"
        )
        return

    await m.answer("🔍 Сканирую...")
    result = await scan_once()

    if result.get("blocked_until"):
        await m.answer(
            "⛔ Авито ограничило доступ. Все дальнейшие запросы остановлены."
        )
        return

    await m.answer(
        f"Готово: {result['scanned']} лотов, новых {result['new']}, "
        f"подходящих {result['sent']}.\n"
        + (
            "\n".join(result["errors"])
            if result["errors"]
            else "Ошибок нет."
        )
    )



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
