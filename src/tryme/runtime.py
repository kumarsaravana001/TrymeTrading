"""The 24/7 loop. Paper by default; testnet needs testnet keys; live needs the approval gate."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from .alerts import Alerts
from .config import Config, live_approved
from .engine import Engine
from .exchange.base import TransientError
from .exchange.paper import TF_MS, PaperExchange
from .journal import Journal
from .policy import PolicyStore
from .reflex import HaikuReflex, RuleReflex
from .risk import RiskLayer
from .state_engine import ClockSkew, check_clock

log = logging.getLogger("tryme")


class LiveFeed:
    """Real public market data for paper mode (no keys)."""

    def __init__(self, public):
        self.x = public
        self.spread_bps = 1.0

    def server_time_ms(self):
        return self.x.server_time_ms()

    def market(self, s):
        return self.x.market(s)

    def candles(self, s, tf, n):
        return self.x.candles(s, tf, n)

    def book(self, s):
        return self.x.book(s)

    def last_price(self, s):
        b = self.x.book(s)
        return (b.bids[0][0] + b.asks[0][0]) / 2


def build(cfg: Config, *, exchange=None, reflex=None, alerts=None) -> Engine:
    store = PolicyStore(cfg.policy_dir)
    version = store.active_version()
    if cfg.mode == "paper" and os.environ.get("TRYME_POLICY"):
        version = os.environ["TRYME_POLICY"]  # shadow-run a candidate; paper only
    policy = store.load(version)
    var = Path(cfg.var_dir)
    if exchange is None:
        from .exchange.ccxt_adapter import CcxtExchange

        if cfg.mode == "paper":
            exchange = PaperExchange(
                LiveFeed(CcxtExchange(cfg.exchange, mode="public")), cfg.paper_balance, cfg.costs
            )
        elif cfg.mode == "testnet":
            exchange = CcxtExchange(cfg.exchange, mode="testnet", testnet_kind=cfg.testnet_kind)
        else:
            ok, why = live_approved(version)
            if not ok:
                raise PermissionError(f"live trading refused: {why}")
            exchange = CcxtExchange(cfg.exchange, mode="live")
    if reflex is None:
        reflex = (
            HaikuReflex(cfg.reflex_model, timeout_s=cfg.reflex_timeout_s)
            if cfg.reflex == "haiku"
            else RuleReflex()
        )
    return Engine(
        cfg,
        exchange,
        reflex,
        policy,
        RiskLayer(cfg.risk, var / "risk_state.json"),
        Journal(var / "journal.jsonl"),
        alerts or Alerts(),
        positions_file=var / "positions.json",
    )


def heartbeat(var_dir: str | Path, now_ms: int) -> None:
    p = Path(var_dir) / "heartbeat"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(now_ms))


def healthy(var_dir: str | Path, tf_ms: int, now_ms: int | None = None) -> tuple[bool, str]:
    p = Path(var_dir) / "heartbeat"
    if not p.exists():
        return False, "no heartbeat yet"
    age = ((now_ms or int(time.time() * 1000)) - int(p.read_text())) / 1000
    return (age <= 2.5 * tf_ms / 1000, f"last heartbeat {age:.0f}s ago")


def tradable_setups(eng: Engine) -> list:
    """Enabled setups whose market passes the liquidity floor. Unknown volume counts as failing."""
    out = []
    for s in eng.cfg.setups:
        if not s.enabled or s.bias == "neutral":
            continue
        v = eng.ex.market(s.symbol).quote_volume_24h
        if v is None or v < eng.cfg.min_quote_volume_24h:
            eng.alerts.send(f"{s.symbol}: refused, 24h volume {v} is under the liquidity floor")
            continue
        out.append(s)
    return out


def step(eng: Engine, setups: list, now_ms: int, n: int) -> None:
    try:
        check_clock(int(time.time() * 1000), eng.ex.server_time_ms(), eng.cfg.max_clock_skew_s)
    except (ClockSkew, TransientError) as e:
        eng.journal.write("miss", now_ms, symbol="*", reason=f"clock: {e}")
        return
    for s in setups:
        try:
            log.info("%s %s", s.symbol, eng.on_candle(s, now_ms))
        except Exception as e:  # one symbol must not take the loop down
            log.exception("candle failed")
            eng.alerts.send(f"{s.symbol}: candle processing error {type(e).__name__}")
    if n % eng.cfg.reconcile_every == 0:
        eng.reconcile(now_ms)
    heartbeat(eng.cfg.var_dir, now_ms)


def run_forever(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    eng = build(cfg)
    tf = TF_MS[cfg.timeframe]
    setups = tradable_setups(eng)
    eng.alerts.send(
        f"tryme started: mode={cfg.mode} policy={eng.policy.version} "
        f"setups={[s.symbol for s in setups]}"
    )
    eng.reconcile(int(time.time() * 1000))
    n = 0
    while True:
        now = int(time.time() * 1000)
        nxt = (now // tf + 1) * tf
        time.sleep(max(0.0, (nxt - now) / 1000) + 2.0)  # 2s after the close so the candle exists
        n += 1
        step(eng, setups, nxt, n)


def seconds_until_utc(hour: int, minute: int) -> float:
    now = datetime.now(UTC)
    tgt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tgt <= now:
        tgt = datetime.fromtimestamp(tgt.timestamp() + 86400, UTC)
    return (tgt - now).total_seconds()
