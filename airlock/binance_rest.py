"""AIRLOCK Binance Spot gateway (testnet.binance.vision default, mainnet opt-in).

Drop-in backend for the 6-chamber roadtone engine. Exposes the same
`call(tool_name, params) -> {"ok": bool, "result": ...}` surface as
AirlockMcpClient, so chambers run unchanged:

    ticker24hr -> GET  /api/v3/ticker/24hr            (public)
    account    -> GET  /api/v3/account                (signed)
    order      -> POST /api/v3/order                   (signed, REAL testnet order)
    order_test -> POST /api/v3/order/test              (signed, no order created)
    cancel     -> DELETE /api/v3/order                 (signed)
    openOrders -> GET  /api/v3/openOrders              (signed)
    myTrades   -> GET  /api/v3/myTrades                (signed)
    ping/time  -> public

Credentials (never hardcode):
    env  AIRLOCK_BINANCE_TESTNET_KEY / AIRLOCK_BINANCE_TESTNET_SECRET
    or   ~/.airlock/binance_testnet.json  {"key": "...", "secret": "..."} (chmod 600)

Fail-loud: any signed endpoint without credentials raises a descriptive error
in the result payload (`ok: false`), never a silent empty success.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TESTNET_BASE = os.environ.get(
    "AIRLOCK_TESTNET_BASE", "https://testnet.binance.vision"
)
MAINNET_BASE = os.environ.get("AIRLOCK_MAINNET_BASE", "https://api.binance.com")
STATE_DIR = Path(os.environ.get("AIRLOCK_HOME", str(Path.home() / ".airlock")))
KEYS_PATH = STATE_DIR / "binance_testnet.json"
RECV_WINDOW_MS = 5000
REQUEST_TIMEOUT_S = 15


def resolve_env() -> dict:
    """Resolve AIRLOCK_ENV -> backend spec (base URL, key file, env-var names).

    testnet (default): testnet.binance.vision, binance_testnet.json
    mainnet:           api.binance.com,          binance_mainnet.json
    EXECUTE/KILL on mainnet additionally require AIRLOCK_I_UNDERSTAND_MAINNET=1
    (enforced in chambers.py — see docs/MAINNET.md).
    """
    env = os.environ.get("AIRLOCK_ENV", "testnet").strip().lower()
    if env == "mainnet":
        return {"env": "mainnet", "base": MAINNET_BASE,
                "key_file": STATE_DIR / "binance_mainnet.json",
                "key_env": "AIRLOCK_BINANCE_MAINNET_KEY",
                "secret_env": "AIRLOCK_BINANCE_MAINNET_SECRET"}
    if env == "testnet":
        return {"env": "testnet", "base": TESTNET_BASE,
                "key_file": KEYS_PATH,
                "key_env": "AIRLOCK_BINANCE_TESTNET_KEY",
                "secret_env": "AIRLOCK_BINANCE_TESTNET_SECRET"}
    raise RuntimeError(f"AIRLOCK_ENV must be 'testnet' or 'mainnet', got {env!r}")

# tool hint -> (http method, path, signed?)
TOOL_MAP: dict[str, tuple[str, str, bool]] = {
    "ticker24hr": ("GET", "/api/v3/ticker/24hr", False),
    "ticker": ("GET", "/api/v3/ticker/24hr", False),
    "price": ("GET", "/api/v3/ticker/price", False),
    "account": ("GET", "/api/v3/account", True),
    "apirestrictions": ("GET", "/sapi/v1/account/apiRestrictions", True),
    "order": ("POST", "/api/v3/order", True),
    "order_test": ("POST", "/api/v3/order/test", True),
    "cancel": ("DELETE", "/api/v3/order", True),
    "cancel_order": ("DELETE", "/api/v3/order", True),
    "openorders": ("GET", "/api/v3/openOrders", True),
    "mytrades": ("GET", "/api/v3/myTrades", True),
    "ping": ("GET", "/api/v3/ping", False),
    "time": ("GET", "/api/v3/time", False),
}


def resolve_tool(hint: str) -> tuple[str, str, bool]:
    """Resolve a chamber hint to (method, path, signed)."""
    h = hint.strip().lower()
    if h in TOOL_MAP:
        return TOOL_MAP[h]
    for known, spec in TOOL_MAP.items():
        if known in h:
            return spec
    raise KeyError(f"unknown tool hint: {hint}")


class BinanceRestClient:
    """HMAC-SHA256 signed REST client with MCP-shaped responses."""

    def __init__(self, base: str | None = None, key: str | None = None,
                 secret: str | None = None) -> None:
        if base is None:
            spec = resolve_env()
            self.env = spec["env"]
            self.key_file = spec["key_file"]
            self._key_env, self._secret_env = spec["key_env"], spec["secret_env"]
            base = spec["base"]
        else:
            self.env = "mainnet" if "binance.com" in base and "testnet" not in base else "testnet"
            self.key_file = KEYS_PATH
            self._key_env = "AIRLOCK_BINANCE_TESTNET_KEY"
            self._secret_env = "AIRLOCK_BINANCE_TESTNET_SECRET"
        self.base = base.rstrip("/")
        self._time_offset_ms = 0
        self.key, self.secret = key, secret
        if self.key is None or self.secret is None:
            self.key, self.secret = self._load_creds()

    # ---- credentials ----
    def _load_creds(self) -> tuple[str | None, str | None]:
        k = os.environ.get(self._key_env)
        s = os.environ.get(self._secret_env)
        if k and s:
            return k, s
        if self.key_file.exists():
            try:
                d = json.loads(self.key_file.read_text())
                return d.get("key"), d.get("secret")
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"{self.env} keys file unreadable: {e}") from e
        return None, None

    @property
    def has_keys(self) -> bool:
        return bool(self.key and self.secret)

    # ---- time sync (Binance rejects |local - server| > recvWindow) ----
    def sync_time(self) -> dict:
        srv = self.call("time")
        if srv.get("ok"):
            server_ms = srv["result"]["content"][0]["serverTime"]
            self._time_offset_ms = server_ms - int(time.time() * 1000)
        return {"offsetMs": self._time_offset_ms}

    # ---- core ----
    def _request(self, method: str, path: str, params: dict,
                 signed: bool) -> dict:
        q = {k: v for k, v in params.items() if v is not None}
        # MCP params arrive JSON-ish; stringify scalars for the query string
        q = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
             for k, v in q.items()}
        if signed:
            if not self.has_keys:
                return {"ok": False, "error": (
                    f"no {self.env} credentials — set {self._key_env}/"
                    f"{self._secret_env} env or write " + str(self.key_file))}
            q["timestamp"] = str(int(time.time() * 1000) + self._time_offset_ms)
            q["recvWindow"] = str(RECV_WINDOW_MS)
            query = urllib.parse.urlencode(q)
            sig = hmac.new(self.secret.encode(), query.encode(),
                           hashlib.sha256).hexdigest()
            query = f"{query}&signature={sig}"
        else:
            query = urllib.parse.urlencode(q)

        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(url, method=method)
        req.add_header("X-MBX-APIKEY", self.key or "")
        req.add_header("User-Agent", "airlock-roadtone/1.0")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode())
            return {"ok": True, "result": {"content": [body]}, "http": 200}
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode())
            except Exception:  # noqa: BLE001
                detail = {"msg": str(e)}
            return {"ok": False, "error": f"HTTP {e.code}: {detail}",
                    "http": e.code}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}

    def call(self, tool_name: str, params: dict | None = None) -> dict:
        """MCP-shaped facade: {"ok": bool, "result": {"content": [...]}}."""
        params = params or {}
        try:
            method, path, signed = resolve_tool(tool_name)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        # normalize chamber-intent params to Binance REST names
        norm = dict(params)
        if "quoteOrderQty" in norm and norm["quoteOrderQty"] is not None:
            norm["quoteOrderQty"] = _trim_num(norm["quoteOrderQty"])
        if "quote_order_qty" in norm:
            norm["quoteOrderQty"] = _trim_num(norm.pop("quote_order_qty"))
        if "order_id" in norm:
            norm["orderId"] = norm.pop("order_id")
        r = self._request(method, path, norm, signed)
        if signed and r.get("ok") is False and "Timestamp" in str(r):
            self.sync_time()
            r = self._request(method, path, norm, signed)  # one re-sync retry
        return r


def _trim_num(v: Any) -> str:
    """Binance rejects floats with excess precision; format deterministically."""
    try:
        f = float(v)
        s = f"{f:.8f}".rstrip("0").rstrip(".")
        return s or "0"
    except (TypeError, ValueError):
        return str(v)


# ---------------- CLI ----------------
def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="AIRLOCK Binance Spot Testnet gateway")
    ap.add_argument("action", nargs="?", default="status",
                    choices=["status", "account", "cycle-test"])
    args = ap.parse_args()
    c = BinanceRestClient()
    print(f"base={c.base}  keys={'PRESENT' if c.has_keys else 'ABSENT'}")
    if args.action == "account":
        r = c.call("account")
        if not r.get("ok"):
            print(f"account: FAIL — {r.get('error')}")
            return 1
        bals = [b for b in r["result"]["content"][0].get("balances", [])
                if float(b.get("free", 0)) > 0]
        print(f"account OK — {len(bals)} non-zero balances:")
        for b in bals:
            print(f"  {b['asset']:8} free={b['free']}")
        return 0
    # status (default)
    for t in ("ping", "time"):
        r = c.call(t)
        print(f"{t:6} -> {'OK' if r.get('ok') else 'FAIL ' + str(r.get('error'))[:80]}"
              + (f" serverTime={r['result']['content'][0]['serverTime']}" if t == "time" and r.get("ok") else ""))
    r = c.call("ticker24hr", {"symbol": "BTCUSDT"})
    if r.get("ok"):
        d = r["result"]["content"][0]
        print(f"BTCUSDT last={d['lastPrice']} 24h%={d['priceChangePercent']} quoteVol={d['quoteVolume']}")
    else:
        print(f"BTCUSDT ticker FAIL: {str(r.get('error'))[:120]}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
