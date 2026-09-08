---
title: Binance Airlock — 6-Chamber Execution Guardrail
description: >-
  Turn plain-English trading instructions into typed, hash-locked, human-approved
  mandates and execute them safely on Binance. Six chambers: EVIDENCE (read-only
  snapshot), MANDATE (intent → typed mandate → SHA-256 approval hash), LOCK (typed
  yes bound to the hash, policy caps enforced), EXECUTE (idempotent clientOrderId
  keyed to the mandate, testnet hard gate), RECONCILE (fill vs mandate,
  MATCH/NO_TRADE/DEVIATION), KILL (cancel all + flatten ledger-owned positions only).
  HMAC REST execution on Binance Spot Testnet works today; Binance MCP Server
  adapter implemented up to /authorize (blocked by client allowlist, error 3346001).
metadata:
  version: 1.0.0
  author: fsriches
license: MIT
---

# Binance Airlock

Deterministic guardrails for LLM-driven execution on Binance. One instruction gets
**one intent → one hash → one typed yes**. No silent retries, no re-pricing, no drift.

## Install

```bash
git clone https://github.com/fsriches/airlock.git && cd airlock
pip install -r requirements.txt
export AIRLOCK_HOME=$HOME/.airlock   # holds binance_testnet.json (chmod 600)
```

## Usage

```bash
airlock check                                  # EVIDENCE: balance + ticker, no keys printed
airlock intent "buy 10 USDT of BNBUSDT market" # MANDATE: typed mandate + approval hash
echo "<8-char-hash>" | airlock yes             # LOCK: approval must match the hash
airlock execute                                # EXECUTE: clientOrderId=AIR-ORDER-<hash20>, testnet only
airlock reconcile --order-id AIR-ORDER-...     # RECONCILE: MATCH / NO_TRADE / DEVIATION
airlock kill                                   # KILL: cancel all + flatten ledger-owned only
airlock verify                                 # audit: SHA-256 ledger chain integrity
```

## Policy

Caps live in `policy.yaml` (max qty per order, notional cap, price tolerance for
reconciliation, leverage forced to 0). DENY reasons are logged with the mandate hash.

## Guarantees

- Every order is traceable to a typed, hash-approved mandate (`AIR-ORDER-<hash20>`).
- Retries are idempotent — Binance rejects duplicate `clientOrderId`.
- Testnet-only hard gate in EXECUTE; mainnet keys are refused.
- KILL never touches balances outside the Airlock ledger (faucet-safe).
- Ledger is a hash-chained SQLite log; `airlock verify` detects any tampering.

## Links

- Repo: https://github.com/fsriches/airlock
- Evidence: `docs/evidence/demo_scenarios_live_20260909.log` (4 live scenarios), `docs/evidence/mcp_init_401.txt`
