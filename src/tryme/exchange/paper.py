"""Paper exchange: simulated fills with fees, slippage, size impact, rejections and outages.

Prices come from a feed (replayed candles in tests and backtests, a real public feed at runtime).
Nothing here is a real order. Fills are SIMULATED and labelled so in the journal.
"""

from __future__ import annotations

import bisect
import random

from ..config import Costs
from ..models import Book, Candle, Fill, OrderIntent
from .base import MarketInfo, OrderRejected, TransientError

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class ReplayFeed:
    """Serves candles up to a movable clock. It cannot return a candle that has not closed."""

    def __init__(self, data: dict[str, list[Candle]], spread_bps: float = 1.0):
        self.data = data
        self._closes = {s: [c.close_ts for c in cs] for s, cs in data.items()}
        self.now_ms = 0
        self.spread_bps = spread_bps

    def set_time(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def server_time_ms(self) -> int:
        return self.now_ms

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        i = bisect.bisect_right(self._closes[symbol], self.now_ms)
        return self.data[symbol][max(0, i - limit) : i]

    def book(self, symbol: str) -> Book | None:
        return None

    def last_price(self, symbol: str) -> float:
        cs = self.candles(symbol, "", 1)
        if not cs:
            raise TransientError(f"no price for {symbol}")
        return cs[-1].close

    def market(self, symbol: str) -> MarketInfo:
        return MarketInfo(symbol)


class PaperExchange:
    def __init__(
        self,
        feed,
        balance: float,
        costs: Costs,
        *,
        reject_rate: float = 0.0,
        outage_rate: float = 0.0,
        seed: int = 0,
    ):
        self.feed = feed
        self.balance = balance
        self.costs = costs
        self.reject_rate = reject_rate
        self.outage_rate = outage_rate
        self.rng = random.Random(seed)
        self.pos: dict[str, tuple[float, float]] = {}  # symbol -> (signed qty, avg entry)
        self.fills: dict[str, Fill] = {}
        self.stops: dict[str, tuple[str, float, float]] = {}
        self.fees_paid = 0.0

    # --- market data passthrough ---
    def server_time_ms(self) -> int:
        return self.feed.server_time_ms()

    def market(self, symbol: str) -> MarketInfo:
        return self.feed.market(symbol)

    def candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        return self.feed.candles(symbol, timeframe, limit)

    def book(self, symbol: str) -> Book | None:
        return self.feed.book(symbol)

    # --- account ---
    def equity(self) -> float:
        return self.balance + sum(
            q * (self.feed.last_price(s) - e) for s, (q, e) in self.pos.items()
        )

    def positions(self) -> dict[str, float]:
        return {s: q for s, (q, _) in self.pos.items()}

    # --- orders ---
    def fill_price(self, side: str, ref: float, notional: float, spread_bps: float) -> float:
        bps = (
            spread_bps / 2
            + self.costs.slippage_bps
            + self.costs.impact_bps_per_10k * notional / 1e4
        )
        return ref * (1 + bps / 1e4) if side == "buy" else ref * (1 - bps / 1e4)

    def submit(self, intent: OrderIntent) -> Fill:
        if intent.client_id in self.fills:  # idempotent on client id
            return self.fills[intent.client_id]
        if self.rng.random() < self.outage_rate:
            raise TransientError("simulated outage")
        if self.rng.random() < self.reject_rate:
            raise OrderRejected("simulated rejection")
        ref = self.feed.last_price(intent.symbol)
        spread = getattr(self.feed, "spread_bps", 1.0)
        px = self.fill_price(intent.side, ref, intent.qty * ref, spread)
        return self._apply(intent, px, self.server_time_ms())

    def fill_at(self, intent: OrderIntent, trigger: float, ts: int) -> Fill:
        """Stop/target fill at a trigger price, still paying slippage and fees."""
        spread = getattr(self.feed, "spread_bps", 1.0)
        return self._apply(
            intent, self.fill_price(intent.side, trigger, intent.qty * trigger, spread), ts
        )

    def _apply(self, intent: OrderIntent, px: float, ts: int) -> Fill:
        signed = intent.qty if intent.side == "buy" else -intent.qty
        q, e = self.pos.get(intent.symbol, (0.0, 0.0))
        if intent.reduce_only and (q == 0 or q * signed > 0 or abs(signed) > abs(q) * (1 + 1e-9)):
            raise OrderRejected("reduce-only would increase position")
        fee = intent.qty * px * self.costs.taker_fee
        self.balance -= fee
        self.fees_paid += fee
        if q == 0 or q * signed > 0:
            nq = q + signed
            e = (abs(q) * e + abs(signed) * px) / abs(nq)
        else:
            closed = min(abs(signed), abs(q))
            self.balance += closed * (px - e) * (1 if q > 0 else -1)
            nq = q + signed
            if nq * q < 0:
                e = px
        if abs(nq) < 1e-12:
            self.pos.pop(intent.symbol, None)
        else:
            self.pos[intent.symbol] = (nq, e)
        f = Fill(
            intent.client_id,
            intent.symbol,
            intent.side,
            intent.qty,
            px,
            fee,
            ts,
            detail="SIMULATED",
        )
        self.fills[intent.client_id] = f
        return f

    def find_fill(self, client_id: str) -> Fill | None:
        return self.fills.get(client_id)

    def place_stop(self, symbol, side, qty, stop_price, client_id) -> None:
        self.stops[symbol] = (side, qty, stop_price)

    def cancel_stops(self, symbol) -> None:
        self.stops.pop(symbol, None)
