"""
One-off: defaultLoan() the 5 zombie loans stuck past expiry.

Each loan past expiryTimestamp can only be closed via V4.defaultLoan(). Anyone
can call it. The contract applies an Aave-style split:
  - 3% bounty   → msg.sender
  - 1% insurance fee
  - debt-equivalent collateral → lender
  - excess collateral → borrower

All 5 lender/borrower pairs are among our 3 wallets (B/C/D), so the recovery
returns most of the value back to us. Cost: ~150k gas × 5 ≈ $0.25 total.

Run once: `python default_zombies.py`. After this, the bot's state.json can
be cleared (or it'll self-clean since currentOwed → 0 for closed loans).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

RPC = "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy"
V4  = Web3.to_checksum_address("0x9d3b61d13a839968ffad94a0eedf73153c2fb31c")
CHAIN_ID = 8453

WALLETS_ENV = Path(__file__).parent / ".wallets.env"

ZOMBIES = [
    "0xacdf447c6378c62db5d0547360a6a540e52ea2cfd55162b7bcffed1d30f1c9e6",
    "0x2211b9a516ed8cfbf5db3980d79085224a80f882ab2605f72c76558bf09c2ae2",
    "0xdfaa23f00b765d6130960cb2915f66fd33ed5854008dd5e65684dfdee275bcba",
    "0x0f9f9913e0d763fb779c9297f38181ab431c1c3bc2be45d197b806cd7354f9d0",
    "0x6c7a6048a18c840d0ec796e0a70fa07caa581c87654712a800e0a9247cdf2523",
]

ABI = [
    {"name": "defaultLoan", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "loanId", "type": "bytes32"}], "outputs": []},
    {"name": "currentOwed", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "loanId", "type": "bytes32"}], "outputs": [{"type": "uint256"}]},
]


def load_env() -> dict:
    out = {}
    for line in WALLETS_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    env = load_env()
    # Use wallet C (most ETH at last check)
    pk = env["WALLET_C_PRIVATE_KEY"]
    acct = Account.from_key(pk)
    print(f"Caller: {acct.address} (Wallet C)")

    w3 = Web3(Web3.HTTPProvider(RPC))
    c = w3.eth.contract(address=V4, abi=ABI)

    print(f"\nETH bal: {w3.eth.get_balance(acct.address) / 1e18:.6f}")
    print(f"Defaulting {len(ZOMBIES)} zombies...\n")

    receipts = []
    for i, lid in enumerate(ZOMBIES, 1):
        lid_b = bytes.fromhex(lid[2:])
        # Sanity: is it still active?
        try:
            owed = c.functions.currentOwed(lid_b).call() / 1e6
        except Exception as e:
            print(f"  [{i}] {lid[:14]}…  currentOwed errored ({e}) — skip")
            continue
        if owed == 0:
            print(f"  [{i}] {lid[:14]}…  already closed (owed=0) — skip")
            continue

        print(f"  [{i}] {lid[:14]}…  owed=${owed:.4f}  → defaultLoan()")
        tx = c.functions.defaultLoan(lid_b).build_transaction({
            "from":  acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": CHAIN_ID,
            "gas":   250_000,
            "maxPriorityFeePerGas": w3.to_wei(0.1, "gwei"),
            "maxFeePerGas":         w3.to_wei(0.25, "gwei"),
        })
        signed = acct.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        print(f"     tx: https://basescan.org/tx/{h.hex()}")
        r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        status = "✓ defaulted" if r.status == 1 else "✗ reverted"
        print(f"     {status}  gas={r.gasUsed}  block={r.blockNumber}\n")
        receipts.append((lid, h.hex(), r.status, r.gasUsed))
        time.sleep(2)   # don't spam nonce

    ok    = sum(1 for *_, s, _ in receipts if s == 1)
    rev   = sum(1 for *_, s, _ in receipts if s != 1)
    total_gas = sum(g for *_, g in receipts)
    print(f"─── Summary ───")
    print(f"  defaulted: {ok}/{len(receipts)}, reverted: {rev}, total gas: {total_gas:,}")


if __name__ == "__main__":
    sys.exit(main())
