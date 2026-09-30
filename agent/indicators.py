def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def sma_series(xs: list[float], n: int) -> list[float | None]:
    return [sum(xs[i + 1 - n:i + 1]) / n if i + 1 >= n else None for i in range(len(xs))]


def atr(bars, n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = [max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)) for p, b in zip(bars, bars[1:])]
    return sum(trs[-n:]) / n
