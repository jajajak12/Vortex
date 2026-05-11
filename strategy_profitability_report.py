"""
Read-only strategy profitability report from trades.db.

Usage:
    python3 strategy_profitability_report.py
    python3 strategy_profitability_report.py --all-history
    python3 strategy_profitability_report.py --validated-only
    python3 strategy_profitability_report.py --since 2026-05-01
"""

from __future__ import annotations

import argparse
import ast
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from strategy_metadata import STRATEGY_META, get_strategy_meta

DB_PATH = Path("/home/prospera/vortex/trades.db")
CONFIG_PATH = Path("/home/prospera/vortex/config.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--validated-only",
        action="store_true",
        help="Print only validated-pairs sections.",
    )
    mode.add_argument(
        "--all-history",
        action="store_true",
        help="Print only the all-history section.",
    )
    parser.add_argument(
        "--since",
        type=str,
        help="Build an additional SINCE-UNIVERSE-FIX summary from trades with time >= YYYY-MM-DD.",
    )
    parser.add_argument(
        "--show-legacy",
        action="store_true",
        help="Include legacy/backtest labels in report output.",
    )
    return parser.parse_args()


def validate_since(since: str | None) -> str | None:
    if since is None:
        return None
    datetime.strptime(since, "%Y-%m-%d")
    return since


def load_validated_pairs() -> list[str]:
    source = CONFIG_PATH.read_text()
    module = ast.parse(source, filename=str(CONFIG_PATH))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VALIDATED_TRADING_PAIRS":
                    return ast.literal_eval(node.value)
    raise RuntimeError("VALIDATED_TRADING_PAIRS not found in config.py")


def fetch_rows() -> list[dict]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT * FROM trades ORDER BY time ASC")]


def filter_since(rows: list[dict], since: str | None) -> list[dict]:
    if since is None:
        return list(rows)
    cutoff = f"{since} 00:00:00"
    return [row for row in rows if (row.get("time") or "") >= cutoff]


def filter_validated(rows: list[dict], validated_pairs: set[str]) -> list[dict]:
    return [row for row in rows if row.get("pair") in validated_pairs]


def pnl_usdt(trade: dict) -> float | None:
    close_price = trade.get("close_price")
    entry = trade.get("entry")
    position_usdt = trade.get("position_usdt") or 0.0
    if close_price is None or not entry or not position_usdt:
        return None

    if trade["direction"] == "LONG":
        pct = (close_price - entry) / entry
    else:
        pct = (entry - close_price) / entry
    return position_usdt * pct


def risk_amount_usdt(trade: dict) -> float | None:
    entry = trade.get("entry")
    sl = trade.get("sl")
    position_usdt = trade.get("position_usdt") or 0.0
    if not entry or sl is None or not position_usdt:
        return None
    sl_pct = abs(entry - sl) / entry
    if sl_pct <= 0:
        return None
    return position_usdt * sl_pct


def r_multiple(trade: dict) -> float | None:
    pnl = pnl_usdt(trade)
    risk = risk_amount_usdt(trade)
    if pnl is None or risk is None or risk == 0:
        return None
    return pnl / risk


def fmt_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.2f}"


def fmt_num(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def build_grouped(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, dict] = defaultdict(lambda: {
        "strategy_name": "?",
        "legacy_label": "?",
        "total_trades": 0,
        "open_trades": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "closed_pnl_values": [],
        "closed_r_values": [],
        "best_trade": None,
        "worst_trade": None,
        "open_rows": [],
    })

    for strategy_id in STRATEGY_META:
        meta = get_strategy_meta(strategy_id)
        grouped[strategy_id]["strategy_name"] = meta.strategy_name
        grouped[strategy_id]["legacy_label"] = meta.legacy_label

    for trade in rows:
        strategy_id = trade.get("strategy") or "?"
        meta = get_strategy_meta(strategy_id)
        group = grouped[strategy_id]
        group["strategy_name"] = meta.strategy_name
        group["legacy_label"] = meta.legacy_label
        group["total_trades"] += 1

        status = trade.get("status")
        if status == "OPEN":
            group["open_trades"] += 1
            group["open_rows"].append(trade)
            continue
        if status != "CLOSED":
            continue

        group["closed_trades"] += 1
        if trade.get("result") == "WIN":
            group["wins"] += 1
        elif trade.get("result") == "LOSS":
            group["losses"] += 1

        pnl = pnl_usdt(trade)
        r_mult = r_multiple(trade)
        trade["_pnl_usdt"] = pnl
        trade["_r_multiple"] = r_mult

        if pnl is not None:
            group["closed_pnl_values"].append(pnl)
            if group["best_trade"] is None or pnl > group["best_trade"]["_pnl_usdt"]:
                group["best_trade"] = trade
            if group["worst_trade"] is None or pnl < group["worst_trade"]["_pnl_usdt"]:
                group["worst_trade"] = trade

        if r_mult is not None:
            group["closed_r_values"].append(r_mult)

    return grouped


def print_pair_coverage(rows: list[dict], validated_pairs: set[str]):
    included_pairs = sorted({row["pair"] for row in rows if row.get("pair")})
    excluded_legacy_pairs = sorted(set(included_pairs) - validated_pairs)
    print("Pair coverage")
    print(f"pairs_included       : {', '.join(included_pairs) if included_pairs else '-'}")
    print(
        "excluded_legacy_pairs: "
        f"{', '.join(excluded_legacy_pairs) if excluded_legacy_pairs else '-'}"
    )
    print()


def print_summary(title: str, rows: list[dict], validated_pairs: set[str], show_legacy: bool):
    grouped = build_grouped(rows)
    print(title)
    print(f"rows_read: {len(rows)}")
    print_pair_coverage(rows, validated_pairs)
    print("Per-strategy summary")

    sorted_groups = sorted(
        grouped.items(),
        key=lambda item: (
            -item[1]["total_trades"],
            -(sum(item[1]["closed_pnl_values"]) if item[1]["closed_pnl_values"] else 0.0),
            item[0],
        ),
    )

    for strategy_id, group in sorted_groups:
        closed_pnls = group["closed_pnl_values"]
        total_pnl = sum(closed_pnls) if closed_pnls else 0.0
        avg_pnl = (total_pnl / group["closed_trades"]) if group["closed_trades"] else None
        avg_r = (
            sum(group["closed_r_values"]) / len(group["closed_r_values"])
            if group["closed_r_values"] else None
        )
        winrate = (
            group["wins"] / group["closed_trades"] * 100.0
            if group["closed_trades"] else 0.0
        )

        legacy_part = f" | legacy={group['legacy_label']}" if show_legacy else ""
        print(
            f"{strategy_id} | {group['strategy_name']}{legacy_part} | "
            f"total={group['total_trades']} open={group['open_trades']} closed={group['closed_trades']} "
            f"wins={group['wins']} losses={group['losses']} winrate={winrate:.1f}% "
            f"total_pnl={fmt_money(total_pnl)} avg_pnl={fmt_money(avg_pnl)} avg_R={fmt_num(avg_r)}"
        )

        best = group["best_trade"]
        worst = group["worst_trade"]
        if best is not None:
            print(
                "  best_trade="
                f"{best['pair']} {best['direction']} {best['result']} "
                f"pnl={fmt_money(best['_pnl_usdt'])} R={fmt_num(best['_r_multiple'])} "
                f"opened={best['time']} closed={best['close_time']}"
            )
        else:
            print("  best_trade=-")
        if worst is not None:
            print(
                "  worst_trade="
                f"{worst['pair']} {worst['direction']} {worst['result']} "
                f"pnl={fmt_money(worst['_pnl_usdt'])} R={fmt_num(worst['_r_multiple'])} "
                f"opened={worst['time']} closed={worst['close_time']}"
            )
        else:
            print("  worst_trade=-")

    print()
    print("Open trades")
    has_open = False
    for strategy_id, group in sorted_groups:
        if not group["open_rows"]:
            continue
        has_open = True
        legacy_part = f" | legacy={group['legacy_label']}" if show_legacy else ""
        print(f"{strategy_id} | {group['strategy_name']}{legacy_part} | open={group['open_trades']}")
        for trade in group["open_rows"]:
            print(
                f"  {trade['pair']} {trade['direction']} entry={trade['entry']} sl={trade['sl']} "
                f"tp={trade['tp']} rr={trade['rr']} opened={trade['time']}"
            )
    if not has_open:
        print("-")
    print()


def main():
    args = parse_args()
    since = validate_since(args.since)
    validated_pairs = set(load_validated_pairs())
    all_rows = fetch_rows()
    validated_rows = filter_validated(all_rows, validated_pairs)
    since_validated_rows = filter_validated(filter_since(all_rows, since), validated_pairs)

    print(f"Database: {DB_PATH}")
    print(f"Validated pairs: {', '.join(sorted(validated_pairs))}")
    print(f"Since filter: {since or '-'}")
    if not args.validated_only and not args.all_history and since is None:
        print("Note: default output includes all-history data, which may include legacy wider-universe trades.")
    print()

    if args.all_history:
        print_summary("ALL-HISTORY SUMMARY", all_rows, validated_pairs, args.show_legacy)
        return

    if args.validated_only:
        print_summary("VALIDATED-ONLY SUMMARY", validated_rows, validated_pairs, args.show_legacy)
        if since is not None:
            print_summary(
                "SINCE-UNIVERSE-FIX SUMMARY",
                since_validated_rows,
                validated_pairs,
                args.show_legacy,
            )
        return

    print_summary("ALL-HISTORY SUMMARY", all_rows, validated_pairs, args.show_legacy)
    print_summary("VALIDATED-ONLY SUMMARY", validated_rows, validated_pairs, args.show_legacy)
    if since is not None:
        print_summary(
            "SINCE-UNIVERSE-FIX SUMMARY",
            since_validated_rows,
            validated_pairs,
            args.show_legacy,
        )


if __name__ == "__main__":
    main()
