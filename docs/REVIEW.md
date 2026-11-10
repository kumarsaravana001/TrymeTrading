# Review (phase 5) and final check

Self-review of the build on 2026-11-10. Findings fixed during review are listed with the test
that now covers them.

## Found and fixed
| Finding | Fix | Test |
|---|---|---|
| Sizing gave a positive Kelly size to a REFLEX with no information (p anchored at 0.5 while the 3:2 payoff breaks even near 0.43) | p anchored at the trade's break-even rate | `test_kelly_math`, `test_size_zero_without_edge_or_room` |
| Simulated stops filled at the stop price even when the candle opened beyond it | Fill at the worse of stop and open | covered by engine stop tests |
| Module named `types.py` shadowed the standard library when run from its folder | Renamed to `models.py` | import in every test |
| Synthetic backtest looked very profitable | Cause: the generator has built-in drift regimes. Added a driftless canary | `test_no_free_money_on_a_driftless_walk` |

## Final check
- **Is the edge organic or incentive driven?** No edge has been shown. The only backtest ran on
  simulated data. On a driftless walk the rule strategy makes about zero before costs and loses
  after them (12 seeds: mean -0.4% cost-free, -3.8% with costs).
- **Is the catalyst priced in?** For both research leads, probably yes (estimate). Both are
  disabled.
- **Does value accrue to the token?** HYPE: yes by policy, figures inconsistent. MON: unknown.
- **Will it survive costs and slippage?** Unknown on real data. Costs are charged on every
  simulated fill; fee tier and slippage numbers are assumptions.
- **Is any hard limit delegated to a model?** No. `test_model_cannot_override_code` and the gate
  tests cover it. The model's `risk_state` can only tighten.
- **Look-ahead, leakage?** Snapshot and engine are tested against tampered future data; the
  replay feed cannot serve an unclosed candle; a model REFLEX is refused on data before
  2026-07-01. Walk-forward picks parameters on the train window only.
- **Untested failure modes.** Listed below.

## WHAT COULD I BE WRONG ABOUT?
1. **The ccxt adapter has never touched Binance.** Order parameters (`newClientOrderId`,
   `reduceOnly`, `STOP_MARKET` with `stopPrice`), position fields and the balance field are from
   ccxt conventions and are tested only against a fake. Hedge-mode accounts, margin mode and
   leverage setting are not handled. Testnet must prove this before anything else.
2. **Looking up an order by client id after a lost reply** may need the symbol on Binance; if the
   lookup fails the engine reports the order as failed and reconciliation then halts trading.
   Safe, but it would need a human.
3. **The Haiku REFLEX has never been called.** Its latency, cost, and whether its confidence
   means anything are unknown. The 0.80 gate may never fire, or may fire on noise.
4. **The rule strategy may simply be a costly way to hold beta.** Trend rules look good on
   trending synthetic data by construction.
5. **Stop behaviour in live mode** relies on the exchange-side stop plus reconciliation noticing
   the position is gone; realised PnL of such exits is not read back from the exchange yet.
6. **One-candle granularity.** Intra-candle moves between a stop and the next check are modelled
   pessimistically in simulation but not at all for targets in live mode.
7. **Funding payments are not modelled** in paper or backtest. On multi-day holds they matter.
8. **Cost assumptions** (0.05% taker, 2 bps slippage) may be too kind for thin books and for a
   1,000 USDT account's minimum sizes on some symbols.
9. **Correlation.** Three open positions in altcoins are close to one position in a selloff. The
   2x leverage cap bounds it; nothing measures it.
10. **The research is thin.** Search snippets, conflicting numbers, no verified listings.
