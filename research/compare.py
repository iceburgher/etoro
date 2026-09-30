"""Ren jämförelse: köp och behåll, SMA200, SMA200 med buffert, gld-v1.1. Samma data, samma kostnader.

Kör: python -m research.compare <h4_full.json> [startdatum, standard 2016-09-01]
Data: eToros GLD CFD (3025), 4H-staplar från 2015-11. Dagsstaplar byggs av 4H-staplarna per UTC-dygn
(avviker i snitt 0,03 % från eToros egna dagsstaplar). Före 2026 finns 4H-staplar bara under ETF:ens
handelstimmar (cirka 2–3 per dag), i live-drift 6 per dag: gld-v1.1 beter sig därför inte exakt som live.

Regler, fasta i förväg (ingen optimering):
- SMA200 = snittet av de 200 senaste dagliga stängningarna. Signal på stängning, affär på nästa dags öppning.
- Buffert: köp när stängning > SMA200 × 1,02, sälj när stängning < SMA200 × 0,98. 2 % valt i förväg.
- gld-v1.1: fryst strategi (agent/strategy_v1.py) via research/backtest.py. Risk 0,5 % av aktuellt kapital
  per affär, hävstång 2x, affär hoppas över om exponeringen skulle bli > 50 % av kapitalet (som live).
Kostnader (eToro cost-endpoint 30 sep 2026), som andel av exponeringen:
- transaktionsavgift 0,15 % vid öppning och 0,15 % vid stängning, spread 0,008 %
- nattavgift: 0 för x1 long och x2 short, 0,028 % per natt för x2 long (helg = 3 nätter)
Whipsaw = en SMA-affär som stängs med förlust inom 20 handelsdagar.
"""
import json
import math
import sys
from collections import OrderedDict
from datetime import datetime, timedelta

from research import backtest as bt

FEE, SPREAD, ON_X2_LONG = 0.0015, 0.00008, 0.00028
START = 1000.0
BUF = 0.02
RISK, LEV, MAX_EXPO = 0.005, 2, 0.50


def load_h4(path):
    return [bt.Bar(datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")), c["open"], c["high"], c["low"],
                   c["close"]) for c in json.load(open(path))]


def to_daily(h4):
    days = OrderedDict()
    for b in h4:
        days.setdefault(b.start.date(), []).append(b)
    return [bt.Bar(datetime(d.year, d.month, d.day, tzinfo=bs[0].start.tzinfo), bs[0].open,
                   max(x.high for x in bs), min(x.low for x in bs), bs[-1].close) for d, bs in days.items()]


# ---------- dagsstrategier (hela kapitalet in eller ut) ----------
def sma_signals(daily, buffer):
    """1 = long, 0 = kontanter, per dag (på stängning). None innan SMA200 finns."""
    out, pos = [], 0
    closes = []
    for b in daily:
        closes.append(b.close)
        if len(closes) < 200:
            out.append(None)
            continue
        s = sum(closes[-200:]) / 200
        if buffer:
            if pos == 0 and b.close > s * (1 + buffer):
                pos = 1
            elif pos == 1 and b.close < s * (1 - buffer):
                pos = 0
        else:
            pos = int(b.close > s)
        out.append(pos)
    return out


def run_daily(daily, sig, a, b):
    """Handlar på nästa dags öppning. Returnerar dagliga kapitalvärden och affärslogg."""
    cash, units, pos, eq, trades, costs, gross_cash = START, 0.0, 0, [], [], 0.0, START
    entry_i = entry_px = None
    for i in range(a, b):
        bar = daily[i]
        want = sig[i - 1]  # gårdagens stängning avgör dagens öppning
        if want is not None and want != pos:
            px = bar.open
            if want == 1:
                c = cash * (FEE + SPREAD)
                units, cash, costs = (cash - c) / px, 0.0, costs + c
                entry_i, entry_px, pos = i, px, 1
            else:
                val = units * px
                c = val * (FEE + SPREAD)
                cash, costs = val - c, costs + c
                trades.append({"in": entry_i, "out": i, "ret": px / entry_px - 1, "days": i - entry_i})
                units, pos = 0.0, 0
        eq.append(cash + units * bar.close)
    if pos:
        trades.append({"in": entry_i, "out": b - 1, "ret": daily[b - 1].close / entry_px - 1, "days": b - 1 - entry_i,
                       "open": True})
    return eq, trades, costs, 0.0


# ---------- gld-v1.1 ----------
def run_v11(daily, h4, t0, t1):
    tr = bt.run(daily, h4)
    tr = [t for t in tr if t0 <= t["entry_t"] and t["exit_t"] < t1]
    dates = [d.start for d in daily if t0 <= d.start < t1]
    closes = {d.start.date(): d.close for d in daily}
    equity, done, skipped, costs, overnight = START, [], 0, 0.0, 0.0
    marks = {}  # datum -> kapital
    ti = 0
    for t in tr:
        f = RISK * t["entry"] / t["r"]  # exponering som andel av kapitalet
        if f > MAX_EXPO:
            skipped += 1
            continue
        expo = f * equity
        n = bt.nights(t["entry_t"], t["exit_t"])
        c = expo * (2 * FEE + SPREAD)
        o = expo * ON_X2_LONG * n if t["dir"] == 1 else 0.0
        pnl = expo * t["dir"] * (t["exit"] / t["entry"] - 1)
        # markera kapitalet dag för dag under affären
        d = t["entry_t"].date()
        while d <= t["exit_t"].date():
            if d in closes:
                marks[d] = equity + expo * t["dir"] * (closes[d] / t["entry"] - 1) - expo * FEE
            d += timedelta(days=1)
        equity += pnl - c - o
        marks[t["exit_t"].date()] = equity
        costs, overnight = costs + c, overnight + o
        done.append({"in_t": t["entry_t"], "out_t": t["exit_t"], "ret": pnl - c - o, "gross": pnl,
                     "days": (t["exit_t"] - t["entry_t"]).total_seconds() / 86400, "f": f})
    eq, last = [], START
    for d in dates:
        last = marks.get(d.date(), last)
        eq.append(last)
    return eq, done, costs, overnight, skipped


# ---------- mått ----------
def metrics(eq, dates):
    years = (dates[-1] - dates[0]).days / 365.25
    rets = [b / a - 1 for a, b in zip(eq, eq[1:])]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
    dn = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))
    peak, dd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1
    by_year, by_q = OrderedDict(), OrderedDict()
    for d, x in zip(dates, eq):
        by_year.setdefault(d.year, [x, x])[1] = x
        by_q.setdefault((d.year, (d.month - 1) // 3 + 1), [x, x])[1] = x
    # årsavkastning mot föregående års slut
    yr, prev = OrderedDict(), eq[0]
    for y, (first, last) in by_year.items():
        yr[y] = last / prev - 1
        prev = last
    qr, prev = [], eq[0]
    for q, (first, last) in by_q.items():
        qr.append((q, last / prev - 1))
        prev = last
    return {"total": eq[-1] / eq[0] - 1, "cagr": cagr, "maxdd": dd, "calmar": cagr / -dd if dd else float("nan"),
            "sharpe": mu / sd * math.sqrt(252) if sd else 0, "sortino": mu / dn * math.sqrt(252) if dn else 0,
            "years": yr, "worst_q": min(qr, key=lambda x: x[1])}


def streak(rets):
    s = cur = 0
    for r in rets:
        cur = cur + 1 if r < 0 else 0
        s = max(s, cur)
    return s


def runs(sig, a, b):
    """Längd på perioder över (1) och under (0) SMA200, i handelsdagar."""
    out = {0: [], 1: []}
    cur, n = sig[a], 0
    for i in range(a, b):
        if sig[i] == cur:
            n += 1
        else:
            out[cur].append(n)
            cur, n = sig[i], 1
    out[cur].append(n)
    return out


def main():
    h4 = load_h4(sys.argv[1])
    t_from = datetime.fromisoformat((sys.argv[2] if len(sys.argv) > 2 else "2016-09-01") + "T00:00:00+00:00")
    daily = to_daily(h4)
    a = next(i for i, d in enumerate(daily) if d.start >= t_from)
    b = len(daily)
    dates = [d.start for d in daily[a:b]]
    print(f"Period {dates[0].date()} -> {dates[-1].date()} ({(dates[-1] - dates[0]).days / 365.25:.1f} år), "
          f"{b - a} handelsdagar. GLD: {daily[a].open:.2f} -> {daily[b - 1].close:.2f}\n")

    res = OrderedDict()
    bh = [1] * len(daily)
    for name, sig in (("Köp och behåll x1", bh), ("SMA200 x1", sma_signals(daily, 0)),
                      ("SMA200 ±2 % x1", sma_signals(daily, BUF))):
        eq, tr, costs, on = run_daily(daily, sig, a, b)
        inv = sum(1 for i in range(a, b) if (sig[i - 1] if i > 0 else None) == 1) / (b - a)
        res[name] = (eq, tr, costs, on, inv, sig, None)
    eq, tr, costs, on, skipped = run_v11(daily, h4, dates[0], dates[-1] + timedelta(days=1))
    held = sum(t["days"] for t in tr) / ((dates[-1] - dates[0]).days or 1)
    res["gld-v1.1 (0,5 %, x2)"] = (eq, tr, costs, on, held, None, skipped)

    hdr = f"{'':28}" + "".join(f"{n:>22}" for n in res)
    print(hdr)
    rows = OrderedDict()
    for n, (eq, tr, costs, on, inv, sig, sk) in res.items():
        m = metrics(eq, dates)
        rets = [t["ret"] for t in tr]
        rows[n] = m | {"trades": len(tr), "hold": (sum(t["days"] for t in tr) / len(tr)) if tr else 0,
                       "inv": inv, "streak": streak(rets), "costs": costs, "on": on,
                       "gross_total": (eq[-1] + costs + on) / START - 1, "skipped": sk}

    def line(label, f):
        print(f"{label:28}" + "".join(f"{f(r):>22}" for r in rows.values()))
    line("Total avkastning", lambda r: f"{r['total']:+.0%}")
    line("  före kostnader (ca)", lambda r: f"{r['gross_total']:+.0%}")
    line("Per år (CAGR)", lambda r: f"{r['cagr']:+.1%}")
    line("Största ras", lambda r: f"{r['maxdd']:.1%}")
    line("Calmar", lambda r: f"{r['calmar']:.2f}")
    line("Sharpe", lambda r: f"{r['sharpe']:.2f}")
    line("Sortino", lambda r: f"{r['sortino']:.2f}")
    line("Antal affärer", lambda r: f"{r['trades']}")
    line("Snittid per affär (dagar)", lambda r: f"{r['hold']:.0f}")
    line("Tid investerad", lambda r: f"{r['inv']:.0%}")
    line("Längsta förlustsvit", lambda r: f"{r['streak']}")
    line("Avgifter+spread (USD)", lambda r: f"{r['costs']:.0f}")
    line("Nattavgifter (USD)", lambda r: f"{r['on']:.0f}")
    line("Värsta kvartal", lambda r: f"{r['worst_q'][0][0]} Q{r['worst_q'][0][1]} {r['worst_q'][1]:+.0%}")
    line("Hoppade över (tak)", lambda r: "-" if r["skipped"] is None else f"{r['skipped']}")
    print("\nPer kalenderår")
    for y in rows[next(iter(rows))]["years"]:
        line(f"  {y}", lambda r: f"{r['years'].get(y, float('nan')):+.1%}")

    # GLD självt per år (referens) och SMA-detaljer
    for n in ("SMA200 x1", "SMA200 ±2 % x1"):
        sig = res[n][5]
        tr = res[n][1]
        r = runs(sig, a, b)
        whip = [t for t in tr if t["days"] <= 20 and t["ret"] < 0]
        cross = sum(1 for i in range(a + 1, b) if sig[i] != sig[i - 1])
        long_ret = math.prod(1 + t["ret"] for t in tr) - 1
        # GLD-avkastning under perioder utanför marknaden
        flat_ret, i = 1.0, a
        for i in range(a + 1, b):
            if sig[i - 1] == 0:
                flat_ret *= daily[i].close / daily[i - 1].close
        print(f"\n{n}: {cross} korsningar, {len(tr)} affärer, {len(whip)} whipsaws "
              f"(förlust inom 20 dagar, sammanlagt {sum(t['ret'] for t in whip):+.1%}), "
              f"snitt {sum(r[1]) / len(r[1]):.0f} dagar investerad per period, "
              f"{sum(r[0]) / len(r[0]):.0f} dagar ute per period.")
        print(f"  GLD när strategin var inne: {long_ret:+.0%} sammanlagt; "
              f"GLD när strategin var ute: {flat_ret - 1:+.0%} (det den missade eller slapp).")
        print("  Affärer:", ", ".join(f"{daily[t['in']].start.date()}→{daily[t['out']].start.date()} {t['ret']:+.0%}"
                                      for t in tr))


if __name__ == "__main__":
    main()
