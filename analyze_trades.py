# analyze_trades.py
import json
from datetime import datetime
from collections import defaultdict

def analyze_trades(file_path="trades.json"):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            trades = json.load(f)
    except FileNotFoundError:
        print("❌ File trades.json tidak ditemukan.")
        return
    except json.JSONDecodeError:
        print("❌ File trades.json rusak atau kosong.")
        return

    if not trades:
        print("❌ trades.json kosong.")
        return

    # Konversi timestamp
    for t in trades:
        if "timestamp" in t:
            t["_dt"] = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))

    df = trades

    print("=" * 70)
    print("📊 VORTEX TRADE ANALYSIS REPORT")
    print("=" * 70)

    total_trades = len(df)
    wins = sum(1 for t in df if t.get("result") == "WIN")
    losses = total_trades - wins
    winrate = (wins / total_trades * 100) if total_trades > 0 else 0

    print(f"Total Closed Trades : {total_trades}")
    print(f"Win Rate            : {winrate:.1f}%  ({wins}W / {losses}L)")

    if "_dt" in df[0] if df else False:
        dts = [t["_dt"] for t in df if "_dt" in t]
        print(f"Period              : {min(dts).date()} → {max(dts).date()}")

    # Performa per Strategi
    strat_map = defaultdict(list)
    for t in df:
        sid = t.get("strategy", "UNKNOWN")
        strat_map[sid].append(t)

    if strat_map:
        print("\n📈 Performa per Strategi:")
        print(f"{'Strategy':<12} {'Trades':>6} {'Wins':>5} {'Winrate':>8} {'AvgRR':>7} {'PnL':>8}")
        print("-" * 52)
        for sid in sorted(strat_map.keys()):
            group = strat_map[sid]
            cnt = len(group)
            w = sum(1 for t in group if t.get("result") == "WIN")
            wr = w / cnt * 100 if cnt > 0 else 0
            rrs = [t["rr"] for t in group if "rr" in t]
            avg_rr = sum(rrs) / len(rrs) if rrs else 0
            pnls = [t["pnl"] for t in group if "pnl" in t]
            total_pnl = sum(pnls) if pnls else 0
            print(f"{sid:<12} {cnt:>6} {w:>5} {wr:>7.1f}% {avg_rr:>7.2f} {total_pnl:>8.2f}")

    # Performa per Pair
    pair_map = defaultdict(list)
    for t in df:
        sym = t.get("symbol", "UNKNOWN")
        pair_map[sym].append(t)

    if pair_map:
        print("\n📍 Performa per Pair:")
        print(f"{'Symbol':<12} {'Trades':>6} {'Winrate':>8} {'AvgRR':>7}")
        print("-" * 36)
        for sym in sorted(pair_map.keys()):
            group = pair_map[sym]
            cnt = len(group)
            w = sum(1 for t in group if t.get("result") == "WIN")
            wr = w / cnt * 100 if cnt > 0 else 0
            rrs = [t["rr"] for t in group if "rr" in t]
            avg_rr = sum(rrs) / len(rrs) if rrs else 0
            print(f"{sym:<12} {cnt:>6} {wr:>7.1f}% {avg_rr:>7.2f}")

    # Statistik RR & Risk
    all_rr = [t["rr"] for t in df if "rr" in t]
    if all_rr:
        print(f"\nRR Statistics:")
        print(f"  Average RR : {sum(all_rr)/len(all_rr):.2f}")
        print(f"  Max RR     : {max(all_rr):.2f}")
        print(f"  Min RR     : {min(all_rr):.2f}")

    all_risk = [t["risk_percent"] for t in df if "risk_percent" in t]
    if all_risk:
        print(f"  Avg Risk % : {sum(all_risk)/len(all_risk):.2f}%")

    # Insight S5 & S6
    for s in ["S5", "S6"]:
        if s in strat_map:
            group = strat_map[s]
            cnt = len(group)
            w = sum(1 for t in group if t.get("result") == "WIN")
            wr = w / cnt * 100 if cnt > 0 else 0
            rrs = [t["rr"] for t in group if "rr" in t]
            avg_rr = sum(rrs) / len(rrs) if rrs else 0
            print(f"\n🔍 {s} Insight: {cnt} trades | Winrate {wr:.1f}% | Avg RR {avg_rr:.2f}")

    print("=" * 70)
    print("✅ Analysis completed. Run again after more trades.")

if __name__ == "__main__":
    analyze_trades()
