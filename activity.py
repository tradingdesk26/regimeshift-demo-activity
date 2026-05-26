"""
RegimeShift demo activity bot — keeps the Loan Registry + Open Order Book
visibly alive on regimeshift.xyz, *and* eats its own dog food by paying for
Agent-SOFR via x402 like any third-party agent would.

Role separation (option C from design discussion):
  Wallet A  — oracle signer (OFF-LIMITS to this script)
  Wallet D  — "data buyer" agent. Periodically pays $0.10 via x402 to fetch
              fresh Agent-SOFR USD rate, caches it on disk. Excluded from
              lender/borrower pools.
  Wallets B + C — "executor" agents. Read cached SOFR, post intents using
              real rates (base + regime + take + spread), originate, repay.

Per fire:
  - First, refresh SOFR cache if older than SOFR_REFRESH_AGE_SEC (D pays).
  - Then opportunistically repay due loans (any wallet).
  - Then pick a random action (35/35/20/10 lend/borrow/paired/no-op).

All loans use small amounts ($0.20-$1.00), durations 15-30 min (long enough
that the bot's 7-15min fire cadence catches the repay window).

Cap: max 5 simultaneous active loans. Self-healing: stuck loans past their
expiry are closed via defaultLoan() fallback automatically.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from eth_account import Account
from web3 import Web3


# ─── Config ──────────────────────────────────────────────────────────────────

API_BASE        = "https://regimeshift.xyz/api"
RPC_URL         = "https://base-mainnet.g.alchemy.com/v2/C1ASgXsGxtYR0ilEB6wIy"
LOG_PATH        = "/opt/regimeshift-demo/activity.log"
STATE_PATH      = "/opt/regimeshift-demo/state.json"
SOFR_CACHE_PATH = "/opt/regimeshift-demo/sofr_cache.json"
WALLETS_ENV     = "/opt/regimeshift-demo/.wallets.env"

# Refresh SOFR cache after 90 min — at $0.001/call ≈ $0.016/day (was $1.60/day at $0.10)
SOFR_REFRESH_AGE_SEC = 5400
DATA_BUYER_NAME      = "D"
EXECUTOR_NAMES       = {"B", "C"}   # only these participate in lend/borrow pools

USDC            = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
WETH            = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
V4              = Web3.to_checksum_address("0x9d3b61d13a839968ffad94a0eedf73153c2fb31c")
CHAIN_ID        = 8453

MAX_ACTIVE_LOANS = 5
MIN_ETH_GAS      = 0.00005     # below this, wallet skipped (out of gas)

# Action probabilities — must sum to 1.0
P_LENDER_ONLY   = 0.35
P_BORROWER_ONLY = 0.35
P_PAIRED        = 0.20
P_REPAY         = 0.10


# ─── ABIs ────────────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"name": "approve",   "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "o", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    # Forward to receive() → deposit() on WETH contract (for wrapping ETH)
    {"name": "deposit",   "type": "function", "stateMutability": "payable",
     "inputs":  [], "outputs": []},
]

REPO_ABI = [
    {"name": "originate", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
       {"name": "q", "type": "tuple", "components": [
         {"name": "borrower",         "type": "address"},
         {"name": "lender",           "type": "address"},
         {"name": "principalToken",   "type": "address"},
         {"name": "principalAmount",  "type": "uint256"},
         {"name": "collateralToken",  "type": "address"},
         {"name": "collateralAmount", "type": "uint256"},
         {"name": "expiryTimestamp",  "type": "uint256"},
         {"name": "rateBps",          "type": "uint256"},
         {"name": "nonce",            "type": "bytes32"},
       ]},
       {"name": "sig", "type": "bytes"},
     ],
     "outputs": [{"type": "bytes32"}]},
    {"name": "repay", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "loanId", "type": "bytes32"}], "outputs": []},
    {"name": "defaultLoan", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "loanId", "type": "bytes32"}], "outputs": []},
    {"name": "currentOwed", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "loanId", "type": "bytes32"}],
     "outputs": [{"type": "uint256"}]},
]


# ─── Logging ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except Exception:
        pass


# ─── State (which loans we own + when to repay them) ─────────────────────────

def state_load() -> dict:
    p = Path(STATE_PATH)
    if not p.exists():
        return {"our_loans": []}
    return json.loads(p.read_text())


def state_save(s: dict) -> None:
    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_PATH).write_text(json.dumps(s, indent=2))


# ─── Wallets ─────────────────────────────────────────────────────────────────

@dataclass
class Wallet:
    name: str
    addr: str
    account: object   # eth_account.LocalAccount

    def __repr__(self) -> str:
        return f"Wallet({self.name} {self.addr[:8]}…)"


def load_wallets() -> list[Wallet]:
    env = {}
    for line in Path(WALLETS_ENV).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    wallets = []
    for name in ["B", "C", "D"]:
        addr = env.get(f"WALLET_{name}_ADDR")
        pk   = env.get(f"WALLET_{name}_PRIVATE_KEY")
        if not addr or not pk:
            log(f"  ⚠ Wallet {name} missing in {WALLETS_ENV}, skipping")
            continue
        acct = Account.from_key(pk)
        assert acct.address.lower() == addr.lower()
        wallets.append(Wallet(name=name, addr=acct.address, account=acct))
    return wallets


# ─── Chain helpers ───────────────────────────────────────────────────────────

class Chain:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(RPC_URL))
        self.usdc = self.w3.eth.contract(address=USDC, abi=ERC20_ABI)
        self.weth = self.w3.eth.contract(address=WETH, abi=ERC20_ABI)
        self.repo = self.w3.eth.contract(address=V4, abi=REPO_ABI)

    def balances(self, addr: str) -> dict:
        addr = Web3.to_checksum_address(addr)
        return {
            "eth":  self.w3.eth.get_balance(addr) / 1e18,
            "usdc": self.usdc.functions.balanceOf(addr).call() / 1e6,
            "weth": self.weth.functions.balanceOf(addr).call() / 1e18,
        }

    def send(self, account, fn, gas: int = 200_000) -> str:
        tx = fn.build_transaction({
            "from":  account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "chainId": CHAIN_ID,
            "gas":   gas,
            "maxPriorityFeePerGas": self.w3.to_wei(0.1, "gwei"),
            "maxFeePerGas":         self.w3.to_wei(0.25, "gwei"),
        })
        signed = account.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        return self.w3.eth.send_raw_transaction(raw).hex()

    def wait(self, tx_hash: str, timeout: int = 90) -> bool:
        h = bytes.fromhex(tx_hash[2:] if tx_hash.startswith("0x") else tx_hash)
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=timeout)
        return r.status == 1

    def ensure_allowance(self, wallet: Wallet, token, amount_raw: int) -> None:
        cur = token.functions.allowance(wallet.addr, V4).call()
        if cur >= amount_raw:
            return
        # Approve 100× to skip future approvals
        tx = self.send(wallet.account, token.functions.approve(V4, amount_raw * 100), gas=80_000)
        log(f"  approve {token.address[:6]}… by {wallet.name}: {tx}")
        if not self.wait(tx):
            raise RuntimeError(f"approve reverted: {tx}")


# ─── Agent-SOFR fetch — PAID via x402 (our own facilitator on Base mainnet) ─
#
# When we tried Coinbase's CDP facilitator (api.cdp.coinbase.com/platform/v2/x402)
# the deployed schema validator rejected payloads from the canonical x402 SDK
# (mid-migration incompatibility). To unblock paid mainnet settlement, we
# built our own minimal facilitator at http://127.0.0.1:8091 on the VM —
# arms-signals talks to it instead of CDP. Every paid call is a real
# USDC.transferWithAuthorization tx, visible on BaseScan.
#
# Wallet D pays $0.001 USDC for fresh Agent-SOFR every SOFR_REFRESH_AGE_SEC
# (was $0.10 — lowered to $0.001 in 2026-05-26 to reduce friction for new agents).
# If the paid x402 call fails for any reason, fall back to reading the most
# recent signed quote from /v1/matches/recent (same data, free path) so the
# bot never blocks on x402 hiccups.

def x402_get_sofr(account) -> dict:
    """Pay $0.001 USDC via x402 to fetch fresh Agent-SOFR. Uses the official
    x402 SDK 2.10 client wrapper on a requests.Session — server-side our
    own facilitator (not CDP) handles verify + settle on Base mainnet."""
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.http.clients import x402_http_adapter

    client = x402ClientSync()
    client.register("eip155:8453", ExactEvmScheme(signer=account))
    s = requests.Session()
    s.mount("https://", x402_http_adapter(client))

    r = s.get(f"{API_BASE}/v1/rate/sofr/usd?horizon=1h", timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"x402 paid call returned {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_sofr_via_recent_match() -> dict | None:
    """Read the most recent signed quote (free) and extract Agent-SOFR.
    Returns a dict matching the paid /v1/rate/sofr/usd response shape, or
    None if no recent match exists."""
    try:
        r = requests.get(f"{API_BASE}/v1/matches/recent?limit=5", timeout=10)
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:
        return None
    for m in matches:
        q = m.get("quote", {})
        dec = q.get("decomposition", {})
        if dec.get("base_anchor_pct") is None:
            continue
        return {
            "rate":              (dec["base_anchor_pct"] + dec.get("variance_premium_bps", 0)/100 + dec.get("regime_premium_bps", 0)/100),
            "decomposition": {
                "base_anchor":      dec["base_anchor_pct"],
                "variance_premium": dec.get("variance_premium_bps", 0) / 100,
                "regime_adjustment": dec.get("regime_premium_bps", 0) / 100,
            },
            "regime":            {"mode": dec.get("regime", "?")},
            "methodology":       q.get("methodology_url") and {
                "version": q.get("methodology_version", "agent-sofr-v1"),
                "url":     q.get("methodology_url"),
            } or {"version": "agent-sofr-v1"},
            "_source": "matches/recent (free read — v1.1 will pay via x402)",
        }
    return None


# ─── SOFR cache (D writes, B+C read) ─────────────────────────────────────────

def _sofr_cache_read() -> dict | None:
    try:
        return json.loads(Path(SOFR_CACHE_PATH).read_text())
    except Exception:
        return None


def _sofr_cache_age() -> int:
    c = _sofr_cache_read()
    if not c:
        return 10**9
    return int(time.time()) - int(c.get("fetched_at", 0))


def refresh_sofr_if_stale(wallets: list, chain) -> None:
    """If SOFR cache > SOFR_REFRESH_AGE_SEC old, refresh Agent-SOFR.

    D plays the data-owner role: it tries the PAID x402 path first
    (USDC.transferWithAuthorization via our own facilitator on Base mainnet).
    If that fails for any reason — facilitator down, x402 SDK error, gas
    spike, etc — it falls back to the FREE /matches/recent path so the bot
    never blocks the trading loop on x402 hiccups."""
    age = _sofr_cache_age()
    if age < SOFR_REFRESH_AGE_SEC:
        log(f"  SOFR cache age {age}s (< {SOFR_REFRESH_AGE_SEC}s) — fresh")
        return

    d = next((w for w in wallets if w.name == DATA_BUYER_NAME), None)
    if not d:
        log(f"  ⚠ no data-owner wallet '{DATA_BUYER_NAME}' — skip SOFR refresh")
        return

    log(f"→ DATA SYNC: {d.name} (data-owner) refreshing SOFR cache (cache age {age}s)")

    data = None
    source = None
    # ── Try PAID x402 path first ────────────────────────────────────────
    bal = chain.balances(d.addr)
    if bal["usdc"] >= 0.005 and bal["eth"] >= MIN_ETH_GAS:
        try:
            log(f"  paying $0.001 USDC via x402 (D balance: ${bal['usdc']:.4f} USDC, {bal['eth']:.5f} ETH)…")
            paid = x402_get_sofr(d.account)
            data = paid
            source = "x402 paid call (own facilitator → Base mainnet)"
            log(f"  ✓ x402 paid call succeeded")
        except Exception as e:
            log(f"  ⚠ x402 paid call failed ({type(e).__name__}: {str(e)[:140]}) — falling back to free read")
    else:
        log(f"  ⚠ D underfunded for paid x402 (need ≥$0.005 USDC + gas) — using free fallback")

    # ── Fallback: free read from /matches/recent ────────────────────────
    if data is None:
        data = fetch_sofr_via_recent_match()
        if data is None:
            log(f"  ⚠ no recent match in book to read SOFR from — keeping stale cache, will retry next fire")
            return
        source = data.get("_source", "matches/recent (free fallback)")

    cache = {
        "fetched_at":           int(time.time()),
        "fetched_by":           d.name,
        "rate_pct":             data["rate"],
        "base_anchor_pct":      data["decomposition"]["base_anchor"],
        "variance_premium_pct": data["decomposition"]["variance_premium"],
        "regime_adj_pct":       data["decomposition"]["regime_adjustment"],
        "regime":               data["regime"]["mode"],
        "methodology_version":  data["methodology"].get("version", "agent-sofr-v1"),
        "source":               source,
    }
    Path(SOFR_CACHE_PATH).write_text(json.dumps(cache, indent=2))
    log(f"  ✓ Agent-SOFR = {cache['rate_pct']:.3f}%  "
        f"(base {cache['base_anchor_pct']:.2f}% + var {cache['variance_premium_pct']:.2f}% "
        f"+ regime {cache['regime_adj_pct']:.2f}%)  regime={cache['regime']}  "
        f"→ cache written for B+C  [source: {cache['source']}]")


def sofr_floor_bps() -> tuple[int, str]:
    """Read cache; return (floor_bps, regime_name). Floor = base + regime + take.
    Falls back to a safe default if cache is missing/empty."""
    c = _sofr_cache_read()
    if not c or c.get("base_anchor_pct") is None:
        return 425, "UNKNOWN(cache-miss)"
    base_bps   = round(c["base_anchor_pct"] * 100)
    regime_bps = round((c.get("regime_adj_pct") or 0) * 100)
    take_bps   = 5
    return base_bps + regime_bps + take_bps, c.get("regime", "?")


# ─── API ─────────────────────────────────────────────────────────────────────

def api_post_lender(wallet: Wallet, amount_usdc: float, min_rate_bps: int, max_duration_sec: int) -> dict:
    r = requests.post(f"{API_BASE}/v1/intent/lend", timeout=15, json={
        "wallet": wallet.addr,
        "asset": "USDC",
        "amount": amount_usdc,
        "max_duration_sec": max_duration_sec,
        "min_rate_bps": min_rate_bps,
        "max_default_prob": 0.001,
    })
    r.raise_for_status()
    return r.json()


def api_post_borrower(wallet: Wallet, principal: float, collat_max: float, duration_sec: int, max_rate_bps: int) -> dict:
    r = requests.post(f"{API_BASE}/v1/intent/borrow", timeout=15, json={
        "wallet": wallet.addr,
        "principal_asset": "USDC",
        "principal_amount": principal,
        "collateral_asset": "WETH",
        "collateral_amount_max": collat_max,
        "duration_sec": duration_sec,
        "max_rate_bps": max_rate_bps,
    })
    r.raise_for_status()
    return r.json()


def api_fetch_match(match_id: str) -> dict | None:
    r = requests.get(f"{API_BASE}/v1/matches/recent?limit=30", timeout=10)
    r.raise_for_status()
    for m in r.json().get("matches", []):
        if m.get("match_id") == match_id:
            return m
    return None


def api_active_loans() -> list[dict]:
    r = requests.get(f"{API_BASE}/v1/loans/registry?limit=30", timeout=15)
    r.raise_for_status()
    return [l for l in r.json().get("loans", []) if l["status"] == "active"]


# ─── Random parameter pickers ────────────────────────────────────────────────

def rand_principal_usdc() -> float:
    """$0.20 – $1.00 in 5-cent increments."""
    return round(random.uniform(0.20, 1.00) / 0.05) * 0.05


def rand_lender_rate_bps() -> int:
    """Lender posts at floor + small spread (he wants to earn premium above floor).
    Floor comes from the cached Agent-SOFR rate that wallet D bought via x402."""
    floor, regime = sofr_floor_bps()
    spread = random.randint(20, 80)
    return floor + spread


def rand_borrower_rate_bps(min_acceptable: int) -> int:
    """Borrower's ceiling — above any reasonable lender ask, but capped.
    Uses cached SOFR floor + generous headroom so matches clear."""
    floor, _ = sofr_floor_bps()
    return max(min_acceptable + 40, floor + random.randint(120, 280))


def rand_duration_sec() -> int:
    """15-30 minute loans.

    Min 900s so the repay-window [repay_after, expiry] is at least 7.5 min
    wide — wider than the 7-15min fire cadence, so the bot reliably catches
    its own loan's repay window even if a fire is skipped."""
    return random.choice([900, 1200, 1800])


def collat_for_principal(principal_usdc: float, eth_price_usd: float = 2080.0) -> float:
    """Post collat_max high enough to fit WORST-CASE regime (EXTREME → LTV cap 55%).
    The matcher will actually pull less based on the live regime, so generosity here
    just means "don't get rejected because of regime drift between book scan and match".
    1.10× buffer for ETH price moves between intent post and match."""
    worst_case_ltv = 0.55     # EXTREME regime cap
    return round(principal_usdc / (worst_case_ltv * eth_price_usd) * 1.10, 6)


# ─── Actions ─────────────────────────────────────────────────────────────────

def act_lender_only(wallets: list[Wallet], chain: Chain) -> None:
    # Only executor wallets (B, C) participate in lender/borrower pools.
    # D is the data-buyer agent and stays out of the trading pool.
    executors = [w for w in wallets if w.name in EXECUTOR_NAMES]
    candidates = [w for w in executors if chain.balances(w.addr)["usdc"] >= 1.0
                                      and chain.balances(w.addr)["eth"] >= MIN_ETH_GAS]
    if not candidates:
        log("  (no executor has USDC+ETH to lend) — skip")
        return
    w = random.choice(candidates)
    amount = rand_principal_usdc()
    rate   = rand_lender_rate_bps()
    dur    = rand_duration_sec()
    log(f"→ LEND by {w.name}: ${amount} USDC @ ≥{rate} bps, ≤{dur}s")
    # Pre-approve USDC so if a stranger matches, originate doesn't fail
    chain.ensure_allowance(w, chain.usdc, int(amount * 1e6))
    resp = api_post_lender(w, amount, rate, dur)
    log(f"  intent_id={resp['intent_id']}  matched={resp.get('matched')}")


def act_borrower_only(wallets: list[Wallet], chain: Chain) -> None:
    executors = [w for w in wallets if w.name in EXECUTOR_NAMES]
    candidates = [w for w in executors if chain.balances(w.addr)["weth"] >= 0.0002
                                      and chain.balances(w.addr)["eth"] >= MIN_ETH_GAS]
    if not candidates:
        log("  (no executor has WETH+ETH to borrow) — skip")
        return
    w = random.choice(candidates)
    principal = rand_principal_usdc()
    dur       = rand_duration_sec()
    max_rate  = rand_borrower_rate_bps(rand_lender_rate_bps())   # generous ceiling
    collat    = collat_for_principal(principal)
    log(f"→ BORROW by {w.name}: ${principal} USDC, ≤{max_rate} bps, {dur}s, collat≤{collat:.6f} WETH")
    chain.ensure_allowance(w, chain.weth, int(collat * 1e18))
    resp = api_post_borrower(w, principal, collat, dur, max_rate)
    log(f"  intent_id={resp['intent_id']}  matched={resp.get('matched')}")


def act_paired(wallets: list[Wallet], chain: Chain) -> None:
    """Post lender + borrower from 2 different EXECUTOR wallets at compatible
    rates → matches. D is excluded from the trading pool (data-buyer only)."""
    executors = [w for w in wallets if w.name in EXECUTOR_NAMES]
    if len(executors) < 2:
        return act_lender_only(wallets, chain)
    a, b = random.sample(executors, 2)
    bal_a = chain.balances(a.addr)
    bal_b = chain.balances(b.addr)
    # Decide which is lender (has more USDC) vs borrower (has WETH)
    if bal_a["usdc"] >= 1.0 and bal_b["weth"] >= 0.0002:
        lender, borrower = a, b
    elif bal_b["usdc"] >= 1.0 and bal_a["weth"] >= 0.0002:
        lender, borrower = b, a
    else:
        log("  (no compatible wallet pair) — skip")
        return
    if chain.balances(lender.addr)["eth"] < MIN_ETH_GAS or chain.balances(borrower.addr)["eth"] < MIN_ETH_GAS:
        log("  (gas too low) — skip")
        return

    state = state_load()
    if len(state["our_loans"]) >= MAX_ACTIVE_LOANS:
        log(f"  (already have {len(state['our_loans'])} active loans — cap is {MAX_ACTIVE_LOANS}) — skip")
        return

    principal     = rand_principal_usdc()
    lender_rate   = rand_lender_rate_bps()
    borrower_rate = rand_borrower_rate_bps(lender_rate)
    duration      = rand_duration_sec()
    collat        = collat_for_principal(principal)

    log(f"→ PAIRED  lender={lender.name} ↔ borrower={borrower.name}  "
        f"${principal} USDC @ {lender_rate}-{borrower_rate} bps, {duration}s")

    chain.ensure_allowance(lender,   chain.usdc, int(principal * 1e6))
    chain.ensure_allowance(borrower, chain.weth, int(collat * 1e18))
    chain.ensure_allowance(borrower, chain.usdc, int(principal * 1.01 * 1e6))  # for future repay

    # Post lender first. find_match runs at API level. Lender might match with a
    # PRE-EXISTING borrower already in the book — capture that.
    lresp = api_post_lender(lender, principal, lender_rate, duration * 2)
    match_id = lresp.get("matched")
    if match_id:
        log(f"  ℹ lender immediately matched with pre-existing borrower: match_id={match_id}")
    else:
        # No match on lender post → post borrower, which fires find_match again
        bresp = api_post_borrower(borrower, principal, collat, duration, borrower_rate)
        match_id = bresp.get("matched")
    if not match_id:
        log(f"  ⚠ paired posts did not match (rates/collat tight?) — book has both intents")
        return
    log(f"  ✓ match_id={match_id}")

    # Fetch the signed quote — actual matched parties may differ from what
    # we picked (the API may have paired our new lender with a pre-existing
    # borrower from another wallet, etc).
    time.sleep(2)
    m = api_fetch_match(match_id)
    if not m:
        log(f"  ⚠ match {match_id} not in /v1/matches/recent — abort")
        return
    q = m["quote"]["quote"]
    sig = m["quote"]["signature"]

    # Find the ACTUAL wallets for this match (may differ from our picks)
    actual_lender   = next((w for w in wallets if w.addr.lower() == q["lender"].lower()),   None)
    actual_borrower = next((w for w in wallets if w.addr.lower() == q["borrower"].lower()), None)
    if not actual_lender or not actual_borrower:
        log(f"  ℹ match involves external wallet (lender={q['lender'][:10]}, borrower={q['borrower'][:10]}) — let the other party originate")
        return

    log(f"  actual pair: lender={actual_lender.name}, borrower={actual_borrower.name}, "
        f"rate={q['rateBps']}bps, collat={int(q['collateralAmount'])/1e18:.6f} WETH")

    # Ensure both sides have correct allowances based on what the quote actually needs
    chain.ensure_allowance(actual_lender,   chain.usdc, int(q["principalAmount"]))
    chain.ensure_allowance(actual_borrower, chain.weth, int(q["collateralAmount"]))
    chain.ensure_allowance(actual_borrower, chain.usdc, int(int(q["principalAmount"]) * 1.02))

    quote_tuple = (
        Web3.to_checksum_address(q["borrower"]),
        Web3.to_checksum_address(q["lender"]),
        Web3.to_checksum_address(q["principalToken"]),
        int(q["principalAmount"]),
        Web3.to_checksum_address(q["collateralToken"]),
        int(q["collateralAmount"]),
        int(q["expiryTimestamp"]),
        int(q["rateBps"]),
        bytes.fromhex(q["nonce"][2:]),
    )
    sig_bytes = bytes.fromhex(sig[2:])
    tx = chain.send(actual_lender.account, chain.repo.functions.originate(quote_tuple, sig_bytes), gas=600_000)
    log(f"  originate by {actual_lender.name}: https://basescan.org/tx/{tx}")
    if not chain.wait(tx):
        log(f"  ✗ originate reverted")
        return
    log(f"  ✓ loan opened: loanId={q['nonce']}")
    now_ts = int(time.time())
    expiry = int(q["expiryTimestamp"])
    state["our_loans"].append({
        "loan_id":     q["nonce"],
        "lender":      actual_lender.name,
        "borrower":    actual_borrower.name,
        "principal":   int(q["principalAmount"]) / 1e6,
        "rate_bps":    int(q["rateBps"]),
        "originated":  now_ts,
        "repay_after": now_ts + duration // 2,
        "expiry":      expiry,   # absolute timestamp — repay blocked after this
    })
    state_save(state)


def _expiry_of(loan: dict) -> int:
    """Get expiry timestamp; fallback for legacy entries that don't store it."""
    if "expiry" in loan:
        return int(loan["expiry"])
    # Legacy fallback: assume max 30-min duration → originated + 1800s
    return int(loan["originated"]) + 1800


def act_repay(wallets: list[Wallet], chain: Chain) -> None:
    """Repay any of OUR loans that are past repay-after.

    Self-healing: if a loan slipped past expiry (repay window closed),
    fall back to defaultLoan() — anyone can call it post-expiry, msg.sender
    gets 3% bounty + Aave-style split returns funds to lender + borrower.
    Either way the loan leaves our state."""
    state = state_load()
    now = int(time.time())

    # ─── Upfront cleanup: drop entries past expiry (repay impossible) ───────
    stale_expired = [l for l in state["our_loans"] if now > _expiry_of(l)]
    if stale_expired:
        log(f"  ℹ {len(stale_expired)} loan(s) past expiry — bot can't repay; will defaultLoan instead")
        # Try to recover via defaultLoan for each — preserves funds
        for loan in stale_expired:
            _try_default(loan, wallets, chain, state)

    due = [l for l in state["our_loans"] if l["repay_after"] <= now and now <= _expiry_of(l)]
    if not due:
        log("  (no loans currently in repay window)")
        return
    loan = random.choice(due)
    borrower = next((w for w in wallets if w.name == loan["borrower"]), None)
    if borrower is None:
        log(f"  ⚠ borrower wallet {loan['borrower']} missing; remove from state")
        state["our_loans"] = [l for l in state["our_loans"] if l["loan_id"] != loan["loan_id"]]
        state_save(state)
        return
    if chain.balances(borrower.addr)["eth"] < MIN_ETH_GAS:
        log(f"  ⚠ {borrower.name} out of gas — skip repay")
        return

    owed_raw = chain.repo.functions.currentOwed(bytes.fromhex(loan["loan_id"][2:])).call()
    if owed_raw == 0:
        log(f"  ℹ loan {loan['loan_id'][:14]}… already closed on chain — removing from state")
        state["our_loans"] = [l for l in state["our_loans"] if l["loan_id"] != loan["loan_id"]]
        state_save(state)
        return

    log(f"→ REPAY by {borrower.name}: loan {loan['loan_id'][:14]}… owed=${owed_raw/1e6:.4f} USDC")
    chain.ensure_allowance(borrower, chain.usdc, int(owed_raw * 2))
    tx = chain.send(borrower.account, chain.repo.functions.repay(bytes.fromhex(loan["loan_id"][2:])), gas=250_000)
    log(f"  repay tx: https://basescan.org/tx/{tx}")
    if not chain.wait(tx):
        log(f"  ✗ repay reverted — likely slipped past expiry; trying defaultLoan fallback")
        _try_default(loan, wallets, chain, state)
        return
    log(f"  ✓ repaid")
    state["our_loans"] = [l for l in state["our_loans"] if l["loan_id"] != loan["loan_id"]]
    state_save(state)


def _try_default(loan: dict, wallets: list[Wallet], chain: Chain, state: dict) -> None:
    """Fallback: call V4.defaultLoan(loanId) on an expired/stuck loan.
    Anyone can call defaultLoan after expiry — msg.sender gets 3% bounty.
    Removes the loan from state whether defaultLoan succeeds or reverts
    (because if it reverts the loan is already closed by someone else)."""
    # Pick first wallet with enough gas — bounty goes to whoever calls
    caller = next((w for w in wallets if chain.balances(w.addr)["eth"] >= MIN_ETH_GAS), None)
    if caller is None:
        log(f"  ⚠ no wallet with gas to call defaultLoan; skip")
        return
    lid_b = bytes.fromhex(loan["loan_id"][2:])
    try:
        tx = chain.send(caller.account, chain.repo.functions.defaultLoan(lid_b), gas=250_000)
        log(f"  defaultLoan({loan['loan_id'][:14]}…) by {caller.name}: {tx}")
        ok = chain.wait(tx)
        log(f"  {'✓ defaulted (collateral split via R1-#4)' if ok else '✗ defaultLoan reverted (loan likely already closed)'}")
    except Exception as e:
        log(f"  ✗ defaultLoan failed to send: {e}")
    # Regardless of outcome — drop from our state; loan is no longer our problem
    state["our_loans"] = [l for l in state["our_loans"] if l["loan_id"] != loan["loan_id"]]
    state_save(state)


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    log("─" * 50)
    log("activity bot — fire start")
    random.seed()

    wallets = load_wallets()
    if not wallets:
        log("no wallets loaded — exiting")
        return 1
    chain = Chain()

    # Balances snapshot
    for w in wallets:
        b = chain.balances(w.addr)
        role = "data-buyer" if w.name == DATA_BUYER_NAME else ("executor" if w.name in EXECUTOR_NAMES else "?")
        log(f"  {w.name} ({role}) {w.addr[:8]}…  ETH={b['eth']:.5f}  USDC=${b['usdc']:.3f}  WETH={b['weth']:.5f}")

    # First: refresh Agent-SOFR cache if stale (D pays $0.10 via x402)
    try:
        refresh_sofr_if_stale(wallets, chain)
    except Exception as e:
        log(f"  ✗ SOFR refresh errored: {e}")

    floor, regime = sofr_floor_bps()
    log(f"  current SOFR floor: {floor} bps (regime={regime})")

    # Always opportunistically repay first if anything is due
    state = state_load()
    due = [l for l in state["our_loans"] if l["repay_after"] <= int(time.time())]
    if due:
        try:
            act_repay(wallets, chain)
        except Exception as e:
            log(f"  ✗ repay action errored: {e}")

    # Then pick a random new action
    r = random.random()
    log(f"random roll: {r:.3f}")
    try:
        if   r < P_LENDER_ONLY:                                  act_lender_only(wallets, chain)
        elif r < P_LENDER_ONLY + P_BORROWER_ONLY:                act_borrower_only(wallets, chain)
        elif r < P_LENDER_ONLY + P_BORROWER_ONLY + P_PAIRED:     act_paired(wallets, chain)
        else:                                                    log("→ NO-OP (skip this tick)")
    except Exception as e:
        log(f"  ✗ action errored: {e}")

    log("fire end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
