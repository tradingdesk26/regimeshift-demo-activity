"""
Rotate V4 oracleSigner + owner from compromised Wallet A to a fresh key.

V2 — fixed order: ALWAYS save new key BEFORE any tx, so a crash mid-flight
doesn't strand us with an inaccessible oracleSigner. Also handles nonce
properly between sequential txs.

Output: only addresses + tx hashes. New private key written directly to
.env files (read-only via tail -c '...' or grep), never printed.
"""

import os
import secrets
import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3


RPC      = "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy"
V4       = Web3.to_checksum_address("0x9d3b61d13a839968ffad94a0eedf73153c2fb31c")
CHAIN_ID = 8453
ARMS_ENV = "/opt/arms-signals/.env"


def load_old_key() -> str:
    for line in Path(ARMS_ENV).read_text().splitlines():
        if line.startswith("ORACLE_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ORACLE_PRIVATE_KEY missing")


def replace_env_key(env_path: str, key_name: str, new_value: str) -> None:
    src = Path(env_path).read_text()
    out = []
    found = False
    for line in src.splitlines():
        if line.startswith(f"{key_name}="):
            out.append(f"{key_name}={new_value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key_name}={new_value}")
    Path(env_path).write_text("\n".join(out) + "\n")
    Path(env_path).chmod(0o600)


def main() -> int:
    w3 = Web3(Web3.HTTPProvider(RPC))

    abi = [
      {"name":"owner","type":"function","inputs":[],"outputs":[{"type":"address"}],"stateMutability":"view"},
      {"name":"oracleSigner","type":"function","inputs":[],"outputs":[{"type":"address"}],"stateMutability":"view"},
      {"name":"setOracleSigner","type":"function","stateMutability":"nonpayable",
       "inputs":[{"name":"a","type":"address"}],"outputs":[]},
      {"name":"transferOwnership","type":"function","stateMutability":"nonpayable",
       "inputs":[{"name":"a","type":"address"}],"outputs":[]},
    ]
    c = w3.eth.contract(address=V4, abi=abi)

    old_pk  = load_old_key()
    old_acc = Account.from_key(old_pk)
    print(f"OLD wallet: {old_acc.address}")
    print(f"  current owner:  {c.functions.owner().call()}")
    print(f"  current signer: {c.functions.oracleSigner().call()}")

    # ─── STEP 1: generate NEW key + persist to .env IMMEDIATELY ────────────
    new_pk    = secrets.token_bytes(32).hex()
    new_acc   = Account.from_key(new_pk)
    print(f"\nNEW wallet: {new_acc.address}  (key written to .env, NEVER printed)")
    replace_env_key(ARMS_ENV, "ORACLE_PRIVATE_KEY", new_pk)
    print(f"  ✓ {ARMS_ENV} updated")

    # Verify .env has new key by reading back and deriving addr
    read_back = load_old_key()  # function name lies — it loads ORACLE_PRIVATE_KEY whatever it currently is
    if Account.from_key(read_back).address != new_acc.address:
        sys.exit("❌ .env write didn't take — refusing to send any tx")
    print(f"  ✓ .env verified: derives to {new_acc.address}")

    # ─── STEP 2: setOracleSigner from OLD key ──────────────────────────────
    print(f"\n[tx 1] setOracleSigner({new_acc.address[:10]}…)")
    nonce = w3.eth.get_transaction_count(old_acc.address)
    tx = c.functions.setOracleSigner(new_acc.address).build_transaction({
        "from": old_acc.address, "nonce": nonce, "chainId": CHAIN_ID,
        "gas": 60000,
        "maxPriorityFeePerGas": w3.to_wei(0.15, "gwei"),
        "maxFeePerGas":         w3.to_wei(0.4, "gwei"),
    })
    signed = old_acc.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    h1 = w3.eth.send_raw_transaction(raw).hex()
    print(f"  tx: https://basescan.org/tx/{h1}")
    r = w3.eth.wait_for_transaction_receipt(bytes.fromhex(h1[2:] if h1.startswith('0x') else h1), timeout=120)
    if r.status != 1:
        sys.exit("  ✗ reverted — rollback your env manually from .env.bak-cdp")
    print(f"  ✓ mined in block {r.blockNumber}")

    # Wait for state to propagate + re-fetch nonce for the next tx
    time.sleep(3)
    actual_signer = c.functions.oracleSigner().call()
    print(f"  on-chain signer (post-confirmation): {actual_signer}")
    if actual_signer != new_acc.address:
        sys.exit(f"  ✗ on-chain signer mismatch — manual recovery needed")

    # ─── STEP 3: transferOwnership from OLD key (fresh nonce) ──────────────
    print(f"\n[tx 2] transferOwnership({new_acc.address[:10]}…)")
    nonce2 = w3.eth.get_transaction_count(old_acc.address)   # MUST re-fetch after tx 1 mined
    print(f"  fresh nonce: {nonce2}")
    tx = c.functions.transferOwnership(new_acc.address).build_transaction({
        "from": old_acc.address, "nonce": nonce2, "chainId": CHAIN_ID,
        "gas": 60000,
        "maxPriorityFeePerGas": w3.to_wei(0.15, "gwei"),
        "maxFeePerGas":         w3.to_wei(0.4, "gwei"),
    })
    signed = old_acc.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    h2 = w3.eth.send_raw_transaction(raw).hex()
    print(f"  tx: https://basescan.org/tx/{h2}")
    r = w3.eth.wait_for_transaction_receipt(bytes.fromhex(h2[2:] if h2.startswith('0x') else h2), timeout=120)
    if r.status != 1:
        sys.exit("  ✗ reverted")
    print(f"  ✓ mined in block {r.blockNumber}")

    time.sleep(3)
    print(f"\n=== Final state ===")
    print(f"  owner:        {c.functions.owner().call()}")
    print(f"  oracleSigner: {c.functions.oracleSigner().call()}")
    print(f"  NEW addr:     {new_acc.address}")
    print(f"\nNext: sudo systemctl restart arms-signals.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
