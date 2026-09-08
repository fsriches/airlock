# Running Airlock on Binance Mainnet

Airlock ships with a testnet-only hard gate in the EXECUTE chamber. That gate exists so nobody sends real money by accident. This guide explains how to run the same six chambers against Binance mainnet, with real funds, on purpose and with the smallest possible blast radius.

Read all of it before creating a key. Real orders are irreversible.

## What changes, what does not

Nothing in the chambers changes. EVIDENCE, MANDATE, LOCK, RECONCILE and KILL run the same code. Two things change:
1. The exchange endpoint: `https://api.binance.com` instead of `https://testnet.binance.vision`.
2. The key: a mainnet API key with trading only, no withdrawal, restricted to your server's IP.

Everything Airlock never does still holds on mainnet: it never withdraws (the key cannot), it never opens risk on its own (every order comes from an intent you approved by typing its hash), and KILL only flattens positions Airlock itself opened.

## Step 1: isolate the money

Pick one of these two setups. Do not run Airlock against your whole exchange balance.

Option A, sub-account (best): create a regular sub-account in the Binance web UI (Sub Accounts > Create), transfer only the amount you are willing to let an agent trade (for a first run, 20 to 50 USDT), and create the API key on that sub-account. Airlock can then only ever see and trade that balance.

Option B, main account with a fenced Spot wallet: move everything you do not want traded out of the Spot wallet into Funding or Earn. Spot trading API keys cannot touch Funding or Earn balances. Leave only the amount you are willing to trade in Spot.

## Step 2: create the key with the right restrictions

Binance web UI: Account > API Management > Create API > System generated. Name it `airlock`. Complete 2FA.

Then Edit restrictions:
- Enable Reading: on (Airlock needs balances and order status).
- Enable Spot & Margin Trading: on.
- Enable Withdrawals: off. Never turn this on for Airlock.
- Enable Futures, Margin loans, Universal Transfer and anything else: off.
- IP access restriction: choose "Restrict access to trusted IPs only" and add the public IP of the machine that runs Airlock. Binance disables trading on unrestricted keys after 90 days anyway; restricting from day one is safer.
Save. Copy the API key and secret once; the secret is shown only once.

## Step 3: give the key to Airlock, never to the repo

```
mkdir -p ~/.airlock && chmod 700 ~/.airlock
cat > ~/.airlock/binance_mainnet.json <<'EOF'
{"key": "YOUR_MAINNET_API_KEY", "secret": "YOUR_MAINNET_SECRET"}
EOF
chmod 600 ~/.airlock/binance_mainnet.json
```

`~/.airlock` is git-ignored. Do not paste keys into chats, tickets or screenshots.

## Step 4: a mainnet policy, tighter than the testnet one

Copy `policy.yaml` to `policy.mainnet.yaml` and lower every cap for the first days:

```
max_order_notional_usdt: 12
max_exposure_usdt: 20
allowed_symbols: [BNBUSDT, BTCUSDT, ETHUSDT]
spot_only: true
max_orders_per_5m: 3
price_sanity_pct: 3
reconcile_price_tolerance_pct: 1.0
```

Binance spot min notional is 5 USDT on most pairs, so 12 USDT per order leaves room for a fill and a flatten. Raise caps only after several sessions with clean MATCH results and a clean `airlock verify`.

## Step 5: lift the testnet gate on purpose

The EXECUTE chamber refuses to send anything unless the environment says testnet. Mainnet requires two explicit opt-ins so a typo cannot flip you to real money:

```
export AIRLOCK_BACKEND=rest
export AIRLOCK_ENV=mainnet
export AIRLOCK_I_UNDERSTAND_MAINNET=1
export AIRLOCK_POLICY=policy.mainnet.yaml
```

If your copy of Airlock does not yet read `AIRLOCK_ENV` and `AIRLOCK_I_UNDERSTAND_MAINNET`, see the note for maintainers at the end; the change is a few lines and the repo will carry it in the next tag.

## Step 6: first run, in this exact order

```
airlock check                                   # EVIDENCE: confirms mainnet host, key permissions, balance
airlock intent "buy 10 USDT of BNBUSDT market"  # MANDATE: prints the approval hash
echo "<hash>" | airlock yes                     # LOCK: type the hash back; anything else is a deny
airlock execute                                 # EXECUTE: one order, clientOrderId derived from the hash
airlock reconcile --order-id AIR-ORDER-...      # RECONCILE: fill vs mandate, expect MATCH
airlock kill                                    # KILL: cancel open orders, sell back what Airlock bought
airlock verify                                  # ledger hash chain must be OK
```

`airlock check` on mainnet should print the host `api.binance.com` and the key's permission flags. If it shows withdrawal enabled, stop, delete the key, and create a new one.

Run this sequence once with 10 USDT before letting any AI client submit intents. The first mainnet session should end flat.

## Step 7: hand intents to an AI client

Airlock does not decide what to buy. Intents come from you (Telegram or terminal) or from an AI client. On mainnet, keep the human LOCK step: the client can propose, Airlock hashes, you type the hash. Do not automate `airlock yes` on mainnet.

## Step 8: kill and revoke

- Immediate stop: `airlock kill` cancels all open orders and flattens Airlock-owned positions.
- Revoke access: Binance web UI > API Management > delete the `airlock` key. Airlock is then blind and harmless.
- If you used a sub-account, transferring the balance back to the master empties Airlock's reach entirely.

## The official Agent OS path (Binance MCP Server)

Binance's MCP Server (`https://agent.binance.com/mcp/agentic`) runs agents inside an Agentic sub-account with no withdrawal scope at all, which is a better isolation model than API keys. At the time of writing Binance only issues OAuth tokens to clients on its allowlist (Claude Code, Claude Desktop, Codex CLI, ChatGPT, VS Code, Grok Bot). Airlock's own OAuth client reaches the authorize page and is rejected with error 3346001.

Until self-built agents are allowed, the supported way to combine the two is a sidecar setup: the supported client (for example Claude Code) holds the Binance MCP connection and places orders; Airlock runs next to it as the policy, hash-lock and reconcile layer. Airlock's chambers are plain commands, so a client can be instructed to call `airlock intent` before proposing and `airlock reconcile` after filling, and to refuse to send anything that did not get a LOCK. Full MCP-backed EXECUTE (Airlock sending through the MCP session itself) is on the roadmap and will ship when the allowlist opens or a partner client id is available.

## Note for maintainers: the code change behind Step 5

1. Read `AIRLOCK_ENV` (`testnet` default, `mainnet` allowed) and pick the base URL and key file from it: `binance_testnet.json` or `binance_mainnet.json`.
2. In EXECUTE and KILL, replace the testnet-only assertion with: allow when `AIRLOCK_ENV == testnet`, or when `AIRLOCK_ENV == mainnet` and `AIRLOCK_I_UNDERSTAND_MAINNET == 1`; otherwise refuse with a clear message.
3. In EVIDENCE, print the host and the key permission flags (`GET /sapi/v1/account/apiRestrictions`) and refuse to continue if `enableWithdrawals` is true.
4. Tag the ledger rows with the environment so testnet and mainnet history never mix; `airlock verify` should report which environment a chain belongs to.
5. Add tests: mainnet without the opt-in flag is refused; withdrawal-enabled key is refused.

## Disclaimer

Airlock is software and software fails. It is not financial advice. Trading carries risk of loss. Keep Binance's own controls (key deletion, sub-account transfer, and for MCP users the Emergency Stop) as your last resort.
