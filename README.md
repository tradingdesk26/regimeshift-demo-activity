# RegimeShift demo activity bot

Autonomous agent that keeps the [regimeshift.xyz](https://regimeshift.xyz)
Loan Registry visibly alive on Base mainnet — and **eats its own dogfood**
by paying for Agent-SOFR via x402 like any external paying agent would.

Every fire (5–15 min, randomized) does one of:

1. Refresh the cached Agent-SOFR rate via paid x402 (if cache > 90 min)
2. Repay any of *our* loans whose `repay_after` window has opened
3. Post a new intent — lender-only / borrower-only / paired
4. Cleanup: `defaultLoan()` any of our loans past their expiry window (3% bounty self-healing)

The bot has been running continuously on a GCE VM since 2026-05-22.
Every action is on-chain and visible in the
[Loan Registry](https://regimeshift.xyz/#loans) section of the landing page.

## Three-role architecture

To make the data dependency *visible* on-chain (instead of just "the
oracle returns a number"), the bot splits across three wallets with
distinct economic roles:

| Wallet | Role | What it does | Excluded from |
|---|---|---|---|
| **D** | Data buyer | Pays $0.001 USDC via x402 to `GET /v1/rate/sofr/usd` every ≥90 min; caches the answer for B + C to read | Lend/borrow pool |
| **B** | Executor | Reads cached SOFR; posts lender + borrower intents at `base + regime + take + spread`; originates loans; repays | — |
| **C** | Executor | Same as B (so paired matches run between two real on-chain identities) | — |

So when you look at on-chain activity, you see:
- D wallet → x402 payment to `payTo` wallet (real `USDC.transferWithAuthorization` tx)
- Then B↔C loan opens at a rate that depends on what D bought
- D wallet has zero loans, only paid x402 settlements

This is the inter-agent capital market loop, end-to-end.

## Why it exists

The Loan Registry on regimeshift.xyz needs continuous real economic
activity to be a credible benchmark. Hand-running test loans is not a
production signal — bots running continuously are.

Equally important: an **Agent-SOFR oracle whose own publisher's agents
don't pay for it** is a weak narrative. With this bot, every refresh
of the cached rate is a real `USDC.transferWithAuthorization` on Base
mainnet. The data publisher is also a customer.

Both ends of the protocol are visible on-chain.

## Files

```
activity.py                      The bot itself (~750 LOC, single file)
regimeshift-demo-activity.service  systemd unit (Type=oneshot, fires once per timer tick)
regimeshift-demo-activity.timer    systemd timer (5-15 min random jitter)
rotate_oracle.py                  Wallet key rotation playbook (full flow)
rotate_oracle_v2.py               Rotation playbook — write-key-to-env-first variant (safer)
default_zombies.py                One-shot cleanup of stuck expired loans
requirements.txt                  Pinned x402 + web3 + eth-account + requests
```

## Running on the VM

Configured via two files (neither in git):

`/opt/regimeshift-demo/.wallets.env` — wallet credentials
```
WALLET_B_ADDR=0x...
WALLET_B_PRIVATE_KEY=0x...
WALLET_C_ADDR=0x...
WALLET_C_PRIVATE_KEY=0x...
WALLET_D_ADDR=0x...
WALLET_D_PRIVATE_KEY=0x...
```

`/opt/regimeshift-demo/state.json` — bot's view of which loans it currently owns
```json
{"our_loans": [{"loan_id": "0x...", "lender": "B", "borrower": "C",
                "principal": 0.20, "rate_bps": 458, "originated": 1779530141,
                "repay_after": 1779530741, "expiry": 1779531341}]}
```

Service supervised by systemd:

```bash
sudo systemctl enable --now regimeshift-demo-activity.timer
sudo journalctl -u regimeshift-demo-activity.service -f
tail -f /opt/regimeshift-demo/activity.log
```

## Companion repos

- [`armsys-signals`](https://github.com/tradingdesk26/armsys-signals) — the
  API server this bot consumes (paid x402 endpoints, two-tier facilitator)
- [`regimeshift-clearinghouse`](https://github.com/tradingdesk26/regimeshift-clearinghouse) — InterAgentRepoV4 on-chain escrow contracts (the bot originates/repays here)
- [`regimeshift-agent-starter`](https://github.com/tradingdesk26/regimeshift-agent-starter) — minimal starter kit if you want to build your own agent

## Operational playbooks (in this repo)

**Key rotation** (`rotate_oracle_v2.py`):
- Generates a fresh keypair, writes new key to `.env` *before* sending any tx,
  then calls `V4.setOracleSigner(new)` + `V4.transferOwnership(new)` from old key.
- Used 2026-05-23 to rotate the V4 owner + oracle signer after a key
  exposure incident. Old wallet retained as x402 facilitator relayer (gas).

**Zombie loan cleanup** (`default_zombies.py`):
- Calls `V4.defaultLoan(loanId)` for loans whose repay window has closed,
  recovering principal + collateral split via the Aave-style waterfall
  in `InterAgentRepoV4`. Used 2026-05-22 to clear 5 expired loans the
  V4 contract was holding (`LoanNotExpired` revert blocked normal
  `repay()` past expiry; `defaultLoan()` is the post-expiry path).

Both are one-shots, idempotent enough to re-run if needed.

## License

TBD — source-code license under review.
