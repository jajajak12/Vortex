from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import db
from exchange.binance_demo import (
    BANNED_UNTIL_BUFFER,
    BinanceDemoAdapter,
    ProtectionVerificationResult,
    RATE_LIMIT_FALLBACK_COOLDOWN,
    find_position_row,
    is_sl_algo_order,
    is_tp_algo_order,
    order_quantity,
    order_reduce_only,
    parse_rate_limit_banned_until,
    position_direction_from_amt,
    to_decimal,
    verify_protection_orders,
)
from exchange.binance_demo_executor import BinanceDemoExecutionConfig


ACTIVE_MAPPING_STATUSES = {"ENTRY_PLACED", "PROTECTION_PLACED", "EXECUTED"}
REJECTED_MAPPING_STATUSES = {"REJECTED_VALIDATION", "SKIPPED_RATE_LIMIT_COOLDOWN"}
FAILED_MAPPING_STATUSES = {"FAILED_CLEANED_UP", "FAILED_CLEANUP_RATE_LIMITED"}
SAFE_DB_REMOTE_CLOSED_FROM = {"EXECUTED"}
REMOTE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")


def _trade_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _normalize_symbol(symbol: str | None) -> str:
    return str(symbol or "").upper()


def _direction_exit_side(direction: str) -> str:
    normalized = str(direction or "").upper()
    if normalized == "LONG":
        return "SELL"
    if normalized == "SHORT":
        return "BUY"
    return ""


def _remote_error_is_rate_limit(error_text: str | None) -> bool:
    lowered = str(error_text or "").lower()
    return (
        "status=418" in lowered
        or "code=-1003" in lowered
        or "too many requests" in lowered
        or "way too many requests" in lowered
        or "banned until" in lowered
    )


def _effective_remote_rate_limit_state(remote_by_symbol: dict[str, "SymbolRemoteState"]) -> tuple[bool, str | None, str | None, str | None]:
    now = datetime.now(timezone.utc)
    active_until: datetime | None = None
    last_error: str | None = None
    for remote in remote_by_symbol.values():
        if not _remote_error_is_rate_limit(remote.remote_error):
            continue
        parsed_ban = parse_rate_limit_banned_until(str(remote.remote_error))
        candidate = (parsed_ban + BANNED_UNTIL_BUFFER) if parsed_ban else (now + RATE_LIMIT_FALLBACK_COOLDOWN)
        if active_until is None or candidate > active_until:
            active_until = candidate
            last_error = remote.remote_error
    return (
        bool(active_until and active_until > now),
        active_until.isoformat() if active_until else None,
        last_error,
        now.isoformat() if active_until else None,
    )


@dataclass
class SymbolRemoteState:
    symbol: str
    position_amt: Decimal
    notional: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    position_direction: str
    standard_orders: list[dict[str, Any]]
    algo_orders: list[dict[str, Any]]
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    active_mappings: list[dict[str, Any]]
    active_direction_values: list[str]
    active_quantity_sum: Decimal
    active_reduce_only_algo_quantity: Decimal
    protection_complete: bool
    remote_error: str | None

    @property
    def has_position(self) -> bool:
        return self.position_direction != "NONE"

    @property
    def has_orders(self) -> bool:
        return bool(self.standard_orders or self.algo_orders)

    @property
    def tp_algo_count(self) -> int:
        return sum(1 for order in self.algo_orders if is_tp_algo_order(order))

    @property
    def sl_algo_count(self) -> int:
        return sum(1 for order in self.algo_orders if is_sl_algo_order(order))


def _build_symbol_remote_state(adapter: BinanceDemoAdapter, symbol: str, active_mappings: list[dict[str, Any]]) -> SymbolRemoteState:
    active_directions = sorted({str(row.get("direction") or "").upper() for row in active_mappings if row.get("direction")})
    active_quantity_sum = sum((to_decimal(row.get("quantity"), default="0") for row in active_mappings), Decimal("0"))
    try:
        position_payload = adapter.get_position_risk(symbol)
        position_row = find_position_row(position_payload, symbol) or {}
        filters = adapter.get_symbol_filters(symbol)
        standard_orders = list(adapter.get_open_orders(symbol) or [])
        algo_orders = list(adapter.get_open_algo_orders(symbol) or [])
        position_amt = to_decimal(position_row.get("positionAmt", "0"))
        active_tp_quantity = sum(
            (order_quantity(order) for order in algo_orders if order_reduce_only(order) and is_tp_algo_order(order)),
            Decimal("0"),
        )
        active_sl_quantity = sum(
            (order_quantity(order) for order in algo_orders if order_reduce_only(order) and is_sl_algo_order(order)),
            Decimal("0"),
        )
        active_reduce_only_algo_quantity = max(active_tp_quantity, active_sl_quantity)
        remote_error = None
    except Exception as exc:
        filters = {"tickSize": "0", "stepSize": "0", "minQty": "0"}
        standard_orders = []
        algo_orders = []
        position_amt = Decimal("0")
        position_row = {}
        active_reduce_only_algo_quantity = Decimal("0")
        remote_error = str(exc)
    return SymbolRemoteState(
        symbol=symbol,
        position_amt=position_amt,
        notional=to_decimal(position_row.get("notional", "0")),
        entry_price=to_decimal(position_row.get("entryPrice", "0")),
        unrealized_pnl=to_decimal(position_row.get("unRealizedProfit", "0")),
        position_direction=position_direction_from_amt(position_amt),
        standard_orders=standard_orders,
        algo_orders=algo_orders,
        tick_size=to_decimal(filters.get("tickSize"), default="0"),
        step_size=to_decimal(filters.get("stepSize"), default="0"),
        min_qty=to_decimal(filters.get("minQty"), default="0"),
        active_mappings=active_mappings,
        active_direction_values=active_directions,
        active_quantity_sum=active_quantity_sum,
        active_reduce_only_algo_quantity=active_reduce_only_algo_quantity,
        protection_complete=False,
        remote_error=remote_error,
    )


def _protection_status_from_verification(
    mapping: dict[str, Any],
    trade: dict[str, Any] | None,
    remote: SymbolRemoteState,
    verification: ProtectionVerificationResult | None,
) -> str:
    status = str(mapping.get("status") or "").upper()
    if status not in ACTIVE_MAPPING_STATUSES:
        return "LOCAL_ONLY"
    if not remote.has_position:
        return "POSITION_MISSING"
    if verification is None:
        return "LOCAL_ONLY"
    if verification.is_protected:
        return "PROTECTED"
    if not verification.tp_order_live and not verification.sl_order_live:
        return "TP_SL_MISSING"
    if not verification.tp_order_live:
        return "TP_MISSING"
    if not verification.sl_order_live:
        return "SL_MISSING"
    return "LOCAL_ONLY"


def _verify_mapping_protection(
    mapping: dict[str, Any],
    trade: dict[str, Any] | None,
    remote: SymbolRemoteState,
) -> tuple[str, bool, bool, bool, ProtectionVerificationResult | None]:
    if str(mapping.get("status") or "").upper() not in ACTIVE_MAPPING_STATUSES:
        return "LOCAL_ONLY", False, False, remote.has_position, None

    if not trade:
        return "LOCAL_ONLY", False, False, remote.has_position, None

    quantity = to_decimal(mapping.get("quantity"), default="0")
    if quantity <= 0:
        return "LOCAL_ONLY", False, False, remote.has_position, None

    verification = verify_protection_orders(
        algo_orders=remote.algo_orders,
        tp_order_id=str(mapping.get("tp_order_id") or ""),
        sl_order_id=str(mapping.get("sl_order_id") or ""),
        expected_side=_direction_exit_side(str(mapping.get("direction") or trade.get("direction") or "")),
        expected_quantity=quantity,
        expected_tp_trigger=to_decimal(trade.get("tp"), default="0"),
        expected_sl_trigger=to_decimal(trade.get("sl"), default="0"),
        qty_tolerance=remote.step_size,
        price_tolerance=remote.tick_size,
    )
    protection_status = _protection_status_from_verification(mapping, trade, remote, verification)
    return (
        protection_status,
        verification.tp_order_live,
        verification.sl_order_live,
        remote.has_position,
        verification,
    )


def determine_reconciliation_class(
    *,
    mapping: dict[str, Any],
    trade: dict[str, Any] | None,
    remote: SymbolRemoteState,
    protection_status: str,
    verification: ProtectionVerificationResult | None,
) -> str:
    status = str(mapping.get("status") or "").upper()
    local_status = str(trade.get("status") or "") if trade else "MISSING"
    error = str(mapping.get("error") or "").lower()

    if local_status == "OPEN" and status in ACTIVE_MAPPING_STATUSES and remote.has_position and protection_status == "PROTECTED":
        return "CONSISTENT_ACTIVE"
    if local_status == "CLOSED" and not remote.has_position and not remote.has_orders:
        return "CONSISTENT_CLOSED"
    if local_status == "OPEN" and status in ACTIVE_MAPPING_STATUSES and not remote.has_position and not remote.has_orders:
        return "LOCAL_OPEN_REMOTE_MISSING"
    if local_status == "CLOSED" and (remote.has_position or remote.has_orders):
        return "LOCAL_CLOSED_REMOTE_OPEN"
    if status in REJECTED_MAPPING_STATUSES:
        if any(token in error for token in (
            "symbol_has_opposite_remote_direction",
            "symbol_has_opposite_active_mapped_direction",
            "symbol_has_orphan_remote_orders",
            "symbol_has_unmapped_remote_position",
            "inconsistent_mapping_directions",
        )):
            return "SYMBOL_BLOCKED_BY_ACTIVE_REMOTE"
        return "REJECTED_NOT_MIRRORED"
    if status in FAILED_MAPPING_STATUSES:
        return "FAILED_NOT_MIRRORED"
    if local_status in {"CLOSED", "MISSING"} and remote.has_orders and not remote.has_position:
        return "ORPHAN_REMOTE_ORDER"
    if status in ACTIVE_MAPPING_STATUSES and protection_status != "PROTECTED":
        return "LOCAL_OPEN_REMOTE_MISSING"
    return "UNCLASSIFIED"


def determine_open_trade_mirror_status(
    *,
    trade: dict[str, Any],
    mapping: dict[str, Any] | None,
    startup_at: str | None,
    remote: SymbolRemoteState,
    protection_status: str,
) -> str:
    if str(trade.get("status")) != "OPEN":
        return "NOT_OPEN"

    if mapping:
        mapping_status = str(mapping.get("status") or "").upper()
        mapping_error = str(mapping.get("error") or "").lower()
        if mapping_status in ACTIVE_MAPPING_STATUSES:
            return "MIRRORED_ACTIVE_PROTECTED" if protection_status == "PROTECTED" else "MIRRORED_ACTIVE_UNPROTECTED"
        if mapping_status in REJECTED_MAPPING_STATUSES:
            if "symbol_has_opposite_remote_direction" in mapping_error or "symbol_has_opposite_active_mapped_direction" in mapping_error:
                return "BLOCKED_OPPOSITE_DIRECTION"
            if "symbol_has_orphan_remote_orders" in mapping_error:
                return "BLOCKED_ORPHAN_REMOTE_ORDER"
            if "symbol_has_unmapped_remote_position" in mapping_error or "inconsistent_mapping_directions" in mapping_error:
                return "BLOCKED_UNMAPPED_REMOTE_POSITION"
            return "LOCAL_ONLY_REJECTED"
        if mapping_status in FAILED_MAPPING_STATUSES:
            return "LOCAL_ONLY_FAILED"
        if mapping_status == "MANUAL_ONLY_PRESTARTUP":
            return "MANUAL_ONLY_PRESTARTUP"

    if startup_at and _trade_time(str(trade["time"])) < datetime.fromisoformat(startup_at).replace(tzinfo=None):
        return "MANUAL_ONLY_PRESTARTUP"

    if remote.has_position and remote.position_direction != str(trade.get("direction") or "").upper():
        return "BLOCKED_OPPOSITE_DIRECTION"
    if remote.has_orders and not remote.has_position:
        return "BLOCKED_ORPHAN_REMOTE_ORDER"
    if remote.has_position and not remote.active_mappings:
        return "BLOCKED_UNMAPPED_REMOTE_POSITION"
    return "LOCAL_ONLY_PENDING_NEW"


def build_reconciliation_snapshot() -> dict[str, Any]:
    config = BinanceDemoExecutionConfig.from_env()
    adapter = BinanceDemoAdapter()
    startup_at = db.get_binance_demo_auto_startup()
    rate_limit_state = db.get_binance_demo_rate_limit_state()
    cooldown_until = rate_limit_state.get("binance_demo_rate_limit_cooldown_until") or None
    last_rate_limit_error = rate_limit_state.get("binance_demo_rate_limit_last_error") or None
    last_rate_limit_seen_at = rate_limit_state.get("binance_demo_rate_limit_last_seen_at") or None
    mappings = db.list_binance_demo_executions()
    all_trades = db.get_all_trades()
    trades_by_id = {int(trade["id"]): trade for trade in all_trades}
    open_trades = [trade for trade in all_trades if str(trade.get("status")) == "OPEN"]

    mappings_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    active_mappings_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        symbol = _normalize_symbol(mapping.get("symbol"))
        mappings_by_symbol[symbol].append(mapping)
        if str(mapping.get("status") or "").upper() in ACTIVE_MAPPING_STATUSES:
            trade = trades_by_id.get(int(mapping["vortex_trade_id"]))
            if trade and str(trade.get("status")) == "OPEN":
                active_mappings_by_symbol[symbol].append(mapping)

    remote_by_symbol = {
        symbol: _build_symbol_remote_state(adapter, symbol, active_mappings_by_symbol.get(symbol, []))
        for symbol in REMOTE_SYMBOLS
    }

    active_mapping_rows: list[dict[str, Any]] = []
    reconciliation_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    remote_unmapped_symbols: list[str] = []
    orphan_order_symbols: list[str] = []

    for mapping in mappings:
        symbol = _normalize_symbol(mapping.get("symbol"))
        trade = trades_by_id.get(int(mapping["vortex_trade_id"]))
        remote = remote_by_symbol[symbol]
        protection_status, tp_live, sl_live, position_live, verification = _verify_mapping_protection(mapping, trade, remote)
        reconciliation_class = determine_reconciliation_class(
            mapping=mapping,
            trade=trade,
            remote=remote,
            protection_status=protection_status,
            verification=verification,
        )
        row = {
            "mapping": mapping,
            "trade": trade,
            "symbol": symbol,
            "remote": remote,
            "reconciliation_class": reconciliation_class,
            "protection_status": protection_status,
            "tp_order_live": tp_live,
            "sl_order_live": sl_live,
            "position_live": position_live,
            "verification": verification,
        }
        reconciliation_rows.append(row)
        if str(mapping.get("status") or "").upper() in ACTIVE_MAPPING_STATUSES and trade and str(trade.get("status")) == "OPEN":
            active_mapping_rows.append(row)

        if remote.remote_error:
            warnings.append(f"Remote state read failed for {symbol}: {remote.remote_error}")
            continue

        if reconciliation_class == "LOCAL_CLOSED_REMOTE_OPEN":
            warnings.append(f"Vortex CLOSED but Binance position/order open for {symbol} trade_id={mapping['vortex_trade_id']}")
        if trade and str(trade.get("status")) == "OPEN" and str(mapping.get("status") or "").upper() == "EXECUTED" and not position_live:
            warnings.append(f"Vortex OPEN + mapping EXECUTED but remote position missing for {symbol} trade_id={mapping['vortex_trade_id']}")
        if tp_live and not sl_live:
            warnings.append(f"TP exists without SL for {symbol} trade_id={mapping['vortex_trade_id']}")
        if sl_live and not tp_live:
            warnings.append(f"SL exists without TP for {symbol} trade_id={mapping['vortex_trade_id']}")
        if verification and verification.error_code and tp_live and sl_live:
            warnings.append(f"Protection verification mismatch for {symbol} trade_id={mapping['vortex_trade_id']} error={verification.error_code}")

    for symbol, remote in remote_by_symbol.items():
        live_rows = [row for row in active_mapping_rows if row["symbol"] == symbol]
        protected_rows = [row for row in live_rows if row["protection_status"] == "PROTECTED"]
        remote.protection_complete = bool(live_rows) and len(protected_rows) == len(live_rows)

        if remote.remote_error:
            warnings.append(f"Remote state read failed for {symbol}: {remote.remote_error}")
            continue

        if len(remote.active_direction_values) > 1:
            warnings.append(f"Mixed active directions on {symbol}: {','.join(remote.active_direction_values)}")

        if remote.tp_algo_count > 0 and remote.sl_algo_count == 0:
            warnings.append(f"TP exists without SL for {symbol}")
        if remote.sl_algo_count > 0 and remote.tp_algo_count == 0:
            warnings.append(f"SL exists without TP for {symbol}")

        if remote.active_reduce_only_algo_quantity > abs(remote.position_amt) and remote.has_position:
            warnings.append(
                f"Active reduceOnly algo quantity exceeds current position quantity for {symbol} "
                f"algo_qty={remote.active_reduce_only_algo_quantity} position_qty={abs(remote.position_amt)}"
            )

        if remote.has_position and not live_rows:
            remote_unmapped_symbols.append(symbol)
            warnings.append(f"Remote position exists with no active mapping for {symbol}")
        if remote.has_orders and not live_rows and not remote.has_position:
            orphan_order_symbols.append(symbol)
            warnings.append(f"Remote order exists with no active mapping for {symbol}")

    open_trade_rows: list[dict[str, Any]] = []
    for trade in sorted(open_trades, key=lambda item: item["time"]):
        symbol = _normalize_symbol(trade.get("pair"))
        mapping = db.get_binance_demo_execution(int(trade["id"]))
        remote = remote_by_symbol[symbol]
        active_row = next((row for row in active_mapping_rows if int(row["mapping"]["vortex_trade_id"]) == int(trade["id"])), None)
        protection_status = active_row["protection_status"] if active_row else "LOCAL_ONLY"
        mirror_status = determine_open_trade_mirror_status(
            trade=trade,
            mapping=mapping,
            startup_at=startup_at,
            remote=remote,
            protection_status=protection_status,
        )
        open_trade_rows.append(
            {
                "trade": trade,
                "mapping": mapping,
                "remote": remote,
                "mirror_status": mirror_status,
                "protection_status": protection_status,
            }
        )

    unique_warnings = list(dict.fromkeys(warnings))
    recommended_action = "NO_ACTION"
    cooldown_active = False
    if cooldown_until:
        try:
            cooldown_active = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00")) > datetime.now().astimezone()
        except ValueError:
            cooldown_active = False
    if not cooldown_active:
        live_cooldown_active, live_cooldown_until, live_last_error, live_last_seen_at = _effective_remote_rate_limit_state(remote_by_symbol)
        if live_cooldown_active:
            cooldown_active = True
            cooldown_until = live_cooldown_until
            last_rate_limit_error = live_last_error
            last_rate_limit_seen_at = live_last_seen_at
    severe = any(
        token in " | ".join(unique_warnings)
        for token in (
            "Remote position exists with no active mapping",
            "Remote order exists with no active mapping",
            "Mixed active directions",
            "Protection verification mismatch",
        )
    )
    if cooldown_active:
        recommended_action = "WAIT_FOR_BINANCE_RATE_LIMIT_COOLDOWN"
    elif config.auto_execution_enabled and severe:
        recommended_action = "DISABLE_AUTO_AND_RECONCILE"
    elif orphan_order_symbols:
        recommended_action = "CLEANUP_ORPHAN_ORDERS"
    elif remote_unmapped_symbols:
        recommended_action = "MANUAL_REVIEW_REMOTE_POSITION"
    elif open_trade_rows and all(
        row["mirror_status"] in {"LOCAL_ONLY_REJECTED", "LOCAL_ONLY_FAILED", "MANUAL_ONLY_PRESTARTUP"}
        for row in open_trade_rows
    ):
        recommended_action = "WAIT_FOR_NEW_TRADE"

    return {
        "config": config,
        "startup_at": startup_at,
        "rate_limit_cooldown_active": cooldown_active,
        "rate_limit_cooldown_until": cooldown_until,
        "last_rate_limit_error": last_rate_limit_error,
        "last_rate_limit_seen_at": last_rate_limit_seen_at,
        "mappings": mappings,
        "reconciliation_rows": reconciliation_rows,
        "active_mapping_rows": active_mapping_rows,
        "open_trade_rows": open_trade_rows,
        "remote_by_symbol": remote_by_symbol,
        "warnings": unique_warnings,
        "recommended_action": recommended_action,
        "diagnostic_note": (
            "Binance UI position TP/SL field may be blank because Vortex uses per-leg reduceOnly "
            "conditional/algo orders. Protection is verified by live TP/SL algo orders per mapping."
        ),
        "adapter_order_calls_attempted": adapter.order_calls_attempted,
        "adapter_order_endpoints_called": adapter.order_endpoints_called,
    }
