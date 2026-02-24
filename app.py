"""
Rapid Rug Filter - Solana Meme Coin Structural Rug Risk Scanner
Production-grade REST API microservice for OnDemand agent tool calling.

v1.2.0 — Auto dev wallet detection from mint address.
Uses Solana JSON-RPC directly (public mainnet endpoint or Helius).
No RapidAPI dependency — calls Solana chain nodes directly for speed.
"""

import os
import time
import json
import struct
import base64
from datetime import datetime, timezone
from flask import Flask, request, jsonify

import requests as http_requests

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
# Solana JSON-RPC Client
# ---------------------------------------------------------------------------
def _rpc_call(method, params, timeout=12):
    """Make a Solana JSON-RPC call."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    try:
        resp = http_requests.post(
            SOLANA_RPC_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code == 429:
            time.sleep(0.5)  # Brief backoff on rate limit
            return {"error": "rate_limited", "detail": "Solana RPC rate limit hit"}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}", "detail": resp.text[:500]}
        data = resp.json()
        if "error" in data:
            return {"error": "rpc_error", "detail": data["error"]}
        return data
    except http_requests.exceptions.Timeout:
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


def fetch_token_accounts_by_owner(owner, mint=None):
    """getTokenAccountsByOwner with optional mint filter."""
    cache_key = f"tao:{owner}:{mint or 'all'}"
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
        time.sleep(0.2)  # Rate limit courtesy

    if not all_sigs:
        result["detection_notes"].append("No signatures found for mint")
        return

    # The last entry in all_sigs (oldest) is the earliest transaction
    earliest_sig = all_sigs[-1]
    if isinstance(earliest_sig, dict):
        earliest_sig = earliest_sig.get("signature", "")

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

    for sig in sig_list[:8]:
        tx_raw = fetch_transaction(sig)
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
# Core Analysis Engine
# ---------------------------------------------------------------------------
def analyze(mint, dev_wallet=None, recent_signatures=None, options=None):
    """
    Main analysis: returns complete risk assessment.

    If dev_wallet is not provided, auto-detects it from the mint's
    on-chain history (deployer signer, mint authority, first recipient).
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    triggered_signals = []
    risk_score = 0

    # -- Step 1: Mint Account Info --
    raw_account = fetch_account_info(mint)
    mint_info = parse_mint_info(raw_account)

    mint_authority_revoked = mint_info.get("mint_authority_revoked")
    freeze_authority_revoked = mint_info.get("freeze_authority_revoked")
    supply = mint_info.get("supply")
    decimals = mint_info.get("decimals")
    parse_error = mint_info.get("parse_error")

    if parse_error:
        triggered_signals.append(f"WARN: Mint parse issue: {parse_error}")

    # Authority Risk (max 30)
    if mint_authority_revoked is False:
        risk_score += 20
        triggered_signals.append("CRITICAL: Mint authority ACTIVE — infinite mint risk (+20)")
    elif mint_authority_revoked is True:
        triggered_signals.append("OK: Mint authority revoked")

    if freeze_authority_revoked is False:
        risk_score += 10
        triggered_signals.append("HIGH: Freeze authority ACTIVE — honeypot risk (+10)")
    elif freeze_authority_revoked is True:
        triggered_signals.append("OK: Freeze authority revoked")

    # -- Step 2: Auto-detect dev wallet if not provided --
    dev_detection = None
    dev_wallet_source = "user_provided" if dev_wallet else None

    if not dev_wallet:
        dev_detection = detect_dev_wallet(mint, mint_info)
        detected = dev_detection.get("probable_dev_wallet")
        if detected:
            dev_wallet = detected
            dev_wallet_source = dev_detection.get("detection_method", "auto")
            triggered_signals.append(
                f"AUTO-DETECT: Dev wallet identified via {dev_detection['detection_method']} "
                f"(confidence: {dev_detection['confidence']}): {dev_wallet[:8]}...{dev_wallet[-6:]}"
            )
        else:
            triggered_signals.append("INFO: Could not auto-detect dev wallet")

    # -- Step 3: Supply Change (max 20) --
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

    # -- Step 4: Dev Holdings (max 20) --
    dev_holdings = None
    if dev_wallet:
        raw_ta = fetch_token_accounts_by_owner(dev_wallet, mint=mint)
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
            if pct_supply > 15:
                risk_score += 20
                triggered_signals.append(f"HIGH: Dev holds {pct_supply:.1f}% of supply (>15%, +20)")
            elif pct_supply > 5:
                risk_score += 10
                triggered_signals.append(f"MODERATE: Dev holds {pct_supply:.1f}% of supply (5-15%, +10)")
            else:
                triggered_signals.append(f"OK: Dev holds {pct_supply:.1f}% of supply (<5%)")
        elif not token_accounts:
            triggered_signals.append("INFO: Dev wallet has no token accounts for this mint")
    else:
        triggered_signals.append("INFO: No dev wallet found — dev exposure skipped")

    # -- Step 5: Transaction Signals (max 30) --
    tx_signals = {
        "large_outbound_count": 0, "fanout_flag": False,
        "total_tx_analyzed": 0, "unique_destination_count": 0, "notes": [],
    }

    if dev_wallet or recent_signatures:
        tx_signals = analyze_transactions(dev_wallet, mint, provided_sigs=recent_signatures)

        if tx_signals["large_outbound_count"] > 0:
            risk_score += 15
            triggered_signals.append(
                f"HIGH: {tx_signals['large_outbound_count']} outbound transfers from dev (+15)"
            )
        if tx_signals["fanout_flag"]:
            risk_score += 15
            triggered_signals.append(
                f"HIGH: Fan-out to {tx_signals['unique_destination_count']} wallets (+15)"
            )
    else:
        triggered_signals.append("INFO: No dev wallet/signatures — tx analysis skipped")

    # -- Step 6: Decision --
    risk_score = min(risk_score, 100)

    if risk_score <= 25:
        decision, risk_level = "BUY", "LOW"
    elif risk_score <= 50:
        decision, risk_level = "CAUTION", "MODERATE"
    elif risk_score <= 75:
        decision, risk_level = "NO_BUY", "HIGH"
    else:
        decision, risk_level = "NO_BUY", "EXTREME"

    if supply_change_flag and mint_authority_revoked is False:
        decision, risk_level = "NO_BUY", "EXTREME"
        risk_score = max(risk_score, 80)

    result = {
        "mint": mint,
        "mint_authority_revoked": mint_authority_revoked,
        "freeze_authority_revoked": freeze_authority_revoked,
        "supply": supply,
        "decimals": decimals,
        "dev_wallet_detection": dev_detection,
        "dev_holdings": dev_holdings,
        "tx_signals": tx_signals,
        "supply_change_flag": supply_change_flag,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "decision": decision,
        "triggered_signals": triggered_signals,
        "timestamp": timestamp,
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
    return jsonify({
        "status": "ok",
        "service": "rapid-rug-filter",
        "version": "1.2.0",
        "features": ["auto_dev_detection", "authority_check", "supply_monitor",
                      "dev_holdings", "tx_pattern_analysis"],
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


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "Rapid Rug Filter",
        "description": "Solana meme coin structural rug risk scanner with auto dev wallet detection",
        "version": "1.2.0",
        "endpoints": {
            "GET /health": "Health check",
            "POST /analyze": "Analyze token mint for rug risk (auto-detects dev wallet)",
            "GET /snapshot?mint=...": "Get cached snapshot",
        },
        "analyze_body": {
            "mint": "(required) Solana token mint address",
            "dev_wallet": "(optional) Override dev wallet — auto-detected if omitted",
            "recent_signatures": "(optional) List of tx signatures to analyze",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    app.run(host="0.0.0.0", port=port, debug=False)
