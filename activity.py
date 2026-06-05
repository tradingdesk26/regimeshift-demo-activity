"""
RegimeShift demo activity bot — keeps the Loan Registry + Open Order Book
visibly alive on regimeshift.xyz, *and* eats its own dog food by paying for
Agent-SOFR via x402 like any third-party agent would.

Role separation (option C from design discussion):
  Wallet A  — oracle signer (OFF-LIMITS to this script)
  Wallet D  — "data buyer" agent. Periodically pays $0.001 via x402 to fetch
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

# Action probabilities — must sum to 1.0.
# NOTE: repay runs UNCONDITIONALLY before this roll (see main()), so P_REPAY is
# only the implicit NO-OP remainder, not a repay trigger. With no organic external
# counterparties, single-side lender/borrower posts just rest unmatched — only
# act_paired (B↔C self-cross) produces settled on-chain loans. Bumped P_PAIRED
# 0.20→0.50 for visible demo activity (2026-06-01); trimmed the single-side and
# NO-OP bands accordingly.
P_LENDER_ONLY   = 0.22
P_BORROWER_ONLY = 0.22
P_PAIRED        = 0.50
P_REPAY         = 0.06

# ─── D-quoter policy ──────────────────────────────────────────────────────────
# Two-tier policy:
#   EXTERNAL wallets — D satisfies if external UNDERCUTS our best internal quote
#     (i.e. better than ALL of our lender/borrower asks on the same side).
#     * Lender side: external.min_rate < OUR_BEST_LENDER_RATE → D takes (D borrows)
#       (external offers cheaper money than B/C — must take before B/C lose deal flow)
#     * Borrower side: external.max_rate > OUR_BEST_BORROWER_RATE → D takes (D lends)
#       (external offers more interest than our borrowers can — better return for D)
#     Duration: D matches external's max_duration_sec (capped at D_MAX_DURATION_SEC
#     for safety) so external doesn't have to wait for matching duration.
#     If external is at-or-worse than our best → no priority. External waits in
#     normal FIFO queue; if they want priority they can re-post at a better price.
#
#   INTERNAL wallets (B/C cross via act_paired) — keep the existing algorithm
#   with spreads + randomization. Unchanged.
#
# Fallback: if no internal lenders/borrowers exist, "best" reference is the
# SOFR floor — D takes externals at-or-below floor (lender) / at-or-above (borrower).
#
# Hard physical limits on D:
D_RESERVE_USDC      = 2.0     # keep ≥$2 USDC for future SOFR refreshes
D_BORROW_MIN_WETH   = 0.0003  # need ≥this much WETH to attempt a borrow
D_MAX_DURATION_SEC  = 86400   # ≤24h on a single D trade (loosened from 1h)
D_INTENT_TTL_SEC    = 900     # D intents expire after 15 min if unmatched

# Legacy: kept for backwards-compat references in code below. Effectively zero.
D_MIN_SPREAD_BPS    = 0       # external policy: undercut to win, no spread
D_BORROW_INTENT_TTL_SEC = D_INTENT_TTL_SEC

# ─── D market-maker — standing depth at fixed size tiers ─────────────────────
# Beyond the reactive quoter functions above, D maintains standing offers at
# fixed dollar size tiers on BOTH sides of the book. This shows visible depth
# to external agents: "D will lend you $1, $3, or $5 at floor; D will borrow
# $1, $3, or $5 from you at up to floor+50bp". External takers match D's
# tier closest to their size.
#
# Skipped tiers (capacity-bound):
#   - Lender side skipped if deployable USDC < tier size
#   - Borrower side skipped if WETH-collateral < required at 0.55 LTV
# Each tier intent has the standard TTL — automatically refreshed each fire
# if matched or expired.
D_MAKER_SIZES_USDC  = [1.0, 3.0, 5.0]
D_MAKER_DURATIONS   = [900, 1200, 1800, 3600]  # 15/20/30/60 min — D keeps standing depth across ALL
D_MAKER_DURATION_SEC = 3600   # standing offer's max duration (lenders) /
                              # exact duration (borrowers). 1h is liquid.


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
    # Full loan struct getter — used by reconcile_external_loans() to read
    # borrower/lender/expiry/flags on-chain as the source of truth.
    {"name": "loans", "type": "function", "stateMutability": "view",
     "inputs":  [{"type": "bytes32"}],
     "outputs": [{"type": "address"}, {"type": "address"}, {"type": "address"},
                 {"type": "uint256"}, {"type": "address"}, {"type": "uint256"},
                 {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
                 {"type": "bool"}, {"type": "bool"}, {"type": "bool"}]},
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

def api_post_lender(wallet: Wallet, amount_usdc: float, min_rate_bps: int, max_duration_sec: int,
                    expires_at: int | None = None) -> dict:
    body = {
        "wallet": wallet.addr,
        "asset": "USDC",
        "amount": amount_usdc,
        "max_duration_sec": max_duration_sec,
        "min_rate_bps": min_rate_bps,
        "max_default_prob": 0.001,
    }
    if expires_at is not None:
        body["expires_at"] = int(expires_at)
    r = requests.post(f"{API_BASE}/v1/intent/lend", timeout=15, json=body)
    r.raise_for_status()
    return r.json()


def api_post_borrower(wallet: Wallet, principal: float, collat_max: float, duration_sec: int,
                      max_rate_bps: int, expires_at: int | None = None) -> dict:
    body = {
        "wallet": wallet.addr,
        "principal_asset": "USDC",
        "principal_amount": principal,
        "collateral_asset": "WETH",
        "collateral_amount_max": collat_max,
        "duration_sec": duration_sec,
        "max_rate_bps": max_rate_bps,
    }
    if expires_at is not None:
        body["expires_at"] = int(expires_at)
    r = requests.post(f"{API_BASE}/v1/intent/borrow", timeout=15, json=body)
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
    # limit must comfortably exceed all loans created during any active loan's
    # lifetime (<=1h) — else an externally-originated loan we owe scrolls out of
    # the window before reconcile_external_loans adopts it and we drift to
    # default (incident 2026-06-05: a runner-originated $1 loan defaulted because
    # limit=30 was too small). Active loans are always recent, so a large recent
    # window captures every one.
    r = requests.get(f"{API_BASE}/v1/loans/registry?limit=400", timeout=15)
    r.raise_for_status()
    return [l for l in r.json().get("loans", []) if l["status"] == "active"]


def api_open_book() -> dict:
    """Return {lenders, borrowers} — all currently open intents on both sides."""
    r = requests.get(f"{API_BASE}/v1/intents/open", timeout=15)
    r.raise_for_status()
    return r.json()


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


_eth_price_cache: dict = {"price": 0.0, "fetched_at": 0.0}

def fetch_live_eth_usd_for_collat() -> float:
    """Read Chainlink ETH/USD on Base — same feed V4 contract uses for LTV check.
    Cached 25s. Falls back to $2000 if RPC fails. Used to size borrower's
    collat_max so it doesn't get under-provisioned when ETH price moves between
    intent post and matcher tick."""
    now = time.time()
    if now - _eth_price_cache["fetched_at"] < 25 and _eth_price_cache["price"] > 0:
        return _eth_price_cache["price"]
    try:
        import json as _j, urllib.request as _u
        req = _u.Request(
            RPC_URL,
            data=_j.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_call",
                "params": [{"to": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
                            "data": "0xfeaf968c"}, "latest"],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        body = _j.loads(_u.urlopen(req, timeout=5).read())
        answer = int(body["result"][2:][64:128], 16)
        price = answer / 1e8
        if 500 < price < 100000:
            _eth_price_cache.update(price=price, fetched_at=now)
            return price
    except Exception:
        pass
    _eth_price_cache.update(price=2000.0, fetched_at=now)
    return 2000.0


def collat_for_principal(principal_usdc: float, eth_price_usd: float | None = None) -> float:
    """Post collat_max high enough to fit WORST-CASE regime (EXTREME → LTV cap 55%).
    The matcher will actually pull less based on the live regime, so generosity here
    just means "don't get rejected because of regime drift between book scan and match".
    Uses live Chainlink ETH price (cached 25s) — falls back to $2000 if RPC fails.
    1.10× buffer for ETH price moves between intent post and match."""
    if eth_price_usd is None:
        eth_price_usd = fetch_live_eth_usd_for_collat()
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


def act_quoter_d(wallets: list[Wallet], chain: Chain) -> None:
    """D as lender-of-last-resort against EXTERNAL borrowers.

    Scans the open order book, filters borrower intents whose wallet is NOT
    internal (B/C/D), and posts a competitive D lender intent if:
       - borrower.max_rate_bps ≥ SOFR_floor + D_MIN_SPREAD_BPS
       - principal fits D's capital - reserve
       - duration ≤ D_MAX_DURATION_SEC

    D posts at SOFR_floor + D_MIN_SPREAD_BPS (its own floor — undercuts other
    lenders posting at higher rates, but captures D's minimum spread). The
    matcher pairs at lender's min_rate, so the clearing rate IS D's floor.
    """
    d = next((w for w in wallets if w.name == DATA_BUYER_NAME), None)
    if not d:
        log("  D-quoter: no D wallet — skip")
        return

    bal = chain.balances(d.addr)
    deployable = bal["usdc"] - D_RESERVE_USDC
    if deployable < 0.5 or bal["eth"] < MIN_ETH_GAS:
        log(f"  D-quoter: underfunded — usdc=${bal['usdc']:.3f} (need ≥${D_RESERVE_USDC + 0.5}), eth={bal['eth']:.5f}")
        return

    # Don't compete if D already has an open lender intent
    try:
        book = api_open_book()
    except Exception as e:
        log(f"  D-quoter: failed to fetch book: {e}")
        return

    d_open_intents = [l for l in book.get("lenders", []) if l.get("wallet", "").lower() == d.addr.lower()]
    if d_open_intents:
        log(f"  D-quoter: already have {len(d_open_intents)} open lender intent(s) — skip")
        return

    internal = {w.addr.lower() for w in wallets}
    floor_bps, regime = sofr_floor_bps()

    # POLICY: D takes only externals that BEAT our best internal borrower bid.
    # If no internal borrowers in book, fallback threshold is the SOFR floor.
    our_borrower_bids = [int(b.get("max_rate_bps", -1)) for b in book.get("borrowers", [])
                          if b.get("wallet", "").lower() in internal]
    our_best_borrower_rate = max(our_borrower_bids) if our_borrower_bids else None
    # Threshold external must exceed to get D's priority
    priority_threshold = (our_best_borrower_rate
                          if our_best_borrower_rate is not None
                          else floor_bps - 1)  # -1 so equal-to-floor still beats
    # D quotes at floor — undercuts B/C (which are at floor+spread) while
    # remaining at the SOFR-fair price.
    d_floor_bps = floor_bps

    candidates = []
    for b in book.get("borrowers", []):
        if b.get("wallet", "").lower() in internal:
            continue
        if b.get("principal_asset") != "USDC":
            continue
        try:
            max_rate = int(b.get("max_rate_bps", 0))
            principal = float(b.get("principal_amount", 0))
            duration = int(b.get("duration_sec", 0))
        except (TypeError, ValueError):
            continue
        # Priority rule: external bid must strictly exceed our best internal
        # borrower bid (or floor-1 if none). At-or-worse → leave for normal FIFO.
        if max_rate <= priority_threshold:
            continue
        # Matcher will quote at d_floor_bps; check borrower still accepts it
        if max_rate < d_floor_bps:
            continue
        # Bounded by D's actual USDC capital, not by an arbitrary cap
        if principal <= 0 or principal > deployable:
            continue
        if duration <= 0 or duration > D_MAX_DURATION_SEC:
            continue
        candidates.append((max_rate, principal, duration, b))

    if not candidates:
        log(f"  D-quoter: no external borrowers beating our best bid "
            f"(threshold={priority_threshold}bps, "
            f"our_best_borrower={our_best_borrower_rate or 'none — using floor'})")
        return

    # Best candidate = highest max_rate (most cushion for D's quote to win)
    candidates.sort(reverse=True, key=lambda x: x[0])
    max_rate, principal, duration, target = candidates[0]
    target_wallet = target.get("wallet", "")[:10]

    log(f"→ D-QUOTE: external borrower {target_wallet}… wants ${principal:.2f} USDC "
        f"@ ≤{max_rate}bps for {duration}s (regime={regime}, floor={floor_bps}, "
        f"D quotes at {d_floor_bps}bps = floor+{D_MIN_SPREAD_BPS}bp spread)")

    # Pre-approve USDC for V4 — one-time per wallet, idempotent
    try:
        chain.ensure_allowance(d, chain.usdc, int(principal * 1e6))
    except Exception as e:
        log(f"  D-quoter: approve failed: {e}")
        return

    # Post D's lender intent at its floor. TTL short so we don't accumulate
    # stale D intents in the book if the borrower disappears.
    try:
        resp = api_post_lender(
            d,
            amount_usdc=principal,
            min_rate_bps=d_floor_bps,
            max_duration_sec=duration,
            expires_at=int(time.time()) + D_INTENT_TTL_SEC,
        )
    except Exception as e:
        log(f"  D-quoter: post failed: {e}")
        return

    intent_id = resp.get("intent_id", "?")
    match_id  = resp.get("matched")
    if match_id:
        log(f"  ✓ D matched as lender: intent_id={intent_id} match_id={match_id} "
            f"(clearing at floor {floor_bps}bps — D earns floor)")
        # If matched against external borrower, D originates the loan on-chain
        _try_originate_d_match(d, match_id, wallets, chain)
    else:
        log(f"  intent_id={intent_id} — posted at {d_floor_bps}bps, TTL {D_INTENT_TTL_SEC}s "
            f"(waiting for matcher cycle or new borrower)")


def act_borrower_quoter_d(wallets: list[Wallet], chain: Chain) -> None:
    """D as borrower-of-last-resort against EXTERNAL lenders.

    Symmetric to act_quoter_d (lender side). Scans open lender intents,
    filters out internal wallets, and if an external lender is offering USDC
    at a rate D can absorb (≤ floor + D_BORROW_RATE_CEILING_BPS), D posts a
    borrow intent for the same amount using its WETH as collateral.

    D's economics here are intentionally take-the-trade: D pays interest to
    the external lender, with no immediate productive use of the borrowed
    USDC. The protocol-level benefit is that organic external lender intents
    actually clear (vs. expiring unmatched), demonstrating the platform
    serves both sides of agent-to-agent traffic.

    Capital limits:
      - max $3 / loan (D_BORROW_MAX_USDC)
      - skip rates > floor + 30bp
      - skip if D's WETH < 0.0003 (under-collateralized)
      - 15-min TTL on D's borrow intent
    """
    d = next((w for w in wallets if w.name == DATA_BUYER_NAME), None)
    if not d:
        return

    bal = chain.balances(d.addr)
    if bal["weth"] < D_BORROW_MIN_WETH:
        log(f"  D-borrower: WETH too low ({bal['weth']:.5f} < {D_BORROW_MIN_WETH}) — skip")
        return
    if bal["eth"] < MIN_ETH_GAS:
        log(f"  D-borrower: gas too low ({bal['eth']:.5f}) — skip")
        return

    try:
        book = api_open_book()
    except Exception as e:
        log(f"  D-borrower: book fetch failed: {e}")
        return

    # Don't compete with self
    d_open_borrows = [b for b in book.get("borrowers", [])
                      if b.get("wallet", "").lower() == d.addr.lower()]
    if d_open_borrows:
        log(f"  D-borrower: already have {len(d_open_borrows)} open borrow intent(s) — skip")
        return

    internal = {w.addr.lower() for w in wallets}
    floor_bps, regime = sofr_floor_bps()
    eth_price = fetch_live_eth_usd_for_collat()

    # POLICY: D takes only externals that BEAT (undercut) our best internal
    # lender ask. If no internal lenders, fallback threshold is the SOFR floor.
    our_lender_asks = [int(l.get("min_rate_bps", 10**9)) for l in book.get("lenders", [])
                        if l.get("wallet", "").lower() in internal]
    our_best_lender_rate = min(our_lender_asks) if our_lender_asks else None
    # Threshold external must beat (be strictly lower) to get D's priority
    priority_threshold = (our_best_lender_rate
                          if our_best_lender_rate is not None
                          else floor_bps + 1)  # +1 so equal-to-floor still beats

    candidates = []
    for l in book.get("lenders", []):
        if l.get("wallet", "").lower() in internal:
            continue
        if l.get("asset") != "USDC":
            continue
        try:
            min_rate = int(l.get("min_rate_bps", 0))
            amount = float(l.get("amount", 0))
            max_dur = int(l.get("max_duration_sec", 0))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        if max_dur < 300:
            continue
        # Priority rule: external ask must strictly undercut our best internal
        # lender ask. At-or-worse → leave for FIFO (B/C cheaper, match them first).
        if min_rate >= priority_threshold:
            continue
        # Bounded by D's actual WETH (worst-case 0.55 LTV + 10% safety buffer)
        collat_needed = amount / 0.55 / eth_price * 1.10
        if collat_needed > bal["weth"] * 0.9:
            continue
        candidates.append((min_rate, amount, max_dur, collat_needed, l))

    if not candidates:
        log(f"  D-borrower: no external lender intents undercutting our best "
            f"(threshold={priority_threshold}bps, "
            f"our_best_lender={our_best_lender_rate or 'none — using floor'}, "
            f"collat-fittable in {bal['weth']:.5f} WETH)")
        return

    # Best candidate = lowest min_rate (the most aggressive undercut)
    candidates.sort(key=lambda x: x[0])
    min_rate, amount, max_dur, collat_needed, target = candidates[0]
    target_wallet = target.get("wallet", "")[:10]
    # Per policy: adjust D's duration to external's max_duration (capped by D's
    # safety limit). External shouldn't have to wait for matching duration.
    duration = min(max_dur, D_MAX_DURATION_SEC)

    # D's max_rate ceiling: be generous so matcher's floor-clamp + any drift
    # between intent post and match can't reject. Hard cap at 50% APR sanity.
    d_max_rate = max(min_rate, floor_bps) + 50

    log(f"→ D-BORROW: external lender {target_wallet}… offers ${amount:.2f} USDC "
        f"@ ≥{min_rate}bps for ≤{max_dur}s (undercuts our best={our_best_lender_rate or 'floor'}). "
        f"D borrows ${amount:.2f} @ ≤{d_max_rate}bps for {duration}s, "
        f"collat ≤ {collat_needed:.5f} WETH (have {bal['weth']:.5f})")

    # Pre-approve: WETH for V4 collateral, USDC for repay
    try:
        chain.ensure_allowance(d, chain.weth, int(collat_needed * 1e18))
        chain.ensure_allowance(d, chain.usdc, int(amount * 1.02 * 1e6))
    except Exception as e:
        log(f"  D-borrower: approve failed: {e}")
        return

    try:
        resp = api_post_borrower(
            d,
            principal=amount,
            collat_max=collat_needed,
            duration_sec=duration,
            max_rate_bps=d_max_rate,
            expires_at=int(time.time()) + D_BORROW_INTENT_TTL_SEC,
        )
    except Exception as e:
        log(f"  D-borrower: post failed: {e}")
        return

    intent_id = resp.get("intent_id", "?")
    match_id  = resp.get("matched")
    if match_id:
        log(f"  ✓ D matched as borrower: intent_id={intent_id} match_id={match_id}")
        # Step 3: D needs to originate the loan on-chain
        _try_originate_d_match(d, match_id, wallets, chain)
    else:
        log(f"  intent_id={intent_id} — posted, TTL {D_BORROW_INTENT_TTL_SEC}s "
            f"(waiting for matcher or external borrower to step in)")


def act_market_make_d(wallets: list[Wallet], chain: Chain) -> None:
    """Standing market depth at D_MAKER_SIZES_USDC tiers, both sides.

    Each fire: check which of $1 / $3 / $5 tiers D doesn't have open intents
    for, and post fresh ones at SOFR floor (lender side) / floor+50bp ceiling
    (borrower side). Capacity-bound — skip tiers that don't fit D's remaining
    deployable USDC (lender) or WETH (borrower).

    Standing depth means external takers always see liquidity at multiple
    size points. Matcher's internal-wallet guard ensures these only match
    against EXTERNAL counter-parties — B/C internal flow is untouched.
    """
    d = next((w for w in wallets if w.name == DATA_BUYER_NAME), None)
    if not d:
        return

    bal = chain.balances(d.addr)
    if bal["eth"] < MIN_ETH_GAS:
        log(f"  D-maker: gas too low ({bal['eth']:.5f}) — skip refresh")
        return

    try:
        book = api_open_book()
    except Exception as e:
        log(f"  D-maker: book fetch failed: {e}")
        return

    floor_bps, regime = sofr_floor_bps()
    eth_price = fetch_live_eth_usd_for_collat()

    # ─── Lender side: post tiers D doesn't have, capped by USDC ─────────────
    d_lender_pairs = {
        (round(float(l.get("amount", 0)), 6), int(l.get("max_duration_sec", 0)))
        for l in book.get("lenders", [])
        if l.get("wallet", "").lower() == d.addr.lower()
    }
    d_lender_sizes = sorted({a for a, _ in d_lender_pairs})  # for the summary log below
    usdc_used     = sum(a for a, _ in d_lender_pairs)
    usdc_avail    = max(0.0, bal["usdc"] - D_RESERVE_USDC - usdc_used)
    posted_lender = []
    for sz in D_MAKER_SIZES_USDC:
        for dur in D_MAKER_DURATIONS:
            # Skip if D already has an offer at exactly this (size, duration)
            if any(abs(s - sz) < 0.001 and dd == dur for s, dd in d_lender_pairs):
                continue
            if usdc_avail < sz:
                continue  # no deployable USDC left for this size
            try:
                chain.ensure_allowance(d, chain.usdc, int(sz * 1e6))  # idempotent
            except Exception as e:
                log(f"  D-maker: lender ${sz} approve failed: {e}")
                continue
            try:
                resp = api_post_lender(
                    d, amount_usdc=sz,
                    min_rate_bps=floor_bps,
                    max_duration_sec=dur,
                    expires_at=int(time.time()) + D_INTENT_TTL_SEC,
                )
            except Exception as e:
                log(f"  D-maker: lender ${sz}/{dur}s post failed: {e}")
                continue
            usdc_avail -= sz
            match_id = resp.get("matched")
            posted_lender.append((sz, resp.get("intent_id"), match_id))
            if match_id:
                _try_originate_d_match(d, match_id, wallets, chain)

    # ─── Borrower side: post tiers D doesn't have, capped by WETH ───────────
    d_borrower_pairs = {
        (round(float(b.get("principal_amount", 0)), 6), int(b.get("duration_sec", 0)))
        for b in book.get("borrowers", [])
        if b.get("wallet", "").lower() == d.addr.lower()
    }
    d_borrower_sizes = sorted({a for a, _ in d_borrower_pairs})  # for the summary log below
    weth_used = sum(a / 0.55 / eth_price * 1.10 for a, _ in d_borrower_pairs)
    weth_avail = max(0.0, bal["weth"] * 0.9 - weth_used)
    d_max_rate = floor_bps + 50
    posted_borrower = []
    for sz in D_MAKER_SIZES_USDC:
        collat = sz / 0.55 / eth_price * 1.10
        for dur in D_MAKER_DURATIONS:
            # Skip if D already has a borrow offer at exactly this (size, duration)
            if any(abs(s - sz) < 0.001 and dd == dur for s, dd in d_borrower_pairs):
                continue
            if weth_avail < collat:
                continue  # not enough deployable WETH for this size
            try:
                chain.ensure_allowance(d, chain.weth, int(collat * 1e18))   # idempotent
                chain.ensure_allowance(d, chain.usdc, int(sz * 1.02 * 1e6))
            except Exception as e:
                log(f"  D-maker: borrow ${sz} approve failed: {e}")
                continue
            try:
                resp = api_post_borrower(
                    d, principal=sz, collat_max=collat,
                    duration_sec=dur,
                    max_rate_bps=d_max_rate,
                    expires_at=int(time.time()) + D_BORROW_INTENT_TTL_SEC,
                )
            except Exception as e:
                log(f"  D-maker: borrow ${sz}/{dur}s post failed: {e}")
                continue
            weth_avail -= collat
            match_id = resp.get("matched")
            posted_borrower.append((sz, resp.get("intent_id"), match_id))
            if match_id:
                _try_originate_d_match(d, match_id, wallets, chain)

    # Summary log
    if posted_lender or posted_borrower:
        l_summary = ", ".join(
            f"${sz}{'⚡'+m[:8] if m else ''}"
            for sz, _, m in posted_lender
        ) or "—"
        b_summary = ", ".join(
            f"${sz}{'⚡'+m[:8] if m else ''}"
            for sz, _, m in posted_borrower
        ) or "—"
        log(f"  D-maker: lender posts [{l_summary}], borrow posts [{b_summary}] "
            f"(floor={floor_bps}bps, regime={regime})")
    else:
        existing_l = ",".join(f"${s}" for s in d_lender_sizes) or "none"
        existing_b = ",".join(f"${s}" for s in d_borrower_sizes) or "none"
        log(f"  D-maker: book already has D depth — lenders [{existing_l}] borrowers [{existing_b}]")


def _try_originate_d_match(d: Wallet, match_id: str, wallets: list[Wallet], chain: Chain) -> None:
    """When D is matched against an external counter-party, D calls originate()
    on-chain. The signed quote is fetched from the API, allowances are confirmed
    on D's side, then D submits originate(). The external counter-party already
    approved their side (e.g. external lender approved USDC for V4 yesterday)."""
    time.sleep(2)  # let DB write settle
    try:
        m = api_fetch_match(match_id)
    except Exception as e:
        log(f"  ⚠ D-originate: fetch match {match_id} failed: {e}")
        return
    if not m:
        log(f"  ⚠ D-originate: match {match_id} not in /v1/matches/recent")
        return

    q = m["quote"]["quote"]
    sig = m["quote"]["signature"]
    d_addr_lo = d.addr.lower()
    d_is_borrower = q["borrower"].lower() == d_addr_lo
    d_is_lender   = q["lender"].lower()   == d_addr_lo
    if not (d_is_borrower or d_is_lender):
        log(f"  ⚠ D-originate: match doesn't involve D, skip")
        return

    # Ensure allowances on D's side
    try:
        if d_is_borrower:
            chain.ensure_allowance(d, chain.weth, int(q["collateralAmount"]))
            chain.ensure_allowance(d, chain.usdc, int(int(q["principalAmount"]) * 1.02))
        else:  # d_is_lender
            chain.ensure_allowance(d, chain.usdc, int(q["principalAmount"]))
    except Exception as e:
        log(f"  ⚠ D-originate: approve failed: {e}")
        return

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

    log(f"  D originating loan with external {'lender' if d_is_borrower else 'borrower'} "
        f"({q['lender' if d_is_borrower else 'borrower'][:14]}…), "
        f"principal=${int(q['principalAmount'])/1e6:.4f} rate={q['rateBps']}bps")
    try:
        tx = chain.send(d.account, chain.repo.functions.originate(quote_tuple, sig_bytes), gas=600_000)
    except Exception as e:
        log(f"  ✗ D-originate: send failed: {e}")
        return
    log(f"  originate tx: https://basescan.org/tx/{tx}")
    if not chain.wait(tx):
        log(f"  ✗ D-originate: reverted on-chain")
        return
    log(f"  ✓ loan opened: loanId={q['nonce']}")

    # Track in state.json so the repay cycle handles it
    state = state_load()
    now_ts = int(time.time())
    state["our_loans"].append({
        "loan_id":     q["nonce"],
        "lender":      d.name if d_is_lender else "EXTERNAL",
        "borrower":    d.name if d_is_borrower else "EXTERNAL",
        "principal":   int(q["principalAmount"]) / 1e6,
        "rate_bps":    int(q["rateBps"]),
        "originated":  now_ts,
        "repay_after": now_ts + int(q["expiryTimestamp"] - now_ts) // 2,
        "expiry":      int(q["expiryTimestamp"]),
    })
    state_save(state)


def _expiry_of(loan: dict) -> int:
    """Get expiry timestamp; fallback for legacy entries that don't store it."""
    if "expiry" in loan:
        return int(loan["expiry"])
    # Legacy fallback: assume max 30-min duration → originated + 1800s
    return int(loan["originated"]) + 1800


def reconcile_external_loans(wallets: list[Wallet], chain: Chain) -> None:
    """Adopt loans where WE are the borrower but an EXTERNAL counterparty called
    originate(), so our normal post-originate tracking never recorded them.

    act_paired (and the D-quoters) `return` early when a match involves an
    external wallet — "let the other party originate" — and therefore never
    append the loan to state["our_loans"]. Such loans are then invisible to
    act_repay, and we silently drift past expiry into default.
    (Incident 2026-05-31: loan 0xdc5af02b… expired untracked, borrower=WALLET_B.)

    The API loan-registry is used only to ENUMERATE candidate loanIds; the CHAIN
    (loans() getter) is the source of truth for borrower/expiry/flags — we never
    trust the API for a repay decision. Only borrower-side loans are adopted:
    those are the ones WE must repay. Lender-side external loans need no action
    from us here (the borrower repays us; on their default anyone may settle)."""
    our = {w.addr.lower(): w for w in wallets}
    state = state_load()
    known = {l["loan_id"].lower() for l in state["our_loans"]}
    try:
        registry = api_active_loans()
    except Exception as e:
        log(f"  reconcile: registry fetch failed ({type(e).__name__}) — skip this fire")
        return

    adopted = 0
    now_ts = int(time.time())
    for rec in registry:
        lid = rec.get("loan_id")
        if not lid or lid.lower() in known:
            continue
        if (rec.get("borrower") or "").lower() not in our:
            continue  # only adopt loans WE must repay (we are the borrower)
        # Chain is source of truth — never trust the API registry for repay.
        try:
            ln = chain.repo.functions.loans(bytes.fromhex(lid[2:])).call()
        except Exception:
            continue
        borrower, lender = ln[0].lower(), ln[1].lower()
        expiry = int(ln[7])
        repaid, defaulted, liquidated = ln[9], ln[10], ln[11]
        if borrower not in our or repaid or defaulted or liquidated:
            continue  # not ours on-chain, or already closed
        state["our_loans"].append({
            "loan_id":     lid,
            "lender":      our[lender].name if lender in our else "EXTERNAL",
            "borrower":    our[borrower].name,
            "principal":   int(ln[3]) / 1e6,
            "rate_bps":    int(ln[8]),
            "originated":  now_ts,
            "repay_after": now_ts,    # discovered late → repay as soon as possible
            "expiry":      expiry,
            "adopted":     True,      # reconciled post-hoc, not bot-originated
        })
        known.add(lid.lower())
        adopted += 1
        ttl = expiry - now_ts
        log(f"  ⚠ ADOPTED untracked loan {lid[:14]}… borrower={our[borrower].name} "
            f"lender={our[lender].name if lender in our else 'EXTERNAL'} "
            f"owed≈${int(ln[3])/1e6:.4f} ttl={ttl}s — "
            f"{'PAST EXPIRY → act_repay will defaultLoan' if ttl <= 0 else 'now tracked for repay'}")
    if adopted:
        state_save(state)
        log(f"  reconcile: adopted {adopted} untracked borrower-side loan(s) into state")


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


# ─── Lender-side recovery ────────────────────────────────────────────────────

MAX_LENDER_CLAIMS_PER_FIRE = 3   # cap per fire; remaining zombies wait for next tick


def claim_lender_side_defaults(wallets: list[Wallet], chain: Chain) -> None:
    """Self-healing (LENDER side): when WE lent and an EXTERNAL borrower ran the
    loan past expiry without repaying, the collateral is ours to recover — but
    until now nothing claimed it. reconcile_external_loans() only adopts
    borrower-side loans (those WE owe) and act_repay() only repays/defaults
    those; lender-side defaults silently piled up.
    (Gap found 2026-06-02: loan 0x771d498c… sat ~7.5h unclaimed until a manual
    defaultLoan via default_one.py.)

    Sweep the registry for active-but-past-expiry loans where lender ∈ our
    wallets and the borrower is external; verify on-chain (loans() getter is the
    source of truth — never trust the API for a settle decision); then call
    defaultLoan(). The Aave-style split returns debt-equivalent collateral to us
    (the lender) + a 3% bounty to the caller (also us). This is the in-cycle
    version of the one-off default_one.py. Capped per fire to avoid nonce floods."""
    our = {w.addr.lower(): w for w in wallets}
    try:
        registry = api_active_loans()
    except Exception as e:
        log(f"  lender-claim: registry fetch failed ({type(e).__name__}) — skip this fire")
        return
    now_ts = int(time.time())
    state = state_load()
    claimed = 0
    for rec in registry:
        if claimed >= MAX_LENDER_CLAIMS_PER_FIRE:
            break
        lid = rec.get("loan_id")
        if not lid:
            continue
        # Only loans where WE are the lender and the borrower is EXTERNAL.
        if (rec.get("lender") or "").lower() not in our:
            continue
        if (rec.get("borrower") or "").lower() in our:
            continue  # internal↔internal default is handled on the borrower side
        # Chain is source of truth.
        try:
            ln = chain.repo.functions.loans(bytes.fromhex(lid[2:])).call()
        except Exception:
            continue
        lender = ln[1].lower()
        expiry = int(ln[7])
        repaid, defaulted, liquidated = ln[9], ln[10], ln[11]
        if lender not in our or repaid or defaulted or liquidated:
            continue          # not ours on-chain, or already closed
        if now_ts <= expiry:
            continue          # not past expiry yet — defaultLoan would revert
        log(f"  ⚠ LENDER-CLAIM: loan {lid[:14]}… lender={our[lender].name} "
            f"borrower=EXTERNAL principal≈${int(ln[3])/1e6:.4f}, "
            f"{now_ts - expiry}s past expiry → defaultLoan() to recover collateral")
        _try_default({"loan_id": lid}, wallets, chain, state)
        claimed += 1
    if claimed:
        log(f"  lender-claim: settled {claimed} defaulted lender-side loan(s) this fire")


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
        role = "data-buyer+quoter" if w.name == DATA_BUYER_NAME else ("executor" if w.name in EXECUTOR_NAMES else "?")
        log(f"  {w.name} ({role}) {w.addr[:8]}…  ETH={b['eth']:.5f}  USDC=${b['usdc']:.3f}  WETH={b['weth']:.5f}")

    # First: refresh Agent-SOFR cache if stale (D pays $0.001 via x402)
    try:
        refresh_sofr_if_stale(wallets, chain)
    except Exception as e:
        log(f"  ✗ SOFR refresh errored: {e}")

    floor, regime = sofr_floor_bps()
    log(f"  current SOFR floor: {floor} bps (regime={regime})")

    # D-quoter passes: D acts as market maker on BOTH sides of organic flow.
    # Three layers, run in priority order:
    #   1. Reactive lender (act_quoter_d): catch externals undercutting our best
    #      borrower bid — D mirrors their size and matches.
    #   2. Reactive borrower (act_borrower_quoter_d): catch externals undercutting
    #      our best lender ask — D mirrors their size and matches.
    #   3. Standing market (act_market_make_d): maintain $1/$3/$5 depth tiers
    #      on both sides at floor (lender) and floor+50bp ceiling (borrower).
    #      Refreshed each fire as tiers get matched or expire.
    try:
        act_quoter_d(wallets, chain)
    except Exception as e:
        log(f"  ✗ D-lender-quoter errored: {e}")
    try:
        act_borrower_quoter_d(wallets, chain)
    except Exception as e:
        log(f"  ✗ D-borrower-quoter errored: {e}")
    try:
        act_market_make_d(wallets, chain)
    except Exception as e:
        log(f"  ✗ D-market-maker errored: {e}")

    # Adopt any externally-originated loans where WE are the borrower. Our
    # post-originate tracking misses those (act_paired/D-quoters return early on
    # an external match), so reconcile them into state BEFORE the repay sweep —
    # otherwise they stay invisible and we drift to default. (Incident 2026-05-31.)
    try:
        reconcile_external_loans(wallets, chain)
    except Exception as e:
        log(f"  ✗ reconcile errored: {e}")

    # Lender side of the same coin: settle loans WE funded where an external
    # borrower defaulted (ran past expiry unrepaid). reconcile above only adopts
    # borrower-side loans, so without this these collateral claims sit forever.
    try:
        claim_lender_side_defaults(wallets, chain)
    except Exception as e:
        log(f"  ✗ lender-claim errored: {e}")

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
