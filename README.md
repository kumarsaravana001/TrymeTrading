# TrymeTrading

A 24/7 crypto trading agent for Binance USDT-margined perpetuals. It starts in paper trading and
cannot place a live order without an explicit, two-part approval.

Two layers that never blur:

- **Code** owns every threshold, size, order and side effect: data checks, the trade gate,
  Kelly sizing, the hard risk layer, execution, reconciliation.
- **The REFLEX** (code rules, or Claude Haiku 5.5 with structured output) only returns a typed
  judgement per candle: regime, direction, confidence, toxic flow, setup quality, risk state.

Read `PROGRESS.md` first. It says what works, what is only simulated, and what is blocked.

## Quick start

```bash
pip install -e .[dev]
python -m pytest -q                      # regression suite
python -m tryme.cli backtest --synthetic # pipeline check on SIMULATED data
python -m tryme.cli check-exchange       # can this host reach Binance?
cp .env.example .env && docker compose up -d --build   # 24/7 paper runtime
```

## Documents

| Phase | File |
|---|---|
| 1 Spec | `docs/SPEC.md` |
| 2 Architecture | `docs/ARCHITECTURE.md` |
| 3 Plan | `docs/PLAN.md` |
| 4 Build (test-first) | `src/tryme/`, `tests/` |
| 5 Review | `docs/REVIEW.md` |
| 6 Ship | `docs/RUNBOOK.md`, `docs/APPROVAL_GATE.md`, `Dockerfile`, `docker-compose.yml` |
| Research | `research/2026-11-10.md`, `config/setups.yaml` |

Nothing here is financial advice or evidence of a trading edge. Every threshold is a hypothesis.
