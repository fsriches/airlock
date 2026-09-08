"""AIRLOCK six-chamber engine — EVIDENCE MANDATE LOCK EXECUTE RECONCILE KILL.

No strategy. No signal. Every write requires:
  1. an operator-typed intent  (`airlock intent "<text>"`)   -> hash
  2. an operator-typed yes     (`airlock yes [hash]`)       -> approval bound to hash
  3. deterministic policy checks (hard limits, spot only, testnet only)

Ledger is an append-only SQLite hash chain. `airlock verify` recomputes it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import yaml

from .binance_rest import BinanceRestClient, TESTNET_BASE, resolve_env

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = Path(os.environ.get("AIRLOCK_POLICY") or (ROOT / "policy.yaml"))
LEDGER = Path(os.environ.get("AIRLOCK_HOME", str(Path.home() / ".airlock"))) / "roadtone_ledger.sqlite3"
KILL_PREFIX = "AIR-KILL-"


def _current_env() -> str:
    e = os.environ.get("AIRLOCK_ENV", "testnet").strip().lower()
    return e if e in ("testnet", "mainnet") else "testnet"


# ---------------- ledger (WAL + hash chain) ----------------
def db() -> sqlite3.Connection:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, chamber TEXT, kind TEXT, payload TEXT, prev_hash TEXT, hash TEXT,
        env TEXT NOT NULL DEFAULT 'testnet')"""
    )
    # migration for ledgers created before env tagging (docs/MAINNET.md)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    if "env" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN env TEXT NOT NULL DEFAULT 'testnet'")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def _prev_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row else "GENESIS"


def emit(conn: sqlite3.Connection, chamber: str, kind: str, payload: dict) -> str:
    ph = _prev_hash(conn)
    blob = json.dumps(payload, sort_keys=True, default=str)
    h = hashlib.sha256((ph + blob).encode()).hexdigest()
    conn.execute(
        "INSERT INTO events (ts,chamber,kind,payload,prev_hash,hash,env) VALUES (?,?,?,?,?,?,?)",
        (time.time(), chamber, kind, blob, ph, h, _current_env()),
    )
    return h


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn: sqlite3.Connection, key: str, val: str) -> None:
    conn.execute("INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, val))


# ---------------- policy (fail-loud loader) ----------------
class Policy:
    def __init__(self, path: Path = POLICY_PATH):
        if not path.exists():
            raise SystemExit(f"POLICY MISSING: {path} — refusing to boot (fail-loud)")
        self.d = yaml.safe_load(path.read_text()) or {}
        for req in ("mode", "symbols", "limits"):
            if req not in self.d:
                raise SystemExit(f"POLICY INVALID: missing '{req}' in {path}")

    @property
    def mode(self) -> str:
        return self.d.get("mode", "gated")

    @property
    def environment(self) -> str:
        return self.d.get("environment", "testnet")

    @property
    def symbols(self) -> list[str]:
        return list(self.d["symbols"].get("allow", []))

    @property
    def limits(self) -> dict:
        return self.d.get("limits", {})

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()[:16]


# ---------------- intent parsing (deterministic, no LLM) ----------------
_NUM = r"(\d+(?:\.\d+)?)"


def parse_intent(text: str) -> dict:
    """Parse operator text into an intent. Raises ValueError on garbage.
    Examples: 'buy 10 USDT of BNBUSDT' | 'sell 10 USDT of BTCUSDT limit -20%'."""
    t = " ".join(text.split())
    m = re.match(rf"(?i)^(buy|sell)\s+{_NUM}\s*usdt\b.*$", t)
    if not m:
        raise ValueError(f"cannot parse intent (need: '<buy|sell> <N> USDT of <SYMBOL>'): {text!r}")
    side = m.group(1).upper()
    notional = float(m.group(2))
    rest = t[m.end(1):].lower()
    kind = "LIMIT" if "limit" in rest else "MARKET"
    lim_pct = None
    lm = re.search(r"limit\s*(-?)" + _NUM + r"\s*%|limit.*?(\d+(?:\.\d+)?)\s*%?\s*(below|under|bawah)", t, re.I)
    if kind == "LIMIT":
        pm = re.search(r"(-?" + _NUM + r")\s*%", t)
        if not pm:
            raise ValueError("LIMIT intent needs a deviation like 'limit -20%'")
        lim_pct = float(pm.group(1))
    sym = None
    for tok in t.replace(",", " ").split():
        cand = tok.strip(".,;:").upper()
        if cand.endswith("USDT") and cand != "USDT":
            sym = cand
            break
    if not sym:
        raise ValueError(f"no <SYMBOL>USDT token in intent: {text!r}")
    intent = {
        "text": t,
        "side": side,
        "symbol": sym,
        "kind": kind,
        "notionalUsdt": notional,
    }
    if lim_pct is not None:
        intent["limitDeviationPct"] = lim_pct
    return intent


def intent_hash(intent: dict) -> str:
    canon = json.dumps(intent, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def client_order_id(intent_hash_hex: str, kind: str = "ORDER") -> str:
    return f"AIR-{kind[:5]}-{intent_hash_hex[:20]}"


# ---------------- Chamber 1: EVIDENCE ----------------
def ch_evidence(client: BinanceRestClient, policy: Policy, conn: sqlite3.Connection) -> dict:
    env = _current_env()
    out: dict = {"chamber": "1-EVIDENCE", "backend": client.base,
                 "keys": "PRESENT" if client.has_keys else "ABSENT",
                 "policyFp": policy.fingerprint, "mode": policy.mode,
                 "env": env}
    # key permission check (docs/MAINNET.md): refuse a withdrawal-enabled key
    if client.has_keys:
        ar = client.call("apiRestrictions")
        if ar.get("ok"):
            a = ar["result"]["content"][0]
            perms = {"enableWithdrawals": a.get("enableWithdrawals"),
                     "enableInternalTransfer": a.get("enableInternalTransfer"),
                     "enableFutures": a.get("enableFutures"),
                     "enableMargin": a.get("enableMargin"),
                     "ipRestricted": a.get("ipRestrict")}
            out["keyPermissions"] = perms
            if a.get("enableWithdrawals"):
                out["refuse"] = True
                out["refuseReason"] = ("key has enableWithdrawals=true — delete it and create a "
                                       "trading-only key (docs/MAINNET.md step 2)")
                emit(conn, "EVIDENCE", "check", out)
                return out
        else:
            out["keyPermissionsErr"] = str(ar.get("error"))[:120]
    r = client.call("time")
    out["serverTimeOk"] = bool(r.get("ok"))
    if out["keys"]:
        client.sync_time()
        acc = client.call("account")
        if acc.get("ok"):
            bals = {b["asset"]: float(b["free"]) for b in acc["result"]["content"][0]["balances"]
                    if float(b["free"]) > 0}
            out["usdtFree"] = round(bals.get("USDT", 0.0), 2)
            out["trackedBalances"] = {a: bals.get(a) for a in ("BNB", "BTC", "ETH") if bals.get(a)}
        else:
            out["accountErr"] = str(acc.get("error"))[:160]
    snaps = {}
    for sym in policy.symbols:
        t = client.call("ticker24hr", {"symbol": sym})
        if t.get("ok"):
            d = t["result"]["content"][0]
            snaps[sym] = {"last": d["lastPrice"], "pct24h": d["priceChangePercent"]}
    out["tickers"] = snaps
    pend = kv_get(conn, "pending_intent")
    out["pendingIntent"] = json.loads(pend)["hash"] if pend else None
    emit(conn, "EVIDENCE", "check", out)
    return out


# ---------------- Chamber 2: MANDATE ----------------
def ch_mandate(conn: sqlite3.Connection, text: str) -> dict:
    intent = parse_intent(text)
    ih = intent_hash(intent)
    record = {"intent": intent, "hash": ih, "ts": time.time(), "status": "PENDING"}
    kv_set(conn, "pending_intent", json.dumps(record))
    emit(conn, "MANDATE", "INTENT", record)
    return record


# ---------------- Chamber 3: LOCK ----------------
def _orders_in_window(conn: sqlite3.Connection, minutes: int) -> int:
    cut = time.time() - minutes * 60
    row = conn.execute("SELECT COUNT(*) FROM events WHERE chamber='EXECUTE' AND kind='ORDER' AND ts>?", (cut,)).fetchone()
    return int(row[0])


def _airlock_exposure_usdt(conn: sqlite3.Connection) -> float:
    """Exposure OWNED BY Airlock (entry notional, USDT): buys − sells,
    reconstructed from the ledger. The sub-account may hold faucet/pre-existing
    funds — those are the operator's, never counted (deterministic, no live price)."""
    rows = conn.execute(
        "SELECT chamber,kind,payload FROM events WHERE (chamber='EXECUTE' AND kind='ORDER')"
        " OR (chamber='KILL' AND kind='FLATTEN')").fetchall()
    gross = 0.0
    for chamber, kind, payload in rows:
        try:
            d = json.loads(payload)
        except Exception:  # noqa: BLE001
            continue
        if chamber == "EXECUTE":
            if not d.get("sent"):
                continue
            req = d.get("request") or {}
            notional = float(req.get("quoteOrderQty") or 0)
            if notional <= 0:
                notional = float(req.get("quantity") or 0) * float(req.get("price") or 0)
            if notional <= 0:
                notional = float(d.get("executedQuote") or 0)
            side = req.get("side")
            gross += notional if side == "BUY" else -notional
        else:  # KILL flatten
            for s in d.get("sold", []):
                if s.get("ok") and s.get("notionalUsdt"):
                    gross -= float(s["notionalUsdt"])
    return max(gross, 0.0)


def ch_lock(client: BinanceRestClient, policy: Policy, conn: sqlite3.Connection,
            supplied: str | None) -> dict:
    """Bind operator 'yes' to the exact intent hash, then run hard policy checks.
    Fail-closed: ANY problem -> DENY and the pending intent dies."""
    out: dict = {"chamber": "3-LOCK"}
    raw = kv_get(conn, "pending_intent")
    if not raw:
        out.update(decision="DENY", reason="no pending intent")
        emit(conn, "LOCK", "DECISION", out)
        return out
    rec = json.loads(raw)
    ih = rec["hash"]
    out["intentHash"] = ih
    if rec.get("status") != "PENDING":
        out.update(decision="DENY", reason=f"intent status {rec.get('status')} (not pending)")
        emit(conn, "LOCK", "DECISION", out)
        return out
    if supplied is not None and not supplied.startswith(ih[:16]):
        out.update(decision="DENY", reason=f"approval hash mismatch: typed {supplied!r} != bound {ih[:16]}…")
        emit(conn, "LOCK", "DECISION", out)
        return out
    intent = rec["intent"]
    lim = policy.limits
    if intent["symbol"] not in policy.symbols:
        out.update(decision="DENY", reason=f"symbol {intent['symbol']} not in allowlist")
    elif intent["kind"] not in ("MARKET", "LIMIT"):
        out.update(decision="DENY", reason="spot only")
    elif not 0 < intent["notionalUsdt"] <= float(lim.get("max_order_notional_usdt", 0)):
        out.update(decision="DENY", reason=f"notional {intent['notionalUsdt']} over cap {lim.get('max_order_notional_usdt')}")
    elif _orders_in_window(conn, 5) >= int(lim.get("max_orders_per_5m", 0)):
        out.update(decision="DENY", reason="rate cap: max_orders_per_5m")
    else:
        try:
            acc = client.call("account")
            if not acc.get("ok"):
                raise RuntimeError(f"no account (fail-closed): {acc.get('error')}")
            bals = {b["asset"]: float(b["free"]) for b in acc["result"]["content"][0]["balances"]}
            free_usdt = bals.get("USDT", 0.0)
            if intent["side"] == "BUY" and free_usdt < intent["notionalUsdt"]:
                out.update(decision="DENY", reason=f"free USDT {free_usdt} < notional {intent['notionalUsdt']}")
            elif intent["side"] == "BUY" and free_usdt < float(lim.get("min_quote_balance_usdt", 0)) - 0:
                out.update(decision="DENY", reason="free USDT below min_quote_balance_usdt")
            else:
                gross = _airlock_exposure_usdt(conn)
                if intent["side"] == "BUY" and gross + intent["notionalUsdt"] > float(lim.get("max_gross_exposure_usdt", 0)):
                    out.update(decision="DENY", reason=f"gross exposure {gross:.2f}+{intent['notionalUsdt']} over cap {lim.get('max_gross_exposure_usdt')}")
                else:
                    out.update(decision="ALLOW", reason="policy-ok", boundHash=ih,
                               freeUsdt=round(free_usdt, 2), grossExposureUsdt=round(gross, 2))
        except Exception as e:  # noqa: BLE001 — fail CLOSED
            out.update(decision="DENY", reason=f"gate-error: {e}")
    if out["decision"] == "DENY":
        rec["status"] = "NO_TRADE"
        kv_set(conn, "pending_intent", json.dumps(rec))
    emit(conn, "LOCK", "DECISION", out)
    return out


# ---------------- exchange filters (cached) ----------------
def get_filters(client: BinanceRestClient, symbol: str) -> dict:
    """LOT_SIZE/PRICE_FILTER/NOTIONAL from exchangeInfo (cached in-process)."""
    global _FILTER_CACHE
    try:
        if symbol in _FILTER_CACHE:
            return _FILTER_CACHE[symbol]
        r = client._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, False)
        f = {}
        if r.get("ok"):
            for x in r["result"]["content"][0]["symbols"][0]["filters"]:
                if x["filterType"] == "LOT_SIZE":
                    f["stepSize"] = float(x["stepSize"])
                elif x["filterType"] == "PRICE_FILTER":
                    f["tickSize"] = float(x["tickSize"])
                elif x["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                    f["minNotional"] = float(x.get("minNotional", x.get("notional", 5.0)))
        _FILTER_CACHE[symbol] = f
        return f
    except Exception:  # noqa: BLE001 — fail closed to conservative defaults
        return {"stepSize": 1e-5, "tickSize": 0.01, "minNotional": 5.0}


_FILTER_CACHE: dict[str, dict] = {}


def _floor_step(value: float, step: float) -> str:
    if step <= 0:
        step = 1e-8
    import math
    floored = math.floor(value / step) * step
    return f"{floored:.8f}".rstrip("0")


def _env_gate(client: BinanceRestClient, policy: Policy) -> tuple[bool, str]:
    """docs/MAINNET.md: allow when AIRLOCK_ENV==testnet (testnet backend), or when
    on mainnet (AIRLOCK_ENV==mainnet OR mainnet backend) AND
    AIRLOCK_I_UNDERSTAND_MAINNET==1; otherwise refuse (fail-closed)."""
    env = _current_env()
    on_mainnet = env == "mainnet" or client.base != TESTNET_BASE
    if on_mainnet:
        if os.environ.get("AIRLOCK_I_UNDERSTAND_MAINNET", "") == "1":
            return True, ""
        return False, ("refused: mainnet requires AIRLOCK_I_UNDERSTAND_MAINNET=1 "
                       "(docs/MAINNET.md step 5)")
    if client.base == TESTNET_BASE and policy.environment == "testnet":
        return True, ""
    return False, (f"refused: AIRLOCK_ENV={env} with backend={client.base} "
                   f"policy.environment={policy.environment} — testnet only")


# ---------------- Chamber 4: EXECUTE ----------------
def ch_execute(client: BinanceRestClient, policy: Policy, conn: sqlite3.Connection) -> dict:
    out: dict = {"chamber": "4-EXECUTE"}
    raw = kv_get(conn, "pending_intent")
    if not raw:
        out.update(sent=False, reason="no pending intent")
        return out
    rec = json.loads(raw)
    if rec["status"] != "PENDING":
        out.update(sent=False, reason=f"intent status {rec['status']} (not PENDING)")
        return out
    ih = rec["hash"]
    intent = rec["intent"]
    # idempotency: query by clientOrderId BEFORE any (re)send
    cid = client_order_id(ih)
    prior = client._request("GET", "/api/v3/openOrders", {"symbol": intent["symbol"]}, True)
    if prior.get("ok"):
        for o in prior["result"]["content"][0] if isinstance(prior["result"]["content"][0], list) else []:
            if o.get("clientOrderId") == cid:
                out.update(sent=False, reason="already open (idempotent skip)", clientOrderId=cid, orderId=o["orderId"])
                emit(conn, "EXECUTE", "ORDER", out)
                return out
    allowed, gate_reason = _env_gate(client, policy)
    if not allowed:
        out.update(sent=False, reason=gate_reason)
        emit(conn, "EXECUTE", "ORDER", out)
        return out
    params: dict[str, Any] = {"symbol": intent["symbol"], "side": intent["side"],
                              "newClientOrderId": cid}
    if intent["kind"] == "MARKET":
        params["type"] = "MARKET"
        params["quoteOrderQty"] = intent["notionalUsdt"]
    else:
        t = client.call("ticker24hr", {"symbol": intent["symbol"]})
        if not t.get("ok"):
            out.update(sent=False, reason=f"limit needs ticker: {t.get('error')}")
            emit(conn, "EXECUTE", "ORDER", out)
            return out
        last = float(t["result"]["content"][0]["lastPrice"])
        dev = float(intent.get("limitDeviationPct", 0)) / 100.0
        fl = get_filters(client, intent["symbol"])
        px = _floor_step(last * (1 + dev), fl.get("tickSize", 0.01))
        qty = _floor_step(intent["notionalUsdt"] / float(px), fl.get("stepSize", 1e-5))
        params.update(type="LIMIT", timeInForce="GTC", quantity=qty, price=px)
    r = client.call("order", params)
    out["request"] = params
    out["clientOrderId"] = cid
    if r.get("ok"):
        d = r["result"]["content"][0]
        out.update(sent=True, orderId=d.get("orderId"), status=d.get("status"))
    else:
        out.update(sent=False, error=str(r.get("error"))[:200])
    rec["status"] = "SENT" if out.get("sent") else "FAILED"
    rec["orderId"] = out.get("orderId")
    rec["clientOrderId"] = cid
    kv_set(conn, "pending_intent", json.dumps(rec))
    emit(conn, "EXECUTE", "ORDER", out)
    return out


# ---------------- Chamber 5: RECONCILE ----------------
def ch_reconcile(client: BinanceRestClient, conn: sqlite3.Connection) -> dict:
    out: dict = {"chamber": "5-RECONCILE"}
    raw = kv_get(conn, "pending_intent")
    if not raw:
        out.update(result="NO_PENDING")
        return out
    rec = json.loads(raw)
    intent = rec["intent"]
    cid = rec.get("clientOrderId") or client_order_id(rec["hash"])
    sym = intent["symbol"]
    oid = rec.get("orderId")
    if oid is not None:
        q = client._request("GET", "/api/v3/order", {"symbol": sym, "orderId": oid}, True)
    else:
        q = client._request("GET", "/api/v3/order", {"symbol": sym, "origClientOrderId": cid}, True)
    if not q.get("ok"):
        out.update(result="ERROR", error=str(q.get("error"))[:160])
        emit(conn, "RECONCILE", "REPORT", out)
        return out
    d = q["result"]["content"][0]
    status = d.get("status")
    executed_quote = float(d.get("cummulativeQuoteQty", 0) or 0)
    tol = 2.0  # default fill tolerance pct; policy may override below
    try:
        tol = float(__import__("yaml").safe_load(POLICY_PATH.read_text())["limits"].get("fill_tolerance_pct", 2.0))
    except Exception:  # noqa: BLE001
        pass
    side_ok = d.get("side") == intent["side"]
    sym_ok = d.get("symbol") == sym
    drift = None
    if executed_quote > 0:
        drift = abs(executed_quote - intent["notionalUsdt"]) / intent["notionalUsdt"] * 100.0
    if status == "FILLED" and side_ok and sym_ok and (drift is None or drift <= tol):
        out.update(result="MATCH", symbol=sym, side=intent["side"], orderId=d.get("orderId"), status=status,
                   executedQuote=executed_quote, executedQtyBase=float(d.get("executedQty", 0) or 0),
                   driftPct=round(drift, 3) if drift is not None else 0.0)
    elif status in ("NEW", "PARTIALLY_FILLED") and float(d.get("executedQty", 0) or 0) == 0:
        out.update(result="NO_TRADE", symbol=sym, side=intent["side"], orderId=d.get("orderId"), status=status,
                   executedQtyBase=0.0,
                   reason="resting unfilled (limit below market)")
    elif status == "CANCELED":
        out.update(result="CANCELED", symbol=sym, side=intent["side"], orderId=d.get("orderId"), status=status,
                   executedQuote=executed_quote, executedQtyBase=float(d.get("executedQty", 0) or 0))
    else:
        out.update(result="DEVIATION", symbol=sym, side=intent["side"], orderId=d.get("orderId"), status=status,
                   sideOk=side_ok, symbolOk=sym_ok, driftPct=round(drift, 3) if drift is not None else None,
                   executedQuote=executed_quote, executedQtyBase=float(d.get("executedQty", 0) or 0))
    rec["reconcile"] = out["result"]
    kv_set(conn, "pending_intent", json.dumps(rec))
    emit(conn, "RECONCILE", "REPORT", out)
    return out


# ---------------- Chamber 6: KILL ----------------
def _airlock_positions(conn: sqlite3.Connection) -> dict[str, float]:
    """Net base-asset position OWNED BY Airlock, reconstructed from RECONCILE
    fills (qty in, qty out per symbol). Faucet/pre-existing operator funds are
    invisible to KILL by design — KILL only unwinds what Airlock opened."""
    pos: dict[str, float] = {}
    for (payload,) in conn.execute(
            "SELECT payload FROM events WHERE chamber='RECONCILE' AND kind='REPORT'"):
        try:
            d = json.loads(payload)
        except Exception:  # noqa: BLE001
            continue
        sym = d.get("symbol")
        base = sym[:-4] if sym and sym.endswith("USDT") else None
        if not base or d.get("side") not in ("BUY", "SELL"):
            continue
        q = float(d.get("executedQtyBase") or 0)
        if q <= 0:
            continue
        pos[base] = pos.get(base, 0.0) + (q if d["side"] == "BUY" else -q)
    return {k: round(v, 8) for k, v in pos.items() if round(v, 8) > 0}


def ch_kill(client: BinanceRestClient, policy: Policy, conn: sqlite3.Connection) -> dict:
    out: dict = {"chamber": "6-KILL", "scope": "ledger-owned positions only (operator faucet funds untouched)",
                 "before": {}, "after": {}}
    allowed, gate_reason = _env_gate(client, policy)
    if not allowed:
        out.update(refused=True, reason=gate_reason)
        emit(conn, "KILL", "FLATTEN", out)
        return out
    acc = client.call("account")
    if not acc.get("ok"):
        out["error"] = f"account: {acc.get('error')}"
        emit(conn, "KILL", "FLATTEN", out)
        return out
    bals = {b["asset"]: float(b["free"]) for b in acc["result"]["content"][0]["balances"]}
    out["before"] = {"usdtFree": round(bals.get("USDT", 0.0), 2),
                     "airlockPositions": _airlock_positions(conn),
                     "note": "account balances shown for reference; KILL unwinds ONLY airlockPositions"}
    # 1. cancel ALL open orders on allowlist symbols (any origin — kill is safety)
    canceled = []
    for sym in policy.symbols:
        oo = client._request("GET", "/api/v3/openOrders", {"symbol": sym}, True)
        if not oo.get("ok"):
            out.setdefault("openOrdersErr", []).append(str(oo.get("error"))[:80])
            continue
        for o in oo["result"]["content"][0] if isinstance(oo["result"]["content"][0], list) else []:
            x = client.call("cancel", {"symbol": sym, "orderId": o["orderId"]})
            canceled.append({"symbol": sym, "orderId": o["orderId"],
                             "status": x["result"]["content"][0].get("status") if x.get("ok") else str(x.get("error"))[:80]})
    out["canceledOrders"] = canceled
    # 2. flatten ONLY ledger-owned positions to USDT (market sell full owned qty)
    pos = _airlock_positions(conn)
    sold = []
    for sym in policy.symbols:
        base = sym[:-4]
        qty = pos.get(base, 0.0)
        if qty <= 0:
            sold.append({"symbol": sym, "skipped": "no Airlock-owned position", "ledgerQty": 0.0})
            continue
        t = client.call("ticker24hr", {"symbol": sym})
        px = float(t["result"]["content"][0]["lastPrice"]) if t.get("ok") else 0.0
        if px * qty < 5.0:  # below min notional — cannot sell, report only
            sold.append({"symbol": sym, "skipped": "value below 5 USDT dust floor", "qty": qty})
            continue
        fl = get_filters(client, sym)
        sqty = _floor_step(qty, fl.get("stepSize", 1e-5))
        r = client.call("order", {"symbol": sym, "side": "SELL", "type": "MARKET",
                                  "quantity": sqty,
                                  "newClientOrderId": KILL_PREFIX + hashlib.sha256(f"{sym}{time.time()}".encode()).hexdigest()[:12]})
        sold.append({"symbol": sym, "qty": sqty, "ok": r.get("ok"),
                     "orderId": r["result"]["content"][0].get("orderId") if r.get("ok") else None,
                     "notionalUsdt": round(px * float(sqty), 2) if r.get("ok") else None,
                     "error": None if r.get("ok") else str(r.get("error"))[:120]})
    out["sold"] = sold
    acc2 = client.call("account")
    if acc2.get("ok"):
        bals2 = {b["asset"]: float(b["free"]) for b in acc2["result"]["content"][0]["balances"]}
        out["after"] = {"usdtFree": round(bals2.get("USDT", 0.0), 2),
                        "airlockPositions": "flat (sold above)" if all(s.get("ok") or s.get("skipped") for s in sold) else pos}
    emit(conn, "KILL", "FLATTEN", out)
    return out


# ---------------- verify ----------------
def verify_ledger(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT seq,ts,chamber,kind,payload,prev_hash,hash,env FROM events ORDER BY seq").fetchall()
    prev = "GENESIS"
    bad = 0
    counts: dict[str, int] = {}
    envs: dict[str, int] = {}
    for seq, ts, chamber, kind, payload, prev_h, h, env in rows:
        if prev_h != prev or hashlib.sha256((prev_h + payload).encode()).hexdigest() != h:
            bad += 1
        prev = h
        counts[chamber] = counts.get(chamber, 0) + 1
        envs[env or "testnet"] = envs.get(env or "testnet", 0) + 1
    return {"events": len(rows), "chainBroken": bad, "chamberCounts": counts,
            "environments": envs,
            "chainEnvironment": (next(iter(envs)) if len(envs) == 1 and envs else "mixed")}


def ch_verify(conn: sqlite3.Connection) -> dict:
    out = verify_ledger(conn)
    out["ok"] = out["chainBroken"] == 0
    out["verdict"] = "OK" if out["ok"] else "FAIL"
    emit(conn, "VERIFY", "REPORT", {"events": out["events"], "chainBroken": out["chainBroken"], "verdict": out["verdict"]})
    return out
