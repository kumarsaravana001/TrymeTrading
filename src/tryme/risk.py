"""Hard risk layer. Pure code, checked before every order. Nothing a model returns can loosen it:
the model's risk_state can only make the outcome stricter, never more permissive."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import RiskLimits
from .models import Account, OrderIntent, ReflexDecision, RiskState


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    escalate: bool = False


class RiskLayer:
    def __init__(self, limits: RiskLimits, state_file: str | Path | None = None):
        self.limits = limits
        self.state_file = Path(state_file) if state_file else None
        self.killed = False
        self.kill_reason = ""
        self.equity_peak = 0.0
        self.day = ""
        self.day_start_equity = 0.0
        self.halted_reason = ""  # set by reconciliation mismatches; cleared by a clean reconcile
        self._load()

    # --- persistence: the kill switch must survive restarts -------------------------------
    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            d = json.loads(self.state_file.read_text())
            self.killed = d.get("killed", False)
            self.kill_reason = d.get("kill_reason", "")
            self.equity_peak = d.get("equity_peak", 0.0)
            self.day = d.get("day", "")
            self.day_start_equity = d.get("day_start_equity", 0.0)

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(
                    {
                        "killed": self.killed,
                        "kill_reason": self.kill_reason,
                        "equity_peak": self.equity_peak,
                        "day": self.day,
                        "day_start_equity": self.day_start_equity,
                    }
                )
            )

    # --- state -----------------------------------------------------------------------------
    def update(self, equity: float, now_ms: int) -> None:
        day = datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_equity = day, equity
        self.equity_peak = max(self.equity_peak, equity)
        if not self.killed and self.drawdown(equity) >= self.limits.kill_drawdown:
            self.killed = True
            self.kill_reason = f"drawdown {self.drawdown(equity):.1%} hit kill switch"
        self._save()

    def drawdown(self, equity: float) -> float:
        return max(0.0, 1 - equity / self.equity_peak) if self.equity_peak > 0 else 0.0

    def daily_loss(self, equity: float) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return max(0.0, 1 - equity / self.day_start_equity)

    def reset_kill(self, approved_by: str) -> None:
        if not approved_by.strip():
            raise PermissionError("kill switch reset needs a named human approver")
        self.killed, self.kill_reason = False, ""
        self.equity_peak = 0.0  # peak restarts from current equity on next update
        self._save()

    def risk_state(self, account: Account) -> RiskState:
        L = self.limits
        dd, dl = self.drawdown(account.equity), self.daily_loss(account.equity)
        if self.killed or self.halted_reason or dd >= L.kill_drawdown or dl >= L.max_daily_loss:
            return "reduce"
        f = L.near_limit_fraction
        if (
            dd >= f * L.kill_drawdown
            or dl >= f * L.max_daily_loss
            or len(account.positions) >= L.max_open_positions
        ):
            return "near_limit"
        return "safe"

    # --- the check -------------------------------------------------------------------------
    def check(
        self,
        intent: OrderIntent,
        account: Account,
        marks: dict[str, float],
        decision: ReflexDecision | None,
        stop_distance: float,
    ) -> Verdict:
        """`decision` is None for code-driven exits. Reducing orders always pass."""
        L = self.limits
        if intent.qty <= 0 or intent.ref_price <= 0:
            return Verdict(False, "non-positive quantity or price")
        pos = account.positions.get(intent.symbol)
        if intent.reduce_only:
            if pos is None:
                return Verdict(False, "reduce-only with no position")
            closing = (pos.qty > 0 and intent.side == "sell") or (
                pos.qty < 0 and intent.side == "buy"
            )
            if not closing or intent.qty > abs(pos.qty) * (1 + 1e-9):
                return Verdict(False, "reduce-only order would increase or flip the position")
            return Verdict(True, "reduce")

        if self.killed:
            return Verdict(False, f"kill switch: {self.kill_reason}", escalate=True)
        if self.halted_reason:
            return Verdict(False, f"halted: {self.halted_reason}", escalate=True)
        if decision is None:
            return Verdict(False, "new risk requires a REFLEX decision")
        if decision.regime == "crisis":
            return Verdict(False, "regime is crisis: new risk paused", escalate=True)
        if decision.direction_confidence < L.pause_confidence:
            return Verdict(
                False, "REFLEX confidence below pause level: new risk paused", escalate=True
            )
        if decision.risk_state != "safe":
            return Verdict(False, f"REFLEX risk_state={decision.risk_state}")
        code_state = self.risk_state(account)
        if code_state != "safe":
            return Verdict(False, f"risk layer state={code_state}")
        if self.daily_loss(account.equity) >= L.max_daily_loss:
            return Verdict(False, "daily loss limit reached")
        if pos is not None:
            return Verdict(False, "already positioned in symbol")
        if len(account.positions) >= L.max_open_positions:
            return Verdict(False, "max open positions")
        if stop_distance <= 0:
            return Verdict(False, "no stop distance")
        risk_usd = intent.qty * stop_distance
        if risk_usd > L.risk_per_trade * account.equity * (1 + 1e-9):
            return Verdict(False, f"trade risk {risk_usd:.2f} exceeds per-trade limit")
        gross = account.exposure(marks) + intent.qty * intent.ref_price
        if gross > L.max_leverage * account.equity * (1 + 1e-9):
            return Verdict(False, f"leverage {gross / account.equity:.2f}x exceeds limit")
        return Verdict(True, "ok")
