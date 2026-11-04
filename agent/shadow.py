"""Skuggläge (steg 2): miljöklassning + papperstrategier. Inga ordrar, ingen kapitalstyrning.

Efter varje färdig 4H-stapel sparas en rad: stapelns stängning, miljön (klassning) och vad varje strategi
hade velat göra (LONG/SHORT/FLAT). Papperssiffrorna räknas ur raderna (paper_stats): en position beslutad vid
stapel t ger avkastningen från t till t+1. 1x, utan avgifter, spread eller nattavgift, så här syns bara
strategins riktning och timing. Köp-och-behåll (bh) är ribban.

Skuggdelen körs EFTER handelsjobbet och utanför låset, rör aldrig tillstånd eller ordrar och sväljer alla fel.
"""
from datetime import timedelta

from . import strategy_v1
from .indicators import sma

STRATEGIES = ("bh", "sma200", "donchian", "tsmom", "gld-v1.1")
DIR = {"LONG": 1, "SHORT": -1, "FLAT": 0}
DAILY_BARS, H4_BARS = 300, 200
DAY = timedelta(days=1)
H4 = timedelta(hours=4)


# ---------- miljöklassning ----------
def efficiency_ratio(closes: list[float], n: int) -> float | None:
    """Nettorörelse / summan av alla rörelser, 0..1. Högt = ren trend, lågt = hackigt/sidledes."""
    if len(closes) < n + 1:
        return None
    w = closes[-(n + 1):]
    path = sum(abs(b - a) for a, b in zip(w, w[1:]))
    return abs(w[-1] - w[0]) / path if path else 0.0


def atr_pct_series(bars, n: int = 14) -> list[float]:
    """ATR(n) i procent av stängningen, för varje stapel från n+1:a."""
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars, bars[1:])]
    return [sum(trs[i - n:i]) / n / bars[i].close for i in range(n, len(trs) + 1)]


def classify(daily, h4) -> dict:
    """Miljön just nu. Siffrorna sparas råa så att gränserna kan omprövas i efterhand."""
    dcl, hcl = [b.close for b in daily], [b.close for b in h4]
    reg = strategy_v1.regime(dcl)
    direction = {"LONG": "UPP", "SHORT": "NER"}.get(reg, "OKLAR")
    er = efficiency_ratio(dcl, 20)
    strength = None if er is None else "STARK" if er >= 0.30 else "MEDEL" if er >= 0.15 else "SVAG"
    series = atr_pct_series(h4) if len(h4) > 15 else []
    vol, atr_pct = None, None
    if series:
        atr_pct = series[-1]
        rank = sum(1 for x in series if x <= atr_pct) / len(series)
        vol = "HÖG" if rank > 2 / 3 else "LÅG" if rank <= 1 / 3 else "NORMAL"
    ma50 = sma(hcl, 50)
    h4_side = None if ma50 is None else "ÖVER" if hcl[-1] > ma50 else "UNDER"
    return {"riktning_dag": direction, "styrka_dag": strength, "er20_dag": er, "vol_4h": vol,
            "atr_pct_4h": atr_pct, "h4_mot_ma50": h4_side,
            "etikett": "/".join(x or "?" for x in (direction, strength, vol))}


# ---------- papperstrategier (lärobokens standardvärden, inte optimerade) ----------
def side_bh(daily) -> str:
    return "LONG"


def side_sma200(daily) -> str | None:
    s = sma([b.close for b in daily], 200)
    return None if s is None else "LONG" if daily[-1].close > s else "FLAT"


def side_donchian(daily, n_in: int = 55, n_out: int = 20) -> str | None:
    """Turtle long-only: in på 55-dagars högsta, ut på 20-dagars lägsta. Spelas upp från början av historiken."""
    if len(daily) <= n_in:
        return None
    held = False
    for i in range(n_in, len(daily)):
        if daily[i].close > max(b.high for b in daily[i - n_in:i]):
            held = True
        elif daily[i].close < min(b.low for b in daily[i - n_out:i]):
            held = False
    return "LONG" if held else "FLAT"


def side_tsmom(daily, look: int = 252) -> str | None:
    """12-månadersmomentum: long om stängningen är över stängningen för 252 dagar sedan."""
    return None if len(daily) <= look else "LONG" if daily[-1].close > daily[-1 - look].close else "FLAT"


def paper_sides(daily, v11_side: str) -> dict:
    return {"bh": side_bh(daily), "sma200": side_sma200(daily), "donchian": side_donchian(daily),
            "tsmom": side_tsmom(daily), "gld-v1.1": v11_side}


# ---------- inspelning ----------
def record(broker, store, cfg, st, latest: str, now, bar_id) -> str:
    """Spara skuggraden för senaste färdiga 4H-stapel. Returnerar vad som hände (för loggen)."""
    if cfg.vercel_env in ("preview", "development"):
        return "disabled_outside_production"
    if st.last_strategy_bar != latest:
        return "waiting_for_strategy"      # gld-v1.1:s sida är inte beslutad för den här stapeln än
    if store.has_shadow_bar(latest):
        return "already"
    daily = [b for b in broker.candles(cfg.instrument, "OneDay", DAILY_BARS) if b.start + DAY <= now]
    h4 = [b for b in broker.candles(cfg.instrument, "FourHours", H4_BARS) if b.start + H4 <= now]
    if not h4 or bar_id(h4[-1].start) != latest:
        return "data_not_ready"
    v11 = st.desired.get("side", "FLAT") if st.desired.get("side") in DIR else "FLAT"
    store.save_shadow_bar(latest, h4[-1].close, classify(daily, h4), paper_sides(daily, v11), cfg.git_sha)
    return "recorded"


# ---------- papperssiffror ----------
def paper_stats(rows: list[dict]) -> dict:
    """rows: [{bar, close, env, sides}] äldst först. Per strategi: avkastning, max ras, tid i marknad,
    antal sidbyten och avkastning per miljö (miljön när beslutet togs)."""
    out = {}
    for name in STRATEGIES:
        eq, peak, dd, bars_in, bars, switches, prev_dir = 1.0, 1.0, 0.0, 0, 0, 0, None
        by_env: dict = {}
        for prev, cur in zip(rows, rows[1:]):
            side = (prev["sides"] or {}).get(name)
            if side is None:
                continue                      # strategin saknade underlag (för kort historik)
            d = DIR[side]
            r = d * (cur["close"] / prev["close"] - 1)
            eq *= 1 + r
            peak, bars = max(peak, eq), bars + 1
            dd = min(dd, eq / peak - 1)
            bars_in += d != 0
            switches += prev_dir is not None and d != prev_dir
            prev_dir = d
            e = by_env.setdefault((prev["env"] or {}).get("etikett", "?"), {"staplar": 0, "avkastning": 0.0})
            e["staplar"] += 1
            e["avkastning"] += r
        out[name] = {"staplar": bars, "avkastning": eq - 1, "max_ras": dd,
                     "andel_i_marknad": bars_in / bars if bars else 0.0, "sidbyten": switches, "per_miljo": by_env}
    return out
