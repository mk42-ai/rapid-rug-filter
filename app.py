"""
Rapid Rug Filter - Solana Meme Coin Structural Rug Risk Scanner
Production-grade REST API microservice for OnDemand agent tool calling.

Uses RapidAPI "Solana & Near crypto APIs" (APISOLUTION) as primary data provider.
"""

import os
import time
import json
import hashlib
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, request, jsonify

import requests as http_requests

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "solana-near-crypto-apis.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}/solana"
NETWORK = "mainnet-beta"  # production Solana network

CACHE_TTL_SECONDS = 20  # short TTL for pump scenarios
SNAPSHOT_TTL_SECONDS = 120  # snapshot cache lives longer

# In-memory caches (use Redis in true production)
_account_cache = {}   # key -> (timestamp, data)
_snapshot_cache = {}  # mint -> (timestamp, analysis_result)

# SPL Token Program ID
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rapid_headers():
    return {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }


def _cache_get(cache, key, ttl):
    entry = cache.get(key)
    if entry and (time.time() - entry[0]) < ttl:
        return entry[1]
    return None


def _cache_set(cache, key, data):
    cache[key] = (time.time(), data)


def _rapid_get(endpoint, params, timeout=8):
    """Call a RapidAPI GET endpoint with rate-limit handling."""
    url = f"{RAPIDAPI_BASE}/{endpoint}"
    params["network"] = NETWORK
    try:
        resp = http_requests.get(url, headers=_rapid_headers(), params=params, timeout=timeout)
        # Handle rate limits
        if resp.status_code == 429:
            return {"error": "rate_limited", "detail": "RapidAPI rate limit hit. Retry after cooldown."}
        if resp.status_code != 200:
            return {"error": f"http_{resp.status_code}", "detail": resp.text[:500]}
        return resp.json()
    except http_requests.exceptions.Timeout:
        return {"error": "timeout", "detail": f"Request to {endpoint} timed out"}
    except Exception as e:
        return {"error": "request_failed", "detail": str(e)[:300]}


# ---------------------------------------------------------------------------
# RapidAPI Data Fetchers
# ---------------------------------------------------------------------------
def fetch_account_info(pubkey):
    """Fetch account info for a Solana public key (mint or wallet)."""
    cache_key = f"acct:{pubkey}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rapid_get("getAccountInfo", {"pubKey": pubkey})
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_token_accounts_by_owner(owner):
    """Fetch all token accounts owned by a wallet."""
    cache_key = f"tao:{owner}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rapid_get("getTokenAccountsByOwner", {"pubKey": owner})
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_token_account_balance(token_account_pubkey):
    """Fetch balance of a specific token account."""
    data = _rapid_get("getTokenAccountBalance", {"pubKey": token_account_pubkey})
    return data


def fetch_transaction_detail(pubkey, number=10):
    """Fetch recent transaction details for a public key."""
    cache_key = f"txd:{pubkey}:{number}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rapid_get("getTransactionDetail", {"pubKey": pubkey, "number": str(number)})
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_transaction_with_signature(signature):
    """Fetch transaction by signature."""
    data = _rapid_get("getTransitionWithSignature", {"pubKey": signature})
    return data


# ---------------------------------------------------------------------------
# Parsers - Extract structured data from RapidAPI responses
# ---------------------------------------------------------------------------
def parse_mint_info(raw):
    """
    Parse mint account info from getAccountInfo response.
    Handles both jsonParsed format and raw/base64 encoded data.
    Returns dict with: mint_authority, freeze_authority, supply, decimals, is_initialized
    """
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

    # Try to navigate the response structure
    # The API may return data in various nested formats
    value = None

    # Standard Solana RPC envelope: result.value
    if isinstance(raw, dict):
        if "result" in raw:
            r = raw["result"]
            if isinstance(r, dict) and "value" in r:
                value = r["value"]
            else:
                value = r
        elif "value" in raw:
            value = raw["value"]
        elif "data" in raw:
            value = raw
        else:
            # The whole response might be the value
            value = raw

    if not value:
        result["parse_error"] = "could_not_find_value_in_response"
        return result

    # Try to find parsed data
    data = value.get("data") if isinstance(value, dict) else None

    if isinstance(data, dict) and "parsed" in data:
        parsed = data["parsed"]
        info = parsed.get("info", {})
        ptype = parsed.get("type", "")

        if ptype == "mint" or "mintAuthority" in info or "supply" in info:
            ma = info.get("mintAuthority")
            fa = info.get("freezeAuthority")
            result["mint_authority"] = ma
            result["mint_authority_revoked"] = ma is None or ma == "" or ma == "null"
            result["freeze_authority"] = fa
            result["freeze_authority_revoked"] = fa is None or fa == "" or fa == "null"
            result["supply"] = info.get("supply")
            result["decimals"] = info.get("decimals")
            result["is_initialized"] = info.get("isInitialized", True)
            return result

    # If data is a list (base64 encoded), try to decode SPL mint layout
    if isinstance(data, list) and len(data) >= 1:
        try:
            import base64
            raw_bytes = base64.b64decode(data[0])
            if len(raw_bytes) >= 76:
                result = _decode_mint_bytes(raw_bytes)
                return result
        except Exception as e:
            result["parse_error"] = f"base64_decode_failed: {str(e)[:100]}"
            return result

    # If data is a string (base64), decode
    if isinstance(data, str):
        try:
            import base64
            raw_bytes = base64.b64decode(data)
            if len(raw_bytes) >= 76:
                result = _decode_mint_bytes(raw_bytes)
                return result
        except Exception:
            pass

    # Try to find the info directly in the value
    if isinstance(value, dict):
        for key in ["mintAuthority", "mint_authority"]:
            if key in value:
                ma = value.get("mintAuthority") or value.get("mint_authority")
                fa = value.get("freezeAuthority") or value.get("freeze_authority")
                result["mint_authority"] = ma
                result["mint_authority_revoked"] = ma is None or ma == "" or ma == "null"
                result["freeze_authority"] = fa
                result["freeze_authority_revoked"] = fa is None or fa == "" or fa == "null"
                result["supply"] = value.get("supply")
                result["decimals"] = value.get("decimals")
                result["is_initialized"] = value.get("isInitialized", True)
                return result

    result["parse_error"] = "unrecognized_response_format"
    return result


def _decode_mint_bytes(raw_bytes):
    """Decode SPL Token Mint account from raw bytes (82 bytes layout)."""
    import struct

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

    # Offset 0: COption<Pubkey> mintAuthority (1 + 32 = 33 bytes)
    ma_option = raw_bytes[0]
    if ma_option == 0:
        result["mint_authority"] = None
        result["mint_authority_revoked"] = True
    elif ma_option == 1:
        import base64
        ma_bytes = raw_bytes[1:33]
        # Convert to base58 (simplified - use hex for now, full b58 below)
        result["mint_authority"] = _bytes_to_base58(ma_bytes)
        result["mint_authority_revoked"] = False
    else:
        result["parse_error"] = f"invalid_mint_authority_option_byte: {ma_option}"
        return result

    # Offset 33: u64 supply (8 bytes LE)
    supply_raw = struct.unpack_from("<Q", raw_bytes, 33)[0]
    result["supply"] = str(supply_raw)

    # Offset 41: u8 decimals
    result["decimals"] = raw_bytes[41]

    # Offset 42: bool isInitialized
    result["is_initialized"] = raw_bytes[42] == 1

    # Offset 43: COption<Pubkey> freezeAuthority (1 + 32 = 33 bytes)
    fa_option = raw_bytes[43]
    if fa_option == 0:
        result["freeze_authority"] = None
        result["freeze_authority_revoked"] = True
    elif fa_option == 1:
        fa_bytes = raw_bytes[44:76]
        result["freeze_authority"] = _bytes_to_base58(fa_bytes)
        result["freeze_authority_revoked"] = False
    else:
        result["freeze_authority"] = None
        result["freeze_authority_revoked"] = True

    return result


def _bytes_to_base58(data):
    """Convert bytes to base58 string (Bitcoin/Solana alphabet)."""
    ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    result = bytearray()
    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(ALPHABET[remainder])
    # Count leading zeros
    for byte in data:
        if byte == 0:
            result.append(ALPHABET[0])
        else:
            break
    return bytes(result[::-1]).decode("ascii")


def parse_token_accounts_for_mint(raw, target_mint):
    """
    Parse getTokenAccountsByOwner response and filter for a specific mint.
    Returns list of {pubkey, mint, amount, decimals, ui_amount}.
    """
    accounts = []
    if not raw or "error" in raw:
        return accounts

    # Navigate response
    value_list = None
    if isinstance(raw, dict):
        if "result" in raw:
            r = raw["result"]
            if isinstance(r, dict) and "value" in r:
                value_list = r["value"]
            elif isinstance(r, list):
                value_list = r
        elif "value" in raw:
            value_list = raw["value"]
        elif isinstance(raw, list):
            value_list = raw

    if not value_list or not isinstance(value_list, list):
        return accounts

    for item in value_list:
        try:
            pubkey = item.get("pubkey", "")
            acct = item.get("account", {})
            data = acct.get("data", {})

            if isinstance(data, dict) and "parsed" in data:
                info = data["parsed"].get("info", {})
                mint = info.get("mint", "")
                token_amount = info.get("tokenAmount", {})

                if mint.lower() == target_mint.lower() or not target_mint:
                    accounts.append({
                        "pubkey": pubkey,
                        "mint": mint,
                        "amount": token_amount.get("amount", "0"),
                        "decimals": token_amount.get("decimals", 0),
                        "ui_amount": token_amount.get("uiAmount", 0),
                    })
        except Exception:
            continue

    return accounts


def parse_transactions_for_signals(raw, dev_wallet=None, target_mint=None):
    """
    Parse transaction details looking for suspicious patterns.
    Returns dict of signals.
    """
    signals = {
        "large_outbound_count": 0,
        "fanout_flag": False,
        "total_tx_analyzed": 0,
        "unique_destinations": set(),
        "notes": [],
    }

    if not raw or "error" in raw:
        signals["notes"].append("Could not fetch transaction data")
        return signals

    # Try to get transaction list
    tx_list = None
    if isinstance(raw, dict):
        if "result" in raw:
            r = raw["result"]
            if isinstance(r, list):
                tx_list = r
            elif isinstance(r, dict):
                tx_list = [r]
        elif isinstance(raw, list):
            tx_list = raw
    elif isinstance(raw, list):
        tx_list = raw

    if not tx_list:
        signals["notes"].append("No transactions found in response")
        return signals

    for tx in tx_list:
        if not isinstance(tx, dict):
            continue
        signals["total_tx_analyzed"] += 1

        # Try to extract instructions
        transaction = tx.get("transaction", tx)
        message = transaction.get("message", {}) if isinstance(transaction, dict) else {}
        instructions = message.get("instructions", []) if isinstance(message, dict) else []

        # Also check meta for token transfers
        meta = tx.get("meta", {})
        if isinstance(meta, dict):
            pre_token = meta.get("preTokenBalances", [])
            post_token = meta.get("postTokenBalances", [])

            # Look for outbound token transfers from dev wallet
            for pre in (pre_token or []):
                if isinstance(pre, dict):
                    owner = pre.get("owner", "")
                    mint = pre.get("mint", "")
                    pre_amt = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)

                    # Find corresponding post balance
                    for post in (post_token or []):
                        if isinstance(post, dict) and post.get("accountIndex") == pre.get("accountIndex"):
                            post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                            if dev_wallet and owner == dev_wallet and pre_amt > post_amt:
                                delta = pre_amt - post_amt
                                if delta > 0:
                                    signals["large_outbound_count"] += 1
                                    signals["notes"].append(
                                        f"Outbound transfer of {delta:.2f} tokens from dev wallet"
                                    )

            # Track unique destination wallets
            for post in (post_token or []):
                if isinstance(post, dict):
                    dest_owner = post.get("owner", "")
                    if dest_owner and dest_owner != dev_wallet:
                        signals["unique_destinations"].add(dest_owner)

        # Parse instructions for transfer patterns
        for ix in instructions:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed", {})
            if isinstance(parsed, dict):
                ix_type = parsed.get("type", "")
                info = parsed.get("info", {})
                if ix_type in ("transfer", "transferChecked"):
                    source = info.get("source", "") or info.get("authority", "")
                    dest = info.get("destination", "")
                    if dest:
                        signals["unique_destinations"].add(dest)
                    if dev_wallet and source == dev_wallet:
                        signals["large_outbound_count"] += 1

    # Fanout detection: many unique destinations from same source
    unique_count = len(signals["unique_destinations"])
    if unique_count >= 5:
        signals["fanout_flag"] = True
        signals["notes"].append(
            f"Fan-out pattern detected: {unique_count} unique destination wallets"
        )

    # Convert set to count for JSON serialization
    signals["unique_destination_count"] = unique_count
    del signals["unique_destinations"]

    return signals


# ---------------------------------------------------------------------------
# Core Analysis Engine
# ---------------------------------------------------------------------------
def analyze(mint, dev_wallet=None, recent_signatures=None, options=None):
    """
    Main analysis function. Returns complete risk assessment.

    Steps:
    1. Fetch mint account info -> authority checks + supply/decimals
    2. If dev_wallet: fetch token accounts -> dev holdings
    3. If dev_wallet or recent_signatures: fetch tx data -> tx signals
    4. Compare against snapshot (if exists) for supply changes
    5. Compute deterministic risk score
    6. Return structured result
    """
    options = options or {}
    timestamp = datetime.now(timezone.utc).isoformat()
    triggered_signals = []
    risk_score = 0

    # -----------------------------------------------------------------------
    # Step 1: Mint Account Info (Authority + Supply)
    # -----------------------------------------------------------------------
    raw_account = fetch_account_info(mint)
    mint_info = parse_mint_info(raw_account)

    mint_authority_revoked = mint_info.get("mint_authority_revoked")
    freeze_authority_revoked = mint_info.get("freeze_authority_revoked")
    supply = mint_info.get("supply")
    decimals = mint_info.get("decimals")
    parse_error = mint_info.get("parse_error")

    if parse_error:
        triggered_signals.append(f"WARN: Could not parse mint data: {parse_error}")

    # Authority Risk (max 30 points)
    if mint_authority_revoked is False:
        risk_score += 20
        triggered_signals.append("CRITICAL: Mint authority is ACTIVE (infinite mint risk, +20)")
    elif mint_authority_revoked is True:
        triggered_signals.append("OK: Mint authority is revoked")

    if freeze_authority_revoked is False:
        risk_score += 10
        triggered_signals.append("HIGH: Freeze authority is ACTIVE (honeypot/freeze risk, +10)")
    elif freeze_authority_revoked is True:
        triggered_signals.append("OK: Freeze authority is revoked")

    # -----------------------------------------------------------------------
    # Step 2: Supply Change Detection (max 20 points)
    # -----------------------------------------------------------------------
    supply_change_flag = False
    prev_snapshot = _snapshot_cache.get(mint)

    if prev_snapshot and supply:
        prev_supply = prev_snapshot[1].get("supply")
        if prev_supply and str(supply) != str(prev_supply):
            try:
                curr = int(supply)
                prev = int(prev_supply)
                if curr > prev:
                    supply_change_flag = True
                    risk_score += 20
                    pct = ((curr - prev) / prev * 100) if prev > 0 else 999
                    triggered_signals.append(
                        f"CRITICAL: Supply INCREASED since last scan "
                        f"({prev} -> {curr}, +{pct:.1f}%, +20)"
                    )
                    if mint_authority_revoked is False:
                        triggered_signals.append(
                            "EXTREME: Supply increased AND mint authority active — likely active minting"
                        )
            except (ValueError, TypeError):
                pass

    # -----------------------------------------------------------------------
    # Step 3: Dev Wallet Holdings (max 20 points, only if dev_wallet provided)
    # -----------------------------------------------------------------------
    dev_holdings = None
    if dev_wallet:
        raw_token_accts = fetch_token_accounts_by_owner(dev_wallet)
        token_accounts = parse_token_accounts_for_mint(raw_token_accts, mint)

        total_dev_tokens = 0
        for ta in token_accounts:
            try:
                total_dev_tokens += int(ta.get("amount", "0"))
            except (ValueError, TypeError):
                pass

        # Calculate percentage of supply
        pct_supply = None
        if supply and int(supply) > 0:
            pct_supply = (total_dev_tokens / int(supply)) * 100

        dev_holdings = {
            "token_amount": str(total_dev_tokens),
            "token_accounts": len(token_accounts),
            "percent_supply": round(pct_supply, 2) if pct_supply is not None else None,
        }

        # Dev Exposure Risk scoring
        if pct_supply is not None:
            if pct_supply > 15:
                risk_score += 20
                triggered_signals.append(
                    f"HIGH: Dev holds {pct_supply:.1f}% of supply (>15%, +20)"
                )
            elif pct_supply > 5:
                risk_score += 10
                triggered_signals.append(
                    f"MODERATE: Dev holds {pct_supply:.1f}% of supply (5-15%, +10)"
                )
            else:
                triggered_signals.append(
                    f"OK: Dev holds {pct_supply:.1f}% of supply (<5%)"
                )
        elif len(token_accounts) == 0:
            triggered_signals.append("INFO: Dev wallet has no token accounts for this mint")
    else:
        triggered_signals.append("INFO: No dev_wallet provided — dev exposure check skipped")

    # -----------------------------------------------------------------------
    # Step 4: Transaction Risk Signals (max 30 points)
    # -----------------------------------------------------------------------
    tx_signals = {
        "large_outbound_count": 0,
        "fanout_flag": False,
        "total_tx_analyzed": 0,
        "unique_destination_count": 0,
        "notes": [],
    }

    if dev_wallet:
        raw_tx = fetch_transaction_detail(dev_wallet, number=15)
        tx_signals = parse_transactions_for_signals(raw_tx, dev_wallet, mint)

        # Transaction Risk scoring
        if tx_signals["large_outbound_count"] > 0:
            risk_score += 15
            triggered_signals.append(
                f"HIGH: {tx_signals['large_outbound_count']} large outbound token transfers "
                f"from dev wallet (+15)"
            )

        if tx_signals["fanout_flag"]:
            risk_score += 15
            triggered_signals.append(
                f"HIGH: Fan-out dispersal pattern detected — "
                f"{tx_signals['unique_destination_count']} unique destinations (+15)"
            )
    elif recent_signatures:
        # Inspect provided signatures
        all_tx_data = []
        for sig in recent_signatures[:5]:  # limit to 5 to stay within rate limits
            tx_data = fetch_transaction_with_signature(sig)
            if tx_data and "error" not in tx_data:
                all_tx_data.append(tx_data)

        if all_tx_data:
            # Combine into a format parse_transactions_for_signals can handle
            combined = {"result": all_tx_data}
            tx_signals = parse_transactions_for_signals(combined, dev_wallet, mint)

            if tx_signals["large_outbound_count"] > 0:
                risk_score += 15
                triggered_signals.append(
                    f"HIGH: {tx_signals['large_outbound_count']} suspicious transfers "
                    f"detected in provided signatures (+15)"
                )
            if tx_signals["fanout_flag"]:
                risk_score += 15
                triggered_signals.append(
                    f"HIGH: Fan-out dispersal in provided signatures — "
                    f"{tx_signals['unique_destination_count']} unique destinations (+15)"
                )
    else:
        triggered_signals.append(
            "INFO: No dev_wallet or recent_signatures — transaction analysis skipped"
        )

    # -----------------------------------------------------------------------
    # Step 5: Compute Final Decision
    # -----------------------------------------------------------------------
    risk_score = min(risk_score, 100)  # cap at 100

    if risk_score <= 25:
        decision = "BUY"
        risk_level = "LOW"
    elif risk_score <= 50:
        decision = "CAUTION"
        risk_level = "MODERATE"
    elif risk_score <= 75:
        decision = "NO_BUY"
        risk_level = "HIGH"
    else:
        decision = "NO_BUY"
        risk_level = "EXTREME"

    # Force NO_BUY if supply increased with active mint authority
    if supply_change_flag and mint_authority_revoked is False:
        decision = "NO_BUY"
        risk_level = "EXTREME"
        if risk_score < 51:
            risk_score = max(risk_score, 80)

    # -----------------------------------------------------------------------
    # Build Result
    # -----------------------------------------------------------------------
    result = {
        "mint": mint,
        "mint_authority_revoked": mint_authority_revoked,
        "freeze_authority_revoked": freeze_authority_revoked,
        "supply": supply,
        "decimals": decimals,
        "dev_holdings": dev_holdings,
        "tx_signals": {
            "large_outbound_count": tx_signals.get("large_outbound_count", 0),
            "fanout_flag": tx_signals.get("fanout_flag", False),
            "total_tx_analyzed": tx_signals.get("total_tx_analyzed", 0),
            "unique_destination_count": tx_signals.get("unique_destination_count", 0),
            "notes": tx_signals.get("notes", []),
        },
        "supply_change_flag": supply_change_flag,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "decision": decision,
        "triggered_signals": triggered_signals,
        "timestamp": timestamp,
    }

    # Save snapshot
    snapshot_data = {
        "supply": supply,
        "mint_authority_revoked": mint_authority_revoked,
        "freeze_authority_revoked": freeze_authority_revoked,
        "risk_score": risk_score,
        "decision": decision,
        "timestamp": timestamp,
    }
    _cache_set(_snapshot_cache, mint, snapshot_data)

    return result


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "rapid-rug-filter",
        "version": "1.0.0",
        "rapidapi_configured": bool(RAPIDAPI_KEY),
        "network": NETWORK,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/analyze", methods=["POST"])
def analyze_endpoint():
    """
    Main analysis endpoint.
    Input JSON:
      - mint (string, required): Solana token mint address
      - dev_wallet (string, optional): suspected dev wallet address
      - recent_signatures (array of strings, optional): tx signatures to inspect
      - options (object, optional): {snapshot: bool, threshold_overrides: object}
    """
    if not RAPIDAPI_KEY:
        return jsonify({"error": "RAPIDAPI_KEY not configured on server"}), 500

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    mint = data.get("mint")
    if not mint or not isinstance(mint, str) or len(mint) < 32:
        return jsonify({
            "error": "Missing or invalid 'mint' field. Must be a valid Solana mint address (32+ chars)."
        }), 400

    dev_wallet = data.get("dev_wallet")
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
    """
    Return the last computed snapshot for a mint address.
    Query param: mint (required)
    """
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
    else:
        return jsonify({
            "mint": mint,
            "snapshot": None,
            "message": "No snapshot available. Run /analyze first.",
        }), 404


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "Rapid Rug Filter",
        "description": "Solana meme coin structural rug risk scanner",
        "version": "1.0.0",
        "endpoints": {
            "GET /health": "Service health check",
            "POST /analyze": "Analyze a token mint for rug risk",
            "GET /snapshot?mint=...": "Get cached analysis snapshot",
        },
        "docs": "POST /analyze with JSON body: {mint: string, dev_wallet?: string, recent_signatures?: string[]}",
    })


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
