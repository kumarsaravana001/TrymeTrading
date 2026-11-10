import dataclasses

import pytest

from conftest import SYM, TF, FixedReflex, decision
from tryme.alerts import Alerts
from tryme.engine import Engine
from tryme.exchange.base import OrderRejected, TransientError
from tryme.exchange.paper import PaperExchange, ReplayFeed
from tryme.journal import Journal
from tryme.models import Candle, OrderIntent
from tryme.policy import Policy
from tryme.reflex.base import ReflexUnavailable
from tryme.risk import RiskLayer


def oi(side, qty, cid, reduce=False):
    return OrderIntent(SYM, side, qty, reduce, cid, 100.0)


def test_fees_and_slippage_always_cost_money(paper, feed):
    px = feed.last_price(SYM)
    buy = paper.submit(oi("buy", 1.0, "a"))
    assert buy.price > px and buy.fee == pytest.approx(buy.price * 0.0005)
    sell = paper.submit(oi("sell", 1.0, "b", reduce=True))
    assert sell.price < px
    # round trip at an unchanged price loses fees plus slippage
    assert paper.equity() < 1000 and paper.positions() == {}
    assert paper.equity() == pytest.approx(1000 - buy.fee - sell.fee - (buy.price - sell.price))


def test_bigger_orders_slip_more(paper, feed):
    px = feed.last_price(SYM)
    small = paper.fill_price("buy", px, 100, 1.0)
    big = paper.fill_price("buy", px, 100_000, 1.0)
    assert big > small > px


def test_idempotent_client_id(paper):
    a = paper.submit(oi("buy", 1.0, "same"))
    b = paper.submit(oi("buy", 1.0, "same"))
    assert a == b and paper.positions()[SYM] == 1.0


def test_reduce_only_cannot_open_or_flip(paper):
    with pytest.raises(OrderRejected):
        paper.submit(oi("sell", 1.0, "x", reduce=True))
    paper.submit(oi("buy", 1.0, "y"))
    with pytest.raises(OrderRejected):
        paper.submit(oi("sell", 2.0, "z", reduce=True))


def test_short_pnl(feed, cfg):
    ex = PaperExchange(feed, 1000.0, cfg.costs)
    f = ex.submit(oi("sell", 2.0, "s"))
    q, e = ex.pos[SYM]
    assert q == -2.0 and e == f.price
    assert ex.equity() == pytest.approx(1000 - f.fee + (-2.0) * (feed.last_price(SYM) - f.price))


def test_replay_feed_cannot_see_the_future(candles):
    f = ReplayFeed({SYM: candles})
    f.set_time(candles[100].close_ts)
    assert f.candles(SYM, "15m", 500)[-1] == candles[100]
    f.set_time(candles[100].close_ts - 1)
    assert f.candles(SYM, "15m", 500)[-1] == candles[99]


# --- engine --------------------------------------------------------------------------------------


def make(cfg, feed, reflex, ex=None, policy=None):
    ex = ex or PaperExchange(feed, 1000.0, cfg.costs)
    alerts = Alerts("", "")
    eng = Engine(
        cfg,
        ex,
        reflex,
        policy or Policy(),
        RiskLayer(cfg.risk),
        Journal(None),
        alerts,
        sleep=lambda s: None,
    )
    return eng, ex, alerts


def kinds(eng):
    return [j["kind"] for j in eng.journal.mem]


def test_open_then_stop_out(cfg, candles, setup_long):
    feed = ReplayFeed({SYM: candles})
    now = candles[300].close_ts
    feed.set_time(now)
    eng, ex, _ = make(cfg, feed, FixedReflex())
    assert eng.on_candle(setup_long, now) == "opened"
    pos = eng.positions[SYM]
    assert pos.qty > 0 and pos.stop < pos.entry < pos.target
    assert SYM in ex.stops  # a protective stop is resting on the exchange
    assert abs(pos.qty) * (pos.entry - pos.stop) <= 0.02 * 1000 * 1.01
    # next candle crashes through the stop
    crash = Candle(now, pos.entry, pos.entry, pos.stop * 0.9, pos.stop * 0.95, 100, now + TF)
    feed2 = ReplayFeed({SYM: candles[:301] + [crash]})
    feed2.set_time(now + TF)
    ex.feed, eng.ex = feed2, ex
    assert eng.on_candle(setup_long, now + TF) == "managed"
    assert SYM not in eng.positions and ex.positions() == {}
    exit_ = [j for j in eng.journal.mem if j["kind"] == "exit"][0]
    assert exit_["reason"] == "stop" and exit_["win"] is False and exit_["simulated"] is True
    assert exit_["pnl"] < 0 and abs(exit_["pnl"]) <= 0.02 * 1000 * 1.1  # loss stays near 2%


def test_stop_wins_when_stop_and_target_in_same_candle(cfg, candles, setup_long):
    feed = ReplayFeed({SYM: candles})
    now = candles[300].close_ts
    feed.set_time(now)
    eng, ex, _ = make(cfg, feed, FixedReflex())
    eng.on_candle(setup_long, now)
    pos = eng.positions[SYM]
    both = Candle(now, pos.entry, pos.target * 1.1, pos.stop * 0.9, pos.entry, 100, now + TF)
    ex.feed = ReplayFeed({SYM: candles[:301] + [both]})
    ex.feed.set_time(now + TF)
    eng.on_candle(setup_long, now + TF)
    assert [j for j in eng.journal.mem if j["kind"] == "exit"][0]["reason"] == "stop"


def test_gate_blocks_weak_decisions(cfg, feed, candles, setup_long):
    now = candles[-1].close_ts
    for d in (
        decision(setup_quality=1),
        decision(direction_confidence=0.8),
        decision(direction="short"),
        decision(risk_state="near_limit"),
    ):
        eng, ex, _ = make(cfg, feed, FixedReflex(d))
        assert eng.on_candle(setup_long, now).startswith("hold")
        assert ex.positions() == {}


def test_crisis_and_low_confidence_escalate(cfg, feed, candles, setup_long):
    now = candles[-1].close_ts
    eng, ex, alerts = make(cfg, feed, FixedReflex(decision(regime="crisis")))
    out = eng.on_candle(setup_long, now)
    assert out.startswith("blocked") and ex.positions() == {}
    assert any("paused" in a for a in alerts.sent)
    eng, ex, alerts = make(cfg, feed, FixedReflex(decision(direction_confidence=0.4)))
    eng.on_candle(setup_long, now)
    assert ex.positions() == {} and any("paused" in a for a in alerts.sent)


def test_slow_reflex_skips_the_candle(cfg, feed, candles, setup_long):
    cfg.reflex_timeout_s = 0.05
    eng, ex, _ = make(cfg, feed, FixedReflex(delay=0.5))
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "miss:reflex"
    assert ex.positions() == {} and "deadline" in eng.journal.mem[-1]["reason"]


def test_reflex_failure_skips_the_candle(cfg, feed, candles, setup_long):
    eng, ex, _ = make(cfg, feed, FixedReflex(exc=ReflexUnavailable("refusal")))
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "miss:reflex"
    eng, ex, _ = make(cfg, feed, FixedReflex(exc=RuntimeError("boom")))
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "miss:reflex"
    assert ex.positions() == {}


def test_bad_data_never_reaches_the_reflex(cfg, candles, setup_long):
    feed = ReplayFeed({SYM: candles[:350] + candles[351:]})
    feed.set_time(candles[-1].close_ts)
    r = FixedReflex()
    eng, ex, _ = make(cfg, feed, r)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "miss:data"
    assert r.calls == 0 and ex.positions() == {}


class Flaky(PaperExchange):
    """Fails the first `fail` submits; optionally applies the order before losing the reply."""

    def __init__(self, *a, fail=0, lose_reply=False, **k):
        super().__init__(*a, **k)
        self.fail, self.lose_reply, self.submits = fail, lose_reply, 0

    def submit(self, intent):
        self.submits += 1
        if self.submits <= self.fail:
            if self.lose_reply:
                super().submit(intent)
            raise TransientError("timeout")
        return super().submit(intent)


def test_transient_errors_are_retried_once_each(cfg, feed, candles, setup_long):
    ex = Flaky(feed, 1000.0, cfg.costs, fail=2)
    eng, _, _ = make(cfg, feed, FixedReflex(), ex=ex)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "opened"
    assert ex.submits == 3 and kinds(eng).count("order_retry") == 2


def test_lost_reply_does_not_double_the_order(cfg, feed, candles, setup_long):
    ex = Flaky(feed, 1000.0, cfg.costs, fail=1, lose_reply=True)
    eng, _, _ = make(cfg, feed, FixedReflex(), ex=ex)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "opened"
    assert ex.positions()[SYM] == pytest.approx(eng.positions[SYM].qty)
    assert len(ex.fills) == 1


def test_permanent_outage_gives_up_and_alerts(cfg, feed, candles, setup_long):
    ex = Flaky(feed, 1000.0, cfg.costs, fail=99)
    eng, _, alerts = make(cfg, feed, FixedReflex(), ex=ex)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "order_failed"
    assert eng.positions == {} and any("order failed" in a for a in alerts.sent)


def test_rejected_order_is_not_retried(cfg, feed, candles, setup_long):
    ex = PaperExchange(feed, 1000.0, cfg.costs, reject_rate=1.0)
    eng, _, _ = make(cfg, feed, FixedReflex(), ex=ex)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "order_failed"
    assert kinds(eng).count("order_rejected") == 1 and "order_retry" not in kinds(eng)


def test_failed_stop_placement_closes_the_position(cfg, feed, candles, setup_long):
    class NoStops(PaperExchange):
        def place_stop(self, *a):
            raise OrderRejected("stop refused")

    ex = NoStops(feed, 1000.0, cfg.costs)
    eng, _, alerts = make(cfg, feed, FixedReflex(), ex=ex)
    assert eng.on_candle(setup_long, candles[-1].close_ts) == "closed:no_stop"
    assert ex.positions() == {} and eng.positions == {}
    assert any("stop placement failed" in a for a in alerts.sent)


def test_reconciliation_mismatch_halts_new_risk(cfg, feed, candles, setup_long):
    now = candles[-1].close_ts
    eng, ex, alerts = make(cfg, feed, FixedReflex())
    assert eng.reconcile(now)
    ex.submit(OrderIntent(SYM, "buy", 0.5, False, "rogue", 100.0))  # position the engine never made
    assert not eng.reconcile(now)
    assert any("mismatch" in a for a in alerts.sent)
    assert eng.on_candle(setup_long, now).startswith("blocked:halted")
    ex.submit(OrderIntent(SYM, "sell", 0.5, True, "fix", 100.0))
    assert eng.reconcile(now) and eng.risk.halted_reason == ""


def test_kill_switch_flattens_everything(cfg, candles, setup_long):
    feed = ReplayFeed({SYM: candles})
    now = candles[300].close_ts
    feed.set_time(now)
    eng, ex, alerts = make(cfg, feed, FixedReflex())
    eng.on_candle(setup_long, now)
    ex.balance -= 200  # simulate a 20% hit from elsewhere
    assert eng.on_candle(setup_long, now) == "killed"
    assert ex.positions() == {} and eng.risk.killed
    assert any("KILL SWITCH" in a for a in alerts.sent)
    assert eng.on_candle(setup_long, now).startswith(("hold", "blocked"))


def test_positions_survive_restart(cfg, feed, candles, setup_long, tmp_path):
    ex = PaperExchange(feed, 1000.0, cfg.costs)
    f = tmp_path / "pos.json"
    e1 = Engine(
        cfg,
        ex,
        FixedReflex(),
        Policy(),
        RiskLayer(cfg.risk),
        Journal(None),
        Alerts("", ""),
        positions_file=f,
    )
    e1.on_candle(setup_long, candles[-1].close_ts)
    e2 = Engine(
        cfg,
        ex,
        FixedReflex(),
        Policy(),
        RiskLayer(cfg.risk),
        Journal(None),
        Alerts("", ""),
        positions_file=f,
    )
    assert e2.positions[SYM] == e1.positions[SYM] and e2.reconcile(candles[-1].close_ts)


def test_engine_decisions_ignore_future_candles(cfg, candles, setup_long):
    """Same decisions whether or not the future exists in the dataset."""
    from tryme.reflex.base import RuleReflex

    cut = 330

    def trace(data):
        feed = ReplayFeed({SYM: data})
        eng, _, _ = make(cfg, feed, RuleReflex())
        out = []
        for c in candles[200:cut]:
            feed.set_time(c.close_ts)
            out.append(eng.on_candle(setup_long, c.close_ts))
        return out, [
            {k: v for k, v in j.items() if k != "latency_s"}
            for j in eng.journal.mem
            if j["kind"] in ("decision", "fill")
        ]

    tampered = candles[:cut] + [
        dataclasses.replace(c, close=c.close * 3, high=c.high * 3) for c in candles[cut:]
    ]
    assert trace(candles) == trace(tampered)
