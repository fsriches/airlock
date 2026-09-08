#!/usr/bin/env python3
"""AIRLOCK ROADTONE — 6-chamber deterministic engine on top of Binance MCP.

Chambers:
  1 IDENTITY   token check (sha256 prefix only), config hash, boot event
  2 DATA       market snapshots via MCP read tools -> ledger
  3 SIGNAL     deterministic rules on snapshots (no LLM)
  4 RISK       policy gate: every intent simulated vs policy.yaml (fail-closed)
  5 EXECUTION  write tools via MCP (observe/dry_run -> journal sim only)
  6 TELEMETRY  equity, drawdown vs persisted day-peak, action stats

Policy-as-code, fail-closed, reduce-only always allowed (Bouncer pattern).
Run:  .venv/bin/python -m airlock.roadtone --cycles 1
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import yaml

from .mcp_client import AirlockMcpClient, STATE_DIR, TOKEN_PATH

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "policy.yaml"
LEDGER = STATE_DIR / "roadtone_ledger.sqlite3"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]

# Tool-name resolution: hints resolved against discovered tools_list.json
TOOL_HINTS = {
    "ticker": ["ticker24hr", "ticker_24hr", "ticker24", "ticker"],
    "account": ["account", "get_account", "balances", "balance"],
    "order": ["new_order", "place_order", "create_order", "order_place"],
    "cancel": ["cancel_order", "cancel"],
}


def load_tools_list() -> list[dict]:
    p = STATE_DIR / "tools_list.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def resolve_tool(hint: str) -> str:
    """Map a chamber hint to the real discovered MCP tool name. Fail-closed:
    if tools list exists but nothing matches, return hint anyway and let the
    call fail loudly (result carries the error)."""
    names = [t.get("name", "") for t in load_tools_list()]
    for pat in TOOL_HINTS.get(hint, [hint]):
        for n in names:
            if pat.lower() in n.lower():
                return n
    return hint


# ---------------- ledger (WAL + hash chain) ----------------
def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, chamber TEXT, kind TEXT, payload TEXT, prev_hash TEXT, hash TEXT)"""
    )
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
        "INSERT INTO events (ts,chamber,kind,payload,prev_hash,hash) VALUES (?,?,?,?,?,?)",
        (time.time(), chamber, kind, blob, ph, h),
    )
    return h


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn: sqlite3.Connection, key: str, val: str) -> None:
    conn.execute("INSERT INTO kv (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, val))


# ---------------- Chamber 1: IDENTITY ----------------
def ch1_identity(conn: sqlite3.Connection) -> dict:
    out: dict = {"chamber": "1-IDENTITY"}
    if not TOKEN_PATH.exists():
        out["token"] = "ABSENT (run airlock.auth first)"
        return out
    try:
        tok = json.loads(TOKEN_PATH.read_text())
        at = tok.get("access_token") or ""
        out["token"] = "present" if at else "EMPTY"
        out["token_fp16"] = hashlib.sha256(at.encode()).hexdigest()[:16]
        out["refresh_present"] = bool(tok.get("refresh_token"))
    except Exception as e:  # noqa: BLE001
        out["token"] = f"unreadable: {e}"
    out["policy_fp16"] = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()[:16]
    out["ledger"] = emit(conn, "IDENTITY", "boot", out)
    return out


# ---------------- Chamber 2: DATA ----------------
def ch2_data(client: AirlockMcpClient | None, conn: sqlite3.Connection) -> dict:
    out: dict = {"chamber": "2-DATA", "snapshots": []}
    tool = resolve_tool("ticker")
    for sym in SYMBOLS:
        snap = {"symbol": sym}
        try:
            if client is None:
                raise RuntimeError("no MCP client (no token)")
            r = client.call(tool, {"symbol": sym})
            if not r.get("ok"):
                raise RuntimeError(str(r.get("error"))[:150])
            res = r.get("result") or {}
            data = res.get("content") if isinstance(res.get("content"), list) else res
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                for k in ("lastPrice", "priceChangePercent", "quoteVolume"):
                    v = data.get(k)
                    if isinstance(v, dict):
                        v = v.get("value")
                    snap[k] = v
            snap["ok"] = True
            snap["tool"] = tool
        except Exception as e:  # noqa: BLE001
            snap.update(ok=False, err=str(e)[:150])
        out["snapshots"].append(snap)
    emit(conn, "DATA", "snapshot", out)
    return out


def _f(x: dict, key: str, default: float = 0.0) -> float:
    v = x.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------- Chamber 3: SIGNAL (deterministic) ----------------
def ch3_signal(data_out: dict) -> list[dict]:
    """Fixed rule: fade extreme 24h moves on liquid pairs. No LLM, no ML.
    score = -priceChangePercent; |score|>1.0 AND quoteVolume>10M -> intent.
    Upside extremes are SKIP (no short facility on spot)."""
    intents = []
    for s in data_out.get("snapshots", []):
        if not s.get("ok"):
            continue
        score = -_f(s, "priceChangePercent")
        vol = _f(s, "quoteVolume")
        intent = {"symbol": s["symbol"], "score": round(score, 2), "quoteVolume": vol}
        if score > 1.0 and vol > 10_000_000:
            intent["side"] = "BUY"
            intents.append(intent)
        elif score < -1.0:
            intent["side"] = "SKIP"
            intents.append(intent)
    return intents


# ---------------- Chamber 4: RISK (policy gate, fail-closed) ----------------
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
        return self.d.get("mode", "observe")

    @property
    def dry_run(self) -> bool:
        return True if self.mode == "observe" else bool(self.d.get("dry_run", True))

    @property
    def limits(self) -> dict:
        return self.d.get("limits", {})


class RiskGate:
    """Every intent simulated against policy. Checks apply to the INTENT's own
    symbol (not just snapshot state). Reduce-only ALWAYS allowed. Unknown ->
    deny. Fail-closed on any exception inside the gate."""

    def __init__(self, policy: Policy, conn: sqlite3.Connection):
        self.p = policy
        self.conn = conn

    def _orders_in_window(self, minutes: int) -> int:
        cutoff = time.time() - minutes * 60
        row = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE chamber='EXEC' AND kind='ORDER' AND ts>?",
            (cutoff,),
        ).fetchone()
        return int(row[0])

    def simulate(self, intent: dict, account: dict | None) -> dict:
        try:
            return self._simulate(intent, account)
        except Exception as e:  # noqa: BLE001  — fail CLOSED
            return {"decision": "DENY", "reason": f"gate-error: {e}"}

    def _simulate(self, intent: dict, account: dict | None) -> dict:
        sym = intent.get("symbol", "")
        side = intent.get("side", "")
        if side == "SKIP":
            return {"decision": "SKIP", "reason": "signal-side skip"}
        # 1. intent-level symbol checks FIRST (new-intent bypass lesson)
        for pat in self.p.d["symbols"].get("block", []):
            if fnmatch.fnmatch(sym, pat):
                return {"decision": "DENY", "reason": f"symbol blocked: {pat}"}
        if sym not in self.p.d["symbols"].get("allow", []):
            return {"decision": "DENY", "reason": "symbol not in allowlist"}
        if side == "REDUCE":  # cancel/close/flatten
            return {"decision": "ALLOW", "reason": "reduce-only always allowed"}
        # 2. observe mode: nothing non-reduce goes upstream
        if self.p.mode == "observe":
            return {"decision": "DENY", "reason": "observe-mode (no writes)"}
        lim = self.p.limits
        notional = float(intent.get("notionalUsdt", lim.get("max_order_notional_usdt", 0)))
        if notional <= 0 or notional > float(lim.get("max_order_notional_usdt", 0)):
            return {"decision": "DENY", "reason": f"notional {notional} over cap {lim.get('max_order_notional_usdt')}"}
        if account is None:
            return {"decision": "DENY", "reason": "no account snapshot (fail-closed)"}
        free_usdt = _f(account, "freeUsdt")
        if free_usdt < float(lim.get("min_quote_balance_usdt", 0)):
            return {"decision": "DENY", "reason": f"free USDT {free_usdt} below min"}
        if notional > free_usdt:
            return {"decision": "DENY", "reason": f"notional {notional} > free {free_usdt}"}
        if notional + _f(account, "grossExposureUsdt") > float(lim.get("max_gross_exposure_usdt", 0)):
            return {"decision": "DENY", "reason": "gross exposure cap"}
        if self._orders_in_window(5) >= int(lim.get("max_orders_per_5m", 0)):
            return {"decision": "DENY", "reason": "rate cap: max_orders_per_5m"}
        return {"decision": "ALLOW", "reason": "sim-ok", "notionalUsdt": notional}


# ---------------- Chamber 5: EXECUTION ----------------
def ch5_execute(client: AirlockMcpClient | None, gate: RiskGate, conn: sqlite3.Connection,
                intents: list[dict], account: dict | None) -> list[dict]:
    actions = []
    for intent in intents:
        sim = gate.simulate(intent, account)
        entry = {"intent": intent, "sim": sim}
        if sim["decision"] == "ALLOW" and client is not None:
            if gate.p.dry_run:
                entry["exec"] = "SIM (dry_run) — not sent upstream"
            else:
                tool = resolve_tool("order")
                r = client.call(tool, {
                    "symbol": intent["symbol"],
                    "side": intent.get("side", "BUY"),
                    "type": "MARKET",
                    "quoteOrderQty": sim.get("notionalUsdt"),
                })
                entry["exec"] = r
                # fold upstream result; re-verify lesson: mark failure if !ok
                if not r.get("ok"):
                    entry["exec_result"] = "UPSTREAM-FAIL"
        emit(conn, "EXEC", "ORDER", entry)
        actions.append(entry)
    return actions


# ---------------- Chamber 6: TELEMETRY ----------------
def ch6_telemetry(client: AirlockMcpClient | None, conn: sqlite3.Connection,
                  data_out: dict) -> dict:
    """Equity = free USDT + coin positions valued at last snapshot price.
    uPnL ownership: single source (positions valued HERE, never double-counted).
    Drawdown uses persisted day-peak (UTC), never the current value as baseline."""
    out: dict = {"chamber": "6-TELEMETRY"}
    prices = {s["symbol"]: _f(s, "lastPrice") for s in data_out.get("snapshots", []) if s.get("ok")}
    balances: dict[str, float] = {}
    if client is not None:
        try:
            tool = resolve_tool("account")
            r = client.call(tool, {})
            res = r.get("result") or {}
            blob = json.dumps(res, default=str)
            balances = parse_balances(blob)
        except Exception as e:  # noqa: BLE001
            out["account_err"] = str(e)[:150]
    free_usdt = balances.get("USDT", 0.0)
    coin_val = 0.0
    for asset, qty in balances.items():
        if asset == "USDT" or qty <= 0:
            continue
        px = prices.get(f"{asset}USDT")
        if px:
            coin_val += qty * px
    equity = free_usdt + coin_val
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    peak_key = f"peak:{today}"
    stored = kv_get(conn, peak_key)
    if stored is None:
        # new UTC day: seed from yesterday's peak is WRONG baseline — seed at
        # current equity but only if we have a live snapshot; else defer
        peak = equity if prices else None
        if peak is not None:
            kv_set(conn, peak_key, repr(peak))
    else:
        peak = max(float(stored), equity)
        kv_set(conn, peak_key, repr(peak))
    dd = 0.0
    if peak:
        dd = (peak - equity) / peak * 100.0
    out.update(
        equityUsdt=round(equity, 2),
        freeUsdt=round(free_usdt, 2),
        coinValueUsdt=round(coin_val, 2),
        dayPeak=round(peak, 2) if peak else None,
        drawdownPct=round(dd, 2),
        dailyLossPctCap=float(Policy().limits.get("daily_loss_pct", 2.0)),
    )
    emit(conn, "TELEMETRY", "equity", out)
    return out


def parse_balances(blob: str) -> dict[str, float]:
    """Best-effort balance extraction from MCP account result JSON.
    Looks for {asset, free} pairs anywhere in the payload."""
    out: dict[str, float] = {}
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return out
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            asset = cur.get("asset")
            if isinstance(asset, str):
                free = cur.get("free", cur.get("available", cur.get("balance")))
                try:
                    out[asset] = out.get(asset, 0.0) + float(free or 0)
                except (TypeError, ValueError):
                    pass
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


# ---------------- main loop ----------------
def run_cycle(n: int, policy: Policy, client: AirlockMcpClient | None, conn: sqlite3.Connection) -> dict:
    print(f"\n--- CYCLE {n} (mode={policy.mode}, dry_run={policy.dry_run}) ---", flush=True)
    c1 = ch1_identity(conn)
    print(f"[1 IDENTITY ] {c1}")
    c2 = ch2_data(client, conn)
    ok_n = sum(1 for s in c2["snapshots"] if s.get("ok"))
    print(f"[2 DATA     ] {ok_n}/{len(SYMBOLS)} snapshots ok")
    for s in c2["snapshots"]:
        if s.get("ok"):
            print(f"    {s['symbol']:9} last={s.get('lastPrice')} 24h%={s.get('priceChangePercent')} vol={s.get('quoteVolume')}")
    c3 = ch3_signal(c2)
    print(f"[3 SIGNAL   ] {len(c3)} intent(s): {c3 if c3 else 'none'}")
    gate = RiskGate(policy, conn)
    c5 = ch5_execute(client, gate, conn, c3, None)  # account snapshot wired post-discovery
    for a in c5:
        print(f"[5 EXEC     ] {a['intent'].get('symbol')} {a['intent'].get('side')} -> {a['sim']['decision']} ({a['sim']['reason']})")
    c6 = ch6_telemetry(client, conn, c2)
    print(f"[6 TELEMETRY] equity={c6.get('equityUsdt')} dd={c6.get('drawdownPct')}% peak={c6.get('dayPeak')}")
    return {"c1": c1, "c2": c2, "c3": c3, "c5": c5, "c6": c6}


def main() -> int:
    ap = argparse.ArgumentParser(description="AIRLOCK ROADTONE 6-chamber engine")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--interval", type=int, default=30, help="seconds between cycles")
    ap.add_argument("--autopilot", action="store_true", help="required to let mode=autopilot actually write")
    args = ap.parse_args()

    policy = Policy()
    if policy.mode == "autopilot" and not args.autopilot:
        print("mode=autopilot in policy but --autopilot flag not given -> running OBSERVE instead (fail-safe)")
        policy.d["mode"] = "observe"

    print("=" * 46)
    print(" AIRLOCK ROADTONE — 6 CHAMBERS")
    print("=" * 46)

    conn = db()
    client: AirlockMcpClient | None = None
    if TOKEN_PATH.exists():
        try:
            import asyncio

            client = AirlockMcpClient()
            asyncio.run(client.connect())
            print("MCP client connected")
        except Exception as e:  # noqa: BLE001
            print(f"MCP connect failed ({e}) — chambers run in degraded read-only mode")
            client = None

    try:
        for i in range(1, args.cycles + 1):
            run_cycle(i, policy, client, conn)
            if i < args.cycles:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        if client is not None:
            import asyncio
            try:
                asyncio.get_event_loop().run_until_complete(client.close())
            except Exception:
                pass
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
