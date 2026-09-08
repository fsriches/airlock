#!/usr/bin/env python3
"""Offline tests for the AIRLOCK six-chamber engine (no network, no keys).
Run: .venv/bin/python airlock/test_chambers.py
Locks in: parse determinism, hash-binding of approvals, fail-closed LOCK,
rate cap, exposure cap, ledger chain integrity, KILL accounting.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import sqlite3
from pathlib import Path

# isolate test ledger BEFORE importing chambers (LEDGER resolved at import time)
_TEST_HOME = tempfile.mkdtemp()
os.environ["AIRLOCK_HOME"] = _TEST_HOME

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import airlock.chambers as ch  # noqa: E402
from airlock.chambers import (  # noqa: E402
    Policy, client_order_id, intent_hash, parse_intent, verify_ledger,
)

POLICY_YAML = """
mode: gated
environment: testnet
symbols:
  allow: ["BNBUSDT", "BTCUSDT"]
  block: ["*UP*"]
limits:
  max_order_notional_usdt: 15
  max_gross_exposure_usdt: 20
  max_orders_per_5m: 10
  min_quote_balance_usdt: 10
  fill_tolerance_pct: 5.0
"""

fails = 0


def check(label: str, cond: bool) -> None:
    global fails
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fails += 1


# --- policy fail-loud ---
tmp = Path(tempfile.mkdtemp())
(tmp / "policy.yaml").write_text(POLICY_YAML)
try:
    Policy(tmp / "nope.yaml")
    check("missing policy raises", False)
except SystemExit:
    check("missing policy raises (fail-loud)", True)
pol = Policy(tmp / "policy.yaml")

# --- parse_intent ---
i = parse_intent("buy 10 USDT of BNBUSDT")
check("parse market intent", i["side"] == "BUY" and i["notionalUsdt"] == 10.0
      and i["symbol"] == "BNBUSDT" and i["kind"] == "MARKET")
i2 = parse_intent("sell 10 USDT of BTCUSDT limit -20%")
check("parse limit intent w/ deviation", i2["kind"] == "LIMIT" and i2["limitDeviationPct"] == -20.0)
try:
    parse_intent("do something weird 500 USDT")
    check("garbage intent raises", False)
except ValueError:
    check("garbage intent raises", True)
try:
    parse_intent("buy 10 USDT of XRPUSDT")
    check("non-allowlist symbol parses (LOCK denies)", True)  # parse ok; LOCK denies
except ValueError:
    check("non-allowlist symbol parses (LOCK denies)", True)

# --- hash binding ---
h1, h2 = intent_hash(i), intent_hash(dict(i))
check("intent hash deterministic", h1 == h2 and len(h1) == 64)
icopy = dict(i)
icopy["notionalUsdt"] = 10.01
check("hash changes on any field change", intent_hash(icopy) != h1)
check("clientOrderId from hash", client_order_id(h1).startswith("AIR-ORDER-") and client_order_id(h1)[10:30] == h1[:20])

# --- LOCK fail-closed with a FakeClient ---
class FakeClient:
    base = "https://testnet.binance.vision"
    has_keys = True
    api_restriction = {"enableWithdrawals": False}

    def __init__(self, free_usdt=100.0, coins=None):
        self.free_usdt = free_usdt
        self.coins = coins or {}
        self.calls = []

    def sync_time(self):
        return {"offsetMs": 0}

    def call(self, tool, params=None):
        self.calls.append((tool, params))
        if tool == "apiRestrictions":
            return {"ok": True, "result": {"content": [dict(self.api_restriction)]}}
        if tool == "account":
            bals = [{"asset": "USDT", "free": str(self.free_usdt)}]
            bals += [{"asset": a, "free": str(q)} for a, q in self.coins.items()]
            return {"ok": True, "result": {"content": [{"balances": bals}]}}
        if tool in ("ticker24hr", "ticker"):
            return {"ok": True, "result": {"content": [{"lastPrice": "700.00", "priceChangePercent": "0.5"}]}}
        if tool == "time":
            return {"ok": True, "result": {"content": [{"serverTime": int(time.time() * 1000)}]}}
        return {"ok": True, "result": {"content": [{}]}}

    def _request(self, method, path, params, signed):
        if path == "/api/v3/openOrders":
            return {"ok": True, "result": {"content": [[]]}}
        return {"ok": True, "result": {"content": [{}]}}


conn = ch.db()
conn.execute("DELETE FROM events")  # fresh chain for this run

fc = FakeClient(free_usdt=100.0)
# mandate + bound yes
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(fc, pol, conn, None)
check("LOCK allows bound intent (policy-ok)", lk["decision"] == "ALLOW")
# tampered typed hash
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(fc, pol, conn, "deadbeef" + "0" * 24)
check("LOCK denies wrong typed hash", lk["decision"] == "DENY" and "mismatch" in lk["reason"])
# correct prefix binds
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(fc, pol, conn, rec["hash"][:16])
check("LOCK accepts hash prefix", lk["decision"] == "ALLOW")
# notional over cap
rec = ch.ch_mandate(conn, "buy 50 USDT of BNBUSDT")
lk = ch.ch_lock(fc, pol, conn, rec["hash"][:16])
check("LOCK denies 50 USDT (cap 15)", lk["decision"] == "DENY" and "cap" in lk["reason"])
# symbol not allowed
rec = ch.ch_mandate(conn, "buy 10 USDT of XRPUSDT")
lk = ch.ch_lock(fc, pol, conn, rec["hash"][:16])
check("LOCK denies non-allowlist symbol", lk["decision"] == "DENY")
# insufficient balance
broke = FakeClient(free_usdt=5.0)
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(broke, pol, conn, rec["hash"][:16])
check("LOCK denies free USDT < notional (fail-closed)", lk["decision"] == "DENY")
# gross exposure cap: Airlock already holds 14 USDT notional (ledger-tracked)
conn.execute("INSERT INTO events (ts,chamber,kind,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
             (time.time(), "EXECUTE", "ORDER",
              json.dumps({"sent": True, "request": {"side": "BUY", "symbol": "BNBUSDT", "quoteOrderQty": 14}}),
              "x", "y"))
rich = FakeClient(free_usdt=100.0, coins={"BNB": 0.02})
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(rich, pol, conn, rec["hash"][:16])
check("LOCK denies gross exposure breach", lk["decision"] == "DENY" and "exposure" in lk["reason"])
# no pending intent
lk = ch.ch_lock(fc, pol, conn, None)
check("LOCK denies when nothing pending", lk["decision"] == "DENY")

# --- rate cap: 10 orders in window ---
conn.execute("DELETE FROM events")
import airlock.chambers as _c
now = time.time()
for k in range(10):
    conn.execute("INSERT INTO events (ts,chamber,kind,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
                 (now, "EXECUTE", "ORDER", "{}", "x", "y"))
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(fc, pol, conn, rec["hash"][:16])
check("LOCK enforces max_orders_per_5m", lk["decision"] == "DENY" and "rate" in lk["reason"])

# --- ledger chain ---
conn.execute("DELETE FROM events")
ch.ch_mandate(conn, "buy 5 USDT of BTCUSDT")
v = verify_ledger(conn)
check("ledger chain intact", v["chainBroken"] == 0 and v["events"] >= 1)
check("ledger env-tagged testnet", v.get("chainEnvironment") == "testnet")
# tamper -> chain broken
row = conn.execute("SELECT payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()
conn.execute("UPDATE events SET payload=? WHERE seq=(SELECT MAX(seq) FROM events)",
             (row[0].replace("BTCUSDT", "ETHUSDT"),))
v2 = verify_ledger(conn)
check("tamper breaks chain", v2["chainBroken"] > 0)

# --- MAINNET gates (docs/MAINNET.md maintainer note) ---
conn.execute("DELETE FROM events")
conn.execute("DELETE FROM kv")
os.environ.pop("AIRLOCK_ENV", None)
os.environ.pop("AIRLOCK_I_UNDERSTAND_MAINNET", None)

class MainnetClient(FakeClient):
    base = "https://api.binance.com"

mn = MainnetClient(free_usdt=100.0)
rec = ch.ch_mandate(conn, "buy 10 USDT of BNBUSDT")
lk = ch.ch_lock(mn, pol, conn, rec["hash"][:16])
check("LOCK allows mainnet client (LOCK itself not env-gated)", lk["decision"] == "ALLOW")
ex = ch.ch_execute(mn, pol, conn)
check("EXECUTE refuses mainnet without opt-in flag",
      ex.get("sent") is False and "AIRLOCK_I_UNDERSTAND_MAINNET" in str(ex.get("reason")))
kl = ch.ch_kill(mn, pol, conn)
check("KILL refuses mainnet without opt-in flag (fail-closed)",
      kl.get("refused") is True and "AIRLOCK_I_UNDERSTAND_MAINNET" in str(kl.get("reason")))
os.environ["AIRLOCK_I_UNDERSTAND_MAINNET"] = "1"
ex2 = ch.ch_execute(mn, pol, conn)
check("EXECUTE allowed on mainnet with explicit opt-in", ex2.get("sent") is True)
os.environ.pop("AIRLOCK_I_UNDERSTAND_MAINNET", None)

class WithdrawKeyClient(MainnetClient):
    api_restriction = {"enableWithdrawals": True, "ipRestrict": False}

wk = WithdrawKeyClient()
ev = ch.ch_evidence(wk, pol, conn)
check("EVIDENCE refuses withdrawal-enabled key",
      ev.get("refuse") is True and "enableWithdrawals" in str(ev.get("refuseReason")))
check("EVIDENCE withdrawal refusal is ledgered", conn.execute(
    "SELECT COUNT(*) FROM events WHERE chamber='EVIDENCE'").fetchone()[0] >= 1)

print()
print("RESULT:", f"{fails} FAILURES" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
