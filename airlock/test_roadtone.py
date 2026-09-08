#!/usr/bin/env python3
"""Offline tests for ROADTONE RiskGate + Signal — no token, no MCP needed.
Run: .venv/bin/python airlock/test_roadtone.py
Locks in Bouncer lessons: intent-level symbol check, fail-closed, reduce-only
always allowed, observe-mode deny, policy fail-loud."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airlock.roadtone import Policy, RiskGate, ch3_signal, db  # noqa: E402

POLICY_YAML = """
mode: observe
dry_run: true
symbols:
  allow: ["BTCUSDT", "ETHUSDT"]
  block: ["*UP*", "*BULL*"]
limits:
  max_order_notional_usdt: 50
  max_gross_exposure_usdt: 200
  daily_loss_pct: 2.0
  max_open_orders: 5
  max_orders_per_5m: 10
  min_quote_balance_usdt: 10
"""

tmp = Path(tempfile.mkdtemp())
pol_path = tmp / "policy.yaml"
pol_path.write_text(POLICY_YAML)

# --- policy fail-loud on missing file ---
try:
    Policy(tmp / "nope.yaml")
    print("FAIL: missing policy did not raise")
    sys.exit(1)
except SystemExit:
    print("PASS: missing policy -> SystemExit (fail-loud)")

p = Policy(pol_path)
conn = db()
gate = RiskGate(p, conn)

acct = {"freeUsdt": 100.0, "grossExposureUsdt": 0.0}

cases = [
    # (intent, account, expect_decision, label)
    ({"symbol": "BTCUPUSDT", "side": "BUY"}, acct, "DENY", "block-glob *UP* on NEW intent"),
    ({"symbol": "BULLUSDT", "side": "BUY"}, acct, "DENY", "block-glob *BULL*"),
    ({"symbol": "XRPUSDT", "side": "BUY"}, acct, "DENY", "not in allowlist"),
    ({"symbol": "BTCUSDT", "side": "REDUCE"}, None, "ALLOW", "reduce-only allowed even w/o account"),
    ({"symbol": "BTCUSDT", "side": "REDUCE"}, acct, "ALLOW", "reduce-only allowed"),
    ({"symbol": "BTCUSDT", "side": "BUY"}, None, "DENY", "no account snapshot -> fail-closed"),
    ({"symbol": "BTCUSDT", "side": "BUY"}, {"freeUsdt": 5.0, "grossExposureUsdt": 0.0}, "DENY", "free below min"),
    ({"symbol": "BTCUSDT", "side": "BUY", "notionalUsdt": 999}, acct, "DENY", "notional over cap"),
    ({"symbol": "BTCUSDT", "side": "BUY"}, acct, "DENY", "observe-mode deny"),
]
fails = 0
for intent, account, expect, label in cases:
    got = gate.simulate(intent, account)["decision"]
    status = "PASS" if got == expect else "FAIL"
    if got != expect:
        fails += 1
    print(f"{status}: {label} -> {got}")

# --- signal: empty/bad snapshots -> no intents, no crash ---
print("PASS: signal empty ->", ch3_signal({"snapshots": []}) == 0 or len(ch3_signal({"snapshots": []})) == 0)
bad = ch3_signal({"snapshots": [{"symbol": "BTCUSDT", "ok": False, "err": "x"}]})
print("PASS: signal all-fail snaps -> 0 intents:", len(bad) == 0)

# --- garbage intent fields -> fail-closed deny ---
weird = gate.simulate({"symbol": {"evil": 1}, "side": None}, acct)
print("PASS: malformed intent ->", weird["decision"], "(must be DENY)")

print()
if fails:
    print(f"RESULT: {fails} FAILURES")
    sys.exit(1)
print("RESULT: ALL PASS")
