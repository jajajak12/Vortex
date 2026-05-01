"""
db.py — P2.1: SQLite persistence layer for Vortex trades.
Replaces trades.json. ACID writes, indexed queries, no trim needed.

Migration: auto-imports existing trades.json on first run, then renames it.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from vortex_logger import get_logger

log = get_logger(__name__)

DB_PATH      = Path("/home/prospera/vortex/trades.db")
LEGACY_JSON  = Path("/home/prospera/vortex/trades.json")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY,
    time                TEXT    NOT NULL,
    pair                TEXT    NOT NULL,
    direction           TEXT    NOT NULL,
    entry               REAL    NOT NULL,
    sl                  REAL    NOT NULL,
    tp                  REAL    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'OPEN',
    result              TEXT,
    close_price         REAL,
    close_time          TEXT,
    strategy            TEXT,
    confluence_score    INTEGER DEFAULT 0,
    regime_state        TEXT,
    position_usdt       REAL    DEFAULT 0,
    rr                  REAL    DEFAULT 0,
    candles_to_resolve  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pair_status   ON trades(pair, status);
CREATE INDEX IF NOT EXISTS idx_strategy      ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_time          ON trades(time DESC);
CREATE INDEX IF NOT EXISTS idx_status        ON trades(status);
"""


@contextmanager
def _conn():
    """Thread-safe connection. Each call gets its own connection (SQLite WAL mode)."""
    con = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")   # concurrent reads + single writer
    con.execute("PRAGMA synchronous=NORMAL") # fast enough, safe enough
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def init_db():
    """Create schema, enable WAL, run migration from trades.json if needed."""
    with _conn() as con:
        con.executescript(_SCHEMA)
    _migrate_from_json()
    purge_duplicates()


def _migrate_from_json():
    """One-time migration from trades.json → trades.db. Safe to call multiple times."""
    if not LEGACY_JSON.exists():
        return
    with _conn() as con:
        existing = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        if existing > 0:
            return  # sudah pernah migrate

    try:
        with open(LEGACY_JSON) as f:
            trades = json.load(f)
        if not isinstance(trades, list) or not trades:
            return

        with _conn() as con:
            for t in trades:
                con.execute("""
                    INSERT OR IGNORE INTO trades
                      (id, time, pair, direction, entry, sl, tp, status, result,
                       close_price, close_time, strategy, confluence_score,
                       regime_state, position_usdt, rr, candles_to_resolve)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    t.get("id"),               t.get("time"),
                    t.get("pair"),             t.get("direction"),
                    t.get("entry"),            t.get("sl"),
                    t.get("tp"),               t.get("status", "OPEN"),
                    t.get("result"),           t.get("close_price"),
                    t.get("close_time"),       t.get("strategy"),
                    t.get("confluence_score", 0), t.get("regime_state"),
                    t.get("position_usdt", 0),    t.get("rr", 0),
                    t.get("candles_to_resolve"),
                ))

        log.info(f"[DB] Migrated {len(trades)} trades: trades.json → trades.db")
        LEGACY_JSON.rename(LEGACY_JSON.with_suffix(".json.migrated"))
    except Exception as e:
        log.error(f"[DB] Migration failed: {e}")


# ── Deduplication ─────────────────────────────────────────────────────────────

def purge_duplicates() -> int:
    """Mark duplicate trades DUPLICATE. Keep earliest per (pair, strategy, direction, 10-min bucket).
    Idempotent — safe to call on every startup."""
    from datetime import datetime
    with _conn() as con:
        rows = con.execute(
            "SELECT id, pair, strategy, direction, time FROM trades "
            "WHERE status != 'DUPLICATE' ORDER BY id ASC"
        ).fetchall()

    seen: dict = {}
    to_mark: list[int] = []
    for row in rows:
        try:
            t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        bucket = int(t.timestamp() // 600)  # 10-min bucket
        key = (row["pair"], row["strategy"] or "", row["direction"], bucket)
        if key in seen:
            to_mark.append(row["id"])
        else:
            seen[key] = row["id"]

    if to_mark:
        with _conn() as con:
            con.executemany(
                "UPDATE trades SET status='DUPLICATE' WHERE id=?",
                [(i,) for i in to_mark],
            )
        log.warning(f"[DB] purge_duplicates: marked {len(to_mark)} trades DUPLICATE — ids={to_mark}")
    return len(to_mark)


# ── Reset ─────────────────────────────────────────────────────────────────────

def reset_all_trades():
    """Delete all rows from trades table. Use for paper-trading resets."""
    with _conn() as con:
        deleted = con.execute("DELETE FROM trades").rowcount
    log.warning(f"[DB] reset_all_trades: {deleted} rows deleted from trades.db")
    return deleted


# ── Write operations ───────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> dict:
    with _conn() as con:
        con.execute("""
            INSERT INTO trades
              (id, time, pair, direction, entry, sl, tp, status, result,
               close_price, close_time, strategy, confluence_score,
               regime_state, position_usdt, rr, candles_to_resolve)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["id"],              trade["time"],
            trade["pair"],            trade["direction"],
            trade["entry"],           trade["sl"],
            trade["tp"],              trade.get("status", "OPEN"),
            trade.get("result"),      trade.get("close_price"),
            trade.get("close_time"),  trade.get("strategy"),
            trade.get("confluence_score", 0), trade.get("regime_state"),
            trade.get("position_usdt", 0),    trade.get("rr", 0),
            trade.get("candles_to_resolve"),
        ))
        # Auto-dedup: if an OPEN trade for same pair+strategy+direction exists within last 10 min,
        # this insertion is a duplicate — mark it immediately so it never enters stats or monitoring.
        existing = con.execute("""
            SELECT id FROM trades
            WHERE pair=? AND strategy=? AND direction=?
              AND status='OPEN' AND id != ?
              AND (julianday('now') - julianday(time)) * 1440 < 10
            ORDER BY id ASC LIMIT 1
        """, (
            trade["pair"], trade.get("strategy", ""), trade["direction"], trade["id"],
        )).fetchone()
        if existing:
            con.execute("UPDATE trades SET status='DUPLICATE' WHERE id=?", (trade["id"],))
            log.warning(
                f"[DB] Duplicate trade {trade['id']} "
                f"({trade['pair']} {trade.get('strategy')} {trade['direction']}) "
                f"— parent={existing['id']}, marked DUPLICATE"
            )
    return trade


def close_trade(trade_id: int, result: str, close_price: float,
                close_time: str, candles_to_resolve: int | None):
    with _conn() as con:
        con.execute("""
            UPDATE trades SET
                status = 'CLOSED', result = ?, close_price = ?,
                close_time = ?, candles_to_resolve = ?
            WHERE id = ?
        """, (result, close_price, close_time, candles_to_resolve, trade_id))


# ── Read operations ────────────────────────────────────────────────────────────

def get_open_trades(pair: str | None = None) -> list[dict]:
    with _conn() as con:
        if pair:
            rows = con.execute(
                "SELECT * FROM trades WHERE status='OPEN' AND pair=? ORDER BY time DESC",
                (pair,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY time DESC"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_trades(limit: int = 0) -> list[dict]:
    with _conn() as con:
        if limit:
            rows = con.execute(
                "SELECT * FROM trades ORDER BY time DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM trades ORDER BY time DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    # DUPLICATE trades are excluded from all counts — they never reach status='CLOSED'
    with _conn() as con:
        total_open = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status='OPEN'"
        ).fetchone()[0]

        rows = con.execute(
            "SELECT result, COUNT(*) as cnt FROM trades WHERE status='CLOSED' GROUP BY result"
        ).fetchall()

        wins   = next((r["cnt"] for r in rows if r["result"] == "WIN"),   0)
        losses = next((r["cnt"] for r in rows if r["result"] == "LOSS"),  0)
        total  = wins + losses

        # Per-strategy breakdown
        strat_rows = con.execute("""
            SELECT strategy,
                   SUM(CASE WHEN result='WIN'  THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) losses,
                   COUNT(*) total
            FROM trades WHERE status='CLOSED'
            GROUP BY strategy
        """).fetchall()

    by_strategy = {}
    for r in strat_rows:
        s = r["strategy"] or "?"
        t = r["total"] or 0
        w = r["wins"]  or 0
        by_strategy[s] = {
            "wins":    w,
            "losses":  r["losses"] or 0,
            "total":   t,
            "winrate": round(w / t * 100, 1) if t else 0.0,
        }

    return {
        "total":       total,
        "wins":        wins,
        "losses":      losses,
        "winrate":     round(wins / total * 100, 1) if total else 0.0,
        "open":        total_open,
        "by_strategy": by_strategy,
    }
