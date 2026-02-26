"""
Rapid Rug Filter - Solana Meme Coin Structural Rug Risk Scanner
Production-grade REST API microservice for OnDemand agent tool calling.

v5.0.0 — Portfolio Auto-Pilot + Continuous Wallet Monitoring.
Zero-agent post-buy protection: auto-detects new token purchases in wallet,
runs analyze → watch → surveillance → auto-sell pipeline automatically.
Portfolio scanner thread monitors wallet holdings every 10s as source of truth.

Built on v4.1.0 — Auto-Sell Guardian + Priority Fee Bribe + LP Analysis.
Uses Solana JSON-RPC via Helius (Business), Jupiter Swap API for auto-sell.
No RapidAPI dependency — calls Solana chain nodes directly for speed.
"""

import os
import time
import json
import struct
import base64
import threading
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections import deque
from flask import Flask, request, jsonify

import requests as http_requests

try:
    from solders.keypair import Keypair as SoldersKeypair
    from solders.transaction import VersionedTransaction as SoldersVersionedTx
    SOLDERS_AVAILABLE = True
except ImportError:
    SOLDERS_AVAILABLE = False
    print("[WARN] solders not available — auto-sell disabled")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

CACHE_TTL_SECONDS = 20
SNAPSHOT_TTL_SECONDS = 120

_account_cache = {}
_snapshot_cache = {}

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Known system / program addresses to exclude from dev detection
SYSTEM_ADDRESSES = {
    "11111111111111111111111111111111",
    TOKEN_PROGRAM_ID,
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # ATA program
    "SysvarRent111111111111111111111111111111111",
    "SysvarC1ock11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",  # Metaplex
    "TSWAPaqyCSx2KABk68Shruf4rp7CxcNi8hAsbdwmHbN",  # Tensor
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun program
}

# ---------------------------------------------------------------------------
# Surveillance Configuration (v3.0)
# ---------------------------------------------------------------------------
MONITOR_DEFAULT_INTERVAL = 10    # seconds between polls per mint
MONITOR_MAX_MINTS = 25           # max concurrent watches (Helius Business tier)
MONITOR_HISTORY_MAXLEN = 60      # rolling snapshot history (~10 min at 10s)

# Dev dump thresholds
DEV_DUMP_THRESHOLD_PCT = 5.0       # 5% drop from entry within 5 min → HIGH
DEV_SEVERE_DUMP_THRESHOLD_PCT = 10.0  # 10% drop from entry at any time → EXTREME
DEV_FANOUT_MIN_WALLETS = 3        # dev sends to 3+ wallets within 5 min → HIGH

# Whale thresholds
WHALE_TOP1_DUMP_SUPPLY_PCT = 2.0   # top1 holder loses 2% of supply in 5 min → HIGH
WHALE_ANY_DUMP_SUPPLY_PCT = 1.0    # any top10 loses 1% of supply in 5 min → MODERATE
WHALE_AGG_DROP_PCT = 5.0           # sum(topN) drops 5% from entry in 10 min → HIGH
WHALE_FANOUT_MIN_WALLETS = 3       # any whale sends to 3+ wallets in 5 min → MODERATE

# Known exchange deposit addresses (Solana hot wallets)
KNOWN_EXCHANGE_ADDRESSES = {
    # Binance
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    # Coinbase
    "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm",
    # OKX
    "5VCwKtCXgCJ6kit5FybXjvFnPe2FKEV4NMF4gD5MiSyn",
    "JBGUGVkCYBe24KMTGE2TvoEU1EBJHvTGPDqnNJKbLXiW",
    # Bybit
    "AC5RDfQFmDS1deWZos921JfqscXdByf2BqcRbZES4VVk",
    # Kraken
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5",
    "CnXhMid6m8FKM9u7o95qXdZtaXq3yJLcQB3Uq2hTj2sM",
}

# ---------------------------------------------------------------------------
# Jupiter Swap Configuration (v4.0)
# ---------------------------------------------------------------------------
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"
AUTO_SELL_SEVERITY_THRESHOLD = "HIGH"  # Minimum severity to trigger auto-sell
AUTO_SELL_DEFAULT_SLIPPAGE_BPS = 1200  # 12% emergency slippage for auto-sell
SEVERITY_ORDER = {"NONE": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3, "EXTREME": 4}

# ---------------------------------------------------------------------------
# Priority Fee Profiles (v4.1) — "Bribe" tactic for rug survival
# ---------------------------------------------------------------------------
# NORMAL profile: standard priority, reasonable slippage
NORMAL_SLIPPAGE_BPS = 500              # 5%
NORMAL_CU_PRICE_MICRO_LAMPORTS = 0     # let Jupiter auto-optimize

# EMERGENCY profile: maximum priority, wide slippage, aggressive retries
EMERGENCY_SLIPPAGE_BPS = 1200          # 12% panic slippage
EMERGENCY_CU_PRICE_MICRO_LAMPORTS = 200_000  # high bribe per CU
EMERGENCY_MAX_RETRIES = 3
EMERGENCY_RETRY_DELAYS = [0.2, 0.5, 1.0]  # exponential backoff seconds

# Idempotency: track executed emergency sells to prevent duplicates
_emergency_event_ids = set()
_emergency_event_ids_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Liquidity Pool Analysis Configuration (v4.0)
# ---------------------------------------------------------------------------
RAYDIUM_AMM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_PROGRAM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
LP_BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",  # Solana incinerator
    "11111111111111111111111111111111",              # System program
}

# ---------------------------------------------------------------------------
# Portfolio Auto-Pilot Configuration (v5.0)
# ---------------------------------------------------------------------------
PORTFOLIO_SCAN_INTERVAL = 10         # seconds between wallet scans
PORTFOLIO_WATCH_STAGGER_DELAY = 1.0  # seconds between auto-watch setups
PORTFOLIO_EVENTS_MAXLEN = 500        # max events in portfolio event log
PORTFOLIO_MAX_HOLDINGS = 30          # max tokens to track simultaneously

# Tokens to NEVER watch (stablecoins, wrapped SOL)
PORTFOLIO_IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",     # Wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# ---------------------------------------------------------------------------
# Surveillance Global State (v3.0)
# ---------------------------------------------------------------------------
_watched_mints = {}            # mint -> entry dict
_watched_mints_lock = threading.Lock()
_global_alerts = deque(maxlen=200)
_global_alerts_lock = threading.Lock()
_monitor_thread = None
_monitor_stop_event = threading.Event()

# ---------------------------------------------------------------------------
# Portfolio Auto-Pilot Global State (v5.0)
# ---------------------------------------------------------------------------
_portfolio_state = {}               # single portfolio state dict (empty = inactive)
_portfolio_state_lock = threading.Lock()
_portfolio_thread = None
_portfolio_stop_event = threading.Event()


# ---------------------------------------------------------------------------
# Solana JSON-RPC Client
# ---------------------------------------------------------------------------
def _rpc_call(method, params, timeout=12, retries=2):
    """Make a Solana JSON-RPC call with retry on rate limit."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    for attempt in range(retries + 1):
        try:
            resp = http_requests.post(
                SOLANA_RPC_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code == 429:
                if attempt < retries:
                    time.sleep(0.8 * (attempt + 1))  # Exponential backoff
                    continue
                return {"error": "rate_limited", "detail": "Solana RPC rate limit hit"}
            if resp.status_code != 200:
                return {"error": f"http_{resp.status_code}", "detail": resp.text[:500]}
            data = resp.json()
            if "error" in data:
                err = data["error"]
                # Retry on server errors
                if isinstance(err, dict) and err.get("code", 0) in (-32005, -32009):
                    if attempt < retries:
                        time.sleep(0.5)
                        continue
                return {"error": "rpc_error", "detail": err}
            return data
        except http_requests.exceptions.Timeout:
            if attempt < retries:
                continue
            return {"error": "timeout", "detail": f"RPC call {method} timed out"}
        except Exception as e:
            return {"error": "request_failed", "detail": str(e)[:300]}


def _cache_get(cache, key, ttl):
    entry = cache.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _cache_set(cache, key, data):
    cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# Data Fetchers
# ---------------------------------------------------------------------------
def fetch_account_info(pubkey):
    """getAccountInfo with jsonParsed encoding."""
    cache_key = f"acct:{pubkey}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rpc_call("getAccountInfo", [
        pubkey,
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_token_accounts_by_owner(owner, mint=None, bypass_cache=False):
    """getTokenAccountsByOwner with optional mint filter.
    bypass_cache=True skips cache lookup (used by portfolio scanner for fresh data)."""
    cache_key = f"tao:{owner}:{mint or 'all'}"
    if not bypass_cache:
        cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
        if cached is not None:
            return cached
    filter_obj = {"programId": TOKEN_PROGRAM_ID}
    if mint:
        filter_obj = {"mint": mint}
    data = _rpc_call("getTokenAccountsByOwner", [
        owner,
        filter_obj,
        {"encoding": "jsonParsed", "commitment": "confirmed"}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_signatures_for_address(address, limit=15, before=None):
    """getSignaturesForAddress — list recent tx signatures."""
    cache_key = f"sigs:{address}:{limit}:{before or ''}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    opts = {"limit": limit, "commitment": "confirmed"}
    if before:
        opts["before"] = before
    data = _rpc_call("getSignaturesForAddress", [address, opts])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_transaction(signature):
    """getTransaction with jsonParsed encoding."""
    cache_key = f"tx:{signature}"
    cached = _cache_get(_account_cache, cache_key, 120)
    if cached is not None:
        return cached
    data = _rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "commitment": "confirmed",
         "maxSupportedTransactionVersion": 0}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_token_largest_accounts(mint_pubkey):
    """getTokenLargestAccounts — returns top 20 holders for a mint."""
    cache_key = f"largest:{mint_pubkey}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rpc_call("getTokenLargestAccounts", [
        mint_pubkey,
        {"commitment": "confirmed"}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


# ---------------------------------------------------------------------------
# Holder Concentration Analysis (NEW in v2.0)
# ---------------------------------------------------------------------------
def analyze_holder_concentration(mint, supply_str):
    """
    Analyze top holder concentration using getTokenLargestAccounts.
    Returns concentration metrics and risk signals.
    """
    result = {
        "top1_pct": None,
        "top5_pct": None,
        "top10_pct": None,
        "top20_pct": None,
        "num_holders_above_1pct": 0,
        "largest_holder_address": None,
        "largest_holder_amount": None,
        "concentration_score": 0,
        "signals": [],
        "error": None,
    }

    if not supply_str:
        result["error"] = "no_supply_data"
        return result

    try:
        total_supply = int(supply_str)
    except (ValueError, TypeError):
        result["error"] = "invalid_supply"
        return result

    if total_supply <= 0:
        result["error"] = "zero_supply"
        return result

    raw = fetch_token_largest_accounts(mint)
    if not raw or "error" in raw:
        result["error"] = raw.get("error", "fetch_failed") if raw else "null_response"
        return result

    holders = raw.get("result", {}).get("value", [])
    if not holders:
        result["error"] = "no_holders"
        return result

    # Parse holder amounts
    holder_pcts = []
    for h in holders:
        try:
            amount = int(h.get("amount", "0"))
            pct = (amount / total_supply) * 100
            holder_pcts.append({
                "address": h.get("address", ""),
                "amount": amount,
                "pct": round(pct, 2),
            })
        except (ValueError, TypeError):
            continue

    # Sort by amount descending
    holder_pcts.sort(key=lambda x: x["amount"], reverse=True)

    if holder_pcts:
        result["largest_holder_address"] = holder_pcts[0]["address"]
        result["largest_holder_amount"] = str(holder_pcts[0]["amount"])

    # Calculate concentration metrics
    top1 = holder_pcts[0]["pct"] if len(holder_pcts) >= 1 else 0
    top5 = sum(h["pct"] for h in holder_pcts[:5])
    top10 = sum(h["pct"] for h in holder_pcts[:10])
    top20 = sum(h["pct"] for h in holder_pcts[:20])
    big_holders = sum(1 for h in holder_pcts if h["pct"] > 1.0)

    result["top1_pct"] = round(top1, 2)
    result["top5_pct"] = round(top5, 2)
    result["top10_pct"] = round(top10, 2)
    result["top20_pct"] = round(top20, 2)
    result["num_holders_above_1pct"] = big_holders

    # Score concentration risk
    conc_score = 0

    if top1 > 30:
        conc_score += 15
        result["signals"].append(
            f"CRITICAL: Top 1 holder owns {top1:.1f}% of supply (>30%, +15)"
        )
    elif top1 > 15:
        conc_score += 10
        result["signals"].append(
            f"HIGH: Top 1 holder owns {top1:.1f}% of supply (>15%, +10)"
        )
    elif top1 > 8:
        conc_score += 5
        result["signals"].append(
            f"MODERATE: Top 1 holder owns {top1:.1f}% of supply (>8%, +5)"
        )

    if top5 > 50:
        conc_score += 10
        result["signals"].append(
            f"HIGH: Top 5 holders own {top5:.1f}% of supply (>50%, +10)"
        )
    elif top5 > 35:
        conc_score += 5
        result["signals"].append(
            f"MODERATE: Top 5 holders own {top5:.1f}% of supply (>35%, +5)"
        )

    if top10 > 75:
        conc_score += 5
        result["signals"].append(
            f"HIGH: Top 10 holders own {top10:.1f}% of supply (>75%, +5)"
        )

    result["concentration_score"] = conc_score
    return result


# ---------------------------------------------------------------------------
# Dev Wallet Detection
# ---------------------------------------------------------------------------
def detect_dev_wallet(mint, mint_info):
    """
    Auto-detect the probable developer wallet from a mint address.

    Strategy (ordered by signal strength):
      1. If mintAuthority is still active → that wallet is the dev
      2. Find earliest transaction on the mint → extract signer (deployer)
      3. In earliest tx, find mintTo/initializeMint instructions → get
         destination token account owner (first token recipient)

    Returns dict with:
      - probable_dev_wallet: str or None
      - detection_method: str describing how it was found
      - confidence: "high" | "medium" | "low"
      - detection_notes: list of strings
    """
    result = {
        "probable_dev_wallet": None,
        "detection_method": None,
        "confidence": None,
        "detection_notes": [],
        "deployer_wallet": None,
        "first_token_recipient": None,
        "creation_blocktime": None,  # v2.0: for token age scoring
    }

    # --- Method 1: Active mint authority ---
    mint_authority = mint_info.get("mint_authority")
    if mint_authority and mint_authority not in SYSTEM_ADDRESSES:
        result["probable_dev_wallet"] = mint_authority
        result["detection_method"] = "active_mint_authority"
        result["confidence"] = "high"
        result["detection_notes"].append(
            f"Mint authority is still active: {mint_authority}"
        )
        # Still try to find deployer for extra intel
        _enrich_with_earliest_tx(mint, result)
        return result

    # --- Method 2 & 3: Find earliest transaction ---
    _enrich_with_earliest_tx(mint, result)

    # Pick best candidate
    if result["deployer_wallet"]:
        result["probable_dev_wallet"] = result["deployer_wallet"]
        result["detection_method"] = "earliest_tx_signer"
        result["confidence"] = "high"
        result["detection_notes"].append(
            f"Deployer (earliest tx signer): {result['deployer_wallet']}"
        )
    elif result["first_token_recipient"]:
        result["probable_dev_wallet"] = result["first_token_recipient"]
        result["detection_method"] = "first_token_recipient"
        result["confidence"] = "medium"
        result["detection_notes"].append(
            f"First token recipient: {result['first_token_recipient']}"
        )

    if not result["probable_dev_wallet"]:
        result["detection_method"] = "none"
        result["confidence"] = "none"
        result["detection_notes"].append(
            "Could not detect dev wallet — no early tx data available"
        )

    return result


def _enrich_with_earliest_tx(mint, result):
    """
    Walk backward through mint signatures to find the earliest transaction.
    Extract: deployer (signer) and first token recipient (mintTo destination).
    """
    # Get signatures — walk backward to find the very first one
    all_sigs = []
    before = None
    for _ in range(3):  # Max 3 pages to avoid rate limits
        sigs_raw = fetch_signatures_for_address(mint, limit=50, before=before)
        if not sigs_raw or "error" in sigs_raw:
            break
        batch = sigs_raw.get("result", [])
        if not batch:
            break
        all_sigs.extend(batch)
        if len(batch) < 50:
            break  # Reached the beginning
        before = batch[-1].get("signature")

    if not all_sigs:
        result["detection_notes"].append("No signatures found for mint")
        return

    # The last entry in all_sigs (oldest) is the earliest transaction
    earliest_entry = all_sigs[-1]
    earliest_sig = ""
    if isinstance(earliest_entry, dict):
        earliest_sig = earliest_entry.get("signature", "")
        # v2.0: capture blockTime for token age
        block_time = earliest_entry.get("blockTime")
        if block_time:
            result["creation_blocktime"] = block_time
    elif isinstance(earliest_entry, str):
        earliest_sig = earliest_entry

    if not earliest_sig:
        result["detection_notes"].append("Could not extract earliest signature")
        return

    result["detection_notes"].append(f"Earliest tx: {earliest_sig[:16]}...")

    # Fetch the earliest transaction
    tx_raw = fetch_transaction(earliest_sig)
    if not tx_raw or "error" in tx_raw:
        result["detection_notes"].append("Could not fetch earliest transaction")
        return

    tx_result = tx_raw.get("result")
    if not tx_result:
        result["detection_notes"].append("Earliest tx result is null")
        return

    # Extract signer (deployer)
    try:
        tx_data = tx_result.get("transaction", {})
        message = tx_data.get("message", {})

        # Get account keys
        account_keys = message.get("accountKeys", [])
        signers = []
        for ak in account_keys:
            if isinstance(ak, dict):
                if ak.get("signer"):
                    pubkey = ak.get("pubkey", "")
                    if pubkey and pubkey not in SYSTEM_ADDRESSES:
                        signers.append(pubkey)
            elif isinstance(ak, str):
                signers.append(ak)

        if signers:
            result["deployer_wallet"] = signers[0]

        # Parse instructions for mintTo / initializeMint
        instructions = message.get("instructions", [])
        inner_instructions = tx_result.get("meta", {}).get("innerInstructions", [])

        # Also check inner instructions (program invocations)
        all_instructions = list(instructions)
        for inner in (inner_instructions or []):
            all_instructions.extend(inner.get("instructions", []))

        for ix in all_instructions:
            parsed = ix.get("parsed")
            if not parsed:
                continue
            if not isinstance(parsed, dict):
                continue

            ix_type = parsed.get("type", "")
            info = parsed.get("info", {})

            # initializeMint — the mint authority at creation time
            if ix_type == "initializeMint":
                ma = info.get("mintAuthority", "")
                if ma and ma not in SYSTEM_ADDRESSES:
                    if not result["deployer_wallet"]:
                        result["deployer_wallet"] = ma
                    result["detection_notes"].append(
                        f"initializeMint authority: {ma}"
                    )

            # mintTo — first token recipient
            if ix_type == "mintTo" or ix_type == "mintToChecked":
                dest_account = info.get("account", "")
                # The 'account' is a token account, we need its owner
                # Try to get it from authority or multisigAuthority
                authority = info.get("authority") or info.get("multisigAuthority", "")
                if authority and authority not in SYSTEM_ADDRESSES:
                    if not result["deployer_wallet"]:
                        result["deployer_wallet"] = authority
                    result["detection_notes"].append(
                        f"mintTo authority: {authority}"
                    )
                if dest_account:
                    # Resolve token account owner
                    owner = _resolve_token_account_owner(dest_account)
                    if owner and owner not in SYSTEM_ADDRESSES:
                        result["first_token_recipient"] = owner
                        result["detection_notes"].append(
                            f"First mintTo recipient (owner): {owner}"
                        )

            # transfer / transferChecked — early token movement
            if ix_type in ("transfer", "transferChecked") and not result["first_token_recipient"]:
                source = info.get("source", "")
                authority = info.get("authority", "")
                if authority and authority not in SYSTEM_ADDRESSES:
                    result["detection_notes"].append(
                        f"Early transfer authority: {authority}"
                    )

    except Exception as e:
        result["detection_notes"].append(f"Tx parse error: {str(e)[:200]}")


def _resolve_token_account_owner(token_account_pubkey):
    """Resolve a token account address to its owner wallet."""
    cache_key = f"owner:{token_account_pubkey}"
    cached = _cache_get(_account_cache, cache_key, 300)
    if cached is not None:
        return cached

    raw = fetch_account_info(token_account_pubkey)
    if not raw or "error" in raw:
        return None

    try:
        value = raw.get("result", {}).get("value")
        if not value:
            return None
        data = value.get("data", {})
        if isinstance(data, dict) and "parsed" in data:
            info = data["parsed"].get("info", {})
            owner = info.get("owner", "")
            if owner:
                _cache_set(_account_cache, cache_key, owner)
                return owner
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
def parse_mint_info(raw):
    """Parse mint account from getAccountInfo jsonParsed response."""
    result = {
        "mint_authority": None,
        "mint_authority_revoked": None,
        "freeze_authority": None,
        "freeze_authority_revoked": None,
        "supply": None,
        "decimals": None,
        "is_initialized": None,
        "parse_error": None,
    }

    if not raw or "error" in raw:
        result["parse_error"] = raw.get("error", "empty_response") if raw else "null_response"
        return result

    try:
        value = raw.get("result", {}).get("value")
        if not value:
            result["parse_error"] = "account_not_found_or_null"
            return result

        data = value.get("data", {})

        if isinstance(data, dict) and "parsed" in data:
            parsed = data["parsed"]
            info = parsed.get("info", {})
            ptype = parsed.get("type", "")

            if ptype == "mint" or "supply" in info:
                ma = info.get("mintAuthority")
                fa = info.get("freezeAuthority")
                result["mint_authority"] = ma
                result["mint_authority_revoked"] = (ma is None)
                result["freeze_authority"] = fa
                result["freeze_authority_revoked"] = (fa is None)
                result["supply"] = info.get("supply")
                result["decimals"] = info.get("decimals")
                result["is_initialized"] = info.get("isInitialized", True)
                return result

        # Fallback: decode raw base64 bytes
        if isinstance(data, list) and len(data) >= 1 and isinstance(data[0], str):
            raw_bytes = base64.b64decode(data[0])
            if len(raw_bytes) >= 76:
                return _decode_mint_bytes(raw_bytes)

        result["parse_error"] = "not_a_mint_account"
    except Exception as e:
        result["parse_error"] = f"parse_exception: {str(e)[:200]}"

    return result


def _decode_mint_bytes(raw_bytes):
    """Decode SPL Token Mint from raw 82-byte layout."""
    result = {
        "mint_authority": None, "mint_authority_revoked": None,
        "freeze_authority": None, "freeze_authority_revoked": None,
        "supply": None, "decimals": None, "is_initialized": None, "parse_error": None,
    }
    ma_opt = raw_bytes[0]
    result["mint_authority_revoked"] = (ma_opt == 0)
    result["mint_authority"] = _bytes_to_base58(raw_bytes[1:33]) if ma_opt == 1 else None
    result["supply"] = str(struct.unpack_from("<Q", raw_bytes, 33)[0])
    result["decimals"] = raw_bytes[41]
    result["is_initialized"] = (raw_bytes[42] == 1)
    fa_opt = raw_bytes[43]
    result["freeze_authority_revoked"] = (fa_opt == 0)
    result["freeze_authority"] = _bytes_to_base58(raw_bytes[44:76]) if fa_opt == 1 else None
    return result


def _bytes_to_base58(data):
    ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    result = bytearray()
    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(ALPHABET[remainder])
    for byte in data:
        if byte == 0:
            result.append(ALPHABET[0])
        else:
            break
    return bytes(result[::-1]).decode("ascii")


def parse_token_accounts_for_mint(raw, target_mint):
    """Parse getTokenAccountsByOwner response, filter by mint."""
    accounts = []
    if not raw or "error" in raw:
        return accounts
    try:
        value_list = raw.get("result", {}).get("value", [])
        for item in value_list:
            acct = item.get("account", {})
            data = acct.get("data", {})
            if isinstance(data, dict) and "parsed" in data:
                info = data["parsed"].get("info", {})
                mint = info.get("mint", "")
                ta = info.get("tokenAmount", {})
                if mint == target_mint:
                    accounts.append({
                        "pubkey": item.get("pubkey", ""),
                        "mint": mint,
                        "amount": ta.get("amount", "0"),
                        "decimals": ta.get("decimals", 0),
                        "ui_amount": ta.get("uiAmount", 0),
                    })
    except Exception:
        pass
    return accounts


def _parse_all_token_accounts(raw):
    """
    Parse getTokenAccountsByOwner response for ALL mints (v5.0 Portfolio).
    Returns dict: mint -> {amount: int, decimals: int, ui_amount: float}
    Filters out zero-balance and PORTFOLIO_IGNORE_MINTS.
    """
    result = {}
    if not raw or "error" in raw:
        return result
    try:
        value_list = raw.get("result", {}).get("value", [])
        for item in value_list:
            acct = item.get("account", {})
            data = acct.get("data", {})
            if isinstance(data, dict) and "parsed" in data:
                info = data["parsed"].get("info", {})
                mint = info.get("mint", "")
                ta = info.get("tokenAmount", {})
                amount = int(ta.get("amount", "0"))
                # Skip zero balance and ignored mints
                if amount <= 0:
                    continue
                if mint in PORTFOLIO_IGNORE_MINTS:
                    continue
                result[mint] = {
                    "amount": amount,
                    "decimals": ta.get("decimals", 0),
                    "ui_amount": ta.get("uiAmount", 0) or 0,
                }
    except Exception:
        pass
    return result


def analyze_transactions(dev_wallet, target_mint, signatures_raw=None, provided_sigs=None):
    """
    Fetch and analyze recent transactions for dev wallet.
    Returns tx signal dict.
    """
    signals = {
        "large_outbound_count": 0,
        "fanout_flag": False,
        "total_tx_analyzed": 0,
        "unique_destination_count": 0,
        "notes": [],
    }

    sig_list = []
    if provided_sigs:
        sig_list = provided_sigs[:10]
    elif dev_wallet:
        sigs_raw = fetch_signatures_for_address(dev_wallet, limit=15)
        if sigs_raw and "result" in sigs_raw:
            for s in sigs_raw["result"]:
                if isinstance(s, dict) and "signature" in s:
                    sig_list.append(s["signature"])

    if not sig_list:
        signals["notes"].append("No transaction signatures found")
        return signals

    unique_dests = set()

    # Parallel fetch all transactions at once (was sequential — major bottleneck)
    sigs_to_fetch = sig_list[:8]
    tx_results_map = {}
    with ThreadPoolExecutor(max_workers=8) as tx_exec:
        fut_map = {tx_exec.submit(fetch_transaction, sig): sig for sig in sigs_to_fetch}
        for fut in as_completed(fut_map):
            sig = fut_map[fut]
            try:
                tx_results_map[sig] = fut.result()
            except Exception:
                tx_results_map[sig] = None

    # Process results in original order
    for sig in sigs_to_fetch:
        tx_raw = tx_results_map.get(sig)
        if not tx_raw or "error" in tx_raw:
            continue

        tx_result = tx_raw.get("result")
        if not tx_result:
            continue

        signals["total_tx_analyzed"] += 1
        meta = tx_result.get("meta", {})
        if not meta:
            continue

        pre_token = meta.get("preTokenBalances") or []
        post_token = meta.get("postTokenBalances") or []

        pre_map = {}
        for b in pre_token:
            if isinstance(b, dict) and b.get("mint") == target_mint:
                idx = b.get("accountIndex")
                owner = b.get("owner", "")
                amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                pre_map[idx] = (owner, amt)

        for b in post_token:
            if isinstance(b, dict) and b.get("mint") == target_mint:
                idx = b.get("accountIndex")
                owner = b.get("owner", "")
                post_amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                pre_owner, pre_amt = pre_map.get(idx, (owner, 0))

                if dev_wallet and pre_owner == dev_wallet and pre_amt > post_amt:
                    delta = pre_amt - post_amt
                    if delta > 0:
                        signals["large_outbound_count"] += 1
                        signals["notes"].append(
                            f"Outbound: {delta:,.2f} tokens from dev in tx {sig[:12]}..."
                        )

                if owner and owner != dev_wallet:
                    unique_dests.add(owner)

    signals["unique_destination_count"] = len(unique_dests)
    if len(unique_dests) >= 5:
        signals["fanout_flag"] = True
        signals["notes"].append(
            f"Fan-out: {len(unique_dests)} unique destination wallets detected"
        )

    return signals


# ---------------------------------------------------------------------------
# Surveillance Helpers (v3.0)
# ---------------------------------------------------------------------------
def _resolve_whale_owners(mint, holders_raw, top_n=10):
    """
    Resolve token account addresses from getTokenLargestAccounts to wallet owners.
    Returns list of (owner_wallet, balance_amount) tuples.
    """
    holders = holders_raw.get("result", {}).get("value", [])
    if not holders:
        return []
    resolved = []
    for h in holders[:top_n]:
        token_acct = h.get("address", "")
        amount = int(h.get("amount", "0"))
        if amount <= 0:
            continue
        owner = _resolve_token_account_owner(token_acct)
        if owner and owner not in SYSTEM_ADDRESSES and owner not in KNOWN_EXCHANGE_ADDRESSES:
            resolved.append((owner, amount))
    return resolved


def _get_wallet_balance_for_mint(wallet, mint):
    """Get a wallet's token balance for a specific mint. Returns int amount."""
    raw = fetch_token_accounts_by_owner(wallet, mint=mint)
    accounts = parse_token_accounts_for_mint(raw, mint)
    if accounts:
        return int(accounts[0].get("amount", "0"))
    return 0


def _take_snapshot(mint, dev_wallet, whale_wallets, supply):
    """Capture dev + whale balances at current moment."""
    dev_balance = 0
    if dev_wallet:
        dev_balance = _get_wallet_balance_for_mint(dev_wallet, mint)

    whale_balances = {}
    whale_total = 0
    for w in whale_wallets:
        bal = _get_wallet_balance_for_mint(w, mint)
        whale_balances[w] = bal
        whale_total += bal

    return {
        "dev_balance": dev_balance,
        "whale_balances": whale_balances,
        "whale_total": whale_total,
        "timestamp": time.time(),
    }


def _detect_fanout_from_recent_tx(wallet, mint, time_window=300):
    """
    Check if wallet has sent tokens to many distinct wallets recently.
    Returns set of destination wallets.
    """
    destinations = set()
    sigs_raw = fetch_signatures_for_address(wallet, limit=10)
    if not sigs_raw or "error" in sigs_raw:
        return destinations

    now = time.time()
    sigs = sigs_raw.get("result", [])

    for s in sigs[:8]:
        if not isinstance(s, dict):
            continue
        block_time = s.get("blockTime")
        if block_time and (now - block_time) > time_window:
            continue  # Too old
        sig = s.get("signature", "")
        if not sig:
            continue

        tx_raw = fetch_transaction(sig)
        if not tx_raw or "error" in tx_raw:
            continue
        tx_result = tx_raw.get("result")
        if not tx_result:
            continue

        meta = tx_result.get("meta", {})
        if not meta:
            continue

        pre_token = meta.get("preTokenBalances") or []
        post_token = meta.get("postTokenBalances") or []

        pre_map = {}
        for b in pre_token:
            if isinstance(b, dict) and b.get("mint") == mint:
                idx = b.get("accountIndex")
                owner = b.get("owner", "")
                amt = int((b.get("uiTokenAmount") or {}).get("amount", "0"))
                pre_map[idx] = (owner, amt)

        for b in post_token:
            if isinstance(b, dict) and b.get("mint") == mint:
                idx = b.get("accountIndex")
                owner = b.get("owner", "")
                post_amt = int((b.get("uiTokenAmount") or {}).get("amount", "0"))
                pre_owner, pre_amt = pre_map.get(idx, (owner, 0))

                # If this wallet sent tokens (balance decreased) and another received
                if owner and owner != wallet and owner not in SYSTEM_ADDRESSES:
                    # Check that sender's balance decreased
                    for pidx, (po, pa) in pre_map.items():
                        if po == wallet and pa > 0:
                            destinations.add(owner)
                            break

    return destinations


def _create_alert(mint, trigger, severity, message, details=None):
    """Build an alert dict and append to per-mint and global stores."""
    alert = {
        "trigger": trigger,
        "severity": severity,
        "message": message,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mint": mint,
    }
    return alert


def _update_severity(entry):
    """Recompute highest severity from alerts list."""
    severity_order = {"NONE": 0, "LOW": 1, "MODERATE": 2, "HIGH": 3, "EXTREME": 4}
    highest = "NONE"
    for a in entry.get("alerts", []):
        s = a.get("severity", "NONE")
        if severity_order.get(s, 0) > severity_order.get(highest, 0):
            highest = s
    entry["severity"] = highest


def _balance_at_time_ago(history, key, window):
    """Find balance value from snapshot closest to N seconds ago."""
    now = time.time()
    target = now - window
    best = None
    best_diff = float("inf")
    for ts, snap in history:
        diff = abs(ts - target)
        if diff < best_diff:
            best_diff = diff
            best = snap.get(key, 0)
    return best


def _whale_balance_at_time_ago(history, wallet, window):
    """Find a specific whale's balance from snapshot closest to N seconds ago."""
    now = time.time()
    target = now - window
    best = None
    best_diff = float("inf")
    for ts, snap in history:
        diff = abs(ts - target)
        if diff < best_diff:
            best_diff = diff
            best = snap.get("whale_balances", {}).get(wallet, 0)
    return best


def _calc_change_pct(entry_val, current_val):
    """Percentage change: negative means decrease."""
    if entry_val <= 0:
        return 0.0
    return ((current_val - entry_val) / entry_val) * 100.0


# ---------------------------------------------------------------------------
# Jupiter Swap Functions (v4.0)
# ---------------------------------------------------------------------------
def _jupiter_get_quote(input_mint, output_mint, amount_raw, slippage_bps, jupiter_api_key=None):
    """Get swap quote from Jupiter Aggregator."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
        "swapMode": "ExactIn",
        "restrictIntermediateTokens": "true",
        "maxAccounts": "64",
    }
    headers = {}
    if jupiter_api_key:
        headers["x-api-key"] = jupiter_api_key
    try:
        resp = http_requests.get(JUPITER_QUOTE_URL, params=params,
                                 headers=headers, timeout=15)
        if resp.status_code != 200:
            return None, f"Quote API {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except Exception as e:
        return None, f"Quote request failed: {str(e)[:200]}"


def _jupiter_get_swap_tx(quote_response, user_public_key, jupiter_api_key=None,
                         emergency=False):
    """Get serialized swap transaction from Jupiter.
    emergency=True adds high priority fee (ComputeUnitPrice bribe)."""
    payload = {
        "userPublicKey": user_public_key,
        "quoteResponse": quote_response,
        "wrapAndUnwrapSol": True,
        "useSharedAccounts": False,
        "dynamicComputeUnitLimit": True,
        "skipUserAccountsRpcCalls": False,
    }
    # v4.1: Priority fee bribe — validators prioritize higher CU price
    if emergency and EMERGENCY_CU_PRICE_MICRO_LAMPORTS > 0:
        payload["computeUnitPriceMicroLamports"] = EMERGENCY_CU_PRICE_MICRO_LAMPORTS
    elif NORMAL_CU_PRICE_MICRO_LAMPORTS > 0:
        payload["computeUnitPriceMicroLamports"] = NORMAL_CU_PRICE_MICRO_LAMPORTS
    else:
        payload["prioritizationFeeLamports"] = "auto"

    headers = {"Content-Type": "application/json"}
    if jupiter_api_key:
        headers["x-api-key"] = jupiter_api_key
    try:
        resp = http_requests.post(JUPITER_SWAP_URL, json=payload,
                                  headers=headers, timeout=15)
        if resp.status_code != 200:
            return None, f"Swap API {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except Exception as e:
        return None, f"Swap request failed: {str(e)[:200]}"


def _sign_and_send_tx(swap_response, keypair, emergency=False):
    """Sign Jupiter swap transaction and send to Solana RPC.
    emergency=True uses skipPreflight for faster inclusion."""
    if not SOLDERS_AVAILABLE:
        return None, "solders library not installed — cannot sign transactions"
    try:
        tx_bytes = base64.b64decode(swap_response["swapTransaction"])
        tx = SoldersVersionedTx.from_bytes(tx_bytes)
        signed_tx = SoldersVersionedTx(tx.message, [keypair])
        raw_tx = bytes(signed_tx)
        tx_b64 = base64.b64encode(raw_tx).decode("ascii")

        # v4.1: Emergency mode = skipPreflight for speed
        # In a rug everyone sells at once; skip preflight to submit faster
        send_opts = {
            "encoding": "base64",
            "skipPreflight": emergency,  # True in emergency for speed
            "preflightCommitment": "confirmed",
        }

        result = _rpc_call("sendTransaction", [tx_b64, send_opts], timeout=30)

        if "error" in result:
            return None, f"sendTransaction: {result.get('detail', result.get('error'))}"
        signature = result.get("result")
        return signature, None
    except Exception as e:
        return None, f"Sign/send failed: {str(e)[:300]}"


def _emergency_sell_with_retries(mint, keypair, public_key, balance,
                                 jupiter_api_key, slippage_bps, event_id=None):
    """
    Emergency sell with priority fees + aggressive retries.
    Each retry gets a fresh quote (= fresh blockhash) to avoid expiry.
    Returns result dict.
    """
    # v4.1: Idempotency — prevent duplicate emergency sells
    if event_id:
        with _emergency_event_ids_lock:
            if event_id in _emergency_event_ids:
                return {"success": False, "error": f"Duplicate event_id: {event_id}",
                        "idempotent_reject": True}
            _emergency_event_ids.add(event_id)

    last_error = None

    for attempt in range(EMERGENCY_MAX_RETRIES):
        try:
            print(f"[EMERGENCY-SELL] Attempt {attempt + 1}/{EMERGENCY_MAX_RETRIES} "
                  f"for {mint[:12]}... (CU price: {EMERGENCY_CU_PRICE_MICRO_LAMPORTS} "
                  f"micro-lamports, slippage: {slippage_bps}bps)")

            # Fresh quote each retry = fresh blockhash
            quote, err = _jupiter_get_quote(mint, SOL_MINT, balance, slippage_bps,
                                            jupiter_api_key)
            if err:
                last_error = f"Quote attempt {attempt + 1}: {err}"
                if attempt < EMERGENCY_MAX_RETRIES - 1:
                    time.sleep(EMERGENCY_RETRY_DELAYS[attempt])
                continue

            # Get swap tx with EMERGENCY priority fee bribe
            swap_resp, err = _jupiter_get_swap_tx(quote, public_key,
                                                  jupiter_api_key, emergency=True)
            if err:
                last_error = f"SwapTx attempt {attempt + 1}: {err}"
                if attempt < EMERGENCY_MAX_RETRIES - 1:
                    time.sleep(EMERGENCY_RETRY_DELAYS[attempt])
                continue

            # Sign and send with skipPreflight=True for speed
            signature, err = _sign_and_send_tx(swap_resp, keypair, emergency=True)
            if err:
                last_error = f"Send attempt {attempt + 1}: {err}"
                if attempt < EMERGENCY_MAX_RETRIES - 1:
                    time.sleep(EMERGENCY_RETRY_DELAYS[attempt])
                continue

            # SUCCESS
            print(f"[EMERGENCY-SELL] SUCCESS on attempt {attempt + 1} — "
                  f"{mint[:12]}... sig: {signature}")
            return {
                "success": True,
                "signature": signature,
                "tokens_sold": balance,
                "input_mint": mint,
                "output_mint": SOL_MINT,
                "slippage_bps": slippage_bps,
                "out_amount": quote.get("outAmount", "0"),
                "price_impact": quote.get("priceImpactPct", "0"),
                "priority": "EMERGENCY",
                "cu_price_micro_lamports": EMERGENCY_CU_PRICE_MICRO_LAMPORTS,
                "attempts": attempt + 1,
                "event_id": event_id,
            }

        except Exception as e:
            last_error = f"Exception attempt {attempt + 1}: {str(e)[:200]}"
            if attempt < EMERGENCY_MAX_RETRIES - 1:
                time.sleep(EMERGENCY_RETRY_DELAYS[attempt])

    return {
        "success": False,
        "error": f"All {EMERGENCY_MAX_RETRIES} attempts failed. Last: {last_error}",
        "priority": "EMERGENCY",
        "attempts": EMERGENCY_MAX_RETRIES,
        "event_id": event_id,
    }


def _auto_sell_position(mint, wallet_private_key_b58, jupiter_api_key,
                        slippage_bps=None, priority="EMERGENCY", event_id=None):
    """
    Sell ALL tokens of a mint back to SOL via Jupiter.
    priority="EMERGENCY" → high CU price bribe + wide slippage + retries + skipPreflight
    priority="NORMAL"    → standard fees + normal slippage + single attempt
    Called directly from the surveillance loop — zero agent latency.
    """
    if not SOLDERS_AVAILABLE:
        return {"success": False, "error": "solders not installed"}

    is_emergency = (priority == "EMERGENCY")
    if slippage_bps is None:
        slippage_bps = EMERGENCY_SLIPPAGE_BPS if is_emergency else NORMAL_SLIPPAGE_BPS

    try:
        keypair = SoldersKeypair.from_base58_string(wallet_private_key_b58)
        public_key = str(keypair.pubkey())

        # Get wallet's token balance for this mint
        balance = _get_wallet_balance_for_mint(public_key, mint)
        if balance <= 0:
            return {"success": False, "error": "No token balance to sell", "balance": 0}

        tag = "EMERGENCY-SELL" if is_emergency else "AUTO-SELL"
        print(f"[{tag}] Selling {balance} raw tokens of {mint[:12]}... "
              f"priority={priority} slippage={slippage_bps}bps")

        # ---- EMERGENCY MODE: retries + priority fee bribe ----
        if is_emergency:
            return _emergency_sell_with_retries(
                mint, keypair, public_key, balance,
                jupiter_api_key, slippage_bps, event_id)

        # ---- NORMAL MODE: single attempt, standard fees ----
        quote, err = _jupiter_get_quote(mint, SOL_MINT, balance, slippage_bps,
                                        jupiter_api_key)
        if err:
            return {"success": False, "error": f"Quote: {err}", "priority": "NORMAL"}

        swap_resp, err = _jupiter_get_swap_tx(quote, public_key,
                                              jupiter_api_key, emergency=False)
        if err:
            return {"success": False, "error": f"SwapTx: {err}", "priority": "NORMAL"}

        signature, err = _sign_and_send_tx(swap_resp, keypair, emergency=False)
        if err:
            return {"success": False, "error": f"Send: {err}", "priority": "NORMAL"}

        print(f"[AUTO-SELL] SUCCESS — {mint[:12]}... sold, sig: {signature}")
        return {
            "success": True,
            "signature": signature,
            "tokens_sold": balance,
            "input_mint": mint,
            "output_mint": SOL_MINT,
            "slippage_bps": slippage_bps,
            "out_amount": quote.get("outAmount", "0"),
            "price_impact": quote.get("priceImpactPct", "0"),
            "priority": "NORMAL",
            "attempts": 1,
            "event_id": event_id,
        }
    except Exception as e:
        return {"success": False, "error": f"Exception: {str(e)[:300]}"}


# ---------------------------------------------------------------------------
# Liquidity Pool Analysis (v4.0)
# ---------------------------------------------------------------------------
def _check_liquidity_pool(mint, jupiter_api_key=None):
    """
    Check liquidity pool status for a token:
    1. Verify tradeable liquidity exists via Jupiter quote
    2. Get pool type and price impact
    3. Check LP token burn status for Raydium pools
    """
    result = {
        "has_liquidity": False,
        "liquidity_locked": None,
        "pool_type": None,
        "pool_address": None,
        "lp_burn_pct": None,
        "price_impact_pct": None,
        "signals": [],
        "error": None,
    }

    try:
        # Step 1: Jupiter quote with 0.1 SOL to test liquidity
        test_amount = 100_000_000  # 0.1 SOL in lamports
        quote, err = _jupiter_get_quote(SOL_MINT, mint, test_amount, 1000,
                                        jupiter_api_key)
        if err or not quote:
            result["error"] = f"No liquidity: {err}"
            result["signals"].append("CRITICAL: No tradeable liquidity on any DEX")
            return result

        result["has_liquidity"] = True

        # Extract route info
        route_plan = quote.get("routePlan", [])
        if route_plan:
            swap_info = route_plan[0].get("swapInfo", {})
            result["pool_type"] = swap_info.get("label", "unknown")
            result["pool_address"] = swap_info.get("ammKey", "")

        # Price impact
        try:
            result["price_impact_pct"] = round(float(
                quote.get("priceImpactPct", "0")), 4)
        except (ValueError, TypeError):
            pass

        # Step 2: Check LP burn for Raydium pools
        pool_addr = result.get("pool_address")
        if pool_addr:
            lp_info = _check_lp_burn(pool_addr)
            result["liquidity_locked"] = lp_info.get("locked")
            result["lp_burn_pct"] = lp_info.get("burn_pct")
            if lp_info.get("lp_mint"):
                result["lp_mint"] = lp_info["lp_mint"]
            result["signals"].extend(lp_info.get("signals", []))

        # Price impact signals
        if result["price_impact_pct"] is not None:
            impact = abs(result["price_impact_pct"])
            if impact > 10:
                result["signals"].append(
                    f"HIGH: Price impact {impact:.2f}% — very thin liquidity")
            elif impact > 3:
                result["signals"].append(
                    f"MODERATE: Price impact {impact:.2f}% — low liquidity")
            else:
                result["signals"].append(
                    f"OK: Price impact {impact:.2f}% — decent liquidity")

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


def _check_lp_burn(pool_address):
    """Check if LP tokens for a pool are burned/locked."""
    result = {"locked": None, "burn_pct": None, "lp_mint": None, "signals": []}

    try:
        # Fetch pool account raw bytes
        raw = _rpc_call("getAccountInfo", [
            pool_address,
            {"encoding": "base64", "commitment": "confirmed"}
        ])

        if not raw or "error" in raw:
            result["signals"].append("Could not fetch pool account")
            return result

        value = raw.get("result", {}).get("value")
        if not value:
            result["signals"].append("Pool account not found")
            return result

        owner = value.get("owner", "")
        data = value.get("data", [])
        if not data or not isinstance(data, list) or len(data) < 1:
            result["signals"].append("No pool data")
            return result

        # Decode raw bytes
        account_bytes = base64.b64decode(data[0])

        # Raydium AMM v4: LP mint at offset 464 (32 bytes)
        if owner == RAYDIUM_AMM_V4_PROGRAM and len(account_bytes) >= 496:
            lp_mint_bytes = account_bytes[464:496]
            lp_mint = _bytes_to_base58(lp_mint_bytes)
            result["lp_mint"] = lp_mint
            _check_lp_holders(lp_mint, result)

        # Raydium CPMM: LP mint at offset 225 (32 bytes)
        elif owner == RAYDIUM_CPMM_PROGRAM and len(account_bytes) >= 257:
            lp_mint_bytes = account_bytes[225:257]
            lp_mint = _bytes_to_base58(lp_mint_bytes)
            result["lp_mint"] = lp_mint
            _check_lp_holders(lp_mint, result)

        else:
            result["signals"].append(
                f"Pool program {owner[:16]}... — LP burn check not supported")

    except Exception as e:
        result["signals"].append(f"LP check error: {str(e)[:200]}")

    return result


def _check_lp_holders(lp_mint, result):
    """Check LP token holders to determine burn percentage."""
    try:
        # Get LP supply
        lp_info = fetch_account_info(lp_mint)
        lp_parsed = parse_mint_info(lp_info)
        lp_supply_str = lp_parsed.get("supply")
        lp_supply = int(lp_supply_str) if lp_supply_str else 0
        if lp_supply <= 0:
            result["signals"].append("LP supply is zero")
            return

        # Get largest LP holders
        holders_raw = fetch_token_largest_accounts(lp_mint)
        if not holders_raw or "error" in holders_raw:
            result["signals"].append("Could not fetch LP holders")
            return

        holders = holders_raw.get("result", {}).get("value", [])
        if not holders:
            result["signals"].append("No LP holders found")
            return

        # Check each holder — resolve owner to see if it's a burn address
        total_burned = 0
        for h in holders:
            amount = int(h.get("amount", "0"))
            token_acct = h.get("address", "")

            # Check if the token account itself is a burn address
            if token_acct in LP_BURN_ADDRESSES:
                total_burned += amount
                continue

            # Resolve owner wallet
            owner = _resolve_token_account_owner(token_acct)
            if owner and owner in LP_BURN_ADDRESSES:
                total_burned += amount

        burn_pct = (total_burned / lp_supply) * 100.0
        result["burn_pct"] = round(burn_pct, 2)

        if burn_pct > 95:
            result["locked"] = True
            result["signals"].append(
                f"LP {burn_pct:.1f}% burned — liquidity LOCKED")
        elif burn_pct > 50:
            result["locked"] = False
            result["signals"].append(
                f"LP partially burned ({burn_pct:.1f}%) — NOT fully locked")
        else:
            result["locked"] = False
            result["signals"].append(
                f"LP only {burn_pct:.1f}% burned — liquidity UNLOCKED")

    except Exception as e:
        result["signals"].append(f"LP holder check error: {str(e)[:200]}")


# ---------------------------------------------------------------------------
# Trigger Evaluation (v3.0)
# ---------------------------------------------------------------------------
def _evaluate_triggers(entry, current_snap, dev_fanout_dests, whale_fanout_results):
    """
    Evaluate all triggers for a watched mint. Returns list of new alerts.
    Mutates entry flags in place.
    """
    new_alerts = []
    mint = entry["mint"]
    supply = int(entry["supply"]) if entry["supply"] else 0
    entry_snap = entry["entry_snapshot"]
    history = entry["history"]
    flags = entry["flags"]

    # ---- DEV TRIGGERS ----
    if entry.get("dev_wallet") and entry_snap.get("dev_balance", 0) > 0:
        entry_dev = entry_snap["dev_balance"]
        current_dev = current_snap.get("dev_balance", 0)
        dev_5min_ago = _balance_at_time_ago(history, "dev_balance", 300)

        # DEV_DUMP: dev balance drops >=5% of entry balance within 5 min
        if dev_5min_ago is not None and dev_5min_ago > 0:
            drop_from_5min = _calc_change_pct(dev_5min_ago, current_dev)
            if drop_from_5min <= -DEV_DUMP_THRESHOLD_PCT and not flags.get("dev_dump_flag"):
                flags["dev_dump_flag"] = True
                alert = _create_alert(mint, "DEV_DUMP", "HIGH",
                    f"Dev balance dropped {abs(drop_from_5min):.1f}% in last 5 min",
                    {"dev_5min_ago": dev_5min_ago, "dev_now": current_dev})
                new_alerts.append(alert)

        # DEV_SEVERE_DUMP: dev balance drops >=10% from entry at any time
        drop_from_entry = _calc_change_pct(entry_dev, current_dev)
        if drop_from_entry <= -DEV_SEVERE_DUMP_THRESHOLD_PCT and not flags.get("dev_severe_dump_flag"):
            flags["dev_severe_dump_flag"] = True
            alert = _create_alert(mint, "DEV_SEVERE_DUMP", "EXTREME",
                f"Dev balance dropped {abs(drop_from_entry):.1f}% from entry",
                {"entry_dev": entry_dev, "dev_now": current_dev})
            new_alerts.append(alert)

        # DEV_FANOUT: dev sends to >=3 distinct wallets within 5 min
        if len(dev_fanout_dests) >= DEV_FANOUT_MIN_WALLETS and not flags.get("dev_distribution_flag"):
            flags["dev_distribution_flag"] = True
            alert = _create_alert(mint, "DEV_FANOUT", "HIGH",
                f"Dev distributed tokens to {len(dev_fanout_dests)} wallets",
                {"destinations": list(dev_fanout_dests)[:10]})
            new_alerts.append(alert)

    # ---- WHALE TRIGGERS ----
    if supply > 0 and entry.get("whale_wallets"):
        whale_wallets = entry["whale_wallets"]
        current_whale_bals = current_snap.get("whale_balances", {})
        entry_whale_total = entry_snap.get("whale_total", 0)
        current_whale_total = current_snap.get("whale_total", 0)

        # Check each whale
        for i, w in enumerate(whale_wallets):
            current_bal = current_whale_bals.get(w, 0)
            bal_5min_ago = _whale_balance_at_time_ago(history, w, 300)

            if bal_5min_ago is not None and bal_5min_ago > 0:
                drop_tokens = bal_5min_ago - current_bal
                drop_supply_pct = (drop_tokens / supply) * 100.0

                # WHALE_TOP1_DUMP: top1 holder loses >=2% of total supply in 5 min
                if i == 0 and drop_supply_pct >= WHALE_TOP1_DUMP_SUPPLY_PCT and not flags.get("whale_dump_flag"):
                    flags["whale_dump_flag"] = True
                    alert = _create_alert(mint, "WHALE_TOP1_DUMP", "HIGH",
                        f"Top1 whale lost {drop_supply_pct:.1f}% of total supply in 5 min",
                        {"wallet": w[:12] + "...", "drop_tokens": drop_tokens})
                    new_alerts.append(alert)

                # WHALE_ANY_DUMP: any top10 loses >=1% of supply in 5 min
                elif drop_supply_pct >= WHALE_ANY_DUMP_SUPPLY_PCT:
                    alert = _create_alert(mint, "WHALE_ANY_DUMP", "MODERATE",
                        f"Whale #{i+1} lost {drop_supply_pct:.1f}% of total supply in 5 min",
                        {"wallet": w[:12] + "...", "drop_tokens": drop_tokens})
                    new_alerts.append(alert)

            # WHALE_EXCHANGE_EXIT: whale sends to known exchange
            if w in whale_fanout_results:
                for dest in whale_fanout_results[w]:
                    if dest in KNOWN_EXCHANGE_ADDRESSES and not flags.get("top_holder_exit_flag"):
                        flags["top_holder_exit_flag"] = True
                        alert = _create_alert(mint, "WHALE_EXCHANGE_EXIT", "HIGH",
                            f"Whale #{i+1} sent tokens to exchange address",
                            {"wallet": w[:12] + "...", "exchange": dest[:12] + "..."})
                        new_alerts.append(alert)

        # WHALE_FANOUT: any tracked whale sends to >=3 wallets in 5 min
        for w, dests in whale_fanout_results.items():
            if len(dests) >= WHALE_FANOUT_MIN_WALLETS:
                alert = _create_alert(mint, "WHALE_FANOUT", "MODERATE",
                    f"Whale sent to {len(dests)} distinct wallets in 5 min",
                    {"wallet": w[:12] + "...", "destinations": list(dests)[:5]})
                new_alerts.append(alert)

        # WHALE_AGG_DISTRIBUTION: sum(topN) drops >=5% from entry within 10 min
        if entry_whale_total > 0:
            agg_drop = _calc_change_pct(entry_whale_total, current_whale_total)
            if agg_drop <= -WHALE_AGG_DROP_PCT and not flags.get("whale_distribution_flag"):
                flags["whale_distribution_flag"] = True
                alert = _create_alert(mint, "WHALE_AGG_DISTRIBUTION", "HIGH",
                    f"Top whale aggregate holdings dropped {abs(agg_drop):.1f}% from entry",
                    {"entry_total": entry_whale_total, "current_total": current_whale_total})
                new_alerts.append(alert)

    return new_alerts


# ---------------------------------------------------------------------------
# Portfolio Auto-Pilot Helpers (v5.0)
# ---------------------------------------------------------------------------
def _portfolio_add_event(event_type, mint, message, details=None):
    """Append an event to the portfolio event log. Thread-safe."""
    event = {
        "type": event_type,
        "mint": mint,
        "message": message,
        "details": details or {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _portfolio_state_lock:
        if _portfolio_state and "events" in _portfolio_state:
            _portfolio_state["events"].append(event)
    print(f"[PORTFOLIO] {event_type}: {message}")
    return event


def _portfolio_run_analysis(mint):
    """
    Run analyze() internally for a portfolio-detected token.
    Returns compact summary dict or None on failure.
    """
    try:
        result = analyze(mint)
        return {
            "risk_score": result.get("risk_score"),
            "risk_level": result.get("risk_level"),
            "decision": result.get("decision"),
            "token_age_hours": result.get("token_age_hours"),
            "top1_pct": result.get("holder_concentration", {}).get("top1_pct"),
            "has_liquidity": result.get("liquidity_pool", {}).get("has_liquidity")
                if result.get("liquidity_pool") else None,
            "timestamp": result.get("timestamp"),
        }
    except Exception as e:
        return {"error": str(e)[:200], "risk_score": None, "risk_level": "UNKNOWN"}


def _portfolio_auto_watch(mint, wallet_private_key, jupiter_api_key):
    """
    Internal auto-watch for a portfolio-detected token (v5.0).
    Replicates /watch endpoint logic but runs without HTTP context.
    Always enables auto_sell=True with EMERGENCY priority.
    Returns True on success, False on failure.
    """
    try:
        # Check if already watching
        with _watched_mints_lock:
            if mint in _watched_mints:
                _portfolio_add_event("ALREADY_WATCHING", mint,
                    f"Token {mint[:12]}... already in surveillance")
                return True
            if len(_watched_mints) >= MONITOR_MAX_MINTS:
                _portfolio_add_event("WATCH_CAPACITY_FULL", mint,
                    f"Cannot watch {mint[:12]}... — at max capacity ({MONITOR_MAX_MINTS})")
                return False

        # Fetch mint info (reuse existing function)
        raw_mint = fetch_account_info(mint)
        mint_info = parse_mint_info(raw_mint)
        supply = mint_info.get("supply")
        decimals = mint_info.get("decimals")

        if not supply:
            _portfolio_add_event("WATCH_FAILED", mint,
                f"Could not fetch mint info for {mint[:12]}...")
            return False

        # Parallel: dev detection + holder fetch (independent of each other)
        with ThreadPoolExecutor(max_workers=2) as wp:
            fut_dev = wp.submit(detect_dev_wallet, mint, mint_info)
            fut_holders = wp.submit(fetch_token_largest_accounts, mint)
            dev_detection = fut_dev.result()
            holders_raw = fut_holders.result()

        dev_wallet = dev_detection.get("probable_dev_wallet")
        dev_confidence = dev_detection.get("confidence", "none")

        # Resolve whale wallets (needs holders_raw from above)
        whale_owners = _resolve_whale_owners(mint, holders_raw, top_n=10)
        whale_wallets = [w for w, _ in whale_owners]

        # Take entry snapshot (reuse existing function)
        entry_snapshot = _take_snapshot(mint, dev_wallet, whale_wallets, supply)

        now_iso = datetime.now(timezone.utc).isoformat()

        # Build entry dict — identical structure to /watch endpoint
        entry = {
            "mint": mint,
            "entry_timestamp": now_iso,
            "dev_wallet": dev_wallet,
            "dev_confidence": dev_confidence,
            "whale_wallets": whale_wallets,
            "supply": supply,
            "decimals": decimals,
            "entry_snapshot": entry_snapshot,
            "current_snapshot": entry_snapshot,
            "history": deque(maxlen=MONITOR_HISTORY_MAXLEN),
            "flags": {
                "dev_dump_flag": False,
                "dev_severe_dump_flag": False,
                "dev_distribution_flag": False,
                "whale_dump_flag": False,
                "whale_distribution_flag": False,
                "top_holder_exit_flag": False,
            },
            "alerts": [],
            "severity": "NONE",
            "last_checked": time.time(),
            "poll_interval": MONITOR_DEFAULT_INTERVAL,
            # Auto-sell always enabled for portfolio-detected tokens
            "auto_sell": True,
            "auto_sell_slippage_bps": EMERGENCY_SLIPPAGE_BPS,
            "auto_sell_severity": AUTO_SELL_SEVERITY_THRESHOLD,
            "auto_sell_executed": False,
            "auto_sell_result": None,
            "wallet_private_key": wallet_private_key,
            "jupiter_api_key": jupiter_api_key,
            # v5.0: Mark this was auto-detected by portfolio monitor
            "source": "portfolio_auto_detect",
        }
        entry["history"].append((entry_snapshot["timestamp"], entry_snapshot))

        with _watched_mints_lock:
            _watched_mints[mint] = entry

        # Start surveillance monitor if not running
        _start_monitor_thread()

        _portfolio_add_event("WATCH_STARTED", mint,
            f"Auto-watch started for {mint[:12]}... "
            f"dev={'%s...' % dev_wallet[:12] if dev_wallet else 'unknown'} "
            f"whales={len(whale_wallets)} supply={supply}",
            {"dev_wallet": dev_wallet, "dev_confidence": dev_confidence,
             "whale_count": len(whale_wallets), "supply": supply})

        return True

    except Exception as e:
        _portfolio_add_event("WATCH_ERROR", mint,
            f"Failed to auto-watch {mint[:12]}...: {str(e)[:200]}")
        return False


# ---------------------------------------------------------------------------
# Monitor Loop (v3.0)
# ---------------------------------------------------------------------------
def _poll_single_mint(mint):
    """Poll a single watched mint: fetch balances, check fan-out, evaluate triggers.
    v4.0: If auto_sell is enabled and severity >= threshold, execute emergency sell."""
    with _watched_mints_lock:
        if mint not in _watched_mints:
            return
        entry = copy.deepcopy(_watched_mints[mint])

    dev_wallet = entry.get("dev_wallet")
    whale_wallets = entry.get("whale_wallets", [])
    supply_str = entry.get("supply")

    # Take current snapshot (RPC calls outside lock)
    current_snap = _take_snapshot(mint, dev_wallet, whale_wallets, supply_str)

    # Check dev fan-out
    dev_fanout_dests = set()
    if dev_wallet:
        try:
            dev_fanout_dests = _detect_fanout_from_recent_tx(dev_wallet, mint, time_window=300)
        except Exception:
            pass

    # Check whale fan-out (only top1 to limit RPC budget)
    whale_fanout_results = {}
    if whale_wallets:
        try:
            top1 = whale_wallets[0]
            dests = _detect_fanout_from_recent_tx(top1, mint, time_window=300)
            if dests:
                whale_fanout_results[top1] = dests
        except Exception:
            pass

    # Evaluate triggers
    new_alerts = _evaluate_triggers(entry, current_snap, dev_fanout_dests, whale_fanout_results)

    # Write results back under lock
    with _watched_mints_lock:
        if mint not in _watched_mints:
            return
        live = _watched_mints[mint]
        live["current_snapshot"] = current_snap
        live["history"].append((current_snap["timestamp"], current_snap))
        live["last_checked"] = time.time()
        live["flags"] = entry["flags"]  # Updated by _evaluate_triggers
        for a in new_alerts:
            live["alerts"].append(a)
        _update_severity(live)
        current_severity = live["severity"]
        already_sold = live.get("auto_sell_executed", False)

    # Append to global alerts
    if new_alerts:
        with _global_alerts_lock:
            for a in new_alerts:
                _global_alerts.append(a)

    # ---- v4.1: AUTO-SELL CHECK (EMERGENCY PRIORITY) ----
    if (entry.get("auto_sell") and not already_sold and SOLDERS_AVAILABLE):
        threshold = entry.get("auto_sell_severity", AUTO_SELL_SEVERITY_THRESHOLD)
        threshold_val = SEVERITY_ORDER.get(threshold, 3)
        current_val = SEVERITY_ORDER.get(current_severity, 0)

        if current_val >= threshold_val:
            # Surveillance-triggered sells ALWAYS use EMERGENCY priority
            # High CU price bribe + wide slippage + aggressive retries
            print(f"[EMERGENCY-SELL] TRIGGERED for {mint[:12]}... "
                  f"severity={current_severity} — using priority fees")
            wallet_key = entry.get("wallet_private_key", "")
            jupiter_key = entry.get("jupiter_api_key", "")
            event_id = f"auto_{mint[:16]}_{int(time.time())}"

            sell_result = _auto_sell_position(
                mint, wallet_key, jupiter_key,
                slippage_bps=EMERGENCY_SLIPPAGE_BPS,
                priority="EMERGENCY",
                event_id=event_id)

            # Record the sell result
            sell_alert = _create_alert(mint, "AUTO_SELL", current_severity,
                f"Auto-sell executed: {'SUCCESS' if sell_result.get('success') else 'FAILED'}",
                sell_result)

            with _watched_mints_lock:
                if mint in _watched_mints:
                    _watched_mints[mint]["auto_sell_executed"] = True
                    _watched_mints[mint]["auto_sell_result"] = sell_result
                    _watched_mints[mint]["alerts"].append(sell_alert)

            with _global_alerts_lock:
                _global_alerts.append(sell_alert)


def _monitor_loop():
    """Daemon thread: poll watched mints at their configured intervals."""
    while not _monitor_stop_event.is_set():
        try:
            with _watched_mints_lock:
                mints_to_poll = []
                now = time.time()
                for mint, entry in _watched_mints.items():
                    interval = entry.get("poll_interval", MONITOR_DEFAULT_INTERVAL)
                    last = entry.get("last_checked", 0)
                    if (now - last) >= interval:
                        mints_to_poll.append(mint)

            for mint in mints_to_poll:
                if _monitor_stop_event.is_set():
                    break
                try:
                    _poll_single_mint(mint)
                except Exception as e:
                    # Log but don't crash the monitor
                    print(f"[MONITOR] Error polling {mint[:12]}...: {e}")
                time.sleep(0.5)  # Small gap between mints to spread RPC load

        except Exception as e:
            print(f"[MONITOR] Loop error: {e}")

        _monitor_stop_event.wait(2)  # Check every 2 seconds for mints to poll


def _start_monitor_thread():
    """Lazy-start the monitor daemon thread."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_stop_event.clear()
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="rug-monitor")
    _monitor_thread.start()


def _stop_monitor_thread():
    """Stop the monitor thread (called on last unwatch)."""
    global _monitor_thread
    _monitor_stop_event.set()
    if _monitor_thread:
        _monitor_thread.join(timeout=5)
        _monitor_thread = None


# ---------------------------------------------------------------------------
# Portfolio Auto-Pilot Scanner (v5.0)
# ---------------------------------------------------------------------------
def _portfolio_scan_loop():
    """
    Portfolio Auto-Pilot daemon thread.
    Scans wallet holdings every PORTFOLIO_SCAN_INTERVAL seconds.
    Detects new tokens and removed tokens, auto-watches new ones.
    """
    while not _portfolio_stop_event.is_set():
        try:
            with _portfolio_state_lock:
                if not _portfolio_state or not _portfolio_state.get("active"):
                    break
                wallet_address = _portfolio_state["wallet_address"]
                wallet_private_key = _portfolio_state["wallet_private_key"]
                jupiter_api_key = _portfolio_state["jupiter_api_key"]
                known_holdings = {
                    m: dict(h) for m, h in _portfolio_state.get("holdings", {}).items()
                }

            # --- STEP 1: Fetch ALL token accounts (single RPC call, bypass cache) ---
            raw = fetch_token_accounts_by_owner(wallet_address, mint=None, bypass_cache=True)
            if not raw or "error" in raw:
                print(f"[PORTFOLIO] Scan error: RPC returned error or empty")
                with _portfolio_state_lock:
                    if _portfolio_state:
                        _portfolio_state["scan_count"] = _portfolio_state.get("scan_count", 0) + 1
                        _portfolio_state["scan_errors"] = _portfolio_state.get("scan_errors", 0) + 1
                        _portfolio_state["last_scan"] = time.time()
                _portfolio_stop_event.wait(PORTFOLIO_SCAN_INTERVAL)
                continue

            current_tokens = _parse_all_token_accounts(raw)

            # Increment scan_count immediately after successful wallet fetch
            # (processing new/removed tokens can take 10+ seconds)
            with _portfolio_state_lock:
                if _portfolio_state:
                    _portfolio_state["last_scan"] = time.time()
                    _portfolio_state["scan_count"] = _portfolio_state.get("scan_count", 0) + 1

            # --- STEP 2: Detect NEW tokens ---
            new_mints = []
            for mint, token_data in current_tokens.items():
                if mint not in known_holdings or known_holdings[mint].get("watch_status") == "removed":
                    new_mints.append((mint, token_data))

            # --- STEP 3: Detect REMOVED tokens (balance -> 0 or gone from wallet) ---
            removed_mints = []
            for mint, holding in known_holdings.items():
                if holding.get("watch_status") in ("removed", "sold"):
                    continue  # already handled
                if mint not in current_tokens:
                    removed_mints.append(mint)

            # --- STEP 4: Handle NEW tokens (staggered to avoid RPC spike) ---
            for mint, token_data in new_mints:
                if _portfolio_stop_event.is_set():
                    break

                now_iso = datetime.now(timezone.utc).isoformat()
                _portfolio_add_event("NEW_TOKEN_DETECTED", mint,
                    f"New token detected: {mint[:12]}... amount={token_data['ui_amount']}",
                    {"amount": token_data["amount"], "ui_amount": token_data["ui_amount"],
                     "decimals": token_data["decimals"]})

                # Update holding in state as "pending"
                with _portfolio_state_lock:
                    if _portfolio_state:
                        _portfolio_state["holdings"][mint] = {
                            "amount": token_data["amount"],
                            "decimals": token_data["decimals"],
                            "ui_amount": token_data["ui_amount"],
                            "first_seen": now_iso,
                            "last_seen": now_iso,
                            "watch_status": "pending",
                            "analysis_summary": None,
                            "auto_sell_executed": False,
                            "auto_sell_result": None,
                        }

                # Run analysis for record-keeping
                analysis = _portfolio_run_analysis(mint)
                with _portfolio_state_lock:
                    if _portfolio_state and mint in _portfolio_state.get("holdings", {}):
                        _portfolio_state["holdings"][mint]["analysis_summary"] = analysis

                _portfolio_add_event("ANALYSIS_COMPLETE", mint,
                    f"Analysis for {mint[:12]}...: score={analysis.get('risk_score')} "
                    f"level={analysis.get('risk_level')} decision={analysis.get('decision')}",
                    analysis)

                # Auto-watch with surveillance + auto-sell
                success = _portfolio_auto_watch(mint, wallet_private_key, jupiter_api_key)
                with _portfolio_state_lock:
                    if _portfolio_state and mint in _portfolio_state.get("holdings", {}):
                        _portfolio_state["holdings"][mint]["watch_status"] = (
                            "watching" if success else "failed"
                        )

                # Stagger to avoid RPC spike when multiple tokens detected
                if len(new_mints) > 1:
                    time.sleep(PORTFOLIO_WATCH_STAGGER_DELAY)

            # --- STEP 5: Handle REMOVED tokens (balance → 0) ---
            for mint in removed_mints:
                _portfolio_add_event("TOKEN_REMOVED", mint,
                    f"Token {mint[:12]}... no longer in wallet (balance=0)")

                # Auto-unwatch from surveillance
                with _watched_mints_lock:
                    if mint in _watched_mints:
                        removed_entry = _watched_mints.pop(mint)
                        remaining = len(_watched_mints)
                        _portfolio_add_event("AUTO_UNWATCHED", mint,
                            f"Auto-unwatched {mint[:12]}... (removed from wallet)",
                            {"final_severity": removed_entry.get("severity", "NONE"),
                             "alerts_count": len(removed_entry.get("alerts", []))})

                # Check if we should stop surveillance monitor
                with _watched_mints_lock:
                    if len(_watched_mints) == 0:
                        _stop_monitor_thread()

                with _portfolio_state_lock:
                    if _portfolio_state and mint in _portfolio_state.get("holdings", {}):
                        _portfolio_state["holdings"][mint]["watch_status"] = "removed"

            # --- STEP 6: Update existing holdings balances + sync sell status ---
            # First: collect sell status from surveillance (avoid nested locks)
            sell_status = {}
            with _watched_mints_lock:
                for mint in current_tokens:
                    if mint in _watched_mints:
                        wm = _watched_mints[mint]
                        if wm.get("auto_sell_executed"):
                            sell_status[mint] = {
                                "executed": True,
                                "result": copy.deepcopy(wm.get("auto_sell_result")),
                            }

            # Then: update portfolio state
            new_mint_set = {m for m, _ in new_mints}
            with _portfolio_state_lock:
                if _portfolio_state:
                    for mint, token_data in current_tokens.items():
                        if mint in new_mint_set:
                            continue  # Already handled in step 4
                        if mint in _portfolio_state.get("holdings", {}):
                            h = _portfolio_state["holdings"][mint]
                            h["amount"] = token_data["amount"]
                            h["decimals"] = token_data["decimals"]
                            h["ui_amount"] = token_data["ui_amount"]
                            h["last_seen"] = datetime.now(timezone.utc).isoformat()
                            if mint in sell_status:
                                h["auto_sell_executed"] = True
                                h["auto_sell_result"] = sell_status[mint]["result"]
                                h["watch_status"] = "sold"

        except Exception as e:
            print(f"[PORTFOLIO] Scan loop error: {e}")
            with _portfolio_state_lock:
                if _portfolio_state:
                    _portfolio_state["scan_errors"] = _portfolio_state.get("scan_errors", 0) + 1

        _portfolio_stop_event.wait(PORTFOLIO_SCAN_INTERVAL)


def _start_portfolio_thread():
    """Lazy-start the portfolio scanner daemon thread."""
    global _portfolio_thread
    if _portfolio_thread and _portfolio_thread.is_alive():
        return
    _portfolio_stop_event.clear()
    _portfolio_thread = threading.Thread(
        target=_portfolio_scan_loop, daemon=True, name="portfolio-monitor")
    _portfolio_thread.start()


def _stop_portfolio_thread():
    """Stop the portfolio scanner thread."""
    global _portfolio_thread
    _portfolio_stop_event.set()
    if _portfolio_thread:
        _portfolio_thread.join(timeout=1)
        _portfolio_thread = None


# ---------------------------------------------------------------------------
# Core Analysis Engine
# ---------------------------------------------------------------------------
def analyze(mint, dev_wallet=None, recent_signatures=None, options=None):
    """
    Main analysis v2.0: returns complete risk assessment with robust scoring.
    v5.0.0: Parallelized RPC calls for ~50% speed improvement.

    Scoring philosophy: Start skeptical (98.6% of pump.fun tokens are scams).
    Only lower risk when positive on-chain signals are confirmed.

    If dev_wallet is not provided, auto-detects it from the mint's
    on-chain history (deployer signer, mint authority, first recipient).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    triggered_signals = []
    risk_score = 0

    # Grab Jupiter key early (before we enter the thread pool)
    try:
        jupiter_api_key = request.headers.get("X-Jupiter-Api-Key", "")
    except RuntimeError:
        jupiter_api_key = ""

    # =====================================================================
    # PHASE 1 (parallel): Mint info + LP check run concurrently
    # LP check is fully independent — start it immediately
    # =====================================================================
    with ThreadPoolExecutor(max_workers=6) as pool:
        fut_lp = pool.submit(_check_liquidity_pool, mint, jupiter_api_key)
        fut_account = pool.submit(fetch_account_info, mint)

        # Wait for mint info — everything else depends on it
        raw_account = fut_account.result()
        mint_info = parse_mint_info(raw_account)

        mint_authority_revoked = mint_info.get("mint_authority_revoked")
        freeze_authority_revoked = mint_info.get("freeze_authority_revoked")
        supply = mint_info.get("supply")
        decimals = mint_info.get("decimals")
        parse_error = mint_info.get("parse_error")

        if parse_error:
            triggered_signals.append(f"WARN: Mint parse issue: {parse_error}")
            risk_score += 5

        # --- Authority Risk (max 30) ---
        if mint_authority_revoked is False:
            risk_score += 25
            triggered_signals.append("CRITICAL: Mint authority ACTIVE — infinite mint risk (+25)")
        elif mint_authority_revoked is True:
            triggered_signals.append("OK: Mint authority revoked")

        if freeze_authority_revoked is False:
            risk_score += 15
            triggered_signals.append("HIGH: Freeze authority ACTIVE — honeypot risk (+15)")
        elif freeze_authority_revoked is True:
            triggered_signals.append("OK: Freeze authority revoked")

        # =================================================================
        # PHASE 2 (parallel): Dev detection + Holder concentration
        # Both depend on mint_info/supply from Phase 1, run together
        # =================================================================
        fut_concentration = pool.submit(analyze_holder_concentration, mint, supply)

        dev_detection = None
        dev_wallet_source = "user_provided" if dev_wallet else None
        fut_dev = None
        if not dev_wallet:
            fut_dev = pool.submit(detect_dev_wallet, mint, mint_info)

        # Wait for dev detection (critical path — other steps need dev_wallet)
        if fut_dev:
            dev_detection = fut_dev.result()
            detected = dev_detection.get("probable_dev_wallet")
            if detected:
                dev_wallet = detected
                dev_wallet_source = dev_detection.get("detection_method", "auto")
                triggered_signals.append(
                    f"AUTO-DETECT: Dev wallet identified via {dev_detection['detection_method']} "
                    f"(confidence: {dev_detection['confidence']}): {dev_wallet[:8]}...{dev_wallet[-6:]}"
                )
            else:
                triggered_signals.append("WARN: Could not auto-detect dev wallet — unknown origin (+8)")
                risk_score += 8

        # --- Token Age (uses dev_detection, no RPC) ---
        token_age_hours = None
        creation_ts = None
        if dev_detection and dev_detection.get("creation_blocktime"):
            creation_ts = dev_detection["creation_blocktime"]
            age_seconds = time.time() - creation_ts
            token_age_hours = round(age_seconds / 3600, 1)

            if token_age_hours < 1:
                risk_score += 15
                triggered_signals.append(
                    f"CRITICAL: Token is {token_age_hours:.1f}h old (<1h, extremely new, +15)"
                )
            elif token_age_hours < 6:
                risk_score += 10
                triggered_signals.append(
                    f"HIGH: Token is {token_age_hours:.1f}h old (<6h, very new, +10)"
                )
            elif token_age_hours < 24:
                risk_score += 5
                triggered_signals.append(
                    f"MODERATE: Token is {token_age_hours:.1f}h old (<24h, new, +5)"
                )
            elif token_age_hours < 72:
                risk_score += 3
                triggered_signals.append(
                    f"LOW: Token is {token_age_hours:.1f}h old (<72h, +3)"
                )
            else:
                triggered_signals.append(
                    f"OK: Token is {token_age_hours:.1f}h old (>{token_age_hours/24:.0f} days)"
                )
        else:
            triggered_signals.append("WARN: Could not determine token age")

        # =================================================================
        # PHASE 3 (parallel): Dev holdings + TX analysis
        # Both depend on dev_wallet from Phase 2, run together
        # =================================================================
        fut_dev_ta = None
        fut_tx = None
        if dev_wallet:
            fut_dev_ta = pool.submit(fetch_token_accounts_by_owner, dev_wallet, mint)
        if dev_wallet or recent_signatures:
            fut_tx = pool.submit(analyze_transactions, dev_wallet, mint, None, recent_signatures)

        # --- Collect holder concentration result (should be done by now) ---
        concentration = fut_concentration.result()
        if concentration.get("error"):
            triggered_signals.append(
                f"WARN: Holder concentration check failed: {concentration['error']}"
            )
        else:
            risk_score += concentration["concentration_score"]
            triggered_signals.extend(concentration["signals"])
            if not concentration["signals"]:
                triggered_signals.append(
                    f"OK: Holder distribution looks reasonable "
                    f"(top1={concentration['top1_pct']}%, top5={concentration['top5_pct']}%, "
                    f"top10={concentration['top10_pct']}%)"
                )

        # --- Supply Change (no RPC, just cache comparison) ---
        supply_change_flag = False
        prev_snapshot = _snapshot_cache.get(mint)
        if prev_snapshot and supply:
            prev_supply = prev_snapshot[1].get("supply")
            if prev_supply and str(supply) != str(prev_supply):
                try:
                    curr, prev_val = int(supply), int(prev_supply)
                    if curr > prev_val:
                        supply_change_flag = True
                        risk_score += 20
                        pct = ((curr - prev_val) / prev_val * 100) if prev_val > 0 else 999
                        triggered_signals.append(
                            f"CRITICAL: Supply INCREASED ({prev_val} -> {curr}, +{pct:.1f}%, +20)"
                        )
                        if mint_authority_revoked is False:
                            triggered_signals.append(
                                "EXTREME: Supply up AND mint authority active"
                            )
                except (ValueError, TypeError):
                    pass

        # --- Collect dev holdings result ---
        dev_holdings = None
        if dev_wallet and fut_dev_ta:
            raw_ta = fut_dev_ta.result()
            token_accounts = parse_token_accounts_for_mint(raw_ta, mint)

            total_dev_tokens = sum(int(ta.get("amount", "0")) for ta in token_accounts)
            pct_supply = None
            if supply and int(supply) > 0:
                pct_supply = (total_dev_tokens / int(supply)) * 100

            dev_holdings = {
                "wallet": dev_wallet,
                "wallet_source": dev_wallet_source,
                "token_amount": str(total_dev_tokens),
                "token_accounts": len(token_accounts),
                "percent_supply": round(pct_supply, 2) if pct_supply is not None else None,
            }

            if pct_supply is not None:
                if pct_supply > 20:
                    risk_score += 20
                    triggered_signals.append(
                        f"CRITICAL: Dev holds {pct_supply:.1f}% of supply (>20%, dump risk, +20)"
                    )
                elif pct_supply > 5:
                    risk_score += 12
                    triggered_signals.append(
                        f"HIGH: Dev holds {pct_supply:.1f}% of supply (5-20%, +12)"
                    )
                elif pct_supply > 0.01:
                    triggered_signals.append(
                        f"OK: Dev holds {pct_supply:.1f}% of supply (small position)"
                    )
                else:
                    risk_score += 10
                    triggered_signals.append(
                        f"HIGH: Dev holds 0% — already dumped all tokens (+10)"
                    )
            elif not token_accounts:
                risk_score += 10
                triggered_signals.append(
                    "HIGH: Dev wallet has zero token accounts — fully exited (+10)"
                )
        elif not dev_wallet and not dev_detection:
            triggered_signals.append("INFO: No dev wallet provided — dev exposure skipped")

        # --- Collect TX analysis result ---
        tx_signals = {
            "large_outbound_count": 0, "fanout_flag": False,
            "total_tx_analyzed": 0, "unique_destination_count": 0,
            "dev_recent_tx_count": 0, "notes": [],
        }

        if fut_tx:
            tx_signals = fut_tx.result()

            if tx_signals["large_outbound_count"] > 0:
                risk_score += 10
                triggered_signals.append(
                    f"HIGH: {tx_signals['large_outbound_count']} outbound transfers from dev (+10)"
                )
            if tx_signals["fanout_flag"]:
                risk_score += 10
                triggered_signals.append(
                    f"HIGH: Fan-out to {tx_signals['unique_destination_count']} wallets (+10)"
                )

            if dev_wallet and tx_signals["total_tx_analyzed"] == 0:
                risk_score += 5
                triggered_signals.append(
                    "MODERATE: Dev wallet has no analyzable recent transactions — ghost wallet (+5)"
                )
        elif not dev_wallet and not recent_signatures:
            triggered_signals.append("INFO: No dev wallet/signatures — tx analysis skipped")

        # --- Collect LP analysis result (should be long done by now) ---
        lp_analysis = None
        try:
            lp_analysis = fut_lp.result()
            if lp_analysis:
                if not lp_analysis.get("has_liquidity"):
                    risk_score += 15
                    triggered_signals.append(
                        "CRITICAL: No tradeable liquidity found on any DEX (+15)")
                elif lp_analysis.get("liquidity_locked") is False:
                    risk_score += 10
                    triggered_signals.append(
                        f"HIGH: Liquidity NOT locked (LP burn: {lp_analysis.get('lp_burn_pct', 0):.1f}%, +10)")
                elif lp_analysis.get("liquidity_locked") is True:
                    triggered_signals.append(
                        f"OK: Liquidity locked (LP burn: {lp_analysis.get('lp_burn_pct', 0):.1f}%)")
                for sig in lp_analysis.get("signals", []):
                    if sig not in triggered_signals:
                        triggered_signals.append(f"LP: {sig}")
        except Exception as e:
            triggered_signals.append(f"WARN: LP check failed: {str(e)[:100]}")

    # =====================================================================
    # STEP 8: Decision — STRICTER THRESHOLDS (v2.0)
    # =====================================================================
    risk_score = min(risk_score, 100)

    if risk_score <= 10:
        decision, risk_level = "BUY", "LOW"
    elif risk_score <= 35:
        decision, risk_level = "CAUTION", "MODERATE"
    elif risk_score <= 60:
        decision, risk_level = "NO_BUY", "HIGH"
    else:
        decision, risk_level = "NO_BUY", "EXTREME"

    # Override: supply inflated + mint authority active = always EXTREME
    if supply_change_flag and mint_authority_revoked is False:
        decision, risk_level = "NO_BUY", "EXTREME"
        risk_score = max(risk_score, 85)

    result = {
        "mint": mint,
        "mint_authority_revoked": mint_authority_revoked,
        "freeze_authority_revoked": freeze_authority_revoked,
        "supply": supply,
        "decimals": decimals,
        "token_age_hours": token_age_hours,
        "creation_timestamp": (
            datetime.fromtimestamp(creation_ts, tz=timezone.utc).isoformat()
            if creation_ts else None
        ),
        "holder_concentration": {
            "top1_pct": concentration.get("top1_pct"),
            "top5_pct": concentration.get("top5_pct"),
            "top10_pct": concentration.get("top10_pct"),
            "top20_pct": concentration.get("top20_pct"),
            "num_holders_above_1pct": concentration.get("num_holders_above_1pct"),
        },
        "dev_wallet_detection": dev_detection,
        "dev_holdings": dev_holdings,
        "tx_signals": tx_signals,
        "supply_change_flag": supply_change_flag,
        "liquidity_pool": lp_analysis,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "decision": decision,
        "triggered_signals": triggered_signals,
        "timestamp": timestamp,
        "version": "5.0.0",
    }

    _cache_set(_snapshot_cache, mint, {
        "supply": supply,
        "mint_authority_revoked": mint_authority_revoked,
        "freeze_authority_revoked": freeze_authority_revoked,
        "dev_wallet": dev_wallet,
        "risk_score": risk_score,
        "decision": decision,
        "timestamp": timestamp,
    })

    return result


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    with _watched_mints_lock:
        watched_count = len(_watched_mints)
        watched_list = list(_watched_mints.keys())
    with _global_alerts_lock:
        alert_count = len(_global_alerts)
    with _portfolio_state_lock:
        portfolio_active = (
            _portfolio_thread is not None and _portfolio_thread.is_alive()
        )
        portfolio_wallet = _portfolio_state.get("wallet_address", "")
        portfolio_scans = _portfolio_state.get("scan_count", 0)
        portfolio_holdings = len(_portfolio_state.get("holdings", {}))

    return jsonify({
        "status": "ok",
        "service": "rapid-rug-filter",
        "version": "5.0.0",
        "features": ["auto_dev_detection", "authority_check", "supply_monitor",
                      "dev_holdings", "tx_pattern_analysis",
                      "holder_concentration", "token_age", "dev_dump_penalty",
                      "strict_thresholds", "whale_surveillance", "dev_dump_monitor",
                      "fanout_detection", "exchange_exit_detection",
                      "jupiter_auto_sell", "lp_lock_analysis", "manual_sell",
                      "portfolio_autopilot", "auto_watch", "wallet_monitoring"],
        "auto_sell_available": SOLDERS_AVAILABLE,
        "surveillance": {
            "active": _monitor_thread is not None and _monitor_thread.is_alive(),
            "watched_mints": watched_count,
            "watched_list": watched_list,
            "total_alerts": alert_count,
            "max_capacity": MONITOR_MAX_MINTS,
        },
        "portfolio_autopilot": {
            "active": portfolio_active,
            "wallet": (portfolio_wallet[:12] + "...") if portfolio_wallet else None,
            "scan_count": portfolio_scans,
            "holdings_count": portfolio_holdings,
        },
        "rpc_endpoint": SOLANA_RPC_URL[:50] + "...",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/analyze", methods=["POST"])
def analyze_endpoint():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint or not isinstance(mint, str) or len(mint) < 32:
        return jsonify({"error": "Missing or invalid 'mint'. Must be a Solana mint address."}), 400

    dev_wallet = data.get("dev_wallet")  # Optional — auto-detected if not provided
    recent_signatures = data.get("recent_signatures", [])
    options = data.get("options", {})

    try:
        result = analyze(mint, dev_wallet, recent_signatures, options)
        return jsonify(result)
    except Exception as e:
        return jsonify({
            "error": "analysis_failed",
            "detail": str(e)[:500],
            "mint": mint,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


@app.route("/snapshot", methods=["GET"])
def snapshot_endpoint():
    mint = request.args.get("mint")
    if not mint:
        return jsonify({"error": "Missing 'mint' query parameter"}), 400

    cached = _snapshot_cache.get(mint)
    if cached:
        return jsonify({
            "mint": mint,
            "snapshot": cached[1],
            "cached_at": datetime.fromtimestamp(cached[0], tz=timezone.utc).isoformat(),
            "age_seconds": round(time.time() - cached[0], 1),
        })
    return jsonify({"mint": mint, "snapshot": None, "message": "No snapshot. Run /analyze first."}), 404


# ---------------------------------------------------------------------------
# Surveillance Routes (v3.0)
# ---------------------------------------------------------------------------
@app.route("/watch", methods=["POST"])
def watch_endpoint():
    """Register a mint for continuous surveillance after entry.
    v4.0: Accepts auto_sell=true to enable Jupiter auto-sell on rug detection.
    Wallet key and Jupiter key read from headers (OnDemand tool config)."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint or not isinstance(mint, str) or len(mint) < 32:
        return jsonify({"error": "Missing or invalid 'mint'. Must be a Solana mint address."}), 400

    poll_interval = data.get("poll_interval", MONITOR_DEFAULT_INTERVAL)

    # v4.0: Auto-sell configuration
    auto_sell = data.get("auto_sell", False)
    auto_sell_slippage = data.get("auto_sell_slippage_bps", AUTO_SELL_DEFAULT_SLIPPAGE_BPS)
    auto_sell_severity = data.get("auto_sell_severity", AUTO_SELL_SEVERITY_THRESHOLD)

    # Read wallet key and Jupiter key from headers (OnDemand tool config)
    wallet_private_key = request.headers.get("X-Wallet-Private-Key", "")
    jupiter_api_key = request.headers.get("X-Jupiter-Api-Key", "")

    # Validate auto-sell requirements
    if auto_sell:
        if not SOLDERS_AVAILABLE:
            return jsonify({"error": "Auto-sell unavailable: solders library not installed"}), 500
        if not wallet_private_key:
            return jsonify({"error": "Auto-sell requires X-Wallet-Private-Key header"}), 400

    with _watched_mints_lock:
        if mint in _watched_mints:
            return jsonify({
                "status": "already_watching",
                "mint": mint,
                "entry_timestamp": _watched_mints[mint]["entry_timestamp"],
            }), 200
        if len(_watched_mints) >= MONITOR_MAX_MINTS:
            return jsonify({
                "error": f"Max {MONITOR_MAX_MINTS} concurrent watches. Unwatch a mint first.",
                "currently_watching": list(_watched_mints.keys()),
            }), 429

    try:
        # Fetch mint info
        raw_mint = fetch_account_info(mint)
        mint_info = parse_mint_info(raw_mint)
        supply = mint_info.get("supply")
        decimals = mint_info.get("decimals")

        if not supply:
            return jsonify({"error": "Could not fetch mint info or supply is null", "mint": mint}), 400

        # Detect dev wallet
        dev_detection = detect_dev_wallet(mint, mint_info)
        dev_wallet = dev_detection.get("probable_dev_wallet")
        dev_confidence = dev_detection.get("confidence", "none")

        # Resolve whale wallets (top 10)
        holders_raw = fetch_token_largest_accounts(mint)
        whale_owners = _resolve_whale_owners(mint, holders_raw, top_n=10)
        whale_wallets = [w for w, _ in whale_owners]

        # Take entry snapshot
        entry_snapshot = _take_snapshot(mint, dev_wallet, whale_wallets, supply)

        now_iso = datetime.now(timezone.utc).isoformat()

        entry = {
            "mint": mint,
            "entry_timestamp": now_iso,
            "dev_wallet": dev_wallet,
            "dev_confidence": dev_confidence,
            "whale_wallets": whale_wallets,
            "supply": supply,
            "decimals": decimals,
            "entry_snapshot": entry_snapshot,
            "current_snapshot": entry_snapshot,
            "history": deque(maxlen=MONITOR_HISTORY_MAXLEN),
            "flags": {
                "dev_dump_flag": False,
                "dev_severe_dump_flag": False,
                "dev_distribution_flag": False,
                "whale_dump_flag": False,
                "whale_distribution_flag": False,
                "top_holder_exit_flag": False,
            },
            "alerts": [],
            "severity": "NONE",
            "last_checked": time.time(),
            "poll_interval": poll_interval,
            # v4.0: Auto-sell config stored in entry
            "auto_sell": auto_sell,
            "auto_sell_slippage_bps": auto_sell_slippage,
            "auto_sell_severity": auto_sell_severity,
            "auto_sell_executed": False,
            "auto_sell_result": None,
            "wallet_private_key": wallet_private_key if auto_sell else "",
            "jupiter_api_key": jupiter_api_key if auto_sell else "",
        }
        entry["history"].append((entry_snapshot["timestamp"], entry_snapshot))

        with _watched_mints_lock:
            _watched_mints[mint] = entry

        # Start monitor if not running
        _start_monitor_thread()

        return jsonify({
            "status": "watching",
            "mint": mint,
            "dev_wallet": dev_wallet,
            "dev_confidence": dev_confidence,
            "whale_wallets_count": len(whale_wallets),
            "entry_snapshot": {
                "dev_balance": entry_snapshot["dev_balance"],
                "whale_total": entry_snapshot["whale_total"],
                "timestamp": now_iso,
            },
            "poll_interval": poll_interval,
            "supply": supply,
            "auto_sell_enabled": auto_sell,
            "auto_sell_severity": auto_sell_severity if auto_sell else None,
        }), 201

    except Exception as e:
        return jsonify({
            "error": "watch_failed",
            "detail": str(e)[:500],
            "mint": mint,
        }), 500


@app.route("/status", methods=["GET"])
def status_endpoint():
    """Get current surveillance status and flags for a watched mint."""
    mint = request.args.get("mint")
    if not mint:
        return jsonify({"error": "Missing 'mint' query parameter"}), 400

    with _watched_mints_lock:
        if mint not in _watched_mints:
            return jsonify({"error": "Mint not being watched", "mint": mint}), 404
        entry = copy.deepcopy(_watched_mints[mint])

    supply = int(entry["supply"]) if entry["supply"] else 0
    entry_snap = entry["entry_snapshot"]
    current_snap = entry["current_snapshot"]

    # Calculate change percentages
    dev_change_pct = None
    if entry_snap.get("dev_balance", 0) > 0:
        dev_change_pct = round(_calc_change_pct(
            entry_snap["dev_balance"], current_snap.get("dev_balance", 0)), 2)

    whale_change_pct = None
    if entry_snap.get("whale_total", 0) > 0:
        whale_change_pct = round(_calc_change_pct(
            entry_snap["whale_total"], current_snap.get("whale_total", 0)), 2)

    # Convert history deque to serializable list count
    history_len = len(entry.get("history", []))

    return jsonify({
        "mint": mint,
        "status": "watching",
        "entry_timestamp": entry["entry_timestamp"],
        "last_checked": datetime.fromtimestamp(
            entry["last_checked"], tz=timezone.utc).isoformat() if entry["last_checked"] else None,
        "flags": entry["flags"],
        "severity": entry["severity"],
        "dev_wallet": entry.get("dev_wallet"),
        "dev_confidence": entry.get("dev_confidence"),
        "whale_wallets_count": len(entry.get("whale_wallets", [])),
        "entry_snapshot": {
            "dev_balance": entry_snap.get("dev_balance", 0),
            "whale_total": entry_snap.get("whale_total", 0),
        },
        "current_snapshot": {
            "dev_balance": current_snap.get("dev_balance", 0),
            "whale_total": current_snap.get("whale_total", 0),
        },
        "changes": {
            "dev_balance_change_pct": dev_change_pct,
            "whale_total_change_pct": whale_change_pct,
        },
        "history_snapshots": history_len,
        "alerts_count": len(entry.get("alerts", [])),
        "recent_alerts": entry.get("alerts", [])[-5:],  # Last 5
        "poll_interval": entry.get("poll_interval", MONITOR_DEFAULT_INTERVAL),
        # v4.0: Auto-sell status
        "auto_sell": {
            "enabled": entry.get("auto_sell", False),
            "severity_threshold": entry.get("auto_sell_severity"),
            "executed": entry.get("auto_sell_executed", False),
            "result": entry.get("auto_sell_result"),
        },
    })


@app.route("/alerts", methods=["GET"])
def alerts_endpoint():
    """Get all surveillance alerts, with optional mint and severity filters."""
    mint_filter = request.args.get("mint")
    severity_filter = request.args.get("severity")

    with _global_alerts_lock:
        all_alerts = list(_global_alerts)

    if mint_filter:
        all_alerts = [a for a in all_alerts if a.get("mint") == mint_filter]
    if severity_filter:
        all_alerts = [a for a in all_alerts if a.get("severity") == severity_filter.upper()]

    return jsonify({
        "total": len(all_alerts),
        "alerts": all_alerts[-50:],  # Last 50
        "filters": {
            "mint": mint_filter,
            "severity": severity_filter,
        },
    })


@app.route("/unwatch", methods=["POST"])
def unwatch_endpoint():
    """Stop watching a mint."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint:
        return jsonify({"error": "Missing 'mint' in request body"}), 400

    with _watched_mints_lock:
        if mint not in _watched_mints:
            return jsonify({"error": "Mint not being watched", "mint": mint}), 404

        entry = _watched_mints.pop(mint)
        remaining = len(_watched_mints)

    # Stop monitor if no more mints
    if remaining == 0:
        _stop_monitor_thread()

    return jsonify({
        "status": "unwatched",
        "mint": mint,
        "was_watching_since": entry.get("entry_timestamp"),
        "final_severity": entry.get("severity", "NONE"),
        "total_alerts": len(entry.get("alerts", [])),
        "flags": entry.get("flags", {}),
        "remaining_watches": remaining,
        # v4.0: Include auto-sell result if it was executed
        "auto_sell_executed": entry.get("auto_sell_executed", False),
        "auto_sell_result": entry.get("auto_sell_result"),
    })


# ---------------------------------------------------------------------------
# Manual Sell + Emergency Exit (v4.1)
# ---------------------------------------------------------------------------
@app.route("/sell", methods=["POST"])
def sell_endpoint():
    """
    Sell tokens back to SOL via Jupiter.
    priority="EMERGENCY" → high CU bribe + wide slippage + retries + skipPreflight
    priority="NORMAL"    → standard fees + normal slippage + single attempt
    """
    if not SOLDERS_AVAILABLE:
        return jsonify({"error": "solders library not installed — sell unavailable"}), 500

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint or not isinstance(mint, str) or len(mint) < 32:
        return jsonify({"error": "Missing or invalid 'mint'"}), 400

    priority = data.get("priority", "EMERGENCY").upper()
    slippage_bps = data.get("slippage_bps")  # None = use profile default
    event_id = data.get("event_id")

    # Read keys: prefer headers (OnDemand config keys), fallback to body, then portfolio state
    wallet_key = request.headers.get("X-Wallet-Private-Key", "").strip()
    jupiter_key = request.headers.get("X-Jupiter-Api-Key", "").strip()

    # Fallback to body params (some agents pass keys in body per schema)
    if not wallet_key:
        body_key = data.get("wallet_private_key", "")
        if body_key and isinstance(body_key, str) and len(body_key) > 32 and "<" not in body_key:
            wallet_key = body_key
    if not jupiter_key:
        body_jup = data.get("jupiter_api_key", "")
        if body_jup and isinstance(body_jup, str) and len(body_jup) > 8 and "<" not in body_jup:
            jupiter_key = body_jup

    # Last resort: read from active portfolio state
    if not wallet_key or not jupiter_key:
        with _portfolio_state_lock:
            if _portfolio_state and _portfolio_state.get("active"):
                if not wallet_key:
                    wallet_key = _portfolio_state.get("wallet_private_key", "")
                if not jupiter_key:
                    jupiter_key = _portfolio_state.get("jupiter_api_key", "")

    if not wallet_key:
        return jsonify({"error": "Wallet private key required (header X-Wallet-Private-Key, body wallet_private_key, or active portfolio)"}), 400

    try:
        result = _auto_sell_position(
            mint, wallet_key, jupiter_key,
            slippage_bps=slippage_bps,
            priority=priority,
            event_id=event_id,
        )
        # Map specific errors to appropriate HTTP status codes
        if result.get("success"):
            status_code = 200
        elif "No token balance" in result.get("error", ""):
            status_code = 404  # Not found — token not in wallet
        elif "solders not installed" in result.get("error", ""):
            status_code = 503  # Service unavailable
        elif "Quote:" in result.get("error", "") or "SwapTx:" in result.get("error", ""):
            status_code = 502  # Bad gateway — upstream Jupiter error
        else:
            status_code = 500  # Genuine server error
        result["mint"] = mint
        result["timestamp"] = datetime.now(timezone.utc).isoformat()

        # If this mint is being watched, record the sell
        with _watched_mints_lock:
            if mint in _watched_mints:
                _watched_mints[mint]["auto_sell_executed"] = True
                _watched_mints[mint]["auto_sell_result"] = result

        return jsonify(result), status_code
    except Exception as e:
        return jsonify({
            "error": "sell_failed",
            "detail": str(e)[:500],
            "mint": mint,
        }), 500


# ---------------------------------------------------------------------------
# LP Check Endpoint (v4.0)
# ---------------------------------------------------------------------------
@app.route("/lp", methods=["POST"])
def lp_endpoint():
    """Check liquidity pool status for a token."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint or not isinstance(mint, str) or len(mint) < 32:
        return jsonify({"error": "Missing or invalid 'mint'"}), 400

    jupiter_key = request.headers.get("X-Jupiter-Api-Key", "")

    try:
        lp_result = _check_liquidity_pool(mint, jupiter_key)
        lp_result["mint"] = mint
        lp_result["timestamp"] = datetime.now(timezone.utc).isoformat()
        return jsonify(lp_result)
    except Exception as e:
        return jsonify({
            "error": "lp_check_failed",
            "detail": str(e)[:500],
            "mint": mint,
        }), 500


# ---------------------------------------------------------------------------
# Portfolio Auto-Pilot Routes (v5.0)
# ---------------------------------------------------------------------------
@app.route("/portfolio/start", methods=["POST"])
def portfolio_start_endpoint():
    """
    Start Portfolio Auto-Pilot: continuously monitor wallet holdings.
    Idempotent: if already running for same wallet, returns current state.

    Headers required:
      X-Wallet-Address: public key of wallet to monitor
      X-Wallet-Private-Key: private key for auto-sell
      X-Jupiter-Api-Key: Jupiter API key
    """
    wallet_address = request.headers.get("X-Wallet-Address", "").strip()
    wallet_private_key = request.headers.get("X-Wallet-Private-Key", "").strip()
    jupiter_api_key = request.headers.get("X-Jupiter-Api-Key", "").strip()

    if not wallet_address or len(wallet_address) < 32:
        return jsonify({"error": "X-Wallet-Address header required (Solana public key)"}), 400

    if not wallet_private_key:
        return jsonify({"error": "X-Wallet-Private-Key header required for auto-sell"}), 400

    if not SOLDERS_AVAILABLE:
        return jsonify({"error": "Portfolio Auto-Pilot requires solders library for auto-sell"}), 500

    # Idempotent: if already running for this wallet, return current state
    with _portfolio_state_lock:
        if _portfolio_state and _portfolio_state.get("active"):
            if _portfolio_state.get("wallet_address") == wallet_address:
                # Update keys in case they changed
                _portfolio_state["wallet_private_key"] = wallet_private_key
                _portfolio_state["jupiter_api_key"] = jupiter_api_key
                state_copy = copy.deepcopy(_portfolio_state)
                # Sanitize: remove private keys from response
                state_copy.pop("wallet_private_key", None)
                state_copy.pop("jupiter_api_key", None)
                # Convert events deque to list for JSON
                state_copy["events"] = list(state_copy.get("events", []))[-20:]
                return jsonify({
                    "status": "already_running",
                    "portfolio": {
                        "wallet_address": state_copy.get("wallet_address"),
                        "started_at": state_copy.get("started_at"),
                        "scan_count": state_copy.get("scan_count", 0),
                        "holdings_count": len(state_copy.get("holdings", {})),
                        "recent_events": state_copy["events"],
                    },
                }), 200

    # Auto-replace: portfolio running for a DIFFERENT wallet → stop old, start new
    with _portfolio_state_lock:
        if _portfolio_state and _portfolio_state.get("active"):
            running_wallet = _portfolio_state.get("wallet_address", "")
            if running_wallet != wallet_address:
                print(f"[PORTFOLIO] Auto-replacing wallet {running_wallet[:12]}... "
                      f"→ {wallet_address[:12]}...")
                _portfolio_add_event("PORTFOLIO_WALLET_REPLACED", None,
                    f"Auto-replacing wallet {running_wallet[:12]}... "
                    f"with {wallet_address[:12]}...")
                # Fall through to stop + restart below

    # Stop existing portfolio if running (same wallet restart case)
    _stop_portfolio_thread()

    now_iso = datetime.now(timezone.utc).isoformat()

    with _portfolio_state_lock:
        _portfolio_state.clear()
        _portfolio_state.update({
            "wallet_address": wallet_address,
            "wallet_private_key": wallet_private_key,
            "jupiter_api_key": jupiter_api_key,
            "active": True,
            "started_at": now_iso,
            "last_scan": 0.0,
            "scan_count": 0,
            "scan_errors": 0,
            "holdings": {},
            "events": deque(maxlen=PORTFOLIO_EVENTS_MAXLEN),
        })

    _portfolio_add_event("PORTFOLIO_STARTED", None,
        f"Portfolio Auto-Pilot started for wallet {wallet_address[:12]}...")

    _start_portfolio_thread()

    return jsonify({
        "status": "started",
        "wallet_address": wallet_address,
        "started_at": now_iso,
        "scan_interval": PORTFOLIO_SCAN_INTERVAL,
        "ignore_mints": list(PORTFOLIO_IGNORE_MINTS),
        "auto_sell_enabled": True,
        "auto_sell_priority": "EMERGENCY",
        "message": "Portfolio Auto-Pilot active. First scan in progress.",
    }), 201


@app.route("/portfolio", methods=["GET"])
def portfolio_endpoint():
    """
    Get current portfolio state: all holdings, watch status, events.
    This is the source of truth for what the wallet holds.
    """
    with _portfolio_state_lock:
        if not _portfolio_state or not _portfolio_state.get("active"):
            return jsonify({
                "status": "inactive",
                "message": "Portfolio Auto-Pilot is not running. Call POST /portfolio/start first.",
            }), 404
        state_copy = copy.deepcopy(_portfolio_state)

    # Sanitize private keys
    state_copy.pop("wallet_private_key", None)
    state_copy.pop("jupiter_api_key", None)

    # Convert events deque to list
    events_list = list(state_copy.get("events", []))

    # Enrich holdings with live surveillance data
    enriched_holdings = {}
    with _watched_mints_lock:
        for mint, holding in state_copy.get("holdings", {}).items():
            enriched = dict(holding)
            if mint in _watched_mints:
                wm = _watched_mints[mint]
                enriched["severity"] = wm.get("severity", "NONE")
                enriched["flags"] = wm.get("flags", {})
                enriched["alerts_count"] = len(wm.get("alerts", []))
                enriched["recent_alerts"] = [
                    {"trigger": a.get("trigger"), "severity": a.get("severity"),
                     "message": a.get("message"), "timestamp": a.get("timestamp")}
                    for a in wm.get("alerts", [])[-3:]
                ]
                enriched["auto_sell_executed"] = wm.get("auto_sell_executed", False)
                if wm.get("auto_sell_result"):
                    enriched["auto_sell_result"] = {
                        "success": wm["auto_sell_result"].get("success"),
                        "signature": wm["auto_sell_result"].get("signature"),
                        "tokens_sold": wm["auto_sell_result"].get("tokens_sold"),
                    }
            else:
                enriched.setdefault("severity", "NONE")
                enriched.setdefault("flags", {})
                enriched.setdefault("alerts_count", 0)
                enriched.setdefault("recent_alerts", [])
            enriched_holdings[mint] = enriched

    # Summary stats
    total_holdings = len(enriched_holdings)
    watching_count = sum(
        1 for h in enriched_holdings.values() if h.get("watch_status") == "watching"
    )
    sold_count = sum(
        1 for h in enriched_holdings.values() if h.get("watch_status") == "sold"
    )
    high_severity = sum(
        1 for h in enriched_holdings.values()
        if SEVERITY_ORDER.get(h.get("severity", "NONE"), 0) >= SEVERITY_ORDER.get("HIGH", 3)
    )

    return jsonify({
        "status": "active",
        "wallet_address": state_copy.get("wallet_address"),
        "started_at": state_copy.get("started_at"),
        "last_scan": (
            datetime.fromtimestamp(state_copy["last_scan"], tz=timezone.utc).isoformat()
            if state_copy.get("last_scan") else None
        ),
        "scan_count": state_copy.get("scan_count", 0),
        "scan_errors": state_copy.get("scan_errors", 0),
        "summary": {
            "total_holdings": total_holdings,
            "actively_watching": watching_count,
            "auto_sold": sold_count,
            "high_severity_alerts": high_severity,
        },
        "holdings": enriched_holdings,
        "recent_events": events_list[-30:],
        "version": "5.0.0",
    })


@app.route("/portfolio/stop", methods=["POST"])
def portfolio_stop_endpoint():
    """Stop Portfolio Auto-Pilot. Does NOT stop surveillance on already-watched tokens."""
    with _portfolio_state_lock:
        if not _portfolio_state or not _portfolio_state.get("active"):
            return jsonify({
                "error": "Portfolio Auto-Pilot is not running",
                "status": "not_active",
                "hint": "Call POST /portfolio/start first",
            }), 404
        state_copy = copy.deepcopy(_portfolio_state)
        _portfolio_state["active"] = False

    _stop_portfolio_thread()

    # Sanitize
    state_copy.pop("wallet_private_key", None)
    state_copy.pop("jupiter_api_key", None)
    events_list = list(state_copy.get("events", []))

    _portfolio_add_event("PORTFOLIO_STOPPED", None, "Portfolio Auto-Pilot stopped by user")

    return jsonify({
        "status": "stopped",
        "wallet_address": state_copy.get("wallet_address"),
        "ran_since": state_copy.get("started_at"),
        "total_scans": state_copy.get("scan_count", 0),
        "total_holdings_tracked": len(state_copy.get("holdings", {})),
        "note": "Surveillance continues on already-watched tokens. Use /unwatch to stop individual watches.",
        "final_events": events_list[-10:],
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "Rapid Rug Filter",
        "description": "Solana meme coin rug risk scanner with portfolio auto-pilot + auto-sell guardian + LP analysis",
        "version": "5.0.0",
        "endpoints": {
            "GET /health": "Health check with surveillance + portfolio status",
            "POST /analyze": "Analyze token mint for rug risk (includes LP check)",
            "GET /snapshot?mint=...": "Get cached analysis snapshot",
            "POST /watch": "Start surveillance with optional auto-sell protection",
            "GET /status?mint=...": "Get surveillance flags, severity, auto-sell status",
            "GET /alerts": "Get all surveillance alerts (filter by ?mint= and ?severity=)",
            "POST /unwatch": "Stop watching a mint after exit",
            "POST /sell": "Manual emergency sell via Jupiter (token → SOL)",
            "POST /lp": "Check liquidity pool lock status for a token",
            "POST /portfolio/start": "Start Portfolio Auto-Pilot: continuous wallet monitoring + auto-watch",
            "GET /portfolio": "Get live portfolio: all holdings, watch status, severity, events",
            "POST /portfolio/stop": "Stop portfolio monitoring (surveillance continues)",
        },
        "analyze_body": {
            "mint": "(required) Solana token mint address",
            "dev_wallet": "(optional) Override dev wallet — auto-detected if omitted",
            "recent_signatures": "(optional) List of tx signatures to analyze",
        },
        "watch_body": {
            "mint": "(required) Solana token mint address to watch",
            "poll_interval": "(optional) Seconds between polls, default 10",
            "auto_sell": "(optional) Enable auto-sell on rug detection, default false",
            "auto_sell_slippage_bps": "(optional) Slippage for emergency sell, default 500 (5%)",
            "auto_sell_severity": "(optional) Min severity to trigger, default HIGH",
        },
        "sell_body": {
            "mint": "(required) Token mint address to sell",
            "priority": "(optional) EMERGENCY or NORMAL — default EMERGENCY",
            "slippage_bps": "(optional) Override slippage, default 1200 emergency / 500 normal",
            "event_id": "(optional) Idempotency key — prevents duplicate sells",
        },
        "lp_body": {
            "mint": "(required) Token mint address to check LP for",
        },
        "unwatch_body": {
            "mint": "(required) Solana token mint address to stop watching",
        },
        "headers": {
            "X-Wallet-Address": "Solana wallet public key — for /portfolio/start (wallet to monitor)",
            "X-Wallet-Private-Key": "Solana wallet private key (base58) — for /sell, /watch, /portfolio auto_sell",
            "X-Jupiter-Api-Key": "Jupiter API key — for /sell, /watch, /portfolio, and /lp",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    app.run(host="0.0.0.0", port=port, debug=False)
