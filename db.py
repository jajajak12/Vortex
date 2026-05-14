"""
db.py — P2.1: SQLite persistence layer for Vortex trades.
Replaces trades.json. ACID writes, indexed queries, no trim needed.

Migration: auto-imports existing trades.json on first run, then renames it.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    candles_to_resolve  INTEGER,
    close_reason        TEXT,
    current_equity      REAL    DEFAULT 0,
    risk_usd            REAL    DEFAULT 0,
    stop_distance_pct   REAL    DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pair_status   ON trades(pair, status);
CREATE INDEX IF NOT EXISTS idx_strategy      ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_time          ON trades(time DESC);
CREATE INDEX IF NOT EXISTS idx_status        ON trades(status);
"""

_BINANCE_DEMO_EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS binance_demo_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vortex_trade_id INTEGER UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity REAL,
    notional REAL,
    risk_usd REAL,
    entry_order_id TEXT,
    tp_order_id TEXT,
    sl_order_id TEXT,
    status TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_binance_demo_executions_status
    ON binance_demo_executions(status);
CREATE INDEX IF NOT EXISTS idx_binance_demo_executions_symbol
    ON binance_demo_executions(symbol);
CREATE TABLE IF NOT EXISTS binance_demo_execution_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_state_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _ensure_binance_demo_execution_schema() -> None:
    with _conn() as con:
        con.executescript(_BINANCE_DEMO_EXECUTION_SCHEMA)


def _migrate_schema():
    """ALTER TABLE to add new columns to existing DBs without data loss."""
    with _conn() as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(trades)").fetchall()}
        additions = [
            ("close_reason",      "TEXT"),
            ("current_equity",    "REAL DEFAULT 0"),
            ("risk_usd",          "REAL DEFAULT 0"),
            ("stop_distance_pct", "REAL DEFAULT 0"),
        ]
        for col_name, col_def in additions:
            if col_name not in cols:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}")
                log.info(f"[DB] Schema migrated: added {col_name} column")


def init_db():
    """Create schema, enable WAL, run migration from trades.json if needed."""
    with _conn() as con:
        con.executescript(_SCHEMA)
        con.executescript(_BINANCE_DEMO_EXECUTION_SCHEMA)
    _migrate_schema()
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


# ── Equity calculation ─────────────────────────────────────────────────────────

def compute_realized_pnl() -> float:
    """Sum realized PnL from all CLOSED trades using position_usdt and price delta."""
    with _conn() as con:
        rows = con.execute(
            "SELECT direction, entry, close_price, position_usdt FROM trades "
            "WHERE status='CLOSED' AND close_price IS NOT NULL "
            "  AND entry > 0 AND position_usdt > 0"
        ).fetchall()
    total = 0.0
    for r in rows:
        entry = float(r["entry"] or 0)
        close = float(r["close_price"] or 0)
        pos   = float(r["position_usdt"] or 0)
        if entry <= 0 or pos <= 0:
            continue
        if r["direction"] == "LONG":
            total += (close - entry) / entry * pos
        else:
            total += (entry - close) / entry * pos
    return round(total, 4)


# ── Write operations ───────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> dict:
    with _conn() as con:
        con.execute("""
            INSERT INTO trades
              (id, time, pair, direction, entry, sl, tp, status, result,
               close_price, close_time, strategy, confluence_score,
               regime_state, position_usdt, rr, candles_to_resolve,
               current_equity, risk_usd, stop_distance_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            trade.get("current_equity", 0),   trade.get("risk_usd", 0),
            trade.get("stop_distance_pct", 0),
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
                close_time: str, candles_to_resolve: int | None,
                close_reason: str | None = None):
    with _conn() as con:
        con.execute("""
            UPDATE trades SET
                status = 'CLOSED', result = ?, close_price = ?,
                close_time = ?, candles_to_resolve = ?, close_reason = ?
            WHERE id = ?
        """, (result, close_price, close_time, candles_to_resolve, close_reason, trade_id))


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


def get_trade(trade_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


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


def binance_demo_execution_table_exists() -> bool:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='binance_demo_executions'"
        ).fetchone()
    return row is not None


def get_binance_demo_execution(vortex_trade_id: int) -> dict | None:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM binance_demo_executions WHERE vortex_trade_id=?",
            (vortex_trade_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def count_binance_demo_executions() -> int:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM binance_demo_executions").fetchone()
    return int(row[0] if row else 0)


def list_recent_binance_demo_executions(limit: int = 20) -> list[dict]:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM binance_demo_executions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_binance_demo_executions(*, symbol: str | None = None) -> list[dict]:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        if symbol:
            rows = con.execute(
                "SELECT * FROM binance_demo_executions WHERE symbol=? ORDER BY id ASC",
                (symbol.upper(),),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM binance_demo_executions ORDER BY id ASC"
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def upsert_binance_demo_execution(payload: dict) -> dict:
    _ensure_binance_demo_execution_schema()
    now = _utc_now_iso()
    vortex_trade_id = int(payload["vortex_trade_id"])
    existing = get_binance_demo_execution(vortex_trade_id)

    columns = [
        "vortex_trade_id",
        "symbol",
        "strategy",
        "direction",
        "quantity",
        "notional",
        "risk_usd",
        "entry_order_id",
        "tp_order_id",
        "sl_order_id",
        "status",
        "error",
    ]

    if existing is None:
        row = {key: payload.get(key) for key in columns}
        row["created_at"] = payload.get("created_at") or now
        row["updated_at"] = payload.get("updated_at") or now
        with _conn() as con:
            con.execute(
                """
                INSERT INTO binance_demo_executions (
                    vortex_trade_id, symbol, strategy, direction,
                    quantity, notional, risk_usd,
                    entry_order_id, tp_order_id, sl_order_id,
                    status, error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["vortex_trade_id"],
                    row["symbol"],
                    row["strategy"],
                    row["direction"],
                    row["quantity"],
                    row["notional"],
                    row["risk_usd"],
                    row["entry_order_id"],
                    row["tp_order_id"],
                    row["sl_order_id"],
                    row["status"],
                    row["error"],
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        return get_binance_demo_execution(vortex_trade_id) or row

    merged = dict(existing)
    for key in columns:
        if key in payload:
            merged[key] = payload[key]
    merged["updated_at"] = payload.get("updated_at") or now

    with _conn() as con:
        con.execute(
            """
            UPDATE binance_demo_executions
            SET symbol=?,
                strategy=?,
                direction=?,
                quantity=?,
                notional=?,
                risk_usd=?,
                entry_order_id=?,
                tp_order_id=?,
                sl_order_id=?,
                status=?,
                error=?,
                updated_at=?
            WHERE vortex_trade_id=?
            """,
            (
                merged["symbol"],
                merged["strategy"],
                merged["direction"],
                merged["quantity"],
                merged["notional"],
                merged["risk_usd"],
                merged["entry_order_id"],
                merged["tp_order_id"],
                merged["sl_order_id"],
                merged["status"],
                merged["error"],
                merged["updated_at"],
                vortex_trade_id,
            ),
        )
    return get_binance_demo_execution(vortex_trade_id) or merged


def set_binance_demo_auto_startup(started_at: str | None = None) -> str:
    _ensure_binance_demo_execution_schema()
    value = started_at or _utc_now_iso()
    set_binance_demo_runtime_state("last_auto_startup_at", value)
    return value


def get_binance_demo_auto_startup() -> str | None:
    return get_binance_demo_runtime_state("last_auto_startup_at")


def set_binance_demo_runtime_state(key: str, value: Any) -> str:
    _ensure_binance_demo_execution_schema()
    normalized = _normalize_state_value(value)
    updated_at = _utc_now_iso()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO binance_demo_execution_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, normalized, updated_at),
        )
    return normalized


def get_binance_demo_runtime_state(key: str) -> str | None:
    _ensure_binance_demo_execution_schema()
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM binance_demo_execution_state WHERE key=?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row else None


def get_binance_demo_runtime_states(*keys: str) -> dict[str, str | None]:
    _ensure_binance_demo_execution_schema()
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    values = {key: None for key in keys}
    with _conn() as con:
        rows = con.execute(
            f"SELECT key, value FROM binance_demo_execution_state WHERE key IN ({placeholders})",
            tuple(keys),
        ).fetchall()
    for row in rows:
        values[str(row["key"])] = str(row["value"])
    return values


def get_binance_demo_rate_limit_state() -> dict[str, str | None]:
    return get_binance_demo_runtime_states(
        "binance_demo_rate_limit_cooldown_until",
        "binance_demo_rate_limit_last_error",
        "binance_demo_rate_limit_last_seen_at",
    )


def set_binance_demo_rate_limit_state(
    *,
    cooldown_until: str | None,
    last_error: str | None,
    last_seen_at: str | None = None,
) -> dict[str, str | None]:
    seen_at = last_seen_at or _utc_now_iso()
    set_binance_demo_runtime_state("binance_demo_rate_limit_cooldown_until", cooldown_until or "")
    set_binance_demo_runtime_state("binance_demo_rate_limit_last_error", last_error or "")
    set_binance_demo_runtime_state("binance_demo_rate_limit_last_seen_at", seen_at)
    return get_binance_demo_rate_limit_state()


def update_binance_demo_execution_status(vortex_trade_id: int, status: str, error: str | None = None) -> dict | None:
    _ensure_binance_demo_execution_schema()
    updated_at = _utc_now_iso()
    with _conn() as con:
        con.execute(
            """
            UPDATE binance_demo_executions
            SET status=?,
                error=?,
                updated_at=?
            WHERE vortex_trade_id=?
            """,
            (status, error, updated_at, int(vortex_trade_id)),
        )
    return get_binance_demo_execution(int(vortex_trade_id))
