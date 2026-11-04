"""Allt nätverk mot eToro. Inga beslut här, bara översättning till modellerna."""
import time
import uuid
from datetime import datetime, timezone

import requests

from .models import Bar, BrokerPosition, Eligibility, OrderStatus, Quote, Snapshot


class EtoroError(Exception):
    pass


def _g(d: dict, *keys, default=None):
    """eToro blandar instrumentId/instrumentID osv."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _ts(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class EtoroBroker:
    def __init__(self, base_url: str, user_key: str, api_key: str, session=None, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"x-user-key": user_key, "x-api-key": api_key})
        self.sleep = sleep

    def _req(self, method, path, *, request_id=None, allow_404=False, **kw):
        headers = {"x-request-id": request_id or str(uuid.uuid4())}
        for _ in range(3):
            r = self.s.request(method, self.base + path, headers=headers, timeout=20, **kw)
            if r.status_code == 429:
                self.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            if allow_404 and r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise EtoroError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise EtoroError(f"{method} {path}: rate limited")

    # --- läsning ---
    def identity(self) -> dict:
        d = self._req("GET", "/api/v1/me")
        return {"gcid": d.get("gcid"), "username": d.get("username")}

    def snapshot(self) -> Snapshot:
        cp = self._req("GET", "/api/v1/trading/info/real/pnl")["clientPortfolio"]
        positions = tuple(
            BrokerPosition(
                position_id=int(_g(p, "positionID", "positionId")),
                instrument_id=int(_g(p, "instrumentID", "instrumentId")),
                is_buy=bool(p["isBuy"]),
                units=float(_g(p, "units", default=0)),
                amount=float(_g(p, "amount", default=0)),
                open_rate=float(_g(p, "openRate", default=0)),
                stop_loss=_g(p, "stopLossRate"),
                take_profit=_g(p, "takeProfitRate"),
                leverage=int(_g(p, "leverage", default=1)),
                has_stop=not _g(p, "isNoStopLoss", default=False) and bool(_g(p, "stopLossRate")),
            )
            for p in cp.get("positions") or []
        )
        pending = set()
        for key in ("ordersForOpen", "ordersForClose", "ordersForCloseMultiple", "orders"):
            for o in cp.get(key) or []:
                iid = _g(o, "instrumentID", "instrumentId")
                if iid is not None:
                    pending.add(int(iid))
        api_value = float(cp.get("credit") or 0) + sum(p.amount for p in positions) + float(cp.get("unrealizedPnL") or 0)
        return Snapshot(currency_id=cp.get("accountCurrencyId"), api_reported_value=api_value,
                        positions=positions, pending_instruments=frozenset(pending))

    def candles(self, instrument_id: int, interval: str, count: int) -> list[Bar]:
        d = self._req("GET", f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/{interval}/{count}")
        return [Bar(_ts(c["fromDate"]), c["open"], c["high"], c["low"], c["close"]) for c in d["candles"][0]["candles"]]

    def quote(self, instrument_id: int) -> Quote:
        r = self._req("GET", "/api/v2/market-data/rates", params={"instrumentIds": str(instrument_id)})["results"][0]
        return Quote(bid=float(r["bid"]), ask=float(r["ask"]), time=_ts(r["date"]), realtime=r.get("quoteType") == "realtime")

    def eligibility(self, instrument_id: int) -> Eligibility:
        e = self._req("POST", "/api/v2/trading/info/eligibility", json={"instrumentIds": [instrument_id]})["eligibilities"][0]
        configs = {}
        if e.get("allowOpenPosition"):
            for c in e.get("leverageConfigs", []):
                if c.get("settlementType") != "cfd" or c.get("isPotential"):
                    continue
                for lev in c.get("leverageValues", []):
                    configs[(c["direction"], lev)] = (float(c.get("maxStopLossPercentage", 100)),
                                                      float(c.get("minPositionAmount", 0)))
        return Eligibility(symbol=e.get("symbol", ""), min_exposure=float(e.get("minPositionExposure", 0)),
                           max_units=float(e.get("maxUnitsPerOrder", 0)), configs=configs)

    def lookup(self, reference_id: str) -> OrderStatus | None:
        d = self._req("GET", "/api/v2/trading/info/orders:lookup", params={"referenceId": reference_id}, allow_404=True)
        if d is None:
            return None
        st = d.get("status") or {}
        return OrderStatus(status_id=int(st.get("id", 0)), error=st.get("errorMessage"),
                           position_ids=tuple(int(x["positionId"]) for x in d.get("positionExecutions") or []))

    # --- skrivning (bara via ExecutionEngine) ---
    def open_order(self, instrument_id: int, is_buy: bool, units: float, leverage: int,
                   stop: float, target: float, request_id: str) -> dict:
        body = {
            "action": "open",
            "transaction": "buy" if is_buy else "sellShort",
            "instrumentId": instrument_id,
            "settlementType": "cfd",
            "orderType": "mkt",
            "leverage": leverage,
            "units": units,
            "stopLossRate": round(stop, 4),
            "takeProfitRate": round(target, 4),
            "stopLossType": "fixed",
        }
        return self._req("POST", "/api/v3/trading/execution/orders", json=body, request_id=request_id)

    def close_position(self, position_id: int, instrument_id: int, request_id: str) -> dict:
        return self._req("POST", f"/api/v1/trading/execution/market-close-orders/positions/{position_id}",
                         json={"InstrumentId": instrument_id, "UnitsToDeduct": None}, request_id=request_id)
