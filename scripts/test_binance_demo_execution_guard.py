from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from exchange.binance_demo import BinanceDemoAdapter, load_dotenv_file


EXPECTED_ERROR = "Binance execution is disabled; refusing request because BINANCE_EXECUTION_ENABLED is not true."


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def main() -> int:
    load_dotenv_file()

    adapter = BinanceDemoAdapter()
    refusal_happened = False
    refusal_message = ""

    print("Binance Demo Execution Guard Test")
    print("---------------------------------")
    print(f"demo_mode={_fmt_bool(adapter.demo_mode)}")
    print(f"execution_enabled={_fmt_bool(adapter.execution_enabled)}")

    try:
        adapter.place_market_order("BTCUSDT", "BUY", "0.001")
    except RuntimeError as exc:
        refusal_happened = True
        refusal_message = str(exc)
        print(f"refusal_error={refusal_message}")
    else:
        print("refusal_error=<missing>")

    print(f"runtime_error_refusal={_fmt_bool(refusal_happened)}")
    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")

    if adapter.execution_enabled:
        return 1
    if not refusal_happened:
        return 1
    if refusal_message != EXPECTED_ERROR:
        return 1
    if adapter.order_endpoints_called:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
