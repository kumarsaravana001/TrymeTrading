"""Typed contracts shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Regime = Literal["trending", "mean_reverting", "high_vol", "crisis"]
Direction = Literal["long", "short", "neutral"]
RiskState = Literal["safe", "near_limit", "reduce"]
Side = Literal["buy", "sell"]


class ReflexDecision(BaseModel):
    """What the REFLEX returns on every candle. It only judges; code decides."""

    model_config = ConfigDict(extra="forbid")

    regime: Regime
    direction: Direction
    direction_confidence: float = Field(ge=0.0, le=1.0)
    toxic_flow: bool | None = None
    setup_quality: int = Field(ge=0, le=3)
    risk_state: RiskState


class Setup(BaseModel):
    """A research finalist compiled into what the REFLEX evaluates each candle."""

    model_config = ConfigDict(extra="forbid")

    symbol: str  # ccxt unified, e.g. "BTC/USDT:USDT"
    bias: Direction  # only this side may be opened; "neutral" disables the setup
    thesis: str
    quality_3: str  # what a top-grade entry looks like
    invalidation: list[str]  # any of these observed -> setup disabled by a human or nightly
    as_of: str  # date of the research this was compiled from
    enabled: bool = True


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_ts: int  # close time, ms; a candle is usable only when close_ts <= now


@dataclass(frozen=True)
class Book:
    ts: int
    bids: list[tuple[float, float]]  # price, qty; best first
    asks: list[tuple[float, float]]


@dataclass
class Position:
    symbol: str
    qty: float  # signed: >0 long, <0 short
    entry: float
    stop: float
    target: float
    opened_ts: int
    risk_usd: float = 0.0
    p_win: float = 0.5
    setup_as_of: str = ""


@dataclass
class Account:
    equity: float
    balance: float
    positions: dict[str, Position] = field(default_factory=dict)

    def exposure(self, marks: dict[str, float]) -> float:
        return sum(abs(p.qty) * marks.get(s, p.entry) for s, p in self.positions.items())


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    qty: float
    reduce_only: bool
    client_id: str
    ref_price: float
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    client_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    ts: int
    status: Literal["filled", "partial", "rejected"] = "filled"
    detail: str = ""


class StateSnapshot(BaseModel):
    """The only thing the REFLEX sees. All numbers computed in code."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    ts: int
    mid: float
    spread_bps: float
    imbalance: float | None  # top-of-book bid share minus ask share, -1..1; None without a book
    vol: float  # realized volatility of log returns per candle
    atr_pct: float
    ret_1: float
    ret_12: float
    ret_48: float
    trend: float  # (ema_fast - ema_slow) / ema_slow
    range_pos: float  # 0..1 position of close inside the lookback high-low range
    vol_ratio: float  # last volume / average volume
    inventory: float  # signed position notional / equity
    exposure: float  # gross notional / equity
    drawdown: float  # from equity peak, 0..1
    daily_pnl: float  # today's pnl / start-of-day equity
    open_positions: int
    risk_state: RiskState  # computed by the risk layer, not the model

    def to_prompt(self) -> str:
        d = self.model_dump()
        out = []
        for k, v in d.items():
            if isinstance(v, float):
                v = float(f"{v:.5g}")
            out.append(f"{k}={v}")
        return " ".join(out)
