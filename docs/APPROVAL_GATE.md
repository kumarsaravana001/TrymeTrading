# Live approval gate

Live trading is off. The code refuses `mode: live` unless both of these exist, and only the
owner can create them:

1. Environment variable `TRYME_LIVE_APPROVED=I_APPROVE_LIVE_TRADING` on the host.
2. A file `LIVE_APPROVAL` in the working directory (git-ignored) that names the active policy
   version, for example: `approved by NAME on DATE for policy v001`. A new policy version needs a
   new approval.

Plus live keys in `BINANCE_LIVE_API_KEY/SECRET` (trading only, withdrawals off, IP-whitelisted).

## Checklist before the owner signs
- [ ] `tryme check-exchange` passes on the production host
- [ ] Walk-forward backtest on real data after 2026-07-01, fees and slippage on, result vs
      holding BTC read and accepted (if it does not beat BTC, that was stated first)
- [ ] At least 2 weeks of paper trading on the real feed with no reconciliation mismatch
- [ ] Testnet run: entries, exchange-side stops, exits and reconciliation observed working
- [ ] Haiku REFLEX measured: latency inside the deadline, directional Brier score under 0.25
- [ ] At least one setup verified from primary sources and enabled by hand
- [ ] Telegram alerts received on a phone, including a forced kill-switch test in paper
- [ ] Fee tier and slippage assumptions in `config/default.yaml` checked against the account
- [ ] Start size decided (suggest the smallest balance that clears exchange minimums)

Never needs approval: reading data, writing code, tests, backtests, paper trades, safe retries.
Always needs the owner's explicit go-ahead: live orders, moving funds, creating or using keys
with trading rights, deploying to production.
