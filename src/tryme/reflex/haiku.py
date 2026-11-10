"""REFLEX on Claude Haiku 5.5, low effort, structured output validated against ReflexDecision.

UNTESTED against the real API from the build environment (no API key there). Unit tests use a
fake client. Needs ANTHROPIC_API_KEY at runtime.
"""

from __future__ import annotations

import anthropic

from ..models import ReflexDecision, Setup, StateSnapshot
from .base import ReflexUnavailable

SYSTEM = """You judge one crypto perpetual-futures setup on one closed candle.
You receive a numeric state snapshot computed by code and a short setup description.
Return only the structured judgement. You do not size, place or approve trades; code does.

Fields:
- regime: trending | mean_reverting | high_vol | crisis (crisis = disorderly, gap risk, cascade).
- direction: long | short | neutral for the next several candles.
- direction_confidence: your probability, 0 to 1, that direction is right. Say 0.5 when unsure.
  It is scored nightly against outcomes, so do not inflate it.
- toxic_flow: true if order flow looks adverse to the direction, false if not, null if the
  snapshot has no order-book data (imbalance=None).
- setup_quality: 0 to 3, how well this candle matches the setup's ideal entry. 0 = not at all.
- risk_state: safe | near_limit | reduce. Never report a looser state than the snapshot's.

Snapshot keys: ret_N = return over N candles; trend = (fast EMA - slow EMA)/slow EMA;
range_pos = close position in the lookback range (0 low, 1 high); vol = stdev of log returns;
inventory/exposure are fractions of equity; drawdown and daily_pnl are fractions."""


class HaikuReflex:
    is_model = True

    def __init__(self, model: str = "claude-haiku-5-5", client=None, timeout_s: float = 8.0):
        self.model = model
        self.client = client or anthropic.Anthropic(timeout=timeout_s, max_retries=0)

    def decide(self, snap: StateSnapshot, setup: Setup) -> ReflexDecision:
        user = (
            f"SETUP {setup.symbol} bias={setup.bias} as_of={setup.as_of}\n"
            f"thesis: {setup.thesis}\nideal entry: {setup.quality_3}\n"
            f"STATE {snap.to_prompt()}"
        )
        try:
            r = self.client.messages.parse(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low"},
                output_format=ReflexDecision,
            )
        except anthropic.RateLimitError as e:
            raise ReflexUnavailable("rate limited") from e
        except anthropic.APIStatusError as e:
            raise ReflexUnavailable(f"API status {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise ReflexUnavailable("API connection error") from e
        if r.stop_reason != "end_turn" or r.parsed_output is None:
            raise ReflexUnavailable(f"no usable output (stop_reason={r.stop_reason})")
        return r.parsed_output
