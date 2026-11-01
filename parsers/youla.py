"""Парсер Юлы — ЗАГЛУШКА (этап 2).

Юла рендерит товары через JS и закрытый GraphQL (api-gw.youla.io,
интроспекция выключена). Чтобы оживить: сними запрос мобильного приложения
или веба через mitmproxy/Charles и впиши сюда operationName + query.
Интерфейс уже совместим: верни list[Listing] — остальное заработает само.
"""
from .base import Listing, ParserError


async def fetch_listings(query: str, **kwargs) -> list[Listing]:
    raise ParserError("Юла: парсер ещё не подключён (этап 2)")
