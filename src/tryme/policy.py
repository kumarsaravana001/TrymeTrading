"""Versioned trading policy: the thresholds the nightly loop may propose changes to.

Risk limits are deliberately not here. A candidate never becomes active without an explicit
promote call, and every promotion keeps the previous version for rollback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "v001"
    parent: str | None = None
    note: str = "initial hypotheses, no evidence of edge"
    min_setup_quality: int = Field(2, ge=0, le=3)
    min_direction_confidence: float = Field(0.80, ge=0.5, le=1)
    kelly_fraction: float = Field(0.25, gt=0, le=0.25)  # quarter Kelly is the ceiling
    # calibrated p = 0.5 + shrink * (confidence - 0.5); refit nightly from outcomes
    calibration_shrink: float = Field(0.5, ge=0, le=1)
    stop_atr: float = Field(2.0, gt=0)
    target_atr: float = Field(3.0, gt=0)
    max_hold_candles: int = Field(96, ge=1)
    ema_fast: int = Field(12, ge=2)
    ema_slow: int = Field(48, ge=3)


class PolicyStore:
    def __init__(self, root: str | Path = "policies"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self._active_file.exists():
            self.save(Policy())
            self._active_file.write_text("v001\n")
            self._log("init", "v001", "bootstrap")

    @property
    def _active_file(self) -> Path:
        return self.root / "ACTIVE"

    def _log(self, action: str, version: str, who: str) -> None:
        with (self.root / "history.jsonl").open("a") as f:
            f.write(
                json.dumps(
                    {"ts": int(time.time()), "action": action, "version": version, "by": who}
                )
                + "\n"
            )

    def history(self) -> list[dict]:
        p = self.root / "history.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def versions(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("v*.json"))

    def load(self, version: str) -> Policy:
        return Policy.model_validate_json((self.root / f"{version}.json").read_text())

    def save(self, policy: Policy) -> None:
        (self.root / f"{policy.version}.json").write_text(policy.model_dump_json(indent=2) + "\n")

    def active_version(self) -> str:
        return self._active_file.read_text().strip()

    def active(self) -> Policy:
        return self.load(self.active_version())

    def propose(self, changes: dict, note: str) -> Policy:
        """Write a candidate. It is not active and nothing here makes it active."""
        base = self.active()
        n = max(int(v[1:]) for v in self.versions()) + 1
        cand = Policy.model_validate(
            {
                **base.model_dump(),
                **changes,
                "version": f"v{n:03d}",
                "parent": base.version,
                "note": note,
            }
        )
        self.save(cand)
        self._log("propose", cand.version, "nightly")
        return cand

    def promote(self, version: str, approved_by: str) -> None:
        if not approved_by.strip():
            raise PermissionError("promotion needs a named human approver")
        self.load(version)  # must exist and validate
        self._active_file.write_text(version + "\n")
        self._log("promote", version, approved_by)

    def rollback(self, approved_by: str) -> str:
        parent = self.active().parent
        if not parent:
            raise ValueError("active policy has no parent to roll back to")
        self._active_file.write_text(parent + "\n")
        self._log("rollback", parent, approved_by)
        return parent
