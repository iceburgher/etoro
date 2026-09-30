import os
import time
import uuid

import requests


class EtoroError(Exception):
    pass


class Etoro:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(
            {
                "x-user-key": os.environ["ETORO_USER_KEY"],
                "x-api-key": os.environ["ETORO_API_KEY"],
            }
        )

    def _req(self, method, path, **kw):
        headers = {"x-request-id": kw.pop("request_id", None) or str(uuid.uuid4())}
        for _ in range(3):
            r = self.s.request(method, self.base + path, headers=headers, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            if r.status_code >= 400:
                raise EtoroError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise EtoroError(f"{method} {path}: rate limited")

    def daily_closes(self, instrument_id: int, count: int = 120) -> list[float]:
        d = self._req(
            "GET",
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/OneDay/{count}",
        )
        return [c["close"] for c in d["candles"][0]["candles"]]

    def portfolio(self) -> dict:
        # pnl-varianten har unrealizedPnL, annars ser dagliga förlustspärren inga orealiserade förluster
        return self._req("GET", "/api/v1/trading/info/real/pnl")

    def settlement_types(self, instrument_id: int) -> set[str]:
        """Vilka settlementType kontot får köpa (long, hävstång 1) i instrumentet."""
        d = self._req("POST", "/api/v2/trading/info/eligibility", json={"instrumentIds": [instrument_id]})
        return {
            c["settlementType"]
            for e in d.get("eligibilities", []) if e.get("allowOpenPosition")
            for c in e.get("leverageConfigs", [])
            if c.get("direction") == "long" and 1 in c.get("leverageValues", []) and not c.get("isPotential")
        }

    def open_buy(self, instrument_id: int, amount_usd: float, sl_rate: float, tp_rate: float,
                 settlement_type: str) -> dict:
        body = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": instrument_id,
            "settlementType": settlement_type,
            "orderType": "mkt",
            "leverage": 1,
            "amount": round(amount_usd, 2),
            "orderCurrency": "usd",
            "stopLossRate": round(sl_rate, 4),
            "takeProfitRate": round(tp_rate, 4),
            "stopLossType": "fixed",
        }
        rid = str(uuid.uuid4())
        return self._req("POST", "/api/v3/trading/execution/orders", json=body, request_id=rid)
