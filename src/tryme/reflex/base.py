"""REFLEX interface: one call per candle, snapshot in, typed judgement out.

Jev (TypeSafe) would be a third implementation of `Reflex`. None is written because its API is
not known to this codebase and no key exists; nothing about it was guessed.
"""

from __future__ import annotations

import concurrent.futures as cf
import time
from typing import Protocol

from ..models import ReflexDecision, Setup, StateSnapshot


class ReflexUnavailable(Exception):
    """Timeout, refusal, bad output or API failure. The candle is skipped; nothing is opened."""


class Reflex(Protocol):
    is_model: bool  # True when a trained model with a knowledge cutoff makes the judgement

    def decide(self, snap: StateSnapshot, setup: Setup) -> ReflexDecision: ...


class RuleReflex:
    """Deterministic code rules. The baseline, the mock, and the only thing backtested pre-cutoff.

    A plain trend-and-momentum filter. It is a hypothesis, not evidence of an edge.
    """

    is_model = False

    def __init__(self, trend_min: float = 0.004, vol_crisis: float = 0.03, vol_high: float = 0.012):
        self.trend_min = trend_min
        self.vol_crisis = vol_crisis
        self.vol_high = vol_high

    def decide(self, snap: StateSnapshot, setup: Setup) -> ReflexDecision:
        if snap.vol >= self.vol_crisis or snap.ret_12 <= -0.12:
            regime = "crisis"
        elif snap.vol >= self.vol_high:
            regime = "high_vol"
        elif abs(snap.trend) >= self.trend_min:
            regime = "trending"
        else:
            regime = "mean_reverting"
        up = snap.trend > self.trend_min and snap.ret_12 > 0 and snap.ret_48 > 0
        dn = snap.trend < -self.trend_min and snap.ret_12 < 0 and snap.ret_48 < 0
        direction = "long" if up else "short" if dn else "neutral"
        score = 0
        if direction != "neutral":
            strength = min(1.0, abs(snap.trend) / (4 * self.trend_min))
            aligned = direction == setup.bias
            pullback = snap.range_pos < 0.8 if direction == "long" else snap.range_pos > 0.2
            score = int(aligned) + int(strength > 0.5) + int(pullback and snap.vol_ratio < 3)
            conf = 0.55 + 0.4 * strength
        else:
            conf = 0.5
        toxic = (
            None
            if snap.imbalance is None
            else (
                (direction == "long" and snap.imbalance < -0.6)
                or (direction == "short" and snap.imbalance > 0.6)
            )
        )
        return ReflexDecision(
            regime=regime,
            direction=direction,
            direction_confidence=round(conf, 3),
            toxic_flow=toxic,
            setup_quality=score,
            risk_state=snap.risk_state,
        )


_pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="reflex")


def timed_decide(
    reflex: Reflex, snap: StateSnapshot, setup: Setup, timeout_s: float
) -> tuple[ReflexDecision, float]:
    """Returns (decision, latency_s). Raises ReflexUnavailable when the deadline is missed."""
    t0 = time.monotonic()
    fut = _pool.submit(reflex.decide, snap, setup)
    try:
        d = fut.result(timeout=timeout_s)
    except cf.TimeoutError as e:
        fut.cancel()
        raise ReflexUnavailable(f"decision missed the {timeout_s}s deadline") from e
    except ReflexUnavailable:
        raise
    except Exception as e:  # any model/transport failure skips the candle
        raise ReflexUnavailable(f"{type(e).__name__}") from e
    return d, time.monotonic() - t0
