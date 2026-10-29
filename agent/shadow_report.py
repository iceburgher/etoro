"""Skuggrapport: python -m agent.shadow_report (kräver DATABASE_URL). Papperssiffror per strategi och miljö.

Papper = 1x, utan avgifter/spread/nattavgift, beslut vid stapel t ger avkastningen t -> t+1. Bara riktning
och timing jämförs; verkliga kostnader (avgift 1,50 USD per sida, nattavgift på x2) läggs på efteråt.
"""
import json
import os

from .shadow import STRATEGIES, paper_stats
from .store import PgStore


def render(rows: list[dict]) -> str:
    if len(rows) < 2:
        return f"För lite data: {len(rows)} rad(er). Skuggläget behöver minst två färdiga 4H-staplar."
    st = paper_stats(rows)
    lines = [f"{rows[0]['bar']} -> {rows[-1]['bar']}  ({len(rows)} staplar). Köp och behåll är ribban.", "",
             f"{'strategi':10}{'staplar':>8}{'avkastning':>12}{'max ras':>9}{'i marknad':>11}{'sidbyten':>9}"]
    for n in STRATEGIES:
        s = st[n]
        lines.append(f"{n:10}{s['staplar']:8d}{s['avkastning']:12.2%}{s['max_ras']:9.2%}"
                     f"{s['andel_i_marknad']:11.0%}{s['sidbyten']:9d}")
    envs = sorted({e for n in STRATEGIES for e in st[n]["per_miljo"]})
    lines += ["", "Avkastning (summa per stapel) per miljö där beslutet togs:", f"{'miljö':26}" +
              "".join(f"{n:>11}" for n in STRATEGIES) + f"{'staplar':>9}"]
    for e in envs:
        cells = "".join(f"{st[n]['per_miljo'].get(e, {}).get('avkastning', 0.0):11.2%}" for n in STRATEGIES)
        lines.append(f"{e:26}{cells}{max(st[n]['per_miljo'].get(e, {}).get('staplar', 0) for n in STRATEGIES):9d}")
    return "\n".join(lines)


def main():
    print(render(PgStore(os.environ["DATABASE_URL"]).shadow_rows()))


if __name__ == "__main__":
    main()
