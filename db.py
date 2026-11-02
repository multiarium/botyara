"""SQLite: учёт увиденных объявлений, решений и настроек бота."""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "flip_bot.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen (
        uid TEXT PRIMARY KEY,
        platform TEXT, filter_name TEXT, title TEXT,
        price INTEGER, market_price INTEGER, margin_pct REAL,
        risk INTEGER, profit INTEGER, url TEXT,
        notified INTEGER DEFAULT 0, decision TEXT DEFAULT NULL,
        created_at INTEGER
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    return conn


def is_seen(uid: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone() is not None


def save_seen(uid, platform, filter_name, title, price, market_price,
              margin_pct, risk, profit, url, notified):
    with _conn() as c:
        c.execute("""INSERT OR IGNORE INTO seen
            (uid, platform, filter_name, title, price, market_price,
             margin_pct, risk, profit, url, notified, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, platform, filter_name, title, price, market_price,
             margin_pct, risk, profit, url, int(notified), int(time.time())))


def set_decision(uid: str, decision: str):
    with _conn() as c:
        c.execute("UPDATE seen SET decision=? WHERE uid=?", (decision, uid))


def get_setting(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_setting(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        notified = c.execute("SELECT COUNT(*) FROM seen WHERE notified=1").fetchone()[0]
        taken = c.execute("SELECT COUNT(*) FROM seen WHERE decision='take'").fetchone()[0]
        skipped = c.execute("SELECT COUNT(*) FROM seen WHERE decision='skip'").fetchone()[0]
        pot = c.execute("""SELECT COALESCE(SUM(profit),0) FROM seen
                           WHERE decision='take'""").fetchone()[0]
    return {"total": total, "notified": notified, "taken": taken,
            "skipped": skipped, "potential_profit": int(pot)}


def cleanup(days: int = 14):
    """Чистим старые записи, чтобы база не разрасталась на телефоне."""
    cutoff = int(time.time()) - days * 86400
    with _conn() as c:
        c.execute("DELETE FROM seen WHERE created_at < ? AND decision IS NULL", (cutoff,))


# Постоянная защита от ограничений источника.
# Состояние хранится в SQLite и переживает перезапуск бота.

def get_scan_block() -> tuple[int, str]:
    raw_until = get_setting("source_blocked_until")
    reason = get_setting("source_block_reason") or ""

    try:
        until = int(raw_until or 0)
    except (TypeError, ValueError):
        until = 0

    return until, reason


def set_scan_block(until: int, reason: str):
    set_setting("source_blocked_until", str(int(until)))
    set_setting("source_block_reason", reason)


def clear_scan_block():
    with _conn() as c:
        c.execute(
            "DELETE FROM settings WHERE key IN (?, ?, ?)",
            (
                "source_blocked_until",
                "source_block_reason",
                "source_block_notified",
            ),
        )


def scan_block_notified() -> bool:
    return get_setting("source_block_notified") == "1"


def mark_scan_block_notified():
    set_setting("source_block_notified", "1")
