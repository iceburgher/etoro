"""Strategi v1, fryst. 1D-regim + 4H-setup + 4H-trigger, swing/ATR-stop, 2R-mål.

Rena funktioner utan nätverk: används av backtest och (senare) av motorn.
Parametrarna får inte ändras utifrån backtestresultat.
"""
from .indicators import atr, sma, sma_series

VERSION = "gld-v1.1"  # v1.1: trigger måste stänga på rätt sida om 4H-MA50
P = dict(ma_fast=20, ma_slow=50, slope_lb=5, setup_ma=20, setup_lb=6, exit_ma=50,
         atr_len=14, swing_lb=7, stop_buf_atr=0.25, min_stop_atr=1.0, rr=2.0)


def regime(daily_closes: list[float]) -> str | None:
    """'LONG', 'SHORT' eller None. Bara färdiga dagsstaplar."""
    need = P["ma_slow"] + P["slope_lb"]
    if len(daily_closes) < need:
        return None
    f, s = sma(daily_closes, P["ma_fast"]), sma(daily_closes, P["ma_slow"])
    slope = s - sma(daily_closes[:-P["slope_lb"]], P["ma_slow"])
    if f > s and slope > 0:
        return "LONG"
    if f < s and slope < 0:
        return "SHORT"
    return None


def signal(bars, side: str) -> str | None:
    """Setup + trigger på senaste färdiga 4H-stapeln. bars = färdiga staplar, äldst först."""
    n = P["setup_lb"]
    if len(bars) < max(P["setup_ma"], P["exit_ma"]) + n + 1:
        return None
    closes = [b.close for b in bars]
    ma = sma_series(closes, P["setup_ma"])
    ma50 = sma(closes, P["exit_ma"])
    last, prev = bars[-1], bars[-2]
    window = range(len(bars) - 1 - n, len(bars) - 1)
    # Triggern kräver även rätt sida om 4H-MA50, annars skulle exitregeln slå till direkt (v1.1).
    if side == "LONG":
        setup = any(ma[i] is not None and closes[i] < ma[i] for i in window)
        if setup and last.close > ma[-1] and last.close > prev.high and last.close > ma50:
            return "4h_pullback_below_ma20_then_close_above_ma20_ma50_and_prev_high"
    if side == "SHORT":
        setup = any(ma[i] is not None and closes[i] > ma[i] for i in window)
        if setup and last.close < ma[-1] and last.close < prev.low and last.close < ma50:
            return "4h_rally_above_ma20_then_close_below_ma20_ma50_and_prev_low"
    return None


def swing_and_atr(bars, side: str) -> tuple[float, float]:
    """Swingnivå (lägsta botten / högsta topp senaste 7 staplarna) och ATR(14) på signalstapeln."""
    swing = bars[-P["swing_lb"]:]
    level = min(b.low for b in swing) if side == "LONG" else max(b.high for b in swing)
    return level, atr(bars, P["atr_len"])


def stop_from(side: str, swing: float, a: float, entry: float) -> tuple[float, float]:
    """Stop och 2R-mål för en given ingång. Används med faktisk kurs vid order."""
    if side == "LONG":
        stop = min(swing - P["stop_buf_atr"] * a, entry - P["min_stop_atr"] * a)
        return stop, entry + P["rr"] * (entry - stop)
    stop = max(swing + P["stop_buf_atr"] * a, entry + P["min_stop_atr"] * a)
    return stop, entry - P["rr"] * (stop - entry)


def stop_target(bars, side: str, entry: float) -> tuple[float, float]:
    swing, a = swing_and_atr(bars, side)
    return stop_from(side, swing, a, entry)


def exit_reason(side: str, reg: str | None, bars) -> str | None:
    """Strategi-exit på färdig stapel (stop/mål hanteras separat)."""
    if reg != side:
        return f"daily_regime_not_{side.lower()}"
    ma50 = sma([b.close for b in bars], P["exit_ma"])
    if ma50 is None:
        return None
    if side == "LONG" and bars[-1].close < ma50:
        return "4h_close_below_ma50"
    if side == "SHORT" and bars[-1].close > ma50:
        return "4h_close_above_ma50"
    return None
