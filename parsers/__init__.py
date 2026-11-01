from .base import Listing, ParserError
from . import avito, youla

PARSERS = {
    "avito": avito,
    "youla": youla,
}

__all__ = ["Listing", "ParserError", "PARSERS", "avito", "youla"]
