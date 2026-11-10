"""One candle, one symbol: data checks -> exits -> REFLEX -> gate -> size -> risk -> order.

The same engine runs in backtests, paper, testnet and live. Code owns every threshold and every
side effect; the REFLEX only returns a judgement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .alerts import Alerts
from .config import Config
from .exchange.base import Exchange, OrderRejected, TransientError
from .exchange.paper import TF_MS, PaperExchange
from .journal import Journal
from .models import Account, Fill, OrderIntent, Position, Setup
from .policy import Policy
from .reflex.base import Reflex, ReflexUnavailable, timed_decide
from .risk import RiskLayer
from .sizing import gate, size_position
from .state_engine import DataError, build_snapshot, closed_candles


class Engine:
    def __init__(
        self,
        cfg: Config,
        exchange: Exchange,
        reflex: Reflex,
        policy: Policy,
        risk: RiskLayer,
        journal: Journal,
        alerts: Alerts,
        *,
        positions_file: str | Path | None = None,
        sleep=time.sleep,
    ):
        self.cfg, self.ex, self.reflex, self.policy = cfg, exchange, reflex, policy
        self.risk, self.journal, self.alerts = risk, journal, alerts
        self.tf_ms = TF_MS[cfg.timeframe]
        self.sleep = sleep
        self.simulated = isinstance(exchange, PaperExchange)
        self.positions_file = Path(positions_file) if positions_file else None
        self.positions: dict[str, Position] = {}
        if self.positions_file and self.positions_file.exists():
            self.positions = {
                k: Position(**v) for k, v in json.loads(self.positions_file.read_text()).items()
            }

    def _persist(self) -> None:
        if self.positions_file:
            self.positions_file.parent.mkdir(parents=True, exist_ok=True)
            self.positions_file.write_text(
                json.dumps({k: v.__dict__ for k, v in self.positions.items()})
            )

    # --- order plumbing ----------------------------------------------------------------------
    def _send(self, intent: OrderIntent, ts: int) -> Fill | None:
        """Retries only transient errors, always with the same client id, then asks the exchange
        whether the order exists before giving up, so a lost reply cannot double an order."""
        for attempt in range(3):
            try:
                f = self.ex.submit(intent)
                break
            except TransientError as e:
                self.journal.write("order_retry", ts, client_id=intent.client_id, error=str(e))
                self.sleep(2**attempt)
            except OrderRejected as e:
                self.journal.write("order_rejected", ts, client_id=intent.client_id, error=str(e))
                return None
        else:
            try:
                f = self.ex.find_fill(intent.client_id)
            except TransientError:
                f = None
            if f is None:
                self.journal.write("order_failed", ts, client_id=intent.client_id)
                self.alerts.send(f"order failed after retries: {intent.symbol} {intent.side}")
                return None
        if f.status == "rejected" or f.qty <= 0:
            self.journal.write("order_rejected", ts, client_id=intent.client_id, error=f.detail)
            return None
        self.journal.write("fill", ts, **{k: v for k, v in f.__dict__.items() if k != "ts"})
        return f

    def _close(self, pos: Position, ts: int, reason: str, trigger: float | None = None) -> None:
        side = "sell" if pos.qty > 0 else "buy"
        intent = OrderIntent(
            pos.symbol,
            side,
            abs(pos.qty),
            True,
            f"c{ts}{pos.symbol[:6]}".replace("/", ""),
            trigger or pos.entry,
            reason,
        )
        acct = Account(self.ex.equity(), 0.0, dict(self.positions))
        v = self.risk.check(intent, acct, {}, None, 0.0)
        if not v.allowed:  # cannot happen for a well-formed close; surface loudly if it does
            self.alerts.send(f"close blocked for {pos.symbol}: {v.reason}")
            return
        if self.simulated and trigger is not None:
            f = self.ex.fill_at(intent, trigger, ts)
            self.journal.write("fill", ts, **{k: v for k, v in f.__dict__.items() if k != "ts"})
        else:
            f = self._send(intent, ts)
        if f is None:
            return
        sign = 1 if pos.qty > 0 else -1
        pnl = sign * (f.price - pos.entry) * f.qty - f.fee
        self.ex.cancel_stops(pos.symbol)
        if f.qty >= abs(pos.qty) * (1 - 1e-9):
            del self.positions[pos.symbol]
        else:
            pos.qty -= sign * f.qty
        self._persist()
        self.journal.write(
            "exit",
            ts,
            symbol=pos.symbol,
            reason=reason,
            pnl=pnl,
            p_win=pos.p_win,
            win=pnl > 0,
            risk_usd=pos.risk_usd,
            simulated=self.simulated,
        )

    def _manage(self, pos: Position, candles, ts: int) -> None:
        """Exit rules, all code. If stop and target are both inside one candle, assume the stop."""
        last = candles[-1]
        if last.ts < pos.opened_ts:
            return
        long = pos.qty > 0
        hit_stop = last.low <= pos.stop if long else last.high >= pos.stop
        hit_tgt = last.high >= pos.target if long else last.low <= pos.target
        if hit_stop:
            # in testnet/live the exchange-side stop order does this; reconcile() picks it up
            if self.simulated:
                # a candle that opens beyond the stop fills at the open, not at the stop
                self._close(
                    pos, ts, "stop", min(pos.stop, last.open) if long else max(pos.stop, last.open)
                )
        elif hit_tgt:
            self._close(pos, ts, "target", pos.target if self.simulated else None)
        elif (ts - pos.opened_ts) / self.tf_ms >= self.policy.max_hold_candles:
            self._close(pos, ts, "time")

    # --- the candle --------------------------------------------------------------------------
    def on_candle(self, setup: Setup, now_ms: int) -> str:
        sym = setup.symbol
        try:
            candles = closed_candles(
                self.ex.candles(sym, self.cfg.timeframe, self.cfg.lookback), now_ms
            )
            book = self.ex.book(sym)
            equity = self.ex.equity()
        except TransientError as e:
            self.journal.write("miss", now_ms, symbol=sym, reason=f"exchange: {e}")
            return "miss:exchange"
        self.risk.update(equity, now_ms)
        acct = Account(equity, equity, dict(self.positions))
        if self.risk.killed and self.positions:
            self.alerts.send(f"KILL SWITCH: {self.risk.kill_reason}. Flattening.")
            for p in list(self.positions.values()):
                self._close(p, now_ms, "kill")
            return "killed"
        try:
            snap = build_snapshot(
                sym,
                candles,
                book,
                acct,
                now_ms,
                tf_ms=self.tf_ms,
                lookback=self.cfg.lookback,
                risk_state=self.risk.risk_state(acct),
                equity_peak=self.risk.equity_peak,
                day_start_equity=self.risk.day_start_equity,
                max_feed_age_s=self.cfg.max_feed_age_s,
                ema_fast=self.policy.ema_fast,
                ema_slow=self.policy.ema_slow,
            )
        except DataError as e:
            self.journal.write("miss", now_ms, symbol=sym, reason=f"data: {e}")
            return "miss:data"

        if sym in self.positions:
            self._manage(self.positions[sym], candles, now_ms)
            return "managed"

        try:
            d, latency = timed_decide(self.reflex, snap, setup, self.cfg.reflex_timeout_s)
        except ReflexUnavailable as e:
            self.journal.write("miss", now_ms, symbol=sym, reason=f"reflex: {e}")
            return "miss:reflex"
        ok, why = gate(d, setup, self.policy)
        self.journal.write(
            "decision",
            now_ms,
            symbol=sym,
            mid=snap.mid,
            latency_s=round(latency, 4),
            gate=why,
            policy=self.policy.version,
            **d.model_dump(),
        )
        # pause conditions escalate even when the gate already said no
        if d.regime == "crisis" or d.direction_confidence < self.cfg.risk.pause_confidence:
            if d.direction != "neutral" or d.regime == "crisis":
                self.alerts.send(
                    f"{sym}: new risk paused (regime={d.regime}, "
                    f"confidence={d.direction_confidence:.2f})"
                )
        if not ok:
            return f"hold:{why}"

        info = self.ex.market(sym)
        marks = {s: (snap.mid if s == sym else p.entry) for s, p in self.positions.items()}
        s = size_position(
            d,
            snap,
            equity,
            self.policy,
            self.cfg.risk,
            self.cfg.costs,
            gross_exposure=acct.exposure(marks),
            min_notional=info.min_notional,
            qty_step=info.qty_step,
        )
        if s.qty <= 0:
            self.journal.write("skip", now_ms, symbol=sym, reason=s.reason)
            return f"skip:{s.reason}"
        side = "buy" if d.direction == "long" else "sell"
        intent = OrderIntent(
            sym, side, s.qty, False, f"o{now_ms}{sym[:6]}".replace("/", ""), snap.mid, "entry"
        )
        v = self.risk.check(intent, acct, marks, d, s.risk_usd / s.qty)
        if not v.allowed:
            self.journal.write("risk_block", now_ms, symbol=sym, reason=v.reason)
            if v.escalate:
                self.alerts.send(f"{sym}: {v.reason}")
            return f"blocked:{v.reason}"
        f = self._send(intent, now_ms)
        if f is None:
            return "order_failed"
        sign = 1 if side == "buy" else -1
        stop_d, tgt_d = abs(snap.mid - s.stop), abs(s.target - snap.mid)
        pos = Position(
            sym,
            sign * f.qty,
            f.price,
            f.price - sign * stop_d,
            f.price + sign * tgt_d,
            now_ms,
            risk_usd=s.risk_usd * f.qty / s.qty,
            p_win=s.p_win,
            setup_as_of=setup.as_of,
        )
        self.positions[sym] = pos
        self._persist()
        try:
            self.ex.place_stop(
                sym,
                "sell" if sign > 0 else "buy",
                f.qty,
                pos.stop,
                f"s{now_ms}{sym[:6]}".replace("/", ""),
            )
        except (TransientError, OrderRejected) as e:
            # a position without a resting stop is not acceptable: get out
            self.alerts.send(f"{sym}: stop placement failed ({e}); closing position")
            self._close(pos, now_ms, "no_stop")
            return "closed:no_stop"
        return "opened"

    # --- reconciliation ----------------------------------------------------------------------
    def reconcile(self, now_ms: int) -> bool:
        """Local book vs exchange. Any unexplained difference halts new risk until it is clean."""
        try:
            remote = self.ex.positions()
        except TransientError as e:
            self.journal.write("reconcile", now_ms, ok=False, reason=str(e))
            return False
        problems = []
        for sym, pos in list(self.positions.items()):
            rq = remote.get(sym, 0.0)
            if rq == 0.0 and not self.simulated:
                # exchange-side stop fired while we were not looking
                self.journal.write(
                    "exit",
                    now_ms,
                    symbol=sym,
                    reason="stop_on_exchange",
                    pnl=None,
                    p_win=pos.p_win,
                    win=False,
                    risk_usd=pos.risk_usd,
                    simulated=False,
                )
                del self.positions[sym]
                self._persist()
            elif abs(rq - pos.qty) > 1e-9 * max(1.0, abs(pos.qty)):
                problems.append(f"{sym}: local {pos.qty} vs exchange {rq}")
        for sym, rq in remote.items():
            if sym not in self.positions and rq != 0.0:
                problems.append(f"{sym}: unknown exchange position {rq}")
        if problems:
            self.risk.halted_reason = "reconciliation mismatch: " + "; ".join(problems)
            self.alerts.send(self.risk.halted_reason)
        else:
            self.risk.halted_reason = ""
        self.journal.write("reconcile", now_ms, ok=not problems, problems=problems)
        return not problems
