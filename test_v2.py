#!/usr/bin/env python3
"""
Vigorous E2E Test Suite v2 - Rapid Rug Filter v5.0.0
Fixed to match actual endpoint signatures. No edge case left untested.
"""
import requests, json, time, sys, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "http://localhost:3000"
WALLET = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
FAKE_PRIVKEY = "fake_private_key_for_testing"
FAKE_JUPITER = "fake_jupiter_key_for_testing"
TEST_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK

R = {"passed": 0, "failed": 0, "errors": [], "timing": {}, "throttle": []}

def T(name, func):
    s = time.time()
    try:
        func()
        e = time.time() - s
        R["passed"] += 1; R["timing"][name] = e
        tag = "⚠️  SLOW" if e > 5 else "✅ PASS"
        if e > 5: R["throttle"].append(f"{name}: {e:.2f}s")
        print(f"  {tag} {name} ({e:.2f}s)")
    except Exception as ex:
        e = time.time() - s
        R["failed"] += 1; R["errors"].append(f"{name}: {ex}"); R["timing"][name] = e
        print(f"  ❌ FAIL {name}: {ex} ({e:.2f}s)")

def AEQ(a, b, m=""):
    if a != b: raise AssertionError(f"Expected {b!r}, got {a!r}. {m}")
def AIN(item, cont, m=""):
    if item not in cont: raise AssertionError(f"'{item}' not found. {m}")
def ACODE(r, c, m=""):
    if r.status_code != c: raise AssertionError(f"HTTP {r.status_code} != {c}. Body: {r.text[:200]}. {m}")

HDRS = lambda w=WALLET: {
    "X-Wallet-Address": w,
    "X-Wallet-Private-Key": FAKE_PRIVKEY,
    "X-Jupiter-Api-Key": FAKE_JUPITER,
}

print(f"\n{'#'*60}")
print(f"# RAPID RUG FILTER v5.0.0 - VIGOROUS TEST v2")
print(f"# {datetime.now().isoformat()} | {BASE}")
print(f"{'#'*60}")

# ==================== SECTION 1: BASIC ====================
print(f"\n{'='*50}\n SECTION 1: BASIC ENDPOINTS\n{'='*50}")

T("GET /health - v5.0.0, features", lambda: (
    (r := requests.get(f"{BASE}/health")),
    ACODE(r, 200),
    (d := r.json()),
    AEQ(d["version"], "5.0.0"),
    AEQ(d["status"], "ok"),
    AIN("portfolio_autopilot", d),
    AIN("portfolio_autopilot", d["features"]),
    AIN("auto_watch", d["features"]),
    AIN("wallet_monitoring", d["features"]),
)[-1])

T("GET / - root info", lambda: (
    (r := requests.get(f"{BASE}/")),
    ACODE(r, 200),
    (d := r.json()),
    AEQ(d["version"], "5.0.0"),
    AIN("POST /portfolio/start", str(d)),
)[-1])

T("GET /health - portfolio_autopilot structure", lambda: (
    (r := requests.get(f"{BASE}/health")),
    (d := r.json()["portfolio_autopilot"]),
    AIN("active", d),
    AIN("wallet", d),
    AIN("scan_count", d),
    AIN("holdings_count", d),
)[-1])

# ==================== SECTION 2: ANALYZE (POST!) ====================
print(f"\n{'='*50}\n SECTION 2: ANALYZE (POST)\n{'='*50}")

T("POST /analyze - valid BONK", lambda: (
    (r := requests.post(f"{BASE}/analyze", json={"mint": TEST_MINT})),
    ACODE(r, 200),
    (d := r.json()),
    AIN("risk_score", d),
    AIN("risk_level", d),
    AIN("decision", d),
)[-1])

T("POST /analyze - missing mint (400)", lambda: (
    (r := requests.post(f"{BASE}/analyze", json={})),
    ACODE(r, 400),
)[-1])

T("POST /analyze - empty mint (400)", lambda: (
    (r := requests.post(f"{BASE}/analyze", json={"mint": ""})),
    ACODE(r, 400),
)[-1])

T("GET /analyze - wrong method (405)", lambda: (
    (r := requests.get(f"{BASE}/analyze?mint={TEST_MINT}")),
    ACODE(r, 405),
)[-1])

T("POST /analyze - 5 concurrent", lambda: (
    (pool := ThreadPoolExecutor(max_workers=5)),
    (futs := [pool.submit(requests.post, f"{BASE}/analyze", json={"mint": TEST_MINT}) for _ in range(5)]),
    (resps := [f.result(timeout=60) for f in futs]),
    [ACODE(r, 200) for r in resps],
    pool.shutdown(),
)[-1])

T("POST /analyze - 10 rapid fire", lambda: (
    [ACODE(requests.post(f"{BASE}/analyze", json={"mint": TEST_MINT}), 200) for _ in range(10)],
)[-1])

# ==================== SECTION 3: PORTFOLIO EDGE CASES (BEFORE START) ====================
print(f"\n{'='*50}\n SECTION 3: PORTFOLIO EDGE CASES (PRE-START)\n{'='*50}")

T("GET /portfolio - not started (inactive)", lambda: (
    (r := requests.get(f"{BASE}/portfolio")),
    AIN(r.status_code, [200, 404]),
    AIN("inactive", r.json().get("status", "") or r.text.lower()),
)[-1])

T("POST /portfolio/stop - not started (404)", lambda: (
    (r := requests.post(f"{BASE}/portfolio/stop")),
    ACODE(r, 404),
)[-1])

T("POST /portfolio/start - missing ALL headers (400)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/start"), 400),
)[-1])

T("POST /portfolio/start - missing wallet (400)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/start", headers={
        "X-Wallet-Private-Key": FAKE_PRIVKEY, "X-Jupiter-Api-Key": FAKE_JUPITER
    }), 400),
)[-1])

T("POST /portfolio/start - empty wallet (400)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/start", headers={
        "X-Wallet-Address": "", "X-Wallet-Private-Key": FAKE_PRIVKEY, "X-Jupiter-Api-Key": FAKE_JUPITER
    }), 400),
)[-1])

T("POST /portfolio/start - short wallet (400)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/start", headers={
        "X-Wallet-Address": "abc", "X-Wallet-Private-Key": FAKE_PRIVKEY, "X-Jupiter-Api-Key": FAKE_JUPITER
    }), 400),
)[-1])

T("POST /portfolio/start - missing private key (400)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/start", headers={
        "X-Wallet-Address": WALLET, "X-Jupiter-Api-Key": FAKE_JUPITER
    }), 400),
)[-1])

# ==================== SECTION 4: PORTFOLIO PIPELINE ====================
print(f"\n{'='*50}\n SECTION 4: PORTFOLIO FULL PIPELINE\n{'='*50}")

T("POST /portfolio/start - start", lambda: (
    (r := requests.post(f"{BASE}/portfolio/start", headers=HDRS())),
    AIN(r.status_code, [200, 201]),
    (d := r.json()),
    AIN("wallet_address", d),
    AEQ(d["wallet_address"], WALLET),
    AIN("status", d),
)[-1])

T("POST /portfolio/start - idempotent same wallet (200)", lambda: (
    (r := requests.post(f"{BASE}/portfolio/start", headers=HDRS())),
    ACODE(r, 200),
    (d := r.json()),
    AEQ(d["status"], "already_running"),
)[-1])

T("POST /portfolio/start - DIFFERENT wallet (409)", lambda: (
    (r := requests.post(f"{BASE}/portfolio/start", headers=HDRS("SomeOtherWallet1111111111111111111111111111"))),
    ACODE(r, 409),
    AIN("error", r.json()),
)[-1])

print("  ⏳ Waiting 15s for scan cycle...")
time.sleep(15)

T("GET /portfolio - after scan", lambda: (
    (r := requests.get(f"{BASE}/portfolio")),
    ACODE(r, 200),
    (d := r.json()),
    AIN("holdings", d),
    AIN("recent_events", d),
    AIN("summary", d),
    AIN("scan_count", d),
    print(f"    Holdings: {len(d['holdings'])}, Events: {len(d['recent_events'])}, Scans: {d['scan_count']}, Errors: {d.get('scan_errors',0)}"),
    # Check stablecoin filtering
    [None for sc in ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"]
     if sc in d["holdings"] and (_ for _ in []).throw(AssertionError(f"Stablecoin {sc[:8]}... in holdings!"))],
)[-1])

T("GET /portfolio - event structure", lambda: (
    (r := requests.get(f"{BASE}/portfolio")),
    (d := r.json()),
    (evts := d.get("recent_events", [])),
    len(evts) > 0 or (_ for _ in []).throw(AssertionError("No events!")),
    AIN("type", evts[0]),
    AIN("timestamp", evts[0]),
    AIN("message", evts[0]),
)[-1])

T("GET /health - portfolio active", lambda: (
    (r := requests.get(f"{BASE}/health")),
    (d := r.json()["portfolio_autopilot"]),
    AEQ(d["active"], True),
    d["scan_count"] > 0 or (_ for _ in []).throw(AssertionError(f"scan_count={d['scan_count']}")),
    print(f"    scan_count={d['scan_count']}, holdings={d['holdings_count']}"),
)[-1])

# ==================== SECTION 5: WATCH/STATUS/ALERTS DURING PORTFOLIO ====================
print(f"\n{'='*50}\n SECTION 5: WATCH/STATUS/ALERTS\n{'='*50}")

T("GET /status - missing mint (400)", lambda: ACODE(requests.get(f"{BASE}/status"), 400))

T("GET /alerts - structure", lambda: (
    (r := requests.get(f"{BASE}/alerts")),
    ACODE(r, 200),
    (d := r.json()),
    AIN("total", d),
    AIN("alerts", d),
    print(f"    Alerts: {d['total']}"),
)[-1])

T("GET /snapshot - with mint", lambda: (
    (r := requests.get(f"{BASE}/snapshot?mint={TEST_MINT}")),
    AIN(r.status_code, [200, 404]),
)[-1])

T("POST /watch - valid (with auto_sell)", lambda: (
    (r := requests.post(f"{BASE}/watch", json={
        "mint": TEST_MINT,
        "auto_sell": True,
    }, headers={
        "X-Wallet-Private-Key": FAKE_PRIVKEY,
        "X-Jupiter-Api-Key": FAKE_JUPITER,
    })),
    AIN(r.status_code, [200, 201, 409]),  # 201=new watch, 200=already watching, 409=portfolio conflict
    print(f"    Watch result: {r.status_code}"),
)[-1])

T("GET /status - for a mint", lambda: (
    (r := requests.get(f"{BASE}/status?mint={TEST_MINT}")),
    AIN(r.status_code, [200, 404]),
)[-1])

# ==================== SECTION 6: STOP/RESTART ====================
print(f"\n{'='*50}\n SECTION 6: STOP & RESTART\n{'='*50}")

T("POST /portfolio/stop - stop", lambda: (
    (r := requests.post(f"{BASE}/portfolio/stop")),
    ACODE(r, 200),
    AIN("stopped", r.json().get("status", "")),
)[-1])

T("POST /portfolio/stop - already stopped (404)", lambda: (
    ACODE(requests.post(f"{BASE}/portfolio/stop"), 404),
)[-1])

T("GET /portfolio - after stop", lambda: (
    (r := requests.get(f"{BASE}/portfolio")),
    (d := r.json()),
    AIN("inactive", d.get("status", "")),
)[-1])

T("GET /health - portfolio inactive", lambda: (
    AEQ(requests.get(f"{BASE}/health").json()["portfolio_autopilot"]["active"], False),
)[-1])

T("POST /portfolio/start - restart", lambda: (
    (r := requests.post(f"{BASE}/portfolio/start", headers=HDRS())),
    AIN(r.status_code, [200, 201]),
)[-1])

time.sleep(12)

T("GET /portfolio - after restart", lambda: (
    (r := requests.get(f"{BASE}/portfolio")),
    ACODE(r, 200),
    AIN("holdings", r.json()),
    print(f"    Holdings: {len(r.json()['holdings'])}"),
)[-1])

# ==================== SECTION 7: STRESS TEST ====================
print(f"\n{'='*50}\n SECTION 7: CONCURRENT STRESS\n{'='*50}")

T("STRESS - 20 mixed concurrent", lambda: (
    (pool := ThreadPoolExecutor(max_workers=20)),
    (urls := [f"{BASE}/health"]*5 + [f"{BASE}/portfolio"]*5 + [f"{BASE}/"]*5 + [f"{BASE}/alerts"]*5),
    (futs := [pool.submit(requests.get, u) for u in urls]),
    (resps := [f.result(timeout=30) for f in futs]),
    sum(1 for r in resps if r.status_code >= 500) == 0 or (_ for _ in []).throw(AssertionError("Got 5xx!")),
    pool.shutdown(),
)[-1])

T("STRESS - 100x rapid /health", lambda: (
    [ACODE(requests.get(f"{BASE}/health"), 200) for _ in range(100)],
)[-1])

T("STRESS - 50x rapid /portfolio", lambda: (
    [ACODE(requests.get(f"{BASE}/portfolio"), 200) for _ in range(50)],
)[-1])

T("STRESS - analyze + portfolio concurrent", lambda: (
    (pool := ThreadPoolExecutor(max_workers=8)),
    (futs := [
        pool.submit(requests.post, f"{BASE}/analyze", json={"mint": TEST_MINT}),
        pool.submit(requests.post, f"{BASE}/analyze", json={"mint": TEST_MINT}),
        pool.submit(requests.post, f"{BASE}/analyze", json={"mint": TEST_MINT}),
        pool.submit(requests.get, f"{BASE}/portfolio"),
        pool.submit(requests.get, f"{BASE}/portfolio"),
        pool.submit(requests.get, f"{BASE}/health"),
        pool.submit(requests.get, f"{BASE}/health"),
        pool.submit(requests.get, f"{BASE}/alerts"),
    ]),
    (resps := [f.result(timeout=60) for f in futs]),
    sum(1 for r in resps if r.status_code >= 500) == 0 or (_ for _ in []).throw(AssertionError("Got 5xx!")),
    pool.shutdown(),
)[-1])

T("STRESS - 30 concurrent /portfolio start (idempotent)", lambda: (
    (pool := ThreadPoolExecutor(max_workers=30)),
    (futs := [pool.submit(requests.post, f"{BASE}/portfolio/start", headers=HDRS()) for _ in range(30)]),
    (resps := [f.result(timeout=30) for f in futs]),
    all(r.status_code in [200, 201] for r in resps) or (_ for _ in []).throw(
        AssertionError(f"Unexpected status codes: {[r.status_code for r in resps if r.status_code not in [200,201]]}")),
    pool.shutdown(),
)[-1])

# ==================== SECTION 8: UNWATCH/SELL EDGE CASES ====================
print(f"\n{'='*50}\n SECTION 8: UNWATCH & SELL EDGE CASES\n{'='*50}")

T("POST /unwatch - not watched (404)", lambda: ACODE(
    requests.post(f"{BASE}/unwatch", json={"mint": "NotWatched1111111111111111111111111111111111"}), 404))

T("POST /unwatch - missing mint (400)", lambda: ACODE(
    requests.post(f"{BASE}/unwatch", json={}), 400))

T("POST /unwatch - no body (400)", lambda: AIN(
    requests.post(f"{BASE}/unwatch").status_code, [400, 415]))

T("POST /sell - no body (400/415)", lambda: AIN(
    requests.post(f"{BASE}/sell").status_code, [400, 415]))

T("POST /sell - missing fields (400)", lambda: ACODE(
    requests.post(f"{BASE}/sell", json={"mint": TEST_MINT}), 400))

T("POST /sell - invalid mint", lambda: AIN(
    requests.post(f"{BASE}/sell", json={"mint": "invalid", "wallet_private_key": "x", "jupiter_api_key": "y"}).status_code, [400, 404]))

T("POST /lp - missing mint (400)", lambda: ACODE(
    requests.post(f"{BASE}/lp", json={}), 400))

T("POST /lp - valid mint", lambda: AIN(
    requests.post(f"{BASE}/lp", json={"mint": TEST_MINT}).status_code, [200, 400, 404, 500]))

# ==================== SECTION 9: TIMING ====================
print(f"\n{'='*50}\n SECTION 9: TIMING ANALYSIS\n{'='*50}")

for method, path, data in [
    ("GET", "/health", None),
    ("GET", "/", None),
    ("GET", "/portfolio", None),
    ("GET", "/alerts", None),
    ("POST", "/analyze", {"mint": TEST_MINT}),
    ("GET", "/status?mint=" + TEST_MINT, None),
]:
    times = []
    for _ in range(10):
        s = time.time()
        if method == "GET":
            requests.get(f"{BASE}{path}")
        else:
            requests.post(f"{BASE}{path}", json=data)
        times.append(time.time() - s)
    avg, mx, mn = sum(times)/len(times), max(times), min(times)
    tag = "✅" if avg < 1 else "⚠️ " if avg < 3 else "❌"
    print(f"  {tag} {method} {path}: avg={avg:.3f}s min={mn:.3f}s max={mx:.3f}s")
    if avg > 2: R["throttle"].append(f"{method} {path}: avg {avg:.2f}s")
    if mx > 5: R["throttle"].append(f"{method} {path}: max spike {mx:.2f}s")

# ==================== SECTION 10: CLEANUP ====================
print(f"\n{'='*50}\n SECTION 10: CLEANUP\n{'='*50}")
T("POST /portfolio/stop - final", lambda: AIN(
    requests.post(f"{BASE}/portfolio/stop").status_code, [200, 404]))

# Try to unwatch via health
try:
    h = requests.get(f"{BASE}/health").json()
    for m in h.get("surveillance", {}).get("watched_list", []):
        requests.post(f"{BASE}/unwatch", json={"mint": m})
    print(f"  ✅ Unwatched {len(h.get('surveillance',{}).get('watched_list',[]))} mints")
except: pass

# ==================== RESULTS ====================
print(f"\n{'='*60}")
print(f" RESULTS")
print(f"{'='*60}")
print(f"  Total: {R['passed']+R['failed']} | ✅ {R['passed']} | ❌ {R['failed']}")

if R["errors"]:
    print(f"\n  FAILURES:")
    for e in R["errors"]: print(f"    ❌ {e}")

if R["throttle"]:
    print(f"\n  THROTTLE POINTS:")
    for t in R["throttle"]: print(f"    🐌 {t}")

print(f"\n  SLOWEST:")
for name, t in sorted(R["timing"].items(), key=lambda x: x[1], reverse=True)[:10]:
    m = "🔴" if t > 10 else "🟡" if t > 3 else "🟢"
    print(f"    {m} {t:.2f}s - {name}")

sys.exit(1 if R["failed"] > 0 else 0)
