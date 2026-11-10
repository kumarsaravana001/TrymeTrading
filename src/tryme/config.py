"""Runtime configuration. Secrets come from the environment only, never from files in the repo."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .models import Setup


class RiskLimits(BaseModel):
    """Hard limits. Loaded from config only; no model and no nightly job may write these."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_per_trade: float = Field(0.02, gt=0, le=0.05)
    max_daily_loss: float = Field(0.05, gt=0, le=0.2)
    max_open_positions: int = Field(3, ge=1, le=10)
    max_leverage: float = Field(2.0, gt=0, le=5)
    kill_drawdown: float = Field(0.15, gt=0, le=0.5)
    pause_confidence: float = Field(0.60, ge=0, le=1)
    near_limit_fraction: float = Field(0.6, gt=0, lt=1)


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    taker_fee: float = 0.0005  # assumption: Binance USDT-M base taker tier; verify on your account
    slippage_bps: float = 2.0  # assumption, applied against you on every market fill
    impact_bps_per_10k: float = 1.0  # assumption: extra slippage per 10k USDT notional


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["paper", "testnet", "live"] = "paper"
    exchange: str = "binanceusdm"
    testnet_kind: Literal["sandbox", "demo"] = "sandbox"
    timeframe: str = "15m"
    lookback: int = 120
    paper_balance: float = 1000.0
    min_quote_volume_24h: float = 50_000_000.0  # liquidity floor; lower-volume symbols are refused
    max_feed_age_s: float = 30.0
    max_clock_skew_s: float = 2.0
    reflex: Literal["rule", "haiku"] = "rule"
    reflex_model: str = "claude-haiku-5-5"
    reflex_timeout_s: float = 8.0
    reconcile_every: int = 4  # candles
    var_dir: str = "var"
    policy_dir: str = "policies"
    risk: RiskLimits = RiskLimits()
    costs: Costs = Costs()
    setups: list[Setup] = []


def load_config(
    path: str | Path = "config/default.yaml", setups: str | Path | None = None
) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    sp = Path(setups) if setups else Path(path).parent / "setups.yaml"
    if sp.exists():
        raw["setups"] = (yaml.safe_load(sp.read_text()) or {}).get("setups", [])
    if os.environ.get("TRYME_MODE"):
        raw["mode"] = os.environ["TRYME_MODE"]
    if os.environ.get("TRYME_REFLEX"):
        raw["reflex"] = os.environ["TRYME_REFLEX"]
    return Config.model_validate(raw)


LIVE_PHRASE = "I_APPROVE_LIVE_TRADING"


def live_approved(
    policy_version: str, approval_file: str | Path = "LIVE_APPROVAL"
) -> tuple[bool, str]:
    """Live orders need both: the env phrase and an approval file naming the active policy version.

    The file is git-ignored and written by the owner by hand; nothing in this codebase creates it.
    """
    if os.environ.get("TRYME_LIVE_APPROVED") != LIVE_PHRASE:
        return False, "TRYME_LIVE_APPROVED is not set to the approval phrase"
    p = Path(approval_file)
    if not p.exists():
        return False, f"{p} does not exist"
    if policy_version not in p.read_text().split():
        return False, f"{p} does not name active policy {policy_version}"
    return True, "approved"
