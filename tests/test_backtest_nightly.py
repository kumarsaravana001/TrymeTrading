import json

import pytest

from conftest import SYM, TF, FixedReflex
from tryme.backtest import (
    MODEL_CUTOFF_MS,
    LookAheadError,
    btc_hold,
    load_csv,
    run,
    synthetic,
    walk_forward,
)
from tryme.config import LIVE_PHRASE, RiskLimits, live_approved, load_config
from tryme.models import Setup
from tryme.nightly import brier, direction_outcomes, fit_shrink, gates_passed, nightly, review
from tryme.policy import Policy, PolicyStore

BTC = "BTC/USDT:USDT"


def setups(*syms):
    return [
        Setup(symbol=s, bias="long", thesis="t", quality_3="q", invalidation=[], as_of="d")
        for s in syms
    ]


def test_btc_hold_pays_costs(cfg):
    flat = synthetic(200, TF, seed=1, vol=0.0)
    ret, dd = btc_hold(flat, flat[0].close_ts, flat[-1].close_ts, cfg)
    assert ret == pytest.approx(-2 * (0.0005 + 0.0002), abs=1e-5) and dd == pytest.approx(0)


def test_walk_forward_reports_against_btc_first(cfg):
    data = {BTC: synthetic(3000, TF, seed=1), SYM: synthetic(3000, TF, seed=2)}
    wf = walk_forward(
        cfg, data, setups(SYM), Policy(), BTC, train=800, test=400, simulated_data=True
    )
    assert len(wf.folds) >= 3
    first = wf.summary().splitlines()[0]
    assert ("BEATS" in first or "DOES NOT BEAT" in first) and "SIMULATED" in first
    assert wf.beats_btc == (wf.strategy_ret > wf.btc_ret)
    # test windows are contiguous and each starts where its train window ended
    for a, b in zip(wf.folds, wf.folds[1:]):
        assert b["test_start"] == a["test_end"]


def test_costs_make_results_worse(cfg):
    data = {SYM: synthetic(1500, TF, seed=5, drift=0.0004)}
    a, b = data[SYM][200].close_ts, data[SYM][-1].close_ts
    from tryme.reflex.base import RuleReflex

    with_costs = run(cfg, data, setups(SYM), RuleReflex(), Policy(), a, b)
    free = cfg.model_copy(
        update={
            "costs": cfg.costs.model_copy(
                update={"taker_fee": 0.0, "slippage_bps": 0.0, "impact_bps_per_10k": 0.0}
            )
        }
    )
    without = run(free, data, setups(SYM), RuleReflex(), Policy(), a, b)
    assert with_costs.trades > 0 and with_costs.fees > 0
    assert without.ret > with_costs.ret


def test_risk_limits_hold_inside_a_backtest(cfg):
    data = {SYM: synthetic(2500, TF, seed=7, vol=0.01)}
    r = run(
        cfg,
        data,
        setups(SYM),
        FixedReflex(),
        Policy(),
        data[SYM][200].close_ts,
        data[SYM][-1].close_ts,
    )
    assert r.trades > 5
    assert r.max_dd < 0.15 + 0.03  # kill switch latches at 15%; one trade of overshoot at most
    for e in (j for j in r.journal if j["kind"] == "exit"):
        assert e["risk_usd"] <= 0.02 * max(r.equity_curve) * 1.01  # 2% of equity at entry


def test_model_reflex_refused_before_training_cutoff(cfg):
    class Model(FixedReflex):
        is_model = True

    old = {SYM: synthetic(300, TF, seed=1)}  # starts in 2020
    with pytest.raises(LookAheadError):
        run(cfg, old, setups(SYM), Model(), Policy(), old[SYM][200].close_ts, old[SYM][-1].close_ts)
    new = {SYM: synthetic(300, TF, seed=1, start_ms=MODEL_CUTOFF_MS)}
    run(cfg, new, setups(SYM), Model(), Policy(), new[SYM][200].close_ts, new[SYM][-1].close_ts)


def test_load_binance_csv(tmp_path):
    p = tmp_path / "BTCUSDT-15m.csv"
    p.write_text(
        "open_time,open,high,low,close,volume\n1700000000000,1,2,0.5,1.5,10\n"
        "1700000900000000,1.5,2,1,1.8,11\n"
    )
    cs = load_csv(p, TF)
    assert [c.ts for c in cs] == [1700000000000, 1700000900000] and cs[0].close_ts == cs[1].ts


# --- nightly -------------------------------------------------------------------------------------


def test_brier():
    assert brier([(1.0, True), (0.0, False)]) == 0
    assert brier([(0.5, True), (0.5, False)]) == 0.25
    assert brier([]) is None


def journal(n=300, skill=True):
    """A steadily rising price; calls are all long (skill) or all short (no skill)."""
    recs, px = [], 100.0
    for i in range(n):
        recs.append(
            {
                "kind": "decision",
                "ts": i,
                "symbol": SYM,
                "mid": px,
                "direction": "long" if skill else "short",
                "direction_confidence": 0.9,
                "latency_s": 0.2,
            }
        )
        px *= 1.001
    recs.append({"kind": "miss", "ts": 1, "symbol": SYM, "reason": "reflex: timeout"})
    recs.append(
        {
            "kind": "exit",
            "ts": 2,
            "symbol": SYM,
            "pnl": -3.0,
            "p_win": 0.7,
            "win": False,
            "risk_usd": 20,
        }
    )
    return recs


def test_review_scores_calibration():
    good = review(journal(skill=True), Policy())
    bad = review(journal(skill=False), Policy())
    assert good.direction_brier == pytest.approx(0.01) and bad.direction_brier == pytest.approx(
        0.81
    )
    assert good.proposal == {"calibration_shrink": 1.0}
    assert good.misses == {"reflex": 1} and good.trades == 1 and good.pnl == -3.0
    assert good.trade_brier == pytest.approx(0.49) and good.median_latency_s == 0.2
    assert fit_shrink(direction_outcomes(journal(skill=False))) == 0.0
    assert "min_direction_confidence" in bad.proposal and bad.proposal["calibration_shrink"] == 0.0


def test_too_little_data_proposes_nothing(tmp_path):
    store = PolicyStore(tmp_path / "p")
    rv = nightly(journal(n=50), store)
    assert rv.proposal == {} and rv.candidate is None and store.versions() == ["v001"]


def test_nightly_never_promotes(tmp_path):
    store = PolicyStore(tmp_path / "p")
    rv = nightly(journal(skill=False), store, report_dir=tmp_path / "r", day="2026-11-10")
    assert rv.candidate == "v002" and store.active_version() == "v001"
    assert store.load("v002").parent == "v001"
    assert rv.gates["passed"] is False  # gates that did not run do not count as passed
    assert gates_passed(store, "v002")[0] is False
    assert (
        json.loads((tmp_path / "r" / "nightly-2026-11-10.json").read_text())["candidate"] == "v002"
    )
    assert [h["action"] for h in store.history()] == ["init", "propose"]


def test_promote_needs_a_name_and_rollback_works(tmp_path):
    store = PolicyStore(tmp_path / "p")
    store.propose({"min_direction_confidence": 0.85}, "test")
    with pytest.raises(PermissionError):
        store.promote("v002", " ")
    store.promote("v002", "owner")
    assert store.active().min_direction_confidence == 0.85
    assert store.rollback("owner") == "v001" and store.active_version() == "v001"
    assert store.history()[-1] == {**store.history()[-1], "action": "rollback", "by": "owner"}


def test_policy_cannot_exceed_quarter_kelly_or_touch_risk_limits(tmp_path):
    store = PolicyStore(tmp_path / "p")
    with pytest.raises(ValueError):
        store.propose({"kelly_fraction": 0.5}, "too big")
    with pytest.raises(ValueError):
        store.propose({"max_leverage": 10}, "not a policy field")
    with pytest.raises(ValueError):
        RiskLimits(max_leverage=20)


def test_cli_refuses_to_promote_without_gates(tmp_path, capsys):
    from tryme.cli import main

    cfgf = tmp_path / "c.yaml"
    cfgf.write_text(f"policy_dir: {tmp_path / 'p'}\nvar_dir: {tmp_path / 'v'}\n")
    PolicyStore(tmp_path / "p").propose({"min_direction_confidence": 0.9}, "x")
    assert main(["--config", str(cfgf), "policy", "promote", "v002", "--approved-by", "me"]) == 1
    assert "refused" in capsys.readouterr().out
    assert PolicyStore(tmp_path / "p").active_version() == "v001"


# --- live gate and config ------------------------------------------------------------------------


def test_live_needs_env_phrase_and_approval_file(tmp_path, monkeypatch):
    f = tmp_path / "LIVE_APPROVAL"
    monkeypatch.delenv("TRYME_LIVE_APPROVED", raising=False)
    assert not live_approved("v001", f)[0]
    monkeypatch.setenv("TRYME_LIVE_APPROVED", LIVE_PHRASE)
    assert not live_approved("v001", f)[0]
    f.write_text("approved by owner for policy v002\n")
    assert not live_approved("v001", f)[0]  # approval is per policy version
    f.write_text("approved by owner for policy v001\n")
    assert live_approved("v001", f)[0]


def test_live_mode_refused_without_approval(cfg, monkeypatch):
    from tryme.runtime import build

    monkeypatch.delenv("TRYME_LIVE_APPROVED", raising=False)
    cfg.mode = "live"
    with pytest.raises(PermissionError, match="live trading refused"):
        build(cfg)


def test_repo_config_loads_with_brief_defaults():
    c = load_config("config/default.yaml")
    assert c.mode == "paper" and c.paper_balance == 1000 and c.exchange == "binanceusdm"
    assert c.risk == RiskLimits()
    assert not any(s.enabled for s in c.setups)  # nothing trades until research is verified


def test_no_free_money_on_a_driftless_walk(cfg):
    """Look-ahead canary: with no drift there is no edge, so after costs the mean must be <= 0."""
    from tryme.reflex.base import RuleReflex

    rets = []
    for seed in range(5):
        d = {SYM: synthetic(2500, TF, seed=seed, regime_vol=0.0)}
        rets.append(
            run(
                cfg,
                d,
                setups(SYM),
                RuleReflex(),
                Policy(),
                d[SYM][200].close_ts,
                d[SYM][-1].close_ts,
            ).ret
        )
    assert sum(rets) / len(rets) <= 0.0
