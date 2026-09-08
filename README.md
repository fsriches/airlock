# Airlock — 6-Chamber Execution Guardrail for Binance Agent OS

Live page: https://fsriches.github.io/airlock/

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Airlock** turns a plain LLM trading instruction (`"buy 10 USDT of BNBUSDT market"`) into a
typed, hashed, human-approved mandate, then executes it on Binance with deterministic
guardrails — and reconciles every fill against the mandate afterward. One transaction gets
**one intent → one hash → one typed yes**. No silent retries, no re-pricing, no drift.

Built on **Binance Agent OS**:
- **Binance API execution** — HMAC-signed Spot **Testnet** demo (`api.binance.com` / `api-gcp.binance.com`, `testnet.binance.vision` keys, zero mainnet calls).
- **Binance MCP Server adapter** — OAuth 2.0 (CIMD) client implemented, reaches `/authorize`, **blocked by client allowlist (error 3346001)**; evidence in `docs/evidence/`.
- **Skills Hub compatible skill** — see `skills/binance-airlock/SKILL.md`.

## The 6 Chambers

```
intent ──▶ [EVIDENCE] ──▶ [MANDATE] ──▶ [LOCK] ──▶ [EXECUTE] ──▶ [RECONCILE]
                                    typed "yes"                        │
                                          hash-bound                   ▼
                                                                    [KILL] (cancel + flatten, ledger-scoped only)
```

| Chamber | Responsibility |
|---|---|
| **EVIDENCE** | Read-only balance/ticker snapshot (`airlock check`). No keys printed, ever. |
| **MANDATE** | Natural-language intent → typed mandate (action/symbol/quote/qty/type/limit) → SHA-256 → 8-char approval hash. |
| **LOCK** | Human must type the 8-char hash back. Mismatch = DENY. Policy caps (qty, leverage=0, notional) enforced here. |
| **EXECUTE** | Keyed to the hash: `clientOrderId = AIR-ORDER-<hash20>` → idempotent retries, no duplicate fills. Testnet-only hard gate. |
| **RECONCILE** | Query by `clientOrderId`, compare fill vs mandate (price tolerance from `policy.yaml`) → MATCH / NO_TRADE / DEVIATION. |
| **KILL** | Cancel all open orders + flatten **ledger-owned positions only**. Faucet and external balances are never touched. |

Live evidence (4 scenarios, one session): `docs/evidence/demo_scenarios_live_20260909.log` —
MATCH · NO_TRADE · DENY×2 · KILL+flatten, ledger genesis chain `airlock verify` → **OK**.

## Quickstart

```bash
pip install -r requirements.txt
export AIRLOCK_HOME=$HOME/.airlock
airlock check                                        # EVIDENCE
airlock intent "buy 10 USDT of BNBUSDT market"       # MANDATE → prints approval hash
echo "<hash>" | airlock yes                          # LOCK (typed yes, hash-bound)
airlock execute                                      # EXECUTE (testnet gate)
airlock reconcile --order-id AIR-ORDER-...           # RECONCILE
airlock kill                                         # KILL (ledger-scoped)
airlock verify                                       # hash-chain audit
```

Keys live in `$AIRLOCK_HOME/binance_testnet.json` (chmod 600), never in the repo.

## Status

- REST HMAC execution + 4 live demo scenarios: **working** (Binance Spot Testnet).
- MCP Server adapter: implemented up to `/authorize`; OAuth client rejected by allowlist (`3346001`) — see `docs/evidence/mcp_init_401.txt`.
- Roadtone (perp/leverage) deliberately **out of scope** for this submission.

MIT — © 2026 fsriches
