# Spec

## Purpose
Research the crypto market, find asymmetric setups, and trade them automatically on Binance
USDT-margined perpetuals, surviving first and profiting second. Paper trading until the owner
approves live trading and live credentials exist.

## Owner's answers (2026-11-10)
- Exchange and market: Binance USDT-margined perpetuals.
- Paper balance: 1,000 USDT.
- Off-limits: low-liquidity coins (enforced as a 24h quote-volume floor, default 50M USDT).
- REFLEX model: Claude Haiku 5.5 (no Jev key).

## Requirements
1. **State engine.** One numeric snapshot per symbol per candle, under 400 tokens, built only
   from candles that have closed. Missing data, gaps, stale feeds, exchange errors and clock skew
   stop the candle before any order.
2. **REFLEX schema.** `regime` (trending, mean_reverting, high_vol, crisis), `direction` (long,
   short, neutral), `direction_confidence` 0..1, `toxic_flow` (nullable), `setup_quality` 0..3,
   `risk_state` (safe, near_limit, reduce). Anything outside the schema is rejected.
3. **Gate, in code.** Trade only if `setup_quality >= 2`, `direction_confidence > 0.80`,
   `risk_state == safe`, direction matches the setup's bias, and flow is not toxic.
4. **Sizing.** Fractional Kelly from a calibrated probability, never above quarter Kelly, never
   above the per-trade risk cap.
5. **Hard risk layer.** 2% of equity at risk per trade, 5% max daily loss, 3 open positions, 2x
   leverage, kill switch at 15% drawdown. Checked before every order. No model output can loosen
   it. Confidence under 0.60 or regime `crisis` pauses new risk and alerts the owner.
6. **Execution.** Through ccxt, testnet first. Idempotent client order ids, retries only for
   transient errors, a resting protective stop for every position, reconciliation against the
   exchange, halt on mismatch.
7. **Latency.** Every decision is timed. A decision that misses its deadline skips the candle.
8. **Backtests.** Walk-forward only, fees and slippage on, compared with holding BTC, with the
   BTC comparison stated first. A model REFLEX is only backtested on data after June 2026.
9. **Nightly loop.** Read every fill and miss, score calibration with the Brier score, propose
   changes as versioned candidates, run the same gates on each, never promote silently, always
   keep a rollback.
10. **Approvals.** No approval: reading data, code, tests, backtests, paper trades, safe retries.
    Explicit owner go-ahead: live orders, moving funds, keys with trading rights, production.
11. **Secrets.** Environment only, never in the repo, never printed. Exchange keys: trading
    only, withdrawals off, IP-whitelisted.

## Non-goals
Withdrawals or transfers of any kind. Spot trading. High-frequency or sub-candle trading.
Automatic promotion of any policy. Automatic enabling of any research setup.
