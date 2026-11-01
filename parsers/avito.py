"""Парсер Авито через мобильный API приложения (app.avito.ru).

Лёгкий JSON, без браузера — то, что нужно для Termux.
ВАЖНО: это неофициальный API. Если Авито его изменит или заблокирует,
парсер начнёт кидать ParserError — бот сообщит об этом в Telegram.
"""
import re
import asyncio
import random

import httpx

from .base import Listing, ParserError

# Публичный ключ мобильного приложения (используется в open-source парсерах).
# Если перестанет работать — ищи свежий в проектах типа avito-parser на GitHub.
AVITO_KEY = "af0deccbgcgidddjgnvljitntccdduijhdinfgjgfjir"
API_URL = "https://app.avito.ru/api/11/items"

HEADERS = {
    "User-Agent": "AVITO 119.1 (OnePlus ONEPLUS A5000; Android 9; ru)",
    "X-App-Version": "119.1",
    "X-Device-Platform": "Android",
    "Accept": "application/json",
}


def _parse_price(raw: str) -> int:
    """'40\xa0990\xa0₽' -> 40990"""
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else 0


def _best_image(gallery: list) -> str | None:
    """Берём самую большую картинку из галереи."""
    for item in gallery or []:
        val = item.get("value", {})
        for size in ("678x678", "636x636", "558x558", "507x507"):
            if size in val:
                return val[size]
        if val:
            return next(iter(val.values()))
    return None


async def fetch_listings(query: str, *, location_id: int | None = None,
                         category_id: int | None = None, count: int = 30,
                         timeout: float = 20.0) -> list[Listing]:
    """Ищет свежие объявления (сортировка по дате)."""
    params = {
        "key": AVITO_KEY,
        "query": query,
        "count": count,
        "sort": "date",
        "display": "list",
    }
    if location_id:
        params["locationId"] = location_id
    if category_id:
        params["categoryId"] = category_id

    # Вежливая случайная пауза, чтобы не ловить бан
    await asyncio.sleep(random.uniform(0.5, 2.0))

    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.get(API_URL, params=params)
    except httpx.HTTPError as e:
        raise ParserError(f"Авито: сетевая ошибка: {e}") from e

    if resp.status_code == 429:
        raise ParserError("Авито: 429 (слишком много запросов — нас временно ограничили)")
    if resp.status_code != 200:
        raise ParserError(f"Авито: HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        raise ParserError("Авито: ответ не JSON (возможно, капча/бан)") from e

    result = data.get("result") or {}
    raw_items = result.get("items")
    if raw_items is None:
        raise ParserError(f"Авито: неожиданная структура ответа: {list(result.keys())[:5]}")

    listings: list[Listing] = []
    for it in raw_items:
        if it.get("type") != "item":
            continue
        v = it.get("value") or {}
        seller = v.get("sellerInfo") or {}
        uri = v.get("uri") or ""
        uri = uri.split("?")[0]  # отрезаем служебный context
        listings.append(Listing(
            platform="avito",
            id=str(v.get("id", "")),
            title=v.get("title", "").strip(),
            price=_parse_price((v.get("price") or {}).get("current", "")),
            url=f"https://www.avito.ru{uri}" if uri else "",
            image=_best_image(v.get("galleryItems")),
            seller_name=seller.get("name", ""),
            seller_type=seller.get("sellerTypeName", ""),
            seller_since=seller.get("date", ""),
            category_id=v.get("categoryId"),
        ))
    return [l for l in listings if l.id and l.price > 0]
