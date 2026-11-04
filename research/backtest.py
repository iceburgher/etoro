"""Backtest av fryst strategi v1 på GLD CFD (eToro instrument 3025).

Kör: python -m research.backtest <daily.json> <4h.json>
Signal på färdig 4H-stapel, utförs på nästa staplens öppning. Stop före mål om båda nås i samma stapel.
Kostnader från eToros cost-endpoint 30 sep 2026, per enhet: avgift 0,57 USD, spread 0,04 USD,
nattavgift long 0,11 USD per natt (short 0). Avgiften räknas både med en och två gånger per affär.
"""
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent import strategy_v1 as s

RISK_USD, USDSEK = 2.50, 9.98
FEE, SPREAD, ON_LONG = 0.57, 0.04, 0.11


@dataclass(frozen=True)
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float


def load(path):
    return [Bar(datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")), c["open"], c["high"], c["low"], c["close"])
            for c in json.load(open(path))]


def nights(t0, t1):
    n, d = 0, t0.date()
    while d < t1.date():
        n += 3 if d.weekday() == 4 else 1  # fredag -> helg räknas 3 nätter
        d += timedelta(days=3 if d.weekday() == 4 else 1)
    return n


def run(daily, h4):
    h4 = h4[:-1]  # sista staplen är ofärdig
    daily_ends = [(b.start + timedelta(days=1), b.close) for b in daily]
    trades, pos, pending_exit, last_exit = [], None, None, -1

    def reg_at(t):
        return s.regime([c for end, c in daily_ends if end <= t])

    for i in range(len(h4) - 1):
        bar, close_t = h4[i], h4[i].start + timedelta(hours=4)
        if pending_exit and pos:  # strategi-exit på denna staplens öppning
            px = bar.open
            trades.append({**pos, "exit": px, "exit_t": bar.start, "why": pending_exit})
            pos, pending_exit, last_exit = None, None, i - 1
        if pos and bar.start >= pos["entry_t"]:
            d, st, tg = pos["dir"], pos["stop"], pos["target"]
            hit = None
            if d == 1:
                if bar.open <= st: hit = (bar.open, "stop_gap")
                elif bar.low <= st: hit = (st, "stop")
                elif bar.open >= tg: hit = (bar.open, "target_gap")
                elif bar.high >= tg: hit = (tg, "target")
            else:
                if bar.open >= st: hit = (bar.open, "stop_gap")
                elif bar.high >= st: hit = (st, "stop")
                elif bar.open <= tg: hit = (bar.open, "target_gap")
                elif bar.low <= tg: hit = (tg, "target")
            if hit:
                trades.append({**pos, "exit": hit[0], "exit_t": bar.start, "why": hit[1]})
                pos, last_exit = None, i
        hist = h4[:i + 1]
        reg = reg_at(close_t)
        if pos:
            why = s.exit_reason(pos["side"], reg, hist)
            if why:
                pending_exit = why
            continue
        if i <= last_exit or reg is None:
            continue
        reason = s.signal(hist, reg)
        if reason:
            nxt = h4[i + 1]
            entry = nxt.open
            stop, target = s.stop_target(hist, reg, entry)
            r = abs(entry - stop)
            units = int(RISK_USD / r / 0.01) * 0.01
            if units <= 0:
                continue
            pos = {"side": reg, "dir": 1 if reg == "LONG" else -1, "entry": entry, "entry_t": nxt.start,
                   "stop": stop, "target": target, "r": r, "units": units, "reason": reason}
    return trades


def stats(trades, fee_sides):
    rows = []
    for t in trades:
        gross = (t["exit"] - t["entry"]) * t["dir"] * t["units"]
        n = nights(t["entry_t"], t["exit_t"])
        cost = t["units"] * (SPREAD + FEE * fee_sides + (ON_LONG * n if t["dir"] == 1 else 0))
        rows.append({**t, "gross": gross, "net": gross - cost, "R_gross": gross / RISK_USD,
                     "R_net": (gross - cost) / RISK_USD, "hours": (t["exit_t"] - t["entry_t"]).total_seconds() / 3600})
    if not rows:
        return {"trades": 0}, rows
    net = [r["R_net"] for r in rows]
    eq, peak, dd = 0, 0, 0
    for x in net:
        eq += x; peak = max(peak, eq); dd = min(dd, eq - peak)
    streak = cur = 0
    for x in net:
        cur = cur + 1 if x < 0 else 0; streak = max(streak, cur)
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    return {
        "trades": len(rows),
        "long": sum(r["dir"] == 1 for r in rows), "short": sum(r["dir"] == -1 for r in rows),
        "win_rate": len(wins) / len(rows),
        "expectancy_R_net": sum(net) / len(rows),
        "expectancy_R_gross": sum(r["R_gross"] for r in rows) / len(rows),
        "profit_factor_net": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown_R": dd, "max_drawdown_pct_of_capital": dd * 0.25,
        "max_losing_streak": streak,
        "avg_holding_hours": sum(r["hours"] for r in rows) / len(rows),
        "gross_usd": sum(r["gross"] for r in rows), "net_usd": sum(r["net"] for r in rows),
        "gross_sek": sum(r["gross"] for r in rows) * USDSEK, "net_sek": sum(r["net"] for r in rows) * USDSEK,
    }, rows


if __name__ == "__main__":
    daily, h4 = load(sys.argv[1]), load(sys.argv[2])
    tr = run(daily, h4)
    print("period", h4[0].start.date(), "->", h4[-2].start.date(), "4H-staplar", len(h4) - 1)
    for sides in (1, 2):
        st, rows = stats(tr, sides)
        print(f"\n== avgift {sides} gång(er) per affär ==")
        for k, v in st.items():
            print(f"{k:28} {round(v, 3) if isinstance(v, float) else v}")
    _, rows = stats(tr, 2)
    print("\nAffärer (avgift x2):")
    for r in rows:
        print(r["side"][0], r["entry_t"].strftime("%Y-%m-%d %H"), "->", r["exit_t"].strftime("%Y-%m-%d %H"),
              f"in {r['entry']:.2f} stop {r['stop']:.2f} mål {r['target']:.2f} ut {r['exit']:.2f}",
              r["why"], f"R netto {r['R_net']:+.2f}")
