# Runbook

## Start, stop, status
```bash
cp .env.example .env            # fill in what you have; never commit it
docker compose up -d --build    # trader + nightly, restart: unless-stopped
docker compose ps               # trader shows "healthy" after its first candles
docker compose logs -f trader
docker compose down             # stop (open paper positions stay in var/positions.json)
sudo systemctl enable docker    # once, so containers come back after a reboot
```
State lives in `./var` (journal, positions, risk state, heartbeat, nightly reports) and
`./policies`. Back both up.

## Modes
| `TRYME_MODE` | Orders go to | Needs |
|---|---|---|
| `paper` | nowhere (simulated fills on real public prices) | reachability to Binance public API |
| `testnet` | Binance futures testnet | `BINANCE_TESTNET_API_KEY/SECRET` |
| `live` | real exchange | everything in `APPROVAL_GATE.md` |

## Alerts and what to do
| Alert | Meaning | Action |
|---|---|---|
| `new risk paused (regime=crisis ...)` | REFLEX sees crisis or confidence under 0.60 | Nothing opens. Check the market; no action needed to stay safe |
| `KILL SWITCH ... Flattening` | 15% drawdown from peak | All positions closed. Investigate before `tryme risk reset-kill --approved-by NAME` |
| `reconciliation mismatch` | Exchange positions differ from the local book | New risk halted. Fix by hand on the exchange, then it clears on the next clean reconcile |
| `order failed after retries` | Exchange unreachable for an order | Check connectivity and exchange status |
| `stop placement failed; closing position` | No protective stop could be placed | Position was closed; check exchange order rules |
| `refused, 24h volume ... under the liquidity floor` | Symbol too thin or volume unknown | Expected for illiquid coins; remove the setup |
| `nightly proposed policy vNNN (NOT active)` | A candidate exists | Review, see below |

Trading also stops opening positions at a 9% drawdown or a 3% daily loss (`near_limit`).
`tryme risk status` shows the state.

## Health
`tryme health` exits non-zero when the last heartbeat is older than 2.5 candles. Docker marks the
container unhealthy; it does not restart a hung-but-alive process by itself, so watch
`docker compose ps` or add an external monitor.

## Nightly loop and policy changes
Runs at 00:10 UTC. Writes `var/reports/nightly-DATE.json`. With at least 100 scored directional
calls it may write a candidate `policies/vNNN.json` and `vNNN.gates.json`.

A candidate is never active. To adopt one:
1. Read the report and the candidate diff.
2. Backtest it on real data (`tryme backtest`), and shadow it in paper:
   `TRYME_POLICY=vNNN TRYME_MODE=paper` on a second instance.
3. Record the results in `vNNN.gates.json` (each gate `"status": "passed"`).
4. `tryme policy promote vNNN --approved-by YOUR_NAME` (refuses unless every gate passed).
5. Roll back any time: `tryme policy rollback --approved-by YOUR_NAME`.

## Setups
`config/setups.yaml`. A setup trades only when `enabled: true`, its market passes the liquidity
floor, and the REFLEX direction matches its `bias`. Disable a setup the moment one of its
`invalidation` conditions is observed.

## Keys
Trading permission only. Withdrawals off. IP-whitelist the host. Keys only in `.env` on the
host, mode 600. Never in the repo, chat, or logs. Rotate after any suspected exposure.
