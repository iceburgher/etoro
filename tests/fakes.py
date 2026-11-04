"""Låtsas-eToro och styrbar strategi för tester. Inga nätverksanrop."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from agent import strategy_v1
from agent.alerts import SafeAlerter
from agent.config import Config
from agent.engine import MONITOR_JOB, STRATEGY_JOB, Engine, latest_completed_4h
from agent.models import Bar, BrokerPosition, Eligibility, OrderStatus, Quote, Snapshot
from agent.state import State
from agent.store import MemoryStore


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)  # senaste färdiga 4H = 08:00

    def __call__(self):
        return self.t

    def next_bar(self):
        self.t += timedelta(hours=4)

    def minutes(self, m):
        self.t += timedelta(minutes=m)


class FakeBroker:
    instances: list = []

    def __init__(self, clock: Clock):
        FakeBroker.instances.append(self)
        self.duplicates: list = []
        self.clock = clock
        self.positions: dict[int, BrokerPosition] = {}
        self.orders: dict[str, OrderStatus] = {}
        self.pending_instr: set[int] = set()
        self.fill_mode = "fill"      # fill | pending | reject
        self.close_mode = "close"    # close | stuck
        self.timeout_on_open = False  # ordern når eToro men svaret tappas
        self.on_open = None           # krok som körs mitt i ett orderanrop
        self.opened, self.closed = [], []
        self.bid, self.ask = 380.0, 380.1
        self.username = "AI Burger-UMYYUR"
        self.fx_age_s = 0
        self.snapshot_fails = False
        self.quote_fails = False
        self.lookup_fails = False
        self.next_pid = 100

    def identity(self):
        return {"gcid": 1, "username": self.username}

    def eligibility(self, iid):
        return Eligibility("GLD", 10.0, 4914.0, {("long", 2): (50.0, 10.0), ("short", 2): (50.0, 10.0)})

    def snapshot(self):
        if self.snapshot_fails:
            raise ConnectionError("eToro nere")
        return Snapshot(currency_id=1, api_reported_value=10000.0, positions=tuple(self.positions.values()),
                        pending_instruments=frozenset(self.pending_instr))

    def candles(self, iid, interval, n):
        step = timedelta(days=1) if interval == "OneDay" else timedelta(hours=4)
        last = latest_completed_4h(self.clock()) if interval == "FourHours" else \
            self.clock().replace(hour=0, minute=0) - step
        return [Bar(last - step * (n - 1 - i), 380, 381, 379, 380) for i in range(n)]

    def quote(self, iid):
        if self.quote_fails:
            raise ConnectionError("kurs saknas")
        if iid == 58:
            return Quote(9.98, 9.99, self.clock() - timedelta(seconds=self.fx_age_s), True)
        return Quote(self.bid, self.ask, self.clock(), True)

    def lookup(self, key):
        if self.lookup_fails:
            raise ConnectionError("lookup nere")
        return self.orders.get(key)

    def add_position(self, is_buy, stop, target, units=0.3, has_stop=True, open_rate=380.0, instrument=3025):
        pid = self.next_pid
        self.next_pid += 1
        self.positions[pid] = BrokerPosition(pid, instrument, is_buy, units, units * open_rate / 2, open_rate,
                                             stop if has_stop else None, target, 2, has_stop)
        return pid

    def open_order(self, iid, is_buy, units, leverage, stop, target, request_id):
        if request_id in {o["key"] for o in self.opened}:
            self.duplicates.append(request_id)  # kontrolleras efter varje test (conftest.py)
        self.opened.append({"is_buy": is_buy, "units": units, "stop": stop, "target": target, "key": request_id})
        if self.on_open:
            self.on_open()
        if self.fill_mode == "fill":
            pid = self.add_position(is_buy, stop, target, units)
            self.orders[request_id] = OrderStatus(3, None, (pid,))
        elif self.fill_mode == "reject":
            self.orders[request_id] = OrderStatus(4, "rejected by test")
        else:
            self.orders[request_id] = OrderStatus(1, None)
        if self.timeout_on_open:
            raise TimeoutError("read timeout")
        return {"orderId": len(self.opened), "referenceId": request_id}

    def close_position(self, pid, iid, request_id):
        self.closed.append(pid)
        if self.close_mode == "close":
            self.positions.pop(pid, None)
        return {"orderForClose": {"positionID": pid}}


class FakeStrategy:
    VERSION = "test-v1"
    stop_from = staticmethod(strategy_v1.stop_from)

    def __init__(self):
        self.reg, self.sig, self.exit = None, {}, None

    def regime(self, closes):
        return self.reg

    def signal(self, h4, side):
        return self.sig.get(side)

    def exit_reason(self, side, reg, h4):
        return self.exit

    def swing_and_atr(self, h4, side):
        return (370.0, 3.0) if side == "LONG" else (390.0, 3.0)


class FailingProvider:
    def send(self, *a):
        raise ConnectionError("e-post nere")


def make(mode="REAL_MICRO", state: State | None = None, broker=None, clock=None, store=None, strat=None,
         vercel_env="production", provider=None, **cfg_kw):
    clock = clock or Clock()
    broker = broker or FakeBroker(clock)
    cfg = replace(Config(), **{"mode": mode, "vercel_env": vercel_env, "git_sha": "testsha",
                               "order_poll_sleep_s": 0, "order_poll_tries": 2, "kill_switch_env": False,
                               "integration_trade": False, **cfg_kw})
    store = store or MemoryStore(state)
    logs = []
    alert = SafeAlerter(provider, lambda **kw: logs.append(kw))
    strat = strat or FakeStrategy()
    eng = Engine(cfg, broker, store, clock=clock, sleep=lambda s: None, log=lambda **kw: logs.append(kw),
                 alert=alert, strategy=strat)
    return eng, broker, strat, clock, logs


def S(eng):
    return eng.run_job(STRATEGY_JOB)


def M(eng):
    return eng.run_job(MONITOR_JOB)


def edit_state(eng, **kw):
    st = eng.store.load_state()
    for k, v in kw.items():
        setattr(st, k, v)
    eng.store._state = st.to_dict()
