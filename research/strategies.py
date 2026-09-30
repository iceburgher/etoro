"""Jämförelse av vedertagna strategier på GLD (eToro 3025), dagsdata.

Kör: python -m research.strategies <daily.json>
Signal på dagens stängning, affär på nästa dags öppning. Hela kontot i en position (eller kontanter).
Kostnader från eToros cost-endpoint 30 sep 2026 (konto cirka 1 000 USD):
  x1 long:  1,50 USD avgift per affär (öppning och stängning räknas var för sig), spread 0,005 %, ingen nattavgift
  x2 long:  samma avgift, nattavgift 0,28 USD per 1 000 USD exponering och natt (helg = 3 nätter)
Parametrarna är lärobokens standardvärden, inte optimerade på datan (undviker överanpassning).
"""
import json
import math
import sys
from datetime import datetime

FEE_USD, SPREAD, ON_X2 = 1.50, 0.00005, 0.00028
START_USD = 1000.0


def load(path):
    c = json.load(open(path))
    return ([datetime.fromisoformat(x["fromDate"].replace("Z", "+00:00")) for x in c],
            [x["open"] for x in c], [x["high"] for x in c], [x["low"] for x in c], [x["close"] for x in c])


def sma(xs, n, i):
    return sum(xs[i + 1 - n:i + 1]) / n if i + 1 >= n else None


# ---- signaler: 1 = long, 0 = kontanter, beräknat på stängning dag i ----
def buy_hold(d, i):
    return 1


def sma200(d, i):
    s = sma(d["c"], 200, i)
    return None if s is None else int(d["c"][i] > s)


def golden_cross(d, i):
    a, b = sma(d["c"], 50, i), sma(d["c"], 200, i)
    return None if b is None else int(a > b)


def donchian(state, d, i, n_in=55, n_out=20):
    """Turtle: köp på 55-dagars högsta, sälj på 20-dagars lägsta."""
    if i < n_in:
        return None
    hi, lo = max(d["h"][i - n_in:i]), min(d["l"][i - n_out:i])
    if d["c"][i] > hi:
        state["in"] = 1
    elif d["c"][i] < lo:
        state["in"] = 0
    return state.get("in", 0)


def tsmom(d, i, look=252):
    """Tidsseriemomentum: long om 12-månadersavkastningen är positiv."""
    return None if i < look else int(d["c"][i] > d["c"][i - look])


def run(d, sig, lev=1, start=0, end=None):
    end = end or len(d["c"])
    cash, units, debt, pos, trades, eq, first = START_USD, 0.0, 0.0, 0, 0, [], None
    state = {}
    for i in range(start, end - 1):
        s = sig(state, d, i) if sig is donchian else sig(d, i)
        if s is None:
            continue
        first = first if first is not None else i
        px = d["o"][i + 1]
        if s != pos:
            if pos:  # sälj
                cash += units * px * (1 - SPREAD) - FEE_USD - debt
                units, debt, pos, trades = 0.0, 0.0, 0, trades + 1
            else:  # köp: insatsen = kontanterna minus avgiften, resten lånas vid hävstång
                stake = cash - FEE_USD
                units, debt = stake * lev / (px * (1 + SPREAD)), stake * (lev - 1)
                cash, pos, trades = 0.0, 1, trades + 1
        if pos and lev > 1:  # nattavgift på exponeringen
            gap = (d["t"][i + 1] - d["t"][i]).days
            cash -= ON_X2 * units * d["c"][i] * max(gap, 1)
        eq.append(cash + (units * d["c"][i + 1] - debt if pos else 0))
    years = (d["t"][end - 1] - d["t"][(first or start)]).days / 365.25
    peak, dd = eq[0], 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, x / peak - 1)
    rets = [b / a - 1 for a, b in zip(eq, eq[1:]) if a > 0]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
    return {"slut_usd": eq[-1], "cagr": (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 else 0,
            "max_ras": dd, "sharpe": mu / sd * math.sqrt(252) if sd else 0, "affarer": trades,
            "ar": years}


STRATS = [("Köp och behåll x1", buy_hold, 1), ("Köp och behåll x2", buy_hold, 2),
          ("Över 200-dagarssnitt x1", sma200, 1), ("Golden cross 50/200 x1", golden_cross, 1),
          ("Donchian 55/20 (Turtle) x1", donchian, 1), ("12-mån momentum x1", tsmom, 1),
          ("Över 200-dagarssnitt x2", sma200, 2)]

if __name__ == "__main__":
    t, o, h, l, c = load(sys.argv[1])
    d = {"t": t, "o": o, "h": h, "l": l, "c": c}
    n = len(c)
    # Samma startpunkt för alla: första dagen alla signaler finns (252 dagar in), sen två halvor.
    s0 = 252
    mid = s0 + (n - s0) // 2
    for label, a, b in (("HELA", s0, n), ("FÖRSTA HALVAN", s0, mid), ("ANDRA HALVAN", mid, n)):
        print(f"\n== {label}: {t[a].date()} -> {t[b - 1].date()} ==")
        print(f"{'strategi':30}{'slut USD':>10}{'per år':>9}{'max ras':>9}{'sharpe':>8}{'affärer':>9}")
        for name, sig, lev in STRATS:
            r = run(d, sig, lev, a, b)
            print(f"{name:30}{r['slut_usd']:10.0f}{r['cagr']:9.1%}{r['max_ras']:9.1%}{r['sharpe']:8.2f}{r['affarer']:9d}")
