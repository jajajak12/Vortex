from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from exchange.binance_demo import BinanceDemoAdapter, DEFAULT_BASE_URL, load_dotenv_file


SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def main() -> int:
    load_dotenv_file()

    print("Binance Demo Diagnostic")
    print("-----------------------")

    adapter = BinanceDemoAdapter()
    print(f"demo_mode={_fmt_bool(adapter.demo_mode)}")
    print(f"execution_enabled={_fmt_bool(adapter.execution_enabled)}")
    print(f"base_url={adapter.base_url or DEFAULT_BASE_URL}")
    print(f"key_present={bool(adapter.api_key)}")
    print(f"key_prefix={(adapter.api_key[:4] + '***') if adapter.api_key else '<missing>'}")
    print(f"secret_present={bool(adapter.api_secret)}")

    ping_result = "OK"
    server_time_result = "not called"
    api_errors: list[str] = []
    exchange_info = None
    balance_payload = None
    account_payload = None

    try:
        adapter.ping()
    except Exception as exc:
        ping_result = f"ERROR: {exc}"
        api_errors.append(f"ping: {exc}")
    print(f"ping={ping_result}")

    try:
        server_time = adapter.server_time()
        server_ms = int(server_time["serverTime"])
        server_iso = datetime.fromtimestamp(server_ms / 1000, tz=timezone.utc).isoformat()
        server_time_result = f"OK serverTime={server_ms} utc={server_iso}"
    except Exception as exc:
        server_time_result = f"ERROR: {exc}"
        api_errors.append(f"server_time: {exc}")
    print(f"server_time={server_time_result}")

    try:
        exchange_info = adapter.exchange_info(SYMBOLS)
        print(f"exchange_info=OK symbols={','.join(item['symbol'] for item in exchange_info.get('symbols', []))}")
    except Exception as exc:
        print(f"exchange_info=ERROR: {exc}")
        api_errors.append(f"exchange_info: {exc}")

    try:
        balance_payload = adapter.balance()
        print("balance=OK")
    except Exception as balance_exc:
        api_errors.append(f"balance: {balance_exc}")
        print(f"balance=ERROR: {balance_exc}")
        try:
            account_payload = adapter.account_info()
            print("account_info=OK")
        except Exception as account_exc:
            api_errors.append(f"account_info: {account_exc}")
            print(f"account_info=ERROR: {account_exc}")

    usdt_available = None
    if isinstance(balance_payload, list):
        for asset in balance_payload:
            if asset.get("asset") == "USDT":
                usdt_available = asset.get("availableBalance") or asset.get("balance")
                break
    elif isinstance(account_payload, dict):
        for asset in account_payload.get("assets", []):
            if asset.get("asset") == "USDT":
                usdt_available = asset.get("availableBalance") or asset.get("walletBalance")
                break
    print(f"usdt_available={usdt_available if usdt_available is not None else '<unavailable>'}")

    print("symbol_filters:")
    for symbol in SYMBOLS:
        try:
            filters = adapter.get_symbol_filters(symbol)
            print(
                f"{symbol} tickSize={filters.get('tickSize')} stepSize={filters.get('stepSize')} "
                f"minQty={filters.get('minQty')} minNotional={filters.get('minNotional')}"
            )
        except Exception as exc:
            print(f"{symbol} ERROR: {exc}")
            api_errors.append(f"{symbol}: {exc}")

    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print("order_endpoints_called=false")

    if api_errors:
        print("api_errors:")
        for item in api_errors:
            print(f"- {item}")
    else:
        print("api_errors: none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
