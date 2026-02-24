"""
Rapid Rug Filter - Solana Meme Coin Structural Rug Risk Scanner
Production-grade REST API microservice for OnDemand agent tool calling.

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
# Primary: free public Solana RPC. Override with SOLANA_RPC_URL env var.
# For production, use Helius/Quicknode/Triton for higher rate limits.
SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

CACHE_TTL_SECONDS = 20
SNAPSHOT_TTL_SECONDS = 120

_account_cache = {}
_snapshot_cache = {}

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


# ---------------------------------------------------------------------------
# Solana JSON-RPC Client
# ---------------------------------------------------------------------------
def _rpc_call(method, params, timeout=10):
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


def fetch_signatures_for_address(address, limit=15):
    """getSignaturesForAddress — list recent tx signatures."""
    cache_key = f"sigs:{address}:{limit}"
    cached = _cache_get(_account_cache, cache_key, CACHE_TTL_SECONDS)
    if cached is not None:
        return cached
    data = _rpc_call("getSignaturesForAddress", [
        address,
        {"limit": limit, "commitment": "confirmed"}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


def fetch_transaction(signature):
    """getTransaction with jsonParsed encoding."""
    cache_key = f"tx:{signature}"
    cached = _cache_get(_account_cache, cache_key, 60)
    if cached is not None:
        return cached
    data = _rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
    ])
    if "error" not in data:
        _cache_set(_account_cache, cache_key, data)
    return data


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
    # mintAuthority: COption<Pubkey> at offset 0 (1 + 32 bytes)
    ma_opt = raw_bytes[0]
    result["mint_authority_revoked"] = (ma_opt == 0)
    result["mint_authority"] = _bytes_to_base58(raw_bytes[1:33]) if ma_opt == 1 else None
    # supply: u64 LE at offset 33
    result["supply"] = str(struct.unpack_from("<Q", raw_bytes, 33)[0])
    # decimals: u8 at offset 41
    result["decimals"] = raw_bytes[41]
    # isInitialized: bool at offset 42
    result["is_initialized"] = (raw_bytes[42] == 1)
    # freezeAuthority: COption<Pubkey> at offset 43
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

    # Get signatures
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

    # Analyze up to 8 transactions to stay within rate limits
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

        # Build balance delta map: accountIndex -> (owner, mint, pre_amount, post_amount)
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

                # Outbound from dev wallet
                if dev_wallet and pre_owner == dev_wallet and pre_amt > post_amt:
                    delta = pre_amt - post_amt
                    if delta > 0:
                        signals["large_outbound_count"] += 1
                        signals["notes"].append(
                            f"Outbound: {delta:,.2f} tokens from dev in tx {sig[:12]}..."
                        )

                # Track destinations
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
    """Main analysis: returns complete risk assessment."""
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

    # -- Step 2: Supply Change (max 20) --
    supply_change_flag = False
    prev_snapshot = _snapshot_cache.get(mint)
    if prev_snapshot and supply:
        prev_supply = prev_snapshot[1].get("supply")
        if prev_supply and str(supply) != str(prev_supply):
            try:
                curr, prev = int(supply), int(prev_supply)
                if curr > prev:
                    supply_change_flag = True
                    risk_score += 20
                    pct = ((curr - prev) / prev * 100) if prev > 0 else 999
                    triggered_signals.append(
                        f"CRITICAL: Supply INCREASED ({prev} -> {curr}, +{pct:.1f}%, +20)"
                    )
                    if mint_authority_revoked is False:
                        triggered_signals.append(
                            "EXTREME: Supply up AND mint authority active"
                        )
            except (ValueError, TypeError):
                pass

    # -- Step 3: Dev Holdings (max 20) --
    dev_holdings = None
    if dev_wallet:
        raw_ta = fetch_token_accounts_by_owner(dev_wallet, mint=mint)
        token_accounts = parse_token_accounts_for_mint(raw_ta, mint)

        total_dev_tokens = sum(int(ta.get("amount", "0")) for ta in token_accounts)
        pct_supply = None
        if supply and int(supply) > 0:
            pct_supply = (total_dev_tokens / int(supply)) * 100

        dev_holdings = {
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
        triggered_signals.append("INFO: No dev_wallet — dev exposure skipped")

    # -- Step 4: Transaction Signals (max 30) --
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
        triggered_signals.append("INFO: No dev_wallet/signatures — tx analysis skipped")

    # -- Step 5: Decision --
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
        "version": "1.1.0",
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
        "description": "Solana meme coin structural rug risk scanner",
        "version": "1.1.0",
        "endpoints": {
            "GET /health": "Health check",
            "POST /analyze": "Analyze token mint for rug risk",
            "GET /snapshot?mint=...": "Get cached snapshot",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    app.run(host="0.0.0.0", port=port, debug=False)
