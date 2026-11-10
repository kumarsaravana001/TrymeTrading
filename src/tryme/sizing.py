"""Gate and position sizing. Thresholds come from the Policy; all of them are hypotheses."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Costs, RiskLimits
from .models import ReflexDecision, Setup, StateSnapshot
from .policy import Policy


def gate(decision: ReflexDecision, setup: Setup, policy: Policy) -> tuple[bool, str]:
    if not setup.enabled or setup.bias == "neutral":
        return False, "setup disabled"
    if decision.direction == "neutral":
        return False, "neutral"
    if decision.direction != setup.bias:
        return False, "direction against setup bias"
    if decision.toxic_flow:
        return False, "toxic flow"
    if decision.setup_quality < policy.min_setup_quality:
        return False, "setup_quality below threshold"
    if not decision.direction_confidence > policy.min_direction_confidence:
        return False, "direction_confidence below threshold"
    if decision.risk_state != "safe":
        return False, "risk_state not safe"
    return True, "pass"


def calibrated_p(confidence: float, policy: Policy, breakeven: float = 0.5) -> float:
    """Win probability used for sizing.

    Anchored at the trade's break-even win rate, so a REFLEX with no information (confidence 0.5)
    gets exactly zero Kelly size. Only confidence above 0.5, shrunk by a factor the nightly loop
    refits from outcomes, moves it up.
    """
    return min(1.0, max(0.0, breakeven + policy.calibration_shrink * (confidence - 0.5)))


def kelly_fraction(p: float, b: float) -> float:
    """Full-Kelly fraction of bankroll to risk for win probability p and payoff ratio b."""
    if b <= 0:
        return 0.0
    return max(0.0, p - (1 - p) / b)


@dataclass(frozen=True)
class Sizing:
    qty: float
    stop_distance: float
    stop: float
    target: float
    risk_usd: float
    p_win: float
    reason: str = "ok"


def size_position(
    decision: ReflexDecision,
    snap: StateSnapshot,
    equity: float,
    policy: Policy,
    limits: RiskLimits,
    costs: Costs,
    *,
    gross_exposure: float,
    min_notional: float = 5.0,
    qty_step: float = 0.0,
) -> Sizing:
    px = snap.mid
    stop_d = policy.stop_atr * snap.atr_pct * px
    tgt_d = policy.target_atr * snap.atr_pct * px
    # round-trip cost per unit, paid whether the trade wins or loses
    cost = px * (2 * costs.taker_fee + (2 * costs.slippage_bps + snap.spread_bps) / 1e4)
    loss, win = stop_d + cost, tgt_d - cost
    if stop_d <= 0 or win <= 0:
        return Sizing(0.0, stop_d, 0.0, 0.0, 0.0, 0.0, "target does not cover costs")
    p = calibrated_p(decision.direction_confidence, policy, loss / (win + loss))
    zero = Sizing(0.0, stop_d, 0.0, 0.0, 0.0, p)
    f = policy.kelly_fraction * kelly_fraction(p, win / loss)
    if f <= 0:
        return Sizing(**{**zero.__dict__, "reason": "no positive Kelly edge after costs"})
    risk_usd = min(f, limits.risk_per_trade) * equity
    qty = risk_usd / loss
    room = limits.max_leverage * equity - gross_exposure
    qty = min(qty, max(0.0, room) / px)
    if qty_step > 0:
        qty = math.floor(qty / qty_step) * qty_step
    if qty * px < min_notional:
        return Sizing(**{**zero.__dict__, "reason": "below exchange minimum order size"})
    sign = 1 if decision.direction == "long" else -1
    return Sizing(qty, stop_d, px - sign * stop_d, px + sign * tgt_d, qty * loss, p)
