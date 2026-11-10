"""Nightly review: read every decision, fill and miss; score calibration; propose, never promote.

A proposal becomes a candidate policy file plus a gates file. Gates that could not run count as
not passed. Promotion is a separate, human, named action (`tryme policy promote`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .policy import Policy, PolicyStore

MIN_SAMPLES = 100
HORIZON = 12  # candles used to score a directional call


@dataclass
class Review:
    decisions: int = 0
    misses: dict[str, int] = field(default_factory=dict)
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    trade_brier: float | None = None
    direction_brier: float | None = None
    direction_samples: int = 0
    fitted_shrink: float | None = None
    median_latency_s: float | None = None
    proposal: dict = field(default_factory=dict)
    candidate: str | None = None
    gates: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in pairs) / len(pairs)


def direction_outcomes(records: list[dict], horizon: int = HORIZON) -> list[tuple[float, bool]]:
    """(confidence, was the call right `horizon` decisions later) for every non-neutral call.
    Uses only journaled mids, so it scores exactly what the system saw."""
    by_sym: dict[str, list[dict]] = {}
    for r in records:
        if r["kind"] == "decision":
            by_sym.setdefault(r["symbol"], []).append(r)
    out = []
    for rs in by_sym.values():
        rs.sort(key=lambda r: r["ts"])
        for i, r in enumerate(rs[:-horizon]):
            if r["direction"] == "neutral":
                continue
            move = rs[i + horizon]["mid"] / r["mid"] - 1
            out.append(
                (r["direction_confidence"], move > 0 if r["direction"] == "long" else move < 0)
            )
    return out


def fit_shrink(pairs: list[tuple[float, bool]]) -> float | None:
    """Least-squares s in p = 0.5 + s*(conf-0.5). 0 means confidence carries no information."""
    den = sum((c - 0.5) ** 2 for c, _ in pairs)
    if den <= 0:
        return None
    num = sum((c - 0.5) * ((1.0 if y else 0.0) - 0.5) for c, y in pairs)
    return min(1.0, max(0.0, num / den))


def review(records: list[dict], policy: Policy) -> Review:
    rv = Review()
    dec = [r for r in records if r["kind"] == "decision"]
    rv.decisions = len(dec)
    for r in records:
        if r["kind"] == "miss":
            k = r["reason"].split(":")[0]
            rv.misses[k] = rv.misses.get(k, 0) + 1
    exits = [r for r in records if r["kind"] == "exit"]
    rv.trades, rv.wins = len(exits), sum(1 for e in exits if e["win"])
    rv.pnl = sum(e["pnl"] or 0.0 for e in exits)
    rv.trade_brier = brier([(e["p_win"], e["win"]) for e in exits])
    pairs = direction_outcomes(records)
    rv.direction_samples = len(pairs)
    rv.direction_brier = brier(pairs)
    lat = sorted(r["latency_s"] for r in dec)
    rv.median_latency_s = lat[len(lat) // 2] if lat else None

    if len(pairs) < MIN_SAMPLES:
        rv.notes.append(
            f"{len(pairs)} scored directional calls; need {MIN_SAMPLES} before proposing anything"
        )
        return rv
    s = fit_shrink(pairs)
    rv.fitted_shrink = s
    if s is not None and abs(s - policy.calibration_shrink) >= 0.05:
        rv.proposal["calibration_shrink"] = round(s, 2)
        rv.notes.append(
            f"confidence-to-probability shrink refit from {policy.calibration_shrink} to {s:.2f}"
        )
    if rv.direction_brier is not None and rv.direction_brier >= 0.25:
        rv.notes.append(
            "directional Brier is no better than a coin flip: the REFLEX shows no "
            "measurable skill on this sample"
        )
        new = min(0.95, round(policy.min_direction_confidence + 0.02, 2))
        if new > policy.min_direction_confidence:
            rv.proposal["min_direction_confidence"] = new
    return rv


def run_gates(candidate: Policy, *, run_tests: bool, backtest_fn=None) -> dict:
    """Same gates for every change. 'not_run' is a failure for promotion purposes."""
    gates: dict = {}
    if run_tests:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x"], capture_output=True, text=True
        )
        gates["regression_tests"] = {
            "status": "passed" if p.returncode == 0 else "failed",
            "detail": p.stdout.strip().splitlines()[-1:] or [""],
        }
    else:
        gates["regression_tests"] = {"status": "not_run"}
    if backtest_fn is not None:
        try:
            gates["walk_forward"] = backtest_fn(candidate)
        except Exception as e:
            gates["walk_forward"] = {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
    else:
        gates["walk_forward"] = {"status": "not_run", "detail": "no historical data available"}
    # filled in by `tryme policy shadow-result` after a paper run under TRYME_POLICY=<candidate>
    gates["paper_shadow"] = {
        "status": "not_run",
        "detail": "run paper mode with TRYME_POLICY set to this version",
    }
    gates["passed"] = all(g.get("status") == "passed" for g in gates.values())
    return gates


def nightly(
    journal_records: list[dict],
    store: PolicyStore,
    *,
    run_tests: bool = False,
    backtest_fn=None,
    report_dir: str | Path | None = None,
    day: str = "",
) -> Review:
    active = store.active()
    rv = review(journal_records, active)
    if rv.proposal:
        cand = store.propose(rv.proposal, "; ".join(rv.notes))
        rv.candidate = cand.version
        rv.gates = run_gates(cand, run_tests=run_tests, backtest_fn=backtest_fn)
        (store.root / f"{cand.version}.gates.json").write_text(json.dumps(rv.gates, indent=2))
    if report_dir:
        d = Path(report_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"nightly-{day or 'latest'}.json").write_text(json.dumps(rv.__dict__, indent=2))
    return rv


def gates_passed(store: PolicyStore, version: str) -> tuple[bool, str]:
    p = store.root / f"{version}.gates.json"
    if not p.exists():
        return False, "no gates file for this version"
    g = json.loads(p.read_text())
    bad = [k for k, v in g.items() if isinstance(v, dict) and v.get("status") != "passed"]
    return (not bad, "all gates passed" if not bad else "gates not passed: " + ", ".join(bad))
