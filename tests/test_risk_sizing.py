import pytest
from pydantic import ValidationError

from conftest import SYM, account, decision
from tryme.config import Costs, RiskLimits
from tryme.models import OrderIntent, Position, Setup, StateSnapshot
from tryme.policy import Policy
from tryme.risk import RiskLayer
from tryme.sizing import calibrated_p, gate, kelly_fraction, size_position

L = RiskLimits()
NOW = 1_700_000_000_000


def intent(qty=1.0, px=100.0, side="buy", reduce=False, sym=SYM):
    return OrderIntent(sym, side, qty, reduce, "cid", px)


def layer(equity=1000.0, tmp=None):
    r = RiskLayer(L, tmp)
    r.update(equity, NOW)
    return r


def pos(sym=SYM, qty=1.0):
    return Position(sym, qty, 100.0, 95.0, 110.0, NOW)


def test_defaults_match_the_brief():
    assert (
        L.risk_per_trade,
        L.max_daily_loss,
        L.max_open_positions,
        L.max_leverage,
        L.kill_drawdown,
        L.pause_confidence,
    ) == (0.02, 0.05, 3, 2.0, 0.15, 0.60)


def test_limits_are_frozen():
    with pytest.raises(ValidationError):
        L.max_leverage = 50


def test_ok_order_passes():
    assert layer().check(intent(1.0), account(), {}, decision(), 10.0).allowed


def test_per_trade_risk_cap():
    v = layer().check(intent(3.0), account(), {}, decision(), 10.0)  # 30 > 2% of 1000
    assert not v.allowed and "per-trade" in v.reason


def test_leverage_cap():
    v = layer().check(intent(25.0), account(), {}, decision(), 0.5)  # 2500 notional on 1000
    assert not v.allowed and "leverage" in v.reason


def test_max_open_positions():
    held = {f"S{i}": pos(f"S{i}", 0.1) for i in range(3)}
    v = layer().check(intent(), account(positions=held), {}, decision(), 10.0)
    assert not v.allowed


def test_daily_loss_blocks_new_risk():
    r = layer()
    r.update(949.0, NOW + 1000)  # -5.1% on the day
    v = r.check(intent(), account(949.0), {}, decision(), 5.0)
    assert not v.allowed and r.risk_state(account(949.0)) == "reduce"


def test_new_day_resets_daily_loss_only():
    r = layer()
    r.update(949.0, NOW)
    r.update(949.0, NOW + 86_400_000)
    assert r.daily_loss(949.0) == 0 and r.drawdown(949.0) == pytest.approx(0.051)


def test_kill_switch_latches_and_survives_restart(tmp_path):
    f = tmp_path / "risk.json"
    r = layer(tmp=f)
    r.update(849.0, NOW)  # -15.1% from peak
    assert r.killed
    r.update(1000.0, NOW + 1)  # recovery does not un-kill
    assert r.killed
    r2 = RiskLayer(L, f)
    v = r2.check(intent(), account(), {}, decision(), 5.0)
    assert not v.allowed and v.escalate and "kill" in v.reason
    with pytest.raises(PermissionError):
        r2.reset_kill("")
    r2.reset_kill("owner")
    assert not RiskLayer(L, f).killed


def test_model_cannot_override_code():
    """Whatever the model says, a breached limit still blocks; a strict model only tightens."""
    r = layer()
    r.update(849.0, NOW)
    perfect = decision(direction_confidence=1.0, setup_quality=3, risk_state="safe")
    assert not r.check(intent(), account(849.0), {}, perfect, 1.0).allowed
    assert not layer().check(intent(), account(), {}, decision(risk_state="reduce"), 1.0).allowed
    assert not layer().check(intent(), account(), {}, None, 1.0).allowed


def test_crisis_and_low_confidence_pause_and_escalate():
    v = layer().check(intent(), account(), {}, decision(regime="crisis"), 1.0)
    assert not v.allowed and v.escalate
    v = layer().check(intent(), account(), {}, decision(direction_confidence=0.59), 1.0)
    assert not v.allowed and v.escalate


def test_reduce_only_always_allowed_even_when_killed():
    r = layer()
    r.update(800.0, NOW)
    a = account(800.0, {SYM: pos()})
    assert r.check(intent(1.0, side="sell", reduce=True), a, {}, None, 0).allowed
    assert not r.check(intent(2.0, side="sell", reduce=True), a, {}, None, 0).allowed  # would flip
    assert not r.check(intent(1.0, side="buy", reduce=True), a, {}, None, 0).allowed  # would add


def test_reconciliation_halt_blocks():
    r = layer()
    r.halted_reason = "mismatch"
    assert not r.check(intent(), account(), {}, decision(), 1.0).allowed


# --- gate and sizing ---------------------------------------------------------------------------

S = Setup(symbol=SYM, bias="long", thesis="t", quality_3="q", invalidation=[], as_of="d")
P = Policy()


@pytest.mark.parametrize(
    "kw,ok",
    [
        ({}, True),
        ({"setup_quality": 1}, False),
        ({"direction_confidence": 0.80}, False),  # must be strictly greater
        ({"direction_confidence": 0.81}, True),
        ({"risk_state": "near_limit"}, False),
        ({"direction": "short"}, False),
        ({"direction": "neutral"}, False),
        ({"toxic_flow": True}, False),
        ({"toxic_flow": None}, True),
    ],
)
def test_gate(kw, ok):
    assert gate(decision(**kw), S, P)[0] is ok


def test_gate_disabled_setup():
    assert not gate(decision(), S.model_copy(update={"enabled": False}), P)[0]


def test_kelly_math():
    assert kelly_fraction(0.6, 1.0) == pytest.approx(0.2)
    assert kelly_fraction(0.5, 1.0) == 0
    assert kelly_fraction(0.4, 1.0) == 0
    assert calibrated_p(0.9, P) == pytest.approx(0.7)
    assert calibrated_p(0.9, P, 0.43) == pytest.approx(0.63)
    assert calibrated_p(0.5, P, 0.43) == pytest.approx(0.43)  # no information -> break-even


def snapshot(**kw):
    base = dict(
        symbol=SYM,
        ts=NOW,
        mid=100.0,
        spread_bps=1.0,
        imbalance=None,
        vol=0.004,
        atr_pct=0.01,
        ret_1=0,
        ret_12=0,
        ret_48=0,
        trend=0.01,
        range_pos=0.5,
        vol_ratio=1,
        inventory=0,
        exposure=0,
        drawdown=0,
        daily_pnl=0,
        open_positions=0,
        risk_state="safe",
    )
    return StateSnapshot(**{**base, **kw})


def test_size_never_exceeds_quarter_kelly_or_risk_cap():
    s = size_position(
        decision(direction_confidence=1.0), snapshot(), 1000, P, L, Costs(), gross_exposure=0
    )
    assert 0 < s.risk_usd <= 0.02 * 1000 + 1e-9
    # a small edge must be sized by quarter Kelly, below the 2% cap
    s2 = size_position(
        decision(direction_confidence=0.52), snapshot(), 1000, P, L, Costs(), gross_exposure=0
    )
    win, loss = 3 - 0.15, 2 + 0.15
    full = kelly_fraction(s2.p_win, win / loss)
    assert 0 < s2.risk_usd < 20
    assert s2.risk_usd == pytest.approx(0.25 * full * 1000)
    assert s.stop < 100 < s.target


def test_size_zero_without_edge_or_room():
    assert (
        size_position(
            decision(direction_confidence=0.5), snapshot(), 1000, P, L, Costs(), gross_exposure=0
        ).qty
        == 0
    )  # no information -> no Kelly edge after costs
    assert size_position(decision(), snapshot(), 1000, P, L, Costs(), gross_exposure=2000).qty == 0
    tiny = size_position(
        decision(), snapshot(atr_pct=0.0002), 1000, P, L, Costs(), gross_exposure=0
    )
    assert tiny.qty == 0 and "costs" in tiny.reason


def test_size_respects_leverage_and_minimums():
    s = size_position(decision(), snapshot(atr_pct=0.001), 1000, P, L, Costs(), gross_exposure=0)
    assert s.qty * 100 <= 2000 + 1e-6
    assert (
        size_position(
            decision(), snapshot(), 20, P, L, Costs(), gross_exposure=0, min_notional=100
        ).qty
        == 0
    )
    short = size_position(
        decision(direction="short"), snapshot(), 1000, P, L, Costs(), gross_exposure=0
    )
    assert short.stop > 100 > short.target
