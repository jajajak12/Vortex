from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import os
import threading
from typing import Any
from urllib.parse import urlparse

import db
from exchange.binance_demo import (
    BinanceApiError,
    BinanceDemoAdapter,
    DEFAULT_BASE_URL,
    decimal_to_str,
    load_dotenv_file,
    position_amt_from_payload,
    position_direction_from_amt,
    rate_limit_cooldown_until_from_error,
    to_decimal,
    verify_protection_orders,
)
from risk_manager import RiskManager
from vortex_logger import get_logger


ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}
DEFAULT_ALLOWED_STRATEGIES = ("S5", "S7", "S8", "S9", "S10")
DEFAULT_MAX_NOTIONAL_USDT = Decimal("5000")
ZERO_EPSILON = Decimal("0.00000001")
ACTIVE_MAPPING_STATUSES = {"ENTRY_PLACED", "PROTECTION_PLACED", "EXECUTED"}
RATE_LIMIT_REJECT_STATUS = "SKIPPED_RATE_LIMIT_COOLDOWN"
RATE_LIMIT_FAILED_CLEANUP_STATUS = "FAILED_CLEANUP_RATE_LIMITED"


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_allowed_strategies(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_STRATEGIES
    items = [item.strip().upper() for item in raw.split(",") if item.strip()]
    return tuple(dict.fromkeys(items)) or DEFAULT_ALLOWED_STRATEGIES


def _round_down_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RateLimitCooldownState:
    active: bool
    cooldown_until: datetime | None
    cooldown_until_raw: str | None
    last_error: str | None
    last_seen_at: str | None


def _parse_trade_time(value: Any) -> datetime:
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _base_url_host(base_url: str) -> str:
    return (urlparse(base_url).hostname or "").lower()


def _is_mainnet_like_host(base_url: str) -> bool:
    host = _base_url_host(base_url)
    return host in {"api.binance.com", "fapi.binance.com"} or "api.binance.com" in host or "fapi.binance.com" in host


@dataclass(frozen=True)
class BinanceDemoExecutionConfig:
    demo_mode: bool
    execution_enabled: bool
    auto_execution_enabled: bool
    base_url: str
    max_notional_usdt: Decimal
    allowed_strategies: tuple[str, ...]

    @property
    def combined_enabled(self) -> bool:
        return self.demo_mode and self.execution_enabled and self.auto_execution_enabled

    @classmethod
    def from_env(cls) -> "BinanceDemoExecutionConfig":
        load_dotenv_file()
        base_url = (os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        max_notional_raw = (os.getenv("BINANCE_DEMO_MAX_NOTIONAL_USDT") or "").strip()
        max_notional = to_decimal(max_notional_raw or DEFAULT_MAX_NOTIONAL_USDT)
        if max_notional <= 0:
            max_notional = DEFAULT_MAX_NOTIONAL_USDT
        if max_notional > DEFAULT_MAX_NOTIONAL_USDT:
            max_notional = DEFAULT_MAX_NOTIONAL_USDT
        return cls(
            demo_mode=_parse_bool(os.getenv("BINANCE_DEMO_MODE"), default=False),
            execution_enabled=_parse_bool(os.getenv("BINANCE_EXECUTION_ENABLED"), default=False),
            auto_execution_enabled=_parse_bool(os.getenv("BINANCE_AUTO_DEMO_EXECUTION_ENABLED"), default=False),
            base_url=base_url,
            max_notional_usdt=max_notional,
            allowed_strategies=_parse_allowed_strategies(os.getenv("BINANCE_DEMO_ALLOWED_STRATEGIES")),
        )


class BinanceDemoAutoExecutor:
    def __init__(self, *, startup_time: datetime | None = None) -> None:
        self.log = get_logger("exchange.binance_demo_executor")
        self.config = BinanceDemoExecutionConfig.from_env()
        self.startup_time = startup_time or datetime.now(timezone.utc)
        self.startup_time_iso = self.startup_time.isoformat()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="binance-demo-auto")
        self._lock = threading.Lock()
        db.set_binance_demo_auto_startup(self.startup_time_iso)
        self.log_startup_state()

    def log_startup_state(self) -> None:
        if self.config.combined_enabled:
            self.log.info(
                "[BINANCE_DEMO] auto execution enabled "
                "demo_mode=%s execution_enabled=%s auto_enabled=%s base_url=%s allowed_strategies=%s max_notional_usdt=%s",
                self.config.demo_mode,
                self.config.execution_enabled,
                self.config.auto_execution_enabled,
                self.config.base_url,
                ",".join(self.config.allowed_strategies),
                self.config.max_notional_usdt,
            )
            return

        self.log.info(
            "[BINANCE_DEMO] auto execution disabled "
            "demo_mode=%s execution_enabled=%s auto_enabled=%s base_url=%s allowed_strategies=%s max_notional_usdt=%s",
            self.config.demo_mode,
            self.config.execution_enabled,
            self.config.auto_execution_enabled,
            self.config.base_url,
            ",".join(self.config.allowed_strategies),
            self.config.max_notional_usdt,
        )

    def submit_trade(self, trade: dict[str, Any], *, cycle_started_at: datetime | None = None) -> None:
        cycle_started_at_iso = cycle_started_at.isoformat() if cycle_started_at else None
        self._pool.submit(self._mirror_trade_safe, dict(trade), cycle_started_at_iso)

    def _mirror_trade_safe(self, trade: dict[str, Any], cycle_started_at_iso: str | None) -> None:
        try:
            with self._lock:
                self._mirror_trade(trade, cycle_started_at_iso)
        except Exception as exc:
            self.log.error(
                "[BINANCE_DEMO] auto_execution_failed trade_id=%s error=%s",
                trade.get("id"),
                exc,
                exc_info=True,
            )
            self._record_execution_row(
                trade,
                status="REJECTED_VALIDATION",
                error=str(exc),
            )

    def _mirror_trade(self, trade: dict[str, Any], cycle_started_at_iso: str | None) -> None:
        trade_id = int(trade["id"])
        existing = db.get_binance_demo_execution(trade_id)
        if existing is not None:
            self.log.info(
                "[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=duplicate_mapping status=%s",
                trade_id,
                existing.get("status"),
            )
            return

        stale_cutoff = self.startup_time
        if cycle_started_at_iso:
            stale_cutoff = max(stale_cutoff, datetime.fromisoformat(cycle_started_at_iso))

        if trade.get("status") != "OPEN":
            self.log.info("[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=not_open", trade_id)
            self._record_execution_row(trade, status="SKIPPED_NOT_ALLOWED", error="trade_status_not_open")
            return

        if str(trade.get("strategy", "")).upper() not in self.config.allowed_strategies:
            self.log.info(
                "[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=strategy_not_allowed strategy=%s",
                trade_id,
                trade.get("strategy"),
            )
            self._record_execution_row(trade, status="SKIPPED_NOT_ALLOWED", error="strategy_not_allowed")
            return

        if str(trade.get("pair", "")).upper() not in ALLOWED_SYMBOLS:
            self.log.info(
                "[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=symbol_not_allowed symbol=%s",
                trade_id,
                trade.get("pair"),
            )
            self._record_execution_row(trade, status="SKIPPED_NOT_ALLOWED", error="symbol_not_allowed")
            return

        trade_opened_at = _parse_trade_time(trade["time"])
        if trade_opened_at < stale_cutoff:
            self.log.info(
                "[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=stale opened_at=%s cutoff=%s",
                trade_id,
                trade_opened_at.isoformat(),
                stale_cutoff.isoformat(),
            )
            self._record_execution_row(trade, status="SKIPPED_STALE", error="trade_predates_auto_startup")
            return

        if not self.config.combined_enabled:
            self.log.info(
                "[BINANCE_DEMO] auto_execution_skipped reason=disabled trade_id=%s execution_enabled=%s auto_enabled=%s demo_mode=%s",
                trade_id,
                self.config.execution_enabled,
                self.config.auto_execution_enabled,
                self.config.demo_mode,
            )
            self._record_execution_row(trade, status="SKIPPED_DISABLED", error="auto_execution_disabled")
            return

        if _is_mainnet_like_host(self.config.base_url):
            self.log.warning(
                "[BINANCE_DEMO] auto_execution_rejected trade_id=%s reason=mainnet_like_base_url base_url=%s",
                trade_id,
                self.config.base_url,
            )
            self._record_execution_row(trade, status="REJECTED_VALIDATION", error="base_url_looks_like_mainnet")
            return

        cooldown = self.get_rate_limit_cooldown_state()
        if cooldown.active:
            cooldown_until_text = cooldown.cooldown_until_raw or "<unknown>"
            self.log.warning(
                "[BINANCE_DEMO] auto_execution_skipped trade_id=%s reason=rate_limit_cooldown cooldown_until=%s",
                trade_id,
                cooldown_until_text,
            )
            self._record_execution_row(
                trade,
                status=RATE_LIMIT_REJECT_STATUS,
                error=f"binance_rate_limit_cooldown_until={cooldown_until_text}",
            )
            return

        self._execute_trade(trade)

    def _execute_trade(self, trade: dict[str, Any]) -> None:
        trade_id = int(trade["id"])
        symbol = str(trade["pair"]).upper()
        strategy = str(trade["strategy"]).upper()
        direction = str(trade["direction"]).upper()
        adapter = BinanceDemoAdapter()

        entry_order: dict[str, Any] | None = None
        tp_order: dict[str, Any] | None = None
        sl_order: dict[str, Any] | None = None
        quantity = Decimal("0")
        rounded_tp = Decimal("0")
        rounded_sl = Decimal("0")
        exit_side = ""
        entry_executed = False

        try:
            ticker = adapter.ticker_price(symbol)
            filters = adapter.get_symbol_filters(symbol)
            position_payload = adapter.get_position_risk(symbol)
            open_orders = adapter.get_open_orders(symbol)
            open_algo_orders = adapter.get_open_algo_orders(symbol)

            active_symbol_mappings = self._list_active_symbol_mappings(symbol, exclude_trade_id=trade_id)
            eligibility = self._evaluate_symbol_eligibility(
                symbol=symbol,
                trade_direction=direction,
                position_payload=position_payload,
                open_orders=open_orders,
                open_algo_orders=open_algo_orders,
                active_symbol_mappings=active_symbol_mappings,
            )
            if not eligibility["allowed"]:
                error = str(eligibility["reason"])
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error=error)
                self.log.warning(
                    "[BINANCE_DEMO] auto_execution_rejected trade_id=%s symbol=%s reason=%s",
                    trade_id,
                    symbol,
                    error,
                )
                return

            current_price = to_decimal(ticker["price"])
            sl = to_decimal(trade["sl"])
            tp = to_decimal(trade["tp"])
            if direction == "LONG":
                if sl >= current_price:
                    self._record_execution_row(trade, status="REJECTED_VALIDATION", error="long_sl_not_below_market")
                    return
                if tp <= current_price:
                    self._record_execution_row(trade, status="REJECTED_VALIDATION", error="long_tp_not_above_market")
                    return
                entry_side = "BUY"
                exit_side = "SELL"
                risk_per_unit = current_price - sl
            elif direction == "SHORT":
                if sl <= current_price:
                    self._record_execution_row(trade, status="REJECTED_VALIDATION", error="short_sl_not_above_market")
                    return
                if tp >= current_price:
                    self._record_execution_row(trade, status="REJECTED_VALIDATION", error="short_tp_not_below_market")
                    return
                entry_side = "SELL"
                exit_side = "BUY"
                risk_per_unit = sl - current_price
            else:
                self._record_execution_row(trade, status="SKIPPED_NOT_ALLOWED", error="direction_not_supported")
                return

            if risk_per_unit <= 0:
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error="risk_per_unit_not_positive")
                return

            risk_usd = self._risk_usd_for_trade(trade)
            step_size = to_decimal(filters["stepSize"])
            tick_size = to_decimal(filters["tickSize"])
            min_qty = to_decimal(filters["minQty"])
            min_notional = to_decimal(filters["minNotional"])

            raw_qty = risk_usd / risk_per_unit
            rounded_qty = _round_down_to_increment(raw_qty, step_size)
            max_qty_for_cap = _round_down_to_increment(self.config.max_notional_usdt / current_price, step_size)
            quantity = min(rounded_qty, max_qty_for_cap)
            notional = quantity * current_price
            rounded_tp = _round_down_to_increment(tp, tick_size)
            rounded_sl = _round_down_to_increment(sl, tick_size)

            if quantity <= 0:
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error="quantity_zero_after_rounding")
                return
            if quantity < min_qty:
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error="quantity_below_min_qty")
                return
            if notional < min_notional:
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error="notional_below_min_notional")
                return
            if notional > self.config.max_notional_usdt:
                self._record_execution_row(trade, status="REJECTED_VALIDATION", error="notional_above_max_cap")
                return

            self._record_execution_row(
                trade,
                status="ENTRY_PLACED",
                quantity=float(quantity),
                notional=float(notional),
                risk_usd=float(risk_usd),
            )

            entry_order = adapter.place_market_order(
                symbol=symbol,
                side=entry_side,
                quantity=decimal_to_str(quantity),
                reduce_only=False,
            )
            entry_executed = True
            self._record_execution_row(
                trade,
                status="ENTRY_PLACED",
                quantity=float(quantity),
                notional=float(notional),
                risk_usd=float(risk_usd),
                entry_order_id=str(entry_order.get("orderId")),
            )

            tp_order = adapter.place_take_profit_market_order(
                symbol=symbol,
                side=exit_side,
                stop_price=decimal_to_str(rounded_tp),
                quantity=decimal_to_str(quantity),
                reduce_only=True,
            )
            sl_order = adapter.place_stop_market_order(
                symbol=symbol,
                side=exit_side,
                stop_price=decimal_to_str(rounded_sl),
                quantity=decimal_to_str(quantity),
                reduce_only=True,
            )
            self._record_execution_row(
                trade,
                status="PROTECTION_PLACED",
                quantity=float(quantity),
                notional=float(notional),
                risk_usd=float(risk_usd),
                entry_order_id=str(entry_order.get("orderId")),
                tp_order_id=str(tp_order.get("algoId")),
                sl_order_id=str(sl_order.get("algoId")),
            )

            verification = self._verify_live_protection(
                adapter=adapter,
                symbol=symbol,
                exit_side=exit_side,
                quantity=quantity,
                rounded_tp=rounded_tp,
                rounded_sl=rounded_sl,
                tick_size=tick_size,
                step_size=step_size,
                tp_order_id=str(tp_order.get("algoId")),
                sl_order_id=str(sl_order.get("algoId")),
            )
            if not verification.is_protected:
                raise RuntimeError(str(verification.error_code or "protection_verification_failed"))

            self._record_execution_row(
                trade,
                status="EXECUTED",
                quantity=float(quantity),
                notional=float(notional),
                risk_usd=float(risk_usd),
                entry_order_id=str(entry_order.get("orderId")),
                tp_order_id=str(tp_order.get("algoId")),
                sl_order_id=str(sl_order.get("algoId")),
            )
            self.log.info(
                "[BINANCE_DEMO] auto_execution_succeeded trade_id=%s symbol=%s quantity=%s notional=%s",
                trade_id,
                symbol,
                decimal_to_str(quantity),
                decimal_to_str(notional),
            )
        except BinanceApiError as exc:
            error_text = self._format_rate_limit_error(exc)
            if exc.is_rate_limit:
                cooldown = self._activate_rate_limit_cooldown(exc)
                cleanup_error = self._cleanup_new_leg(
                    adapter=adapter,
                    symbol=symbol,
                    exit_side=exit_side,
                    quantity=quantity,
                    tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                    sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
                    entry_executed=entry_executed,
                    allow_cleanup=False,
                )
                if cleanup_error:
                    error_text = f"{error_text} | cleanup={cleanup_error}"
                error_text = (
                    f"{error_text} | cooldown_until={cooldown.cooldown_until_raw or '<unknown>'}"
                    " | manual_review_required=true"
                )
                self._record_execution_row(
                    trade,
                    status="REJECTED_VALIDATION" if not entry_executed else RATE_LIMIT_FAILED_CLEANUP_STATUS,
                    error=error_text,
                    entry_order_id=str(entry_order.get("orderId")) if entry_order else None,
                    tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                    sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
                )
                self.log.error(
                    "[BINANCE_DEMO] rate_limit_fail_closed trade_id=%s symbol=%s entry_executed=%s cooldown_until=%s error=%s",
                    trade_id,
                    symbol,
                    _fmt_bool(entry_executed),
                    cooldown.cooldown_until_raw,
                    error_text,
                )
                return

            cleanup_error = self._cleanup_new_leg(
                adapter=adapter,
                symbol=symbol,
                exit_side=exit_side,
                quantity=quantity,
                tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
                entry_executed=entry_executed,
            )
            if cleanup_error:
                error_text = f"{error_text} | cleanup={cleanup_error}"
            self._record_execution_row(
                trade,
                status="FAILED_CLEANED_UP",
                error=error_text,
                entry_order_id=str(entry_order.get("orderId")) if entry_order else None,
                tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
            )
            self.log.error(
                "[BINANCE_DEMO] auto_execution_cleanup trade_id=%s error=%s",
                trade_id,
                error_text,
            )
        except Exception as exc:
            cleanup_error = self._cleanup_new_leg(
                adapter=adapter,
                symbol=symbol,
                exit_side=exit_side,
                quantity=quantity,
                tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
                entry_executed=entry_executed,
            )
            error_text = str(exc)
            if cleanup_error:
                error_text = f"{error_text} | cleanup={cleanup_error}"
            self._record_execution_row(
                trade,
                status="FAILED_CLEANED_UP",
                error=error_text,
                entry_order_id=str(entry_order.get("orderId")) if entry_order else None,
                tp_order_id=str(tp_order.get("algoId")) if tp_order else None,
                sl_order_id=str(sl_order.get("algoId")) if sl_order else None,
            )
            self.log.error(
                "[BINANCE_DEMO] auto_execution_cleanup trade_id=%s error=%s",
                trade_id,
                error_text,
            )

    def _list_active_symbol_mappings(self, symbol: str, *, exclude_trade_id: int | None = None) -> list[dict[str, Any]]:
        rows = db.list_binance_demo_executions(symbol=symbol)
        active_rows = []
        for row in rows:
            if exclude_trade_id is not None and int(row["vortex_trade_id"]) == int(exclude_trade_id):
                continue
            if str(row.get("status") or "").upper() in ACTIVE_MAPPING_STATUSES:
                trade = db.get_trade(int(row["vortex_trade_id"]))
                if trade and str(trade.get("status")) == "OPEN":
                    active_rows.append(row)
        return active_rows

    def _active_mapping_direction(self, active_symbol_mappings: list[dict[str, Any]]) -> str | None:
        directions = {str(row.get("direction") or "").upper() for row in active_symbol_mappings if row.get("direction")}
        if not directions:
            return None
        if len(directions) > 1:
            return "MIXED"
        return next(iter(directions))

    def _evaluate_symbol_eligibility(
        self,
        *,
        symbol: str,
        trade_direction: str,
        position_payload: Any,
        open_orders: list[dict[str, Any]] | None,
        open_algo_orders: list[dict[str, Any]] | None,
        active_symbol_mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        position_amt = position_amt_from_payload(position_payload, symbol)
        remote_direction = position_direction_from_amt(position_amt)
        active_direction = self._active_mapping_direction(active_symbol_mappings)
        has_orders = bool((open_orders or []) or (open_algo_orders or []))

        if active_direction == "MIXED":
            return {"allowed": False, "reason": "inconsistent_mapping_directions"}

        if has_orders and remote_direction == "NONE":
            return {"allowed": False, "reason": "symbol_has_orphan_remote_orders"}

        if remote_direction != "NONE" and active_direction is None:
            return {"allowed": False, "reason": "symbol_has_unmapped_remote_position"}

        if remote_direction != "NONE" and active_direction not in {None, remote_direction}:
            return {"allowed": False, "reason": "inconsistent_mapping_directions"}

        if remote_direction != "NONE" and remote_direction != trade_direction:
            return {"allowed": False, "reason": "symbol_has_opposite_remote_direction"}

        if active_direction not in {None, trade_direction}:
            return {"allowed": False, "reason": "symbol_has_opposite_active_mapped_direction"}

        return {"allowed": True, "reason": "eligible"}

    def _verify_live_protection(
        self,
        *,
        adapter: BinanceDemoAdapter,
        symbol: str,
        exit_side: str,
        quantity: Decimal,
        rounded_tp: Decimal,
        rounded_sl: Decimal,
        tick_size: Decimal,
        step_size: Decimal,
        tp_order_id: str,
        sl_order_id: str,
    ):
        verification = verify_protection_orders(
            algo_orders=adapter.get_open_algo_orders(symbol) or [],
            tp_order_id=tp_order_id,
            sl_order_id=sl_order_id,
            expected_side=exit_side,
            expected_quantity=quantity,
            expected_tp_trigger=rounded_tp,
            expected_sl_trigger=rounded_sl,
            qty_tolerance=step_size,
            price_tolerance=tick_size,
        )
        return verification

    def _cleanup_new_leg(
        self,
        *,
        adapter: BinanceDemoAdapter,
        symbol: str,
        exit_side: str,
        quantity: Decimal,
        tp_order_id: str | None,
        sl_order_id: str | None,
        entry_executed: bool,
        allow_cleanup: bool = True,
    ) -> str | None:
        if not allow_cleanup:
            return "cleanup_skipped_due_to_rate_limit_manual_review_required"
        errors: list[str] = []
        for algo_id in (tp_order_id, sl_order_id):
            if not algo_id:
                continue
            try:
                adapter.cancel_algo_order(algo_id)
            except Exception as exc:
                errors.append(f"cancel_algo_order[{algo_id}]={exc}")

        if entry_executed and quantity > 0 and exit_side:
            try:
                adapter.place_market_order(
                    symbol=symbol,
                    side=exit_side,
                    quantity=decimal_to_str(quantity),
                    reduce_only=True,
                )
            except Exception as exc:
                errors.append(f"close_leg={exc}")

        if not errors:
            return None
        return "; ".join(errors)

    def get_rate_limit_cooldown_state(self, *, now: datetime | None = None) -> RateLimitCooldownState:
        raw = db.get_binance_demo_rate_limit_state()
        now = now or datetime.now(timezone.utc)
        cooldown_until = _parse_iso_datetime(raw.get("binance_demo_rate_limit_cooldown_until"))
        return RateLimitCooldownState(
            active=bool(cooldown_until and cooldown_until > now),
            cooldown_until=cooldown_until,
            cooldown_until_raw=cooldown_until.isoformat() if cooldown_until else None,
            last_error=raw.get("binance_demo_rate_limit_last_error") or None,
            last_seen_at=raw.get("binance_demo_rate_limit_last_seen_at") or None,
        )

    def _activate_rate_limit_cooldown(self, error: BinanceApiError) -> RateLimitCooldownState:
        now = datetime.now(timezone.utc)
        cooldown_until = rate_limit_cooldown_until_from_error(error, now=now)
        db.set_binance_demo_rate_limit_state(
            cooldown_until=cooldown_until.isoformat(),
            last_error=self._format_rate_limit_error(error),
            last_seen_at=now.isoformat(),
        )
        return self.get_rate_limit_cooldown_state(now=now)

    def _format_rate_limit_error(self, error: BinanceApiError) -> str:
        parts = []
        if error.status_code is not None:
            parts.append(f"status={error.status_code}")
        if error.binance_code is not None:
            parts.append(f"code={error.binance_code}")
        parts.append(f"path={error.path}")
        parts.append(f"message={error.message}")
        if error.banned_until is not None:
            parts.append(f"banned_until={error.banned_until.isoformat()}")
        return "binance_rate_limit " + " ".join(parts)

    def _risk_usd_for_trade(self, trade: dict[str, Any]) -> Decimal:
        stored_risk = to_decimal(trade.get("risk_usd"), default="0")
        if stored_risk > 0:
            return stored_risk
        manager = RiskManager()
        current_equity = to_decimal(manager.get_current_equity())
        return to_decimal(min(float(current_equity) * 0.02, 500.0))

    def _record_execution_row(
        self,
        trade: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
        quantity: float | None = None,
        notional: float | None = None,
        risk_usd: float | None = None,
        entry_order_id: str | None = None,
        tp_order_id: str | None = None,
        sl_order_id: str | None = None,
    ) -> dict:
        return db.upsert_binance_demo_execution(
            {
                "vortex_trade_id": int(trade["id"]),
                "symbol": str(trade["pair"]).upper(),
                "strategy": str(trade["strategy"]).upper(),
                "direction": str(trade["direction"]).upper(),
                "quantity": quantity,
                "notional": notional,
                "risk_usd": risk_usd,
                "entry_order_id": entry_order_id,
                "tp_order_id": tp_order_id,
                "sl_order_id": sl_order_id,
                "status": status,
                "error": error,
            }
        )
