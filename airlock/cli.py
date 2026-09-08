"""AIRLOCK CLI: airlock check | intent | yes | no | execute | reconcile | kill | verify.

Entry point `airlock` (see pyproject [project.scripts]).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from airlock.binance_rest import BinanceRestClient  # noqa: E402
from airlock.chambers import (  # noqa: E402
    Policy, ch_evidence, ch_execute, ch_kill, ch_lock, ch_mandate,
    ch_reconcile, ch_verify, db,
)


def _client_and_policy():
    policy = Policy()
    client = BinanceRestClient()
    return client, policy


def _print(tag: str, obj: dict) -> None:
    print(f"[{tag}]")
    print(json.dumps(obj, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(prog="airlock", description="AIRLOCK six-chamber Agent OS")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="chamber 1 EVIDENCE: backend, balances, tickers")
    p_int = sub.add_parser("intent", help='chamber 2 MANDATE: airlock intent "buy 10 USDT of BNBUSDT"')
    p_int.add_argument("text", nargs="+")
    p_yes = sub.add_parser("yes", help="chamber 3 LOCK: type 'yes' (optionally paste hash prefix)")
    p_yes.add_argument("hash", nargs="?", default=None)
    p_yes.add_argument("--yesfile", default=None, help=argparse.SUPPRESS)
    p_no = sub.add_parser("no", help="discard pending intent")
    sub.add_parser("execute", help="chamber 4 EXECUTE (testnet only)")
    sub.add_parser("reconcile", help="chamber 5 RECONCILE: MATCH / NO_TRADE / DEVIATION")
    sub.add_parser("kill", help="chamber 6 KILL: cancel all + flatten to USDT")
    sub.add_parser("verify", help="recompute ledger hash chain")
    args = ap.parse_args()

    conn = db()
    try:
        if args.cmd == "check":
            client, policy = _client_and_policy()
            _print("1 EVIDENCE", ch_evidence(client, policy, conn))
        elif args.cmd == "intent":
            client, policy = _client_and_policy()
            rec = ch_mandate(conn, " ".join(args.text))
            print(f"intent hash : {rec['hash']}")
            print(f"parsed      : {json.dumps(rec['intent'], default=str)}")
            print(f"status      : {rec['status']}")
            print("→ type `airlock yes` (optionally: airlock yes <hash-prefix>) to LOCK")
        elif args.cmd == "yes":
            client, policy = _client_and_policy()
            typed = args.hash
            if typed is None and args.yesfile:
                typed = Path(args.yesfile).read_text().strip()
            _print("3 LOCK", ch_lock(client, policy, conn, typed))
        elif args.cmd == "no":
            from airlock.chambers import kv_get, kv_set
            raw = kv_get(conn, "pending_intent")
            if raw:
                rec = json.loads(raw)
                rec["status"] = "DISCARDED"
                kv_set(conn, "pending_intent", json.dumps(rec))
                print(f"pending intent {rec['hash'][:16]}… discarded")
            else:
                print("no pending intent")
        elif args.cmd == "execute":
            client, policy = _client_and_policy()
            _print("4 EXECUTE", ch_execute(client, policy, conn))
        elif args.cmd == "reconcile":
            client, policy = _client_and_policy()
            _print("5 RECONCILE", ch_reconcile(client, conn))
        elif args.cmd == "kill":
            client, policy = _client_and_policy()
            _print("6 KILL", ch_kill(client, policy, conn))
        elif args.cmd == "verify":
            _print("VERIFY", ch_verify(conn))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
