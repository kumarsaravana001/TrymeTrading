# Architecture

```
            BRAIN (Claude Code sessions: research, code, tests, nightly review of reports)
                                   |
                 config/setups.yaml, policies/vNNN.json (human-promoted)
                                   v
  exchange data ──> state_engine ──> REFLEX ──> gate ──> sizing ──> RISK LAYER ──> execution
  (candles, book,   (snapshot,       (judges    (code)   (quarter   (hard limits,   (orders, stops,
   account, clock)   <400 tokens)     only)               Kelly)     kill switch)    reconcile)
                                                                         |
                                   journal.jsonl <───────────────────────┘
                                         |
                                  nightly review ──> candidate policy + gates (never active)
```

## Modules (`src/tryme/`)
| File | Role |
|---|---|
| `models.py` | Typed contracts: `ReflexDecision`, `StateSnapshot`, `Setup`, orders, fills, positions |
| `config.py` | Config, frozen `RiskLimits`, cost assumptions, the live approval check |
| `state_engine.py` | Causal snapshot; raises on missing, gapped, stale or skewed data |
| `reflex/base.py` | `Reflex` interface, `RuleReflex` (code rules), deadline wrapper |
| `reflex/haiku.py` | Claude Haiku 5.5, low effort, structured output |
| `sizing.py` | The gate and fractional-Kelly sizing |
| `risk.py` | Hard limits, daily loss, kill switch (persisted), reconciliation halt |
| `exchange/paper.py` | Simulated fills with fees, slippage, impact, rejections, outages |
| `exchange/ccxt_adapter.py` | Binance USDT-M via ccxt: public data, testnet, gated live |
| `engine.py` | One candle for one symbol, shared by backtest, paper, testnet and live |
| `backtest.py` | Walk-forward, BTC benchmark, training-cutoff guard |
| `policy.py` | Versioned thresholds, propose, promote (named human), rollback |
| `nightly.py` | Brier score, calibration refit, proposals, gates |
| `runtime.py` | The 24/7 loop, heartbeat, liquidity floor, clock check |
| `alerts.py`, `journal.py`, `cli.py` | Telegram or log alerts, JSONL journal, commands |

## Decisions and reasons
- **One engine for every mode.** Backtest, paper, testnet and live run the same `Engine`, so what
  is tested is what trades.
- **Risk limits are not policy.** `RiskLimits` is frozen and loaded from config. The nightly loop
  can only write `Policy` fields, and `Policy` caps Kelly at one quarter.
- **The model can only tighten.** The gate needs the model's `risk_state` *and* the code's own
  risk state to be `safe`. A stricter model blocks; a looser model changes nothing.
- **Sizing probability is anchored at break-even.** `p = breakeven + shrink * (confidence - 0.5)`.
  A REFLEX with no information gets zero Kelly size. `shrink` starts at 0.5 as a hypothesis and is
  refit nightly from outcomes.
- **Stops live on the exchange.** Each entry places a reduce-only stop order. If that fails, the
  position is closed at once. A dead process still has a stop in the market.
- **Pessimistic simulation.** If a candle touches both stop and target, the stop is assumed. A
  candle that opens beyond the stop fills at the open. Fees and slippage on every fill.
- **`near_limit` blocks new entries.** At 60% of the daily-loss or drawdown limit (3% on the day,
  9% from peak) the code's risk state leaves `safe` and no new trade opens. A 9% drawdown
  therefore stops trading until the owner reviews and resets it.
- **No Jev adapter.** Its API is unknown to this codebase and no key exists. The `Reflex`
  interface is the slot for it; nothing was guessed.
- **Timeframe 15m.** A model call (seconds) fits comfortably inside the candle. Hypothesis.
