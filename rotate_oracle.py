"""
Rotate V4 oracleSigner + owner from compromised Wallet A to a fresh key.

Sensitive operations: never prints private key. Reads old key from
/opt/arms-signals/.env's ORACLE_PRIVATE_KEY, generates new key, sends two
contract calls (setOracleSigner + transferOwnership), then writes new key
to .env files for arms-signals + facilitator. Restart services after.

Run on the VM. Output only addresses + tx hashes — no key material.
"""

import os
import secrets
import sys
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3


RPC = "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy"
V4  = Web3.to_checksum_address("0x9d3b61d13a839968ffad94a0eedf73153c2fb31c")
CHAIN_ID = 8453

ARMS_ENV     = "/opt/arms-signals/.env"
FACIL_ENV    = "/opt/regimeshift-facilitator/.env"


def load_old_key() -> str:
    for line in Path(ARMS_ENV).read_text().splitlines():
        if line.startswith("ORACLE_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("ORACLE_PRIVATE_KEY not found in " + ARMS_ENV)


def replace_env_key(env_path: str, key_name: str, new_value: str) -> None:
    """Replace a key in an env file in-place, preserving other lines.
    new_value is NEVER echoed."""
    src = Path(env_path).read_text()
    out_lines = []
    found = False
    for line in src.splitlines():
        if line.startswith(f"{key_name}="):
            out_lines.append(f"{key_name}={new_value}")
            found = True
        else:
            out_lines.append(line)
    if not found:
        out_lines.append(f"{key_name}={new_value}")
    Path(env_path).write_text("\n".join(out_lines) + "\n")
    Path(env_path).chmod(0o600)


def send_tx(w3, account, fn, gas=120_000) -> str:
    tx = fn.build_transaction({
        "from":  account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": CHAIN_ID,
        "gas":   gas,
        "maxPriorityFeePerGas": w3.to_wei(0.1, "gwei"),
        "maxFeePerGas":         w3.to_wei(0.3, "gwei"),
    })
    signed = account.sign_transaction(tx)
    raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
    return w3.eth.send_raw_transaction(raw).hex()


def wait_ok(w3, h) -> bool:
    rh = bytes.fromhex(h[2:] if h.startswith("0x") else h)
    r = w3.eth.wait_for_transaction_receipt(rh, timeout=120)
    return r.status == 1


def main() -> int:
    w3 = Web3(Web3.HTTPProvider(RPC))

    # ─── 1. Load OLD key + verify it's the current owner ───────────────────
    old_pk  = load_old_key()
    old_acc = Account.from_key(old_pk)
    print(f"OLD wallet address: {old_acc.address}")

    abi = [
      {"name":"owner","type":"function","inputs":[],"outputs":[{"type":"address"}],"stateMutability":"view"},
      {"name":"oracleSigner","type":"function","inputs":[],"outputs":[{"type":"address"}],"stateMutability":"view"},
      {"name":"setOracleSigner","type":"function","stateMutability":"nonpayable",
       "inputs":[{"name":"new","type":"address"}],"outputs":[]},
      {"name":"transferOwnership","type":"function","stateMutability":"nonpayable",
       "inputs":[{"name":"new","type":"address"}],"outputs":[]},
    ]
    c = w3.eth.contract(address=V4, abi=abi)

    current_owner  = c.functions.owner().call()
    current_signer = c.functions.oracleSigner().call()
    if current_owner.lower() != old_acc.address.lower():
        sys.exit(f"❌ OLD wallet {old_acc.address} is NOT V4 owner ({current_owner})")
    print(f"  ✓ OLD wallet confirmed as V4 owner + oracleSigner")

    # ─── 2. Generate new key — NEVER echo to stdout/log ────────────────────
    new_pk_bytes = secrets.token_bytes(32)
    new_pk_hex   = new_pk_bytes.hex()   # no 0x prefix to keep consistent with .env format
    new_acc      = Account.from_key(new_pk_hex)
    print(f"\nNEW wallet address: {new_acc.address}")
    print(f"  (private key NEVER printed — written directly to .env files)")

    # ─── 3. setOracleSigner(new) — from old wallet ─────────────────────────
    print(f"\n[1/2] setOracleSigner({new_acc.address[:10]}…)")
    h1 = send_tx(w3, old_acc, c.functions.setOracleSigner(new_acc.address), gas=80_000)
    print(f"  tx: https://basescan.org/tx/{h1}")
    if not wait_ok(w3, h1):
        sys.exit("  ✗ setOracleSigner reverted")
    print(f"  ✓ on-chain oracleSigner now: {c.functions.oracleSigner().call()}")

    # ─── 4. transferOwnership(new) — from old wallet ───────────────────────
    print(f"\n[2/2] transferOwnership({new_acc.address[:10]}…)")
    h2 = send_tx(w3, old_acc, c.functions.transferOwnership(new_acc.address), gas=80_000)
    print(f"  tx: https://basescan.org/tx/{h2}")
    if not wait_ok(w3, h2):
        sys.exit("  ✗ transferOwnership reverted")
    print(f"  ✓ on-chain owner now: {c.functions.owner().call()}")

    # ─── 5. Update .env files with NEW key (no echo) ───────────────────────
    print(f"\n[3/3] Updating env files (key written directly, no echo)")
    replace_env_key(ARMS_ENV, "ORACLE_PRIVATE_KEY", new_pk_hex)
    print(f"  ✓ {ARMS_ENV}: ORACLE_PRIVATE_KEY rotated")
    replace_env_key(FACIL_ENV, "EVM_PRIVATE_KEY", new_pk_hex)
    print(f"  ✓ {FACIL_ENV}: EVM_PRIVATE_KEY rotated (facilitator relayer also now A_new)")

    # ─── 6. Reminder for follow-up actions ─────────────────────────────────
    print(f"\n=== ROTATION COMPLETE ===")
    print(f"  OLD address: {old_acc.address}  (no longer V4 owner / signer)")
    print(f"  NEW address: {new_acc.address}  (now V4 owner + signer + facilitator relayer)")
    print(f"\nNext steps (run separately):")
    print(f"  1. Send some ETH to NEW address for relayer gas (settle txs need ~$0.05 each)")
    print(f"  2. sudo systemctl restart arms-signals.service regimeshift-facilitator.service")
    print(f"  3. Verify matcher signs new quotes with NEW key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
