#!/usr/bin/env python3
"""
Diagnostic printer for the active Vortex dry-run strategy lineup.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_registry import STRATEGY_ORDER, get_strategy_definition


def _fmt_pairs(pairs: tuple[str, ...]) -> str:
    return ", ".join(pairs) if pairs else "-"


def _import_status(strategy_id: str) -> str:
    definition = get_strategy_definition(strategy_id)
    try:
        module = import_module(definition.scanner_module)
        getattr(module, definition.scanner_func)
        return "ok"
    except Exception as exc:
        return f"ERROR: {exc}"


def _print_section(title: str, strategy_ids: list[str]) -> None:
    print(title)
    print("-" * len(title))
    for strategy_id in strategy_ids:
        definition = get_strategy_definition(strategy_id)
        mode = "auto_open" if definition.auto_open else "watch_only"
        print(
            f"{definition.strategy_id} | "
            f"{definition.display_name} | "
            f"direction={definition.direction} | "
            f"timeframe={definition.timeframe} | "
            f"planned_rr={definition.planned_rr:.2f} | "
            f"min_rr={definition.min_rr:.2f} | "
            f"active_pairs={_fmt_pairs(definition.active_pairs)} | "
            f"disabled_watch_pairs={_fmt_pairs(definition.watch_pairs)} | "
            f"mode={mode} | "
            f"import={_import_status(strategy_id)}"
        )
    print()


def main() -> None:
    active = [sid for sid in STRATEGY_ORDER if get_strategy_definition(sid).auto_open]
    watch_only = [sid for sid in STRATEGY_ORDER if not get_strategy_definition(sid).auto_open]
    _print_section("Active Auto-Open Strategies", active)
    _print_section("Watch-Only Strategies", watch_only)


if __name__ == "__main__":
    main()
