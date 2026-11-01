"""Оценка выгоды: рыночная цена, чистая прибыль, риск-скоринг."""
import re
import statistics
from dataclasses import dataclass, field

from parsers.base import Listing

# Слова-маркеры проблемного товара в заголовке
RED_FLAGS = {
    "запчаст": ("на запчасти", 6),
    "для запчастей": ("на запчасти", 6),
    "не включается": ("не включается", 7),
    "не работает": ("не работает", 6),
    "разбит": ("разбит", 5),
    "трещин": ("трещина", 4),
    "треснут": ("трещина", 4),
    "утоплен": ("утопленник", 8),
    "вода": ("контакт с водой", 5),
    "icloud": ("проблемы с iCloud", 9),
    "блокиров": ("блокировка", 7),
    "mdm": ("MDM-блокировка", 9),
    "восстановлен": ("восстановленный", 3),
    "refurb": ("восстановленный", 3),
    "реплика": ("реплика/копия", 10),
    "копия": ("реплика/копия", 10),
    "демо": ("демо-образец", 2),
    "срочно": ("срочная продажа", 1),
    "торг": ("торг", 0),
}

_MEM_RE = re.compile(r"(\d{2,4})\s*(гб|gb|тб|tb)", re.IGNORECASE)


# Аксессуары — не цель перекупа, исключаем из анализа совсем
ACCESSORY_WORDS = (
    "чехол", "чехла", "бампер", "стекло", "стёкла", "пленка", "плёнка",
    "кабель", "зарядка", "зарядное", "адаптер", "наушник", "кейс",
    "ремешок", "ремень", "стилус", "держатель", "подставка", "микрофон",
    "док-станция", "переходник", "накладка", "аккумулятор", "провод",
    "модуль", "тачскрин", "задняя крышка", "коробка от",
)


def is_accessory(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in ACCESSORY_WORDS)


def extract_memory(title: str) -> int | None:
    """Достаём объём памяти из заголовка, чтобы сравнивать яблоки с яблоками."""
    m = _MEM_RE.search(title)
    if not m:
        return None
    size = int(m.group(1))
    if m.group(2).lower() in ("тб", "tb"):
        size *= 1024
    return size


@dataclass
class Verdict:
    listing: Listing
    market_price: int = 0          # оценка рыночной цены продажи
    samples: int = 0               # на скольких похожих лотах основано
    profit: int = 0                # чистая прибыль, ₽
    margin_pct: float = 0.0        # маржа, % от вложений
    risk: int = 0                  # 0..10
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    worth_it: bool = False


def _median_without(prices: list[int], own: int) -> tuple[int, int]:
    """Медиана без выбросов (отрезаем 15% хвостов), кандидата исключаем."""
    arr = sorted(p for p in prices if p != own and p > 0)
    if len(arr) < 3:
        return (0, 0)
    cut = max(1, int(len(arr) * 0.15))
    trimmed = arr[cut:-cut] if len(arr) > 2 * cut else arr
    return int(statistics.median(trimmed)), len(arr)


def analyze(listing: Listing, scan_prices: list[tuple[int, str]], cfg: dict) -> Verdict:
    """scan_prices — [(price, title)] всех лотов текущего скана по этому фильтру."""
    v = Verdict(listing=listing)
    s = cfg["settings"]

    # Аксессуары пропускаем сразу — они не цель перекупа
    if is_accessory(listing.title):
        v.reasons.append("аксессуар — пропускаем")
        return v

    # --- рыночная цена: сравниваем только лоты с той же памятью (если она есть) ---
    my_mem = extract_memory(listing.title)
    pool = [(p, t) for p, t in scan_prices if not is_accessory(t)]
    if my_mem:
        pool = [(p, t) for p, t in pool if extract_memory(t) in (my_mem, None)]
    market, n = _median_without([p for p, _ in pool], listing.price)
    v.market_price, v.samples = market, n
    if market == 0 or n < s["min_market_samples"]:
        v.reasons.append(f"мало данных по рынку ({n} лотов)")
        return v

    # Цена в разы ниже рынка — это запчасть, аксессуар или обман, а не выгода
    if listing.price < market * 0.3:
        v.reasons.append("цена <70% ниже рынка — вероятно запчасть/обман")
        v.risk = 9
        return v

    # --- прибыль ---
    resale_price = market * (1 - s["resale_discount_percent"] / 100)
    after_fee = resale_price * (1 - s["platform_fee_percent"] / 100)
    v.profit = int(after_fee - listing.price - s["delivery_cost"])
    v.margin_pct = round(v.profit / listing.price * 100, 1) if listing.price else 0.0

    # --- риск-скоринг ---
    risk, reasons, flags = 2, [], []  # базовые 2: б/у рынок — всегда риск
    discount = (1 - listing.price / market) * 100

    if discount >= 45:
        risk += 4
        reasons.append(f"цена ниже рынка на {discount:.0f}% — подозрительно дёшево")
    elif discount >= 30:
        risk += 2
        reasons.append(f"ниже рынка на {discount:.0f}%")

    title_lower = listing.title.lower()
    hard_flag = False
    for word, (label, score) in RED_FLAGS.items():
        if word in title_lower:
            flags.append(label)
            risk += score
            if score >= 7:
                hard_flag = True  # утопленник, MDM, реплика — в блок сразу

    st = (listing.seller_type or "").lower()
    since = listing.seller_since or ""
    if "компания" in st:
        risk -= 1
        reasons.append("продавец — компания")
    if since:
        m = re.search(r"(\d{4})", since)
        if m and int(m.group(1)) >= 2025:
            risk += 3
            reasons.append(f"свежий аккаунт ({since.lower()})")
    else:
        risk += 1
    if not listing.image:
        risk += 2
        reasons.append("нет фото")

    v.risk = max(0, min(10, risk))
    v.reasons = reasons
    v.red_flags = flags
    v.worth_it = (v.profit > 0
                  and v.margin_pct >= s["min_margin_percent"]
                  and v.risk <= s["max_risk_score"]
                  and not hard_flag)
    return v
