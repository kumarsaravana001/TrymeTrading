# PROGRESS

Read this first in every new session. Last updated 2026-11-10.

## Where things stand
The whole system is built and tested offline. It has never touched a real exchange, a real model
or real market data, because the build environment's network policy blocks them. Nothing trades:
no setup is enabled and the mode is paper.

### Implemented and tested (87 tests, also run inside the Docker build)
- State engine: causal snapshot under 400 tokens; rejects missing, gapped, stale, crossed and
  clock-skewed data.
- Hard risk layer: 2% per trade, 5% daily loss, 3 positions, 2x leverage, 15% kill switch that
  latches and survives restarts; crisis or confidence under 0.60 pauses and alerts.
- Gate and quarter-Kelly sizing.
- Paper exchange with fees, slippage, size impact, rejections, outages.
- Engine: retries, idempotent orders, protective stops, reconciliation halt, kill-switch flatten.
- Walk-forward backtester, BTC benchmark, training-cutoff guard, look-ahead canary.
- Policy versioning, nightly review (Brier score, calibration refit, proposals, gates), rollback.
- Live approval gate, liquidity floor, heartbeat and health check, Telegram alerts.
- Docker image builds and the runtime container starts and stays up (checked 2026-11-10).

### Implemented, tested only against fakes
- `exchange/ccxt_adapter.py` (Binance USDT-M via ccxt): never connected to Binance.
- `reflex/haiku.py` (Claude Haiku 5.5): never called the API. No API key here.
- Telegram sending: tested with a fake HTTP call. No bot token here.

### Simulated only
- The one backtest that ran used generated data: strategy +547% vs "BTC" +171% over 8 folds.
  This says nothing about real markets; the generator has built-in trends. On driftless data the
  strategy averages -3.8% after costs.

### Blocked
| What | Missing piece |
|---|---|
| Real market data, paper trading on a live feed, testnet | Network policy denies `fapi.binance.com`, `testnet.binancefuture.com`, `demo-fapi.binance.com`, `api.binance.com`, `data.binance.vision` |
| Real-data backtest vs BTC | Same; needs Binance klines after 2026-07-01 in `data/` |
| Research to primary-source standard | `api.coingecko.com`, `api.llama.fi`, `defillama.com`, `tokenomist.ai`, `coinglass.com`, news sites not reachable; only search summaries |
| Haiku REFLEX live | `ANTHROPIC_API_KEY` for the runtime |
| Telegram alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `api.telegram.org` reachable |
| Testnet orders | Binance futures testnet API key and secret |
| Jev REFLEX | No key, and its API is unknown; no adapter written |
| Live trading | Owner approval per `docs/APPROVAL_GATE.md` |

## Next
See "Order of the next session" in `docs/PLAN.md`.

## Decisions and reasons
| Date | Decision | Reason |
|---|---|---|
| 2026-11-10 | Binance USDT perps, 1,000 USDT paper balance, exclude low-liquidity coins, Haiku 5.5 REFLEX | Owner's answers |
| 2026-11-10 | Python package `tryme`, one engine for backtest, paper, testnet, live | What is tested is what trades |
| 2026-11-10 | Risk limits frozen in config, separate from the nightly-tunable policy | No model or nightly job may loosen a hard limit |
| 2026-11-10 | Sizing probability anchored at the trade's break-even rate | Zero information must mean zero size |
| 2026-11-10 | Default REFLEX is the code rule set, Haiku opt-in via `TRYME_REFLEX=haiku` | No API key yet; rules are also the pre-cutoff backtest baseline |
| 2026-11-10 | 15m candles, 2 ATR stop, 3 ATR target, 96-candle time stop | Hypotheses; model latency fits inside the candle |
| 2026-11-10 | `near_limit` (3% daily loss or 9% drawdown) blocks new entries | The gate requires `risk_state == safe`; survival first |
| 2026-11-10 | Both research leads (MON short, HYPE long) compiled but disabled | Unverified sources and unverified Binance listings |
| 2026-11-10 | No Jev adapter | Never invent an API |
| 2026-11-10 | Liquidity floor 50M USDT 24h volume; unknown volume is refused | Owner excluded low-liquidity coins |
| 2026-11-10 | Paper-mode only `TRYME_POLICY` override for shadowing candidates | Lets a candidate be paper-tested without promotion |
| 2026-11-10 | Dockerfile base image is a build argument | Docker Hub rate-limited the build host; a mirror worked |
