"""Datagranskning av eToros GLD-data (instrument 3025): 4H och native 1D, per år.

Kör: python -m research.audit <h4_full.json> <d1_full.json>
Förväntat antal 4H-staplar följer live-mönstret sedan maj 2026: 00–20 UTC måndag–torsdag (6 per dag) och
00–16 UTC fredag (5), alltså 29 per vecka. Helgdagar räknas inte bort, så 100 % nås aldrig exakt.
Normalt helguppehåll (fredag 16:00 -> måndag 00:00 = 56 h) räknas inte som datagap.
"""
import json
import sys
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta

HOURS = (0, 4, 8, 12, 16, 20)


def ts(path):
    return [datetime.fromisoformat(c["fromDate"].replace("Z", "+00:00")) for c in json.load(open(path))]


def expected_4h(y0: date, y1: date) -> int:
    n, d = 0, y0
    while d <= y1:
        n += {0: 6, 1: 6, 2: 6, 3: 6, 4: 5}.get(d.weekday(), 0)
        d += timedelta(days=1)
    return n


def gaps(times, normal_weekend_h):
    out = []
    for a, b in zip(times, times[1:]):
        h = (b - a).total_seconds() / 3600
        weekend = a.weekday() == 4 and b.weekday() == 0 and h <= normal_weekend_h
        if not weekend:
            out.append((h, a, b))
    return max(out) if out else (0, None, None)


def main():
    h4, d1 = ts(sys.argv[1]), ts(sys.argv[2])
    print("4H (GET /api/v1/data/instruments/3025/candles?interval=4h)")
    print(f"{'år':6}{'staplar':>9}{'förväntat':>11}{'komplett':>10}  {'andel per UTC-tid 00/04/08/12/16/20':38}{'längsta gap':>14}")
    for y in sorted({t.year for t in h4}):
        ts_y = [t for t in h4 if t.year == y]
        first, last = ts_y[0].date(), ts_y[-1].date()
        exp = expected_4h(first, last)
        hc = Counter(t.hour for t in ts_y)
        days = len({t.date() for t in ts_y})
        share = " ".join(f"{hc.get(h, 0) / days:>5.0%}" for h in HOURS)
        g = gaps(ts_y, 56)
        print(f"{y:<6}{len(ts_y):>9}{exp:>11}{len(ts_y) / exp:>10.0%}  {share:38}{g[0]:>11.0f} h"
              f"  ({g[1]:%Y-%m-%d %H} -> {g[2]:%Y-%m-%d %H})")
    # när blev strukturen som live?
    wk = OrderedDict()
    for t in h4:
        wk.setdefault(t.strftime("%G-W%V"), 0)
        wk[t.strftime("%G-W%V")] += 1
    full = [k for k, v in wk.items() if v >= 23]
    print(f"\nFörsta vecka med live-struktur (>= 23 staplar): {full[0]}; veckor med live-struktur: {len(full)}; "
          f"alla veckor sedan dess: {all(wk[k] >= 23 for k in list(wk)[list(wk).index(full[0]):-1])}")

    print("\n1D native (GET /api/v1/data/instruments/3025/candles?interval=1d, stapel startar 21:00 UTC)")
    print(f"{'år':6}{'staplar':>9}{'vardagar':>10}{'komplett':>10}{'längsta gap':>14}")
    for y in sorted({t.year for t in d1}):
        ts_y = [t for t in d1 if t.year == y]
        first, last = ts_y[0].date(), ts_y[-1].date()
        wd = sum(1 for i in range((last - first).days + 1) if (first + timedelta(days=i)).weekday() in (6, 0, 1, 2, 3))
        g = gaps(ts_y, 73)
        print(f"{y:<6}{len(ts_y):>9}{wd:>10}{len(ts_y) / wd:>10.0%}{g[0]:>11.0f} h  ({g[1]:%Y-%m-%d} -> {g[2]:%Y-%m-%d})")


if __name__ == "__main__":
    main()
