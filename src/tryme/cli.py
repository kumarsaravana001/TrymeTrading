"""Command line: run, backtest, nightly, health, policy, risk, check-exchange."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .exchange.paper import TF_MS
from .journal import Journal
from .policy import PolicyStore


def _data(cfg, data_dir: str):
    from .backtest import load_csv

    out = {}
    for p in sorted(Path(data_dir).glob("*.csv")):
        base = p.stem.split("-")[0].upper().replace("USDT", "")
        out[f"{base}/USDT:USDT"] = load_csv(p, TF_MS[cfg.timeframe])
    return out


def cmd_backtest(a, cfg) -> int:
    from .backtest import synthetic, walk_forward
    from .models import Setup

    store = PolicyStore(cfg.policy_dir)
    btc = "BTC/USDT:USDT"
    if a.synthetic:
        tf = TF_MS[cfg.timeframe]
        data = {btc: synthetic(6000, tf, seed=1), "SIM/USDT:USDT": synthetic(6000, tf, seed=2)}
    else:
        data = _data(cfg, a.data)
        if btc not in data:
            print(
                f"BLOCKED: no BTC candles in {a.data}/. Put Binance kline CSVs there "
                "(e.g. BTCUSDT-15m.csv). No result was produced."
            )
            return 2
    setups = [s for s in cfg.setups if s.symbol in data and s.enabled] or [
        Setup(
            symbol=s, bias=b, thesis="rule baseline", quality_3="n/a", invalidation=[], as_of="n/a"
        )
        for s in data
        for b in ("long",)
    ]
    wf = walk_forward(cfg, data, setups, store.active(), btc, simulated_data=a.synthetic)
    print(wf.summary())
    return 0


def cmd_nightly(a, cfg) -> int:
    from .nightly import nightly

    store = PolicyStore(cfg.policy_dir)
    var = Path(cfg.var_dir)

    def once() -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        rv = nightly(
            Journal.read(var / "journal.jsonl"),
            store,
            run_tests=a.tests,
            report_dir=var / "reports",
            day=day,
        )
        print(json.dumps(rv.__dict__, indent=2))
        if rv.candidate:
            from .alerts import Alerts

            Alerts().send(
                f"nightly proposed policy {rv.candidate} (NOT active): "
                f"{rv.proposal}; gates passed={rv.gates.get('passed')}"
            )

    if not a.loop:
        once()
        return 0
    from .runtime import seconds_until_utc

    while True:
        time.sleep(seconds_until_utc(0, 10))
        once()


def cmd_policy(a, cfg) -> int:
    from .nightly import gates_passed

    store = PolicyStore(cfg.policy_dir)
    if a.action == "list":
        for v in store.versions():
            print(("* " if v == store.active_version() else "  ") + v, store.load(v).note)
    elif a.action == "promote":
        ok, why = gates_passed(store, a.version)
        if not ok:
            print(f"refused: {why}")
            return 1
        store.promote(a.version, a.approved_by)
        print(f"active policy is now {a.version}")
    elif a.action == "rollback":
        print("rolled back to", store.rollback(a.approved_by))
    return 0


def cmd_risk(a, cfg) -> int:
    from .risk import RiskLayer

    r = RiskLayer(cfg.risk, Path(cfg.var_dir) / "risk_state.json")
    if a.action == "status":
        print(
            json.dumps(
                {
                    "killed": r.killed,
                    "reason": r.kill_reason,
                    "equity_peak": r.equity_peak,
                    "limits": cfg.risk.model_dump(),
                },
                indent=2,
            )
        )
    else:
        r.reset_kill(a.approved_by)
        print("kill switch reset")
    return 0


def cmd_check_exchange(a, cfg) -> int:
    """Read-only reachability check. Places no orders."""
    from .exchange.ccxt_adapter import CcxtExchange

    try:
        x = CcxtExchange(cfg.exchange, mode="public")
        t = x.server_time_ms()
        c = x.candles("BTC/USDT:USDT", cfg.timeframe, 3)
        print(f"ok: server time {t}, last BTC close {c[-1].close}")
        return 0
    except Exception as e:
        print(f"UNREACHABLE: {type(e).__name__}: {str(e)[:200]}")
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("tryme")
    ap.add_argument("--config", default="config/default.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("health")
    sub.add_parser("check-exchange")
    b = sub.add_parser("backtest")
    b.add_argument("--data", default="data")
    b.add_argument("--synthetic", action="store_true", help="SIMULATED data, pipeline check only")
    n = sub.add_parser("nightly")
    n.add_argument("--loop", action="store_true")
    n.add_argument("--tests", action="store_true")
    p = sub.add_parser("policy")
    p.add_argument("action", choices=["list", "promote", "rollback"])
    p.add_argument("version", nargs="?")
    p.add_argument("--approved-by", default="")
    r = sub.add_parser("risk")
    r.add_argument("action", choices=["status", "reset-kill"])
    r.add_argument("--approved-by", default="")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "run":
        from .runtime import run_forever

        run_forever(cfg)
        return 0
    if a.cmd == "health":
        from .runtime import healthy

        ok, why = healthy(cfg.var_dir, TF_MS[cfg.timeframe])
        print(why)
        return 0 if ok else 1
    return {
        "backtest": cmd_backtest,
        "nightly": cmd_nightly,
        "policy": cmd_policy,
        "risk": cmd_risk,
        "check-exchange": cmd_check_exchange,
    }[a.cmd](a, cfg)


if __name__ == "__main__":
    sys.exit(main())
