from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from vortex_logger import get_logger


DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
RECV_WINDOW_MS = 5000
ZERO_EPSILON = Decimal("0.00000001")
RATE_LIMIT_FALLBACK_COOLDOWN = timedelta(minutes=15)
BANNED_UNTIL_BUFFER = timedelta(seconds=60)


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def to_decimal(value: Any, *, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def decimal_to_str(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def find_position_row(position_payload: Any, symbol: str) -> dict[str, Any] | None:
    rows = position_payload if isinstance(position_payload, list) else [position_payload]
    for row in rows or []:
        if str(row.get("symbol", "")).upper() == symbol.upper():
            return row
    return None


def position_amt_from_payload(position_payload: Any, symbol: str) -> Decimal:
    row = find_position_row(position_payload, symbol)
    if not row:
        return Decimal("0")
    return to_decimal(row.get("positionAmt", "0"))


def position_direction_from_amt(position_amt: Decimal) -> str:
    if position_amt > ZERO_EPSILON:
        return "LONG"
    if position_amt < -ZERO_EPSILON:
        return "SHORT"
    return "NONE"


def order_type(order: dict[str, Any]) -> str:
    return str(
        order.get("orderType")
        or order.get("type")
        or order.get("strategyType")
        or order.get("algoType")
        or ""
    ).upper()


def is_tp_algo_order(order: dict[str, Any]) -> bool:
    return "TAKE_PROFIT" in order_type(order)


def is_sl_algo_order(order: dict[str, Any]) -> bool:
    normalized = order_type(order)
    return "STOP" in normalized and "TAKE_PROFIT" not in normalized


def order_reduce_only(order: dict[str, Any]) -> bool:
    value = order.get("reduceOnly")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def order_trigger_price(order: dict[str, Any]) -> Decimal:
    return to_decimal(order.get("triggerPrice") or order.get("stopPrice") or order.get("activatePrice") or "0")


def order_quantity(order: dict[str, Any]) -> Decimal:
    return to_decimal(order.get("quantity") or order.get("origQty") or order.get("executedQty") or "0")


def order_identifier(order: dict[str, Any]) -> str:
    value = order.get("algoId")
    if value is None or value == "":
        value = order.get("orderId")
    return str(value or "")


@dataclass(frozen=True)
class ProtectionVerificationResult:
    protection_status: str
    error_code: str | None
    tp_order_live: bool
    sl_order_live: bool
    tp_reduce_only_ok: bool
    sl_reduce_only_ok: bool
    tp_side_ok: bool
    sl_side_ok: bool
    tp_qty_ok: bool
    sl_qty_ok: bool
    tp_type_ok: bool
    sl_type_ok: bool
    tp_trigger_ok: bool
    sl_trigger_ok: bool

    @property
    def is_protected(self) -> bool:
        return self.protection_status == "PROTECTED" and self.error_code is None


@dataclass(frozen=True)
class BinanceApiError(RuntimeError):
    status_code: int | None
    binance_code: int | None
    message: str
    path: str
    response_body: str | None = None
    banned_until: datetime | None = None

    def __str__(self) -> str:
        parts = [f"Binance API error path={self.path}"]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.binance_code is not None:
            parts.append(f"code={self.binance_code}")
        parts.append(f"message={self.message}")
        if self.banned_until is not None:
            parts.append(f"banned_until={self.banned_until.isoformat()}")
        return " ".join(parts)

    @property
    def is_rate_limit(self) -> bool:
        lowered = self.message.lower()
        return (
            self.status_code == 418
            or self.binance_code == -1003
            or "too many requests" in lowered
            or "way too many requests" in lowered
            or "banned until" in lowered
        )


def parse_rate_limit_banned_until(message: str, *, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    patterns = (
        r"banned until\s+(\d{13})",
        r"banned until\s+(\d{10})",
        r"banned until\s+([0-9]{4}-[0-9]{2}-[0-9]{2}[ t][0-9:.]+(?:z|[+-][0-9:]+)?)",
    )
    lowered = message.lower()
    if "banned until" not in lowered:
        return None

    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            if raw.isdigit():
                timestamp = int(raw)
                if len(raw) >= 13:
                    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
                return datetime.fromtimestamp(timestamp, tz=timezone.utc)
            normalized = raw.replace("Z", "+00:00").replace("z", "+00:00").replace(" ", "T")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            continue

    numeric_matches = re.findall(r"\d{10,13}", message)
    for raw in numeric_matches:
        try:
            timestamp = int(raw)
            candidate = datetime.fromtimestamp(
                timestamp / 1000 if len(raw) >= 13 else timestamp,
                tz=timezone.utc,
            )
        except (OSError, OverflowError, ValueError):
            continue
        if candidate >= now - timedelta(minutes=5):
            return candidate
    return None


def rate_limit_cooldown_until_from_error(
    error: BinanceApiError,
    *,
    now: datetime | None = None,
) -> datetime:
    now = now or datetime.now(timezone.utc)
    if error.banned_until is not None:
        return error.banned_until + BANNED_UNTIL_BUFFER
    return now + RATE_LIMIT_FALLBACK_COOLDOWN


def verify_protection_orders(
    *,
    algo_orders: list[dict[str, Any]] | None,
    tp_order_id: str | None,
    sl_order_id: str | None,
    expected_side: str,
    expected_quantity: Decimal,
    expected_tp_trigger: Decimal,
    expected_sl_trigger: Decimal,
    qty_tolerance: Decimal,
    price_tolerance: Decimal,
) -> ProtectionVerificationResult:
    orders_by_id = {order_identifier(order): order for order in (algo_orders or [])}
    tp_order = orders_by_id.get(str(tp_order_id or ""))
    sl_order = orders_by_id.get(str(sl_order_id or ""))

    tp_live = tp_order is not None
    sl_live = sl_order is not None
    if not tp_live and not sl_live:
        return ProtectionVerificationResult(
            protection_status="TP_SL_MISSING",
            error_code="protection_verification_failed_tp_sl_missing",
            tp_order_live=False,
            sl_order_live=False,
            tp_reduce_only_ok=False,
            sl_reduce_only_ok=False,
            tp_side_ok=False,
            sl_side_ok=False,
            tp_qty_ok=False,
            sl_qty_ok=False,
            tp_type_ok=False,
            sl_type_ok=False,
            tp_trigger_ok=False,
            sl_trigger_ok=False,
        )
    if not tp_live:
        return ProtectionVerificationResult(
            protection_status="TP_MISSING",
            error_code="protection_verification_failed_tp_missing",
            tp_order_live=False,
            sl_order_live=True,
            tp_reduce_only_ok=False,
            sl_reduce_only_ok=order_reduce_only(sl_order),
            tp_side_ok=False,
            sl_side_ok=str(sl_order.get("side") or "").upper() == expected_side,
            tp_qty_ok=False,
            sl_qty_ok=abs(order_quantity(sl_order) - expected_quantity) <= qty_tolerance,
            tp_type_ok=False,
            sl_type_ok=is_sl_algo_order(sl_order),
            tp_trigger_ok=False,
            sl_trigger_ok=abs(order_trigger_price(sl_order) - expected_sl_trigger) <= price_tolerance,
        )
    if not sl_live:
        return ProtectionVerificationResult(
            protection_status="SL_MISSING",
            error_code="protection_verification_failed_sl_missing",
            tp_order_live=True,
            sl_order_live=False,
            tp_reduce_only_ok=order_reduce_only(tp_order),
            sl_reduce_only_ok=False,
            tp_side_ok=str(tp_order.get("side") or "").upper() == expected_side,
            sl_side_ok=False,
            tp_qty_ok=abs(order_quantity(tp_order) - expected_quantity) <= qty_tolerance,
            sl_qty_ok=False,
            tp_type_ok=is_tp_algo_order(tp_order),
            sl_type_ok=False,
            tp_trigger_ok=abs(order_trigger_price(tp_order) - expected_tp_trigger) <= price_tolerance,
            sl_trigger_ok=False,
        )

    tp_reduce_only_ok = order_reduce_only(tp_order)
    sl_reduce_only_ok = order_reduce_only(sl_order)
    tp_side_ok = str(tp_order.get("side") or "").upper() == expected_side
    sl_side_ok = str(sl_order.get("side") or "").upper() == expected_side
    tp_qty_ok = abs(order_quantity(tp_order) - expected_quantity) <= qty_tolerance
    sl_qty_ok = abs(order_quantity(sl_order) - expected_quantity) <= qty_tolerance
    tp_type_ok = is_tp_algo_order(tp_order)
    sl_type_ok = is_sl_algo_order(sl_order)
    tp_trigger_ok = abs(order_trigger_price(tp_order) - expected_tp_trigger) <= price_tolerance
    sl_trigger_ok = abs(order_trigger_price(sl_order) - expected_sl_trigger) <= price_tolerance

    if not tp_reduce_only_ok or not sl_reduce_only_ok:
        error_code = "protection_verification_failed_reduce_only_false"
    elif not tp_side_ok or not sl_side_ok:
        error_code = "protection_verification_failed_side_mismatch"
    elif not tp_qty_ok or not sl_qty_ok:
        error_code = "protection_verification_failed_qty_mismatch"
    elif not tp_type_ok or not sl_type_ok:
        error_code = "protection_verification_failed_type_mismatch"
    elif not tp_trigger_ok or not sl_trigger_ok:
        error_code = "protection_verification_failed_trigger_mismatch"
    else:
        error_code = None

    return ProtectionVerificationResult(
        protection_status="PROTECTED" if error_code is None else "UNPROTECTED",
        error_code=error_code,
        tp_order_live=tp_live,
        sl_order_live=sl_live,
        tp_reduce_only_ok=tp_reduce_only_ok,
        sl_reduce_only_ok=sl_reduce_only_ok,
        tp_side_ok=tp_side_ok,
        sl_side_ok=sl_side_ok,
        tp_qty_ok=tp_qty_ok,
        sl_qty_ok=sl_qty_ok,
        tp_type_ok=tp_type_ok,
        sl_type_ok=sl_type_ok,
        tp_trigger_ok=tp_trigger_ok,
        sl_trigger_ok=sl_trigger_ok,
    )


def load_dotenv_file(dotenv_path: str | Path = DEFAULT_DOTENV_PATH, *, override: bool = False) -> None:
    path = Path(dotenv_path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


class BinanceDemoAdapter:
    def __init__(
        self,
        *,
        dotenv_path: str | Path = DEFAULT_DOTENV_PATH,
        allow_non_demo_override: bool | None = None,
        session: requests.Session | None = None,
    ) -> None:
        load_dotenv_file(dotenv_path)

        self.log = get_logger("exchange.binance_demo")
        self.demo_mode = _parse_bool(os.getenv("BINANCE_DEMO_MODE"), default=False)
        self.execution_enabled = _parse_bool(os.getenv("BINANCE_EXECUTION_ENABLED"), default=False)
        self.api_key = (os.getenv("BINANCE_API_KEY") or "").strip()
        self.api_secret = (os.getenv("BINANCE_API_SECRET") or "").strip()
        self.base_url = (os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        env_override = _parse_bool(os.getenv("BINANCE_DEMO_ALLOW_UNSAFE_OVERRIDE"), default=False)
        self.allow_non_demo_override = env_override if allow_non_demo_override is None else allow_non_demo_override
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "vortex-binance-demo/1.0"})
        self.order_calls_attempted = 0
        self.order_endpoints_called = False

        self._validate_configuration()
        if self.api_key:
            self.session.headers["X-MBX-APIKEY"] = self.api_key

        self.log.info(
            "Initialized Binance demo adapter demo_mode=%s execution_enabled=%s base_url=%s credentials_present=%s",
            self.demo_mode,
            self.execution_enabled,
            self.base_url,
            bool(self.api_key and self.api_secret),
        )

    def _base_url_host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    def _is_mainnet_like_host(self) -> bool:
        host = self._base_url_host()
        return host in {"api.binance.com", "fapi.binance.com"} or "api.binance.com" in host or "fapi.binance.com" in host

    def _validate_configuration(self) -> None:
        if not self.demo_mode and not self.allow_non_demo_override:
            raise RuntimeError(
                "Binance demo adapter refused to start because BINANCE_DEMO_MODE is not true. "
                "Set BINANCE_DEMO_MODE=true or explicitly override with BINANCE_DEMO_ALLOW_UNSAFE_OVERRIDE=true."
            )

        host = self._base_url_host()
        if self._is_mainnet_like_host():
            raise RuntimeError(
                f"Refusing demo adapter startup with mainnet host '{host}'. "
                f"Use the demo/testnet base URL instead, defaulting to {DEFAULT_BASE_URL}."
            )

        if not self.base_url.startswith("https://"):
            raise RuntimeError("Binance demo adapter requires an https base URL.")

        if not self.api_key:
            raise RuntimeError("BINANCE_API_KEY is required for Binance demo adapter.")
        if not self.api_secret:
            raise RuntimeError("BINANCE_API_SECRET is required for Binance demo adapter.")

    def _sign_params(self, params: dict[str, Any]) -> str:
        payload = urlencode(params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{payload}&signature={signature}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        request_params = dict(params or {})
        headers: dict[str, str] = {}

        if signed:
            request_params.setdefault("timestamp", int(time.time() * 1000))
            request_params.setdefault("recvWindow", RECV_WINDOW_MS)
            query = self._sign_params(request_params)
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = urlencode(request_params, doseq=True)

        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        response = self.session.request(method=method.upper(), url=url, headers=headers, timeout=15)
        if response.ok:
            if not response.text:
                return None
            return response.json()

        message = response.text
        binance_code: int | None = None
        try:
            payload = response.json()
            message = str(payload.get("msg") or payload.get("message") or json.dumps(payload, sort_keys=True))
            code_raw = payload.get("code")
            if code_raw is not None:
                try:
                    binance_code = int(code_raw)
                except (TypeError, ValueError):
                    binance_code = None
        except ValueError:
            pass

        raise BinanceApiError(
            status_code=response.status_code,
            binance_code=binance_code,
            message=message,
            path=path,
            response_body=response.text,
            banned_until=parse_rate_limit_banned_until(message),
        )

    def _require_execution_guard(self) -> None:
        if not self.execution_enabled:
            raise RuntimeError(
                "Binance execution is disabled; refusing request because BINANCE_EXECUTION_ENABLED is not true."
            )
        if not self.demo_mode:
            raise RuntimeError(
                "Binance demo adapter refused execution because BINANCE_DEMO_MODE is not true."
            )
        if self._is_mainnet_like_host():
            raise RuntimeError(
                f"Binance demo adapter refused execution because base URL '{self.base_url}' looks like mainnet."
            )
        if not self.base_url.startswith("https://"):
            raise RuntimeError("Binance demo adapter refused execution because base URL is not https.")

    def _record_execution_attempt(self) -> None:
        self.order_calls_attempted += 1

    def _send_order_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        self.order_endpoints_called = True
        return self._request(method, path, params=params, signed=True)

    def _normalize_symbol(self, symbol: str) -> str:
        return symbol.strip().upper()

    def _normalize_side(self, side: str) -> str:
        normalized = side.strip().upper()
        if normalized not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported Binance order side: {side}")
        return normalized

    def _stringify_flag(self, value: bool) -> str:
        return "true" if value else "false"

    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def server_time(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/time")

    def exchange_info(self, symbols: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        payload = self._request("GET", "/fapi/v1/exchangeInfo")
        if not symbols:
            return payload

        wanted = {symbol.upper() for symbol in symbols}
        payload["symbols"] = [item for item in payload.get("symbols", []) if item.get("symbol") in wanted]
        return payload

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/ticker/price", params={"symbol": self._normalize_symbol(symbol)})

    def account_info(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def balance(self) -> list[dict[str, Any]]:
        return self._request("GET", "/fapi/v2/balance", signed=True)

    def get_symbol_filters(self, symbol: str) -> dict[str, Any]:
        info = self.exchange_info([symbol.upper()])
        for item in info.get("symbols", []):
            if item.get("symbol") != symbol.upper():
                continue
            filters = {flt.get("filterType"): flt for flt in item.get("filters", [])}
            lot_size = filters.get("LOT_SIZE", {})
            price_filter = filters.get("PRICE_FILTER", {})
            notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
            return {
                "symbol": item.get("symbol"),
                "status": item.get("status"),
                "tickSize": price_filter.get("tickSize"),
                "stepSize": lot_size.get("stepSize"),
                "minQty": lot_size.get("minQty"),
                "minNotional": notional.get("notional") or notional.get("minNotional"),
                "raw_filters": filters,
            }
        raise RuntimeError(f"Symbol not found in exchange info: {symbol}")

    def place_market_order(self, symbol: str, side: str, quantity: float | str, reduce_only: bool = False) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "symbol": self._normalize_symbol(symbol),
            "side": self._normalize_side(side),
            "type": "MARKET",
            "quantity": quantity,
            "reduceOnly": self._stringify_flag(reduce_only),
        }
        return self._send_order_request("POST", "/fapi/v1/order", params=params)

    def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float | str,
        quantity: float | str,
        reduce_only: bool = True,
    ) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "algoType": "CONDITIONAL",
            "symbol": self._normalize_symbol(symbol),
            "side": self._normalize_side(side),
            "type": "STOP_MARKET",
            "triggerPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": self._stringify_flag(reduce_only),
            "closePosition": "false",
        }
        return self._send_order_request("POST", "/fapi/v1/algoOrder", params=params)

    def place_take_profit_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float | str,
        quantity: float | str,
        reduce_only: bool = True,
    ) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "algoType": "CONDITIONAL",
            "symbol": self._normalize_symbol(symbol),
            "side": self._normalize_side(side),
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": self._stringify_flag(reduce_only),
            "closePosition": "false",
        }
        return self._send_order_request("POST", "/fapi/v1/algoOrder", params=params)

    def cancel_order(self, symbol: str, order_id: int | str) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "symbol": self._normalize_symbol(symbol),
            "orderId": order_id,
        }
        return self._send_order_request("DELETE", "/fapi/v1/order", params=params)

    def cancel_algo_order(self, algo_id: int | str) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "algoId": algo_id,
        }
        return self._send_order_request("DELETE", "/fapi/v1/algoOrder", params=params)

    def get_open_orders(self, symbol: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    def get_open_algo_orders(self, symbol: str | None = None, algo_type: str | None = "CONDITIONAL") -> Any:
        params: dict[str, Any] = {}
        if algo_type:
            params["algoType"] = algo_type
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return self._request("GET", "/fapi/v1/openAlgoOrders", params=params, signed=True)

    def get_position_risk(self, symbol: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)
