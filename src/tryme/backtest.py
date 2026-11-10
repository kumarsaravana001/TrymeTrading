"""Walk-forward backtest with fees and slippage on, compared against simply holding BTC.

Rules:
- Parameters are chosen on a train window and scored only on the following unseen test window.
- The engine is the production engine driven by a replay feed that cannot serve unclosed candles.
- A model REFLEX may only be backtested on data after its training cutoff. Earlier data tests
  code rules only; the guard below enforces it.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .alerts import Alerts
from .config import Config
from .engine import Engine
from .exchange.paper import TF_MS, PaperExchange, ReplayFeed
from .journal import Journal
from .models import Candle, Setup
from .policy import Policy
from .reflex.base import Reflex, RuleReflex
from .risk import RiskLayer

# Stated training cutoff for Claude Fable 5.2 and Haiku 5.5 is June 2026; first clean day after.
MODEL_CUTOFF_MS = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000)


class LookAheadError(Exception):
    pass


def load_csv(path: str | Path, tf_ms: int) -> list[Candle]:
    """Binance kline CSV: open_time, open, high, low, close, volume, ... (header optional)."""
    out = []
    for row in csv.reader(Path(path).open()):
        if not row or not row[0].strip().lstrip("-").isdigit():
            continue
        ts = int(row[0])
        if ts > 10**14:  # microsecond timestamps in newer Binance dumps
            ts //= 1000
        out.append(
            Candle(
                ts,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
                ts + tf_ms,
            )
        )
    return out


def synthetic(
    n: int,
    tf_ms: int,
    *,
    seed: int = 1,
    drift: float = 0.0,
    vol: float = 0.004,
    start_ms: int = 1_600_000_000_000,
    start_px: float = 100.0,
    regime_len: int = 300,
    regime_vol: float | None = None,
) -> list[Candle]:
    """SIMULATED random walk. By default it has persistent drift regimes, which any trend rule
    will profit from by construction; regime_vol=0 gives a driftless walk with no edge to find.
    For tests only. Says nothing about markets."""
    rng = np.random.default_rng(seed)
    regime_vol = vol / 6 if regime_vol is None else regime_vol
    px, out, mu = start_px, [], drift
    for i in range(n):
        if i % regime_len == 0:
            mu = drift + rng.normal(0, regime_vol)
        r = rng.normal(mu, vol)
        o, c = px, px * math.exp(r)
        h = max(o, c) * (1 + abs(rng.normal(0, vol / 3)))
        lo = min(o, c) * (1 - abs(rng.normal(0, vol / 3)))
        ts = start_ms + i * tf_ms
        out.append(Candle(ts, o, h, lo, c, float(rng.uniform(50, 150)), ts + tf_ms))
        px = c
    return out


@dataclass
class RunResult:
    ret: float
    max_dd: float
    trades: int
    wins: int
    fees: float
    equity_curve: list[float] = field(default_factory=list)
    journal: list[dict] = field(default_factory=list)


def run(
    cfg: Config,
    data: dict[str, list[Candle]],
    setups: list[Setup],
    reflex: Reflex,
    policy: Policy,
    start_ms: int,
    end_ms: int,
) -> RunResult:
    first = min(cs[0].ts for cs in data.values())
    if reflex.is_model and first < MODEL_CUTOFF_MS:
        raise LookAheadError(
            "model REFLEX backtested on data before its training cutoff: "
            "the model may remember this period"
        )
    tf = TF_MS[cfg.timeframe]
    feed = ReplayFeed(data)
    ex = PaperExchange(feed, cfg.paper_balance, cfg.costs)
    journal = Journal(None)
    eng = Engine(
        cfg, ex, reflex, policy, RiskLayer(cfg.risk), journal, Alerts("", ""), sleep=lambda s: None
    )
    curve, peak, max_dd = [], cfg.paper_balance, 0.0
    t = start_ms
    while t <= end_ms:
        feed.set_time(t)
        for s in setups:
            if s.symbol in data:
                eng.on_candle(s, t)
        eq = ex.equity()
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)
        curve.append(eq)
        t += tf
    exits = [j for j in journal.mem if j["kind"] == "exit"]
    return RunResult(
        curve[-1] / cfg.paper_balance - 1 if curve else 0.0,
        max_dd,
        len(exits),
        sum(1 for e in exits if e["win"]),
        ex.fees_paid,
        curve,
        journal.mem,
    )


def btc_hold(btc: list[Candle], start_ms: int, end_ms: int, cfg: Config) -> tuple[float, float]:
    """Buy at start, sell at end, paying taker fee and slippage both ways. Returns (ret, max_dd)."""
    cs = [c for c in btc if start_ms <= c.close_ts <= end_ms]
    if len(cs) < 2:
        return 0.0, 0.0
    cost = cfg.costs.taker_fee + cfg.costs.slippage_bps / 1e4
    px = np.array([c.close for c in cs])
    eq = px / (px[0] * (1 + cost))
    dd = float((1 - eq / np.maximum.accumulate(eq)).max())
    return float(eq[-1] * (1 - cost) - 1), dd


@dataclass
class WalkForward:
    folds: list[dict]
    strategy_ret: float
    strategy_max_dd: float
    btc_ret: float
    btc_max_dd: float
    trades: int
    simulated_data: bool

    @property
    def beats_btc(self) -> bool:
        return self.strategy_ret > self.btc_ret

    def summary(self) -> str:
        head = (
            "BEATS holding BTC after costs"
            if self.beats_btc
            else "DOES NOT BEAT holding BTC after costs"
        )
        tag = " [SIMULATED DATA - says nothing about real markets]" if self.simulated_data else ""
        return (
            f"{head}{tag}\n"
            f"strategy: {self.strategy_ret:+.2%} (max drawdown {self.strategy_max_dd:.2%}, "
            f"{self.trades} trades, {len(self.folds)} out-of-sample folds)\n"
            f"hold BTC: {self.btc_ret:+.2%} (max drawdown {self.btc_max_dd:.2%})"
        )


GRID = [{"trend_min": tm, "stop_atr": sa} for tm in (0.003, 0.006) for sa in (1.5, 2.5)]


def walk_forward(
    cfg: Config,
    data: dict[str, list[Candle]],
    setups: list[Setup],
    policy: Policy,
    btc_symbol: str,
    *,
    train: int = 1500,
    test: int = 500,
    grid=GRID,
    reflex_factory=None,
    simulated_data: bool = False,
) -> WalkForward:
    """Rolling train/test over candle counts. With a reflex_factory (a model), no fitting is done
    and every window is purely out of sample."""
    tf = TF_MS[cfg.timeframe]
    t0 = max(cs[0].ts for cs in data.values()) + cfg.lookback * tf
    t_end = min(cs[-1].close_ts for cs in data.values())
    folds, total, worst_dd, trades = [], 1.0, 0.0, 0
    start = t0
    while start + (train + test) * tf <= t_end:
        tr_a, tr_b = start, start + train * tf
        te_a, te_b = tr_b, tr_b + test * tf
        if reflex_factory is not None:
            best, reflex, pol = {}, reflex_factory(), policy
        else:
            scored = []
            for g in grid:
                pol_g = policy.model_copy(update={"stop_atr": g["stop_atr"]})
                r = run(cfg, data, setups, RuleReflex(trend_min=g["trend_min"]), pol_g, tr_a, tr_b)
                scored.append((r.ret - r.max_dd, g))  # reward return, penalise drawdown
            best = max(scored, key=lambda x: x[0])[1]
            reflex = RuleReflex(trend_min=best["trend_min"])
            pol = policy.model_copy(update={"stop_atr": best["stop_atr"]})
        r = run(cfg, data, setups, reflex, pol, te_a, te_b)
        folds.append(
            {
                "test_start": te_a,
                "test_end": te_b,
                "params": best,
                "ret": r.ret,
                "max_dd": r.max_dd,
                "trades": r.trades,
            }
        )
        total *= 1 + r.ret
        worst_dd = max(worst_dd, r.max_dd)
        trades += r.trades
        start += test * tf
    if not folds:
        raise ValueError("not enough data for one train+test window")
    b_ret, b_dd = btc_hold(data[btc_symbol], folds[0]["test_start"], folds[-1]["test_end"], cfg)
    return WalkForward(folds, total - 1, worst_dd, b_ret, b_dd, trades, simulated_data)
