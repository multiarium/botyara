"""Общие структуры данных для всех парсеров."""
from dataclasses import dataclass, field
from typing import Optional


class ParserError(Exception):
    """Ошибка парсера (бан, капча, смена API и т.п.)."""


@dataclass
class Listing:
    platform: str          # "avito" | "youla" | ...
    id: str                # уникальный id объявления на площадке
    title: str
    price: int             # цена в рублях
    url: str
    filter_name: str = ""
    image: Optional[str] = None
    seller_name: str = ""
    seller_type: str = ""          # "Частное лицо" / "Компания"
    seller_since: str = ""         # "На Авито с июля 2021"
    category_id: Optional[int] = None
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.platform}:{self.id}"
