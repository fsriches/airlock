#!/usr/bin/env bash
# Airlock demo — 6 chambers on Binance Spot Testnet
set -u
cd "$(dirname "$0")/.."
export AIRLOCK_HOME="$HOME/.airlock"
export PATH="$HOME/airlock/bin:$PATH"

pause() { sleep "${1:-2.5}"; }
say()   { echo; echo "$1"; echo; }
gethash(){ grep -oE "intent hash : [0-9a-f]{16}" | head -1 | awk '{print $4}'; }

clear
echo "AIRLOCK — 6-chamber execution guardrail for Binance Agent OS"
echo "Live on Binance Spot Testnet — $(date -u '+%Y-%m-%d %H:%M:%SZ')"
pause 3

say "### 1/6 EVIDENCE + MANDATE + LOCK: buy 10 USDT of BNBUSDT market on Binance Spot Testnet"
say "-- airlock check (read-only; no keys on screen) --"
airlock check
pause 3

say "-- airlock intent: natural language -> typed mandate + approval hash --"
OUT=$(airlock intent "buy 10 USDT of BNBUSDT"); echo "$OUT"
H=$(echo "$OUT" | gethash)
pause 2.5

say "-- LOCK: operator types the hash back -> ALLOW --"
airlock yes "$H"
pause 2.5

say "-- EXECUTE: clientOrderId = AIR-ORDER-<hash20>, testnet hard gate --"
airlock execute
pause 3

say "-- RECONCILE: fill vs mandate -> MATCH --"
airlock reconcile
pause 3

say "### 2/6 LIMIT 20% BELOW MARKET: resting order, no fill -> NO_TRADE"
OUT=$(airlock intent "buy 10 USDT of BNBUSDT limit -20%")
echo "$OUT"
H=$(echo "$OUT" | gethash)
pause 2.5
airlock yes "$H"
pause 2
airlock execute
pause 2
airlock reconcile
pause 3

say "### 3/6 POLICY DENY: 50 USDT exceeds cap -> DENY, never sent"
OUT=$(airlock intent "buy 50 USDT of BNBUSDT")
echo "$OUT"
H=$(echo "$OUT" | gethash)
pause 2.5
airlock yes "$H"
pause 2.5
airlock no

say "### 4/6 KILL: cancel all + flatten ledger-owned positions only (faucet untouched)"
airlock kill
pause 3

say "### 5/6 VERIFY: hash-chained ledger audit"
airlock verify
pause 3

say "### 6/6 WRAP"
echo "Repo: https://github.com/fsriches/airlock"
echo "Agent OS MCP adapter implemented, OAuth blocked by client allowlist (3346001)"
echo "Demo on Binance Spot Testnet"
pause 2
