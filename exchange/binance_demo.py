from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

from vortex_logger import get_logger


DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
DEFAULT_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
RECV_WINDOW_MS = 5000


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        try:
            payload = response.json()
            message = json.dumps(payload, sort_keys=True)
        except ValueError:
            pass

        raise RuntimeError(
            f"Binance API error status={response.status_code} path={path} response={message}"
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
            "symbol": self._normalize_symbol(symbol),
            "side": self._normalize_side(side),
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": self._stringify_flag(reduce_only),
            "closePosition": "false",
        }
        return self._send_order_request("POST", "/fapi/v1/order", params=params)

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
            "symbol": self._normalize_symbol(symbol),
            "side": self._normalize_side(side),
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": stop_price,
            "quantity": quantity,
            "reduceOnly": self._stringify_flag(reduce_only),
            "closePosition": "false",
        }
        return self._send_order_request("POST", "/fapi/v1/order", params=params)

    def cancel_order(self, symbol: str, order_id: int | str) -> Any:
        self._record_execution_attempt()
        self._require_execution_guard()
        params = {
            "symbol": self._normalize_symbol(symbol),
            "orderId": order_id,
        }
        return self._send_order_request("DELETE", "/fapi/v1/order", params=params)

    def get_open_orders(self, symbol: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return self._request("GET", "/fapi/v1/openOrders", params=params, signed=True)

    def get_position_risk(self, symbol: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = self._normalize_symbol(symbol)
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)
