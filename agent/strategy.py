def sma(xs, n):
    return sum(xs[-n:]) / n


def signal(closes: list[float], fast: int, slow: int) -> str:
    """'buy' när kortsiktigt snitt ligger över långsiktigt OCH priset över kortsiktigt. Annars 'hold'."""
    if len(closes) < slow + 1:
        return "hold"
    f, s = sma(closes, fast), sma(closes, slow)
    return "buy" if f > s and closes[-1] > f else "hold"
