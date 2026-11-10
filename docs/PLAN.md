# Plan

| # | Step | Status |
|---|---|---|
| 1 | Spec, architecture, typed contracts | done |
| 2 | State engine with causality and data-quality tests | done, tested |
| 3 | Hard risk layer, gate, Kelly sizing | done, tested |
| 4 | Paper exchange (fees, slippage, failures) and engine | done, tested |
| 5 | Walk-forward backtester with BTC benchmark and cutoff guard | done, tested on simulated data only |
| 6 | REFLEX on Haiku 5.5 | done, tested with a fake client only |
| 7 | ccxt adapter for Binance USDT-M | done, tested with a fake client only |
| 8 | Nightly loop, policy versioning, rollback | done, tested |
| 9 | Docker Compose runtime, health check, Telegram alerts | done; image build result in PROGRESS.md |
| 10 | Research: regime and 5 to 10 finalists | partial: regime drafted, 2 unverified leads, 0 enabled |
| 11 | Backtest on real post-June-2026 data vs BTC | blocked: no market data reachable |
| 12 | Paper run on real feed (2 weeks minimum) | blocked: needs a host that can reach Binance |
| 13 | Testnet run | blocked: needs testnet keys and reachability |
| 14 | Live | owner approval gate, see `APPROVAL_GATE.md` |

## Order of the next session
1. Allow the hosts in `PROGRESS.md` (or run on a VPS) and run `tryme check-exchange`.
2. Download Binance klines after 2026-07-01 into `data/`, run `tryme backtest`.
3. Redo research with primary sources; verify listings and volume; enable setups by hand.
4. Start the paper runtime; let the nightly loop accumulate at least 100 scored calls.
