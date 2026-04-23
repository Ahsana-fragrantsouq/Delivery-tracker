import os
import logging
import json as _json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import hashlib, hmac

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-me-in-production")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SHOPIFY_API_KEY     = os.getenv("SHOPIFY_API_KEY")
SHOPIFY_API_SECRET  = os.getenv("SHOPIFY_API_SECRET")
SHOPIFY_STORE       = os.getenv("SHOPIFY_STORE")
ACCESS_TOKEN        = os.getenv("SHOPIFY_ACCESS_TOKEN")
FLOW_WEBHOOK_SECRET = os.getenv("FLOW_WEBHOOK_SECRET", "")
SCOPES       = "read_orders,read_fulfillments"
REDIRECT_URI = "https://delivery-tracker-m427.onrender.com/auth/callback"

print("=" * 60)
print("DELIVERY TRACKER — STARTING UP")
print(f"  Store       : {SHOPIFY_STORE}")
print(f"  API Key     : {SHOPIFY_API_KEY}")
print(f"  Access Token: {'SET ✓' if ACCESS_TOKEN else 'MISSING ✗'}")
print(f"  Flow Secret : {'SET ✓' if FLOW_WEBHOOK_SECRET else 'not set (open endpoint)'}")
print("=" * 60)

# ─── Stage labels ─────────────────────────────────────────────────────────────
STAGES = [
    "Created", "Collected", "Departed", "In transit",
    "Arrived at destination", "Out for delivery", "Delivered",
]

EXCEPTIONS = {
    "delivery attempted": "Delivery attempted",
    "delivery attempt":   "Delivery attempted",
    "nobody home":        "Delivery attempted",
    "on hold":            "On hold",
    "returned to sender": "Returned to sender",
    "return to sender":   "Returned to sender",
    "cancelled":          "Cancelled",
    "shipment lost":      "Lost",
}

TERMINAL_STATES = {"Delivered", "Returned to sender", "Cancelled", "Lost"}


# ─── Shopify API helpers ───────────────────────────────────────────────────────
def shopify_headers():
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }

def shopify_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/2026-04/{path}"


def get_fulfilled_undelivered_orders():
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    print(f"Token being used: {token[:20] if token else 'NOT SET!'}")
    print("\n── STEP 1: Fetching fulfilled orders from Shopify ──")
    logger.info("Calling Shopify Admin API → GET /orders.json")

    orders = []
    url    = shopify_url("orders.json")
    params = {
        "status": "any",
        "fulfillment_status": "fulfilled",
        "limit": 250,
        "fields": "id,name,fulfillments,metafields",
    }
    page = 1

    while url:
        print(f"  Fetching page {page}...")
        print(f"  Store URL : {url}")
        print(f"  Params    : {params}")
        resp = requests.get(url, headers=shopify_headers(), params=params)
        print(f"  Shopify HTTP : {resp.status_code}")
        print(f"  Shopify body : {resp.text[:800]}")
        if resp.status_code != 200:
            print(f"  ERROR — stopping")
            break
        resp.raise_for_status()

        data  = resp.json()
        batch = data.get("orders", [])
        orders.extend(batch)
        print(f"  Page {page}: got {len(batch)} orders (running total: {len(orders)})")

        link   = resp.headers.get("Link", "")
        url    = None
        params = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url  = part.split(";")[0].strip().strip("<>")
                    page += 1
                    print(f"  More pages — fetching next...")
                    break

    print(f"  TOTAL fulfilled orders: {len(orders)}")
    return orders


def get_order_metafield(order_id, namespace, key):
    url  = shopify_url(f"orders/{order_id}/metafields.json")
    resp = requests.get(url, headers=shopify_headers(),
                        params={"namespace": namespace, "key": key})
    resp.raise_for_status()
    mfs = resp.json().get("metafields", [])
    return mfs[0] if mfs else None


def set_order_metafield(order_id, namespace, key, value):
    existing = get_order_metafield(order_id, namespace, key)
    if existing:
        print(f"    Metafield exists (id:{existing['id']}) → PUT (update)")
        url    = shopify_url(f"orders/{order_id}/metafields/{existing['id']}.json")
        method = "put"
        payload = {"metafield": {"id": existing["id"], "value": value,
                                 "type": "single_line_text_field"}}
    else:
        print(f"    No metafield yet → POST (create)")
        url    = shopify_url(f"orders/{order_id}/metafields.json")
        method = "post"
        payload = {"metafield": {"namespace": namespace, "key": key,
                                 "value": value, "type": "single_line_text_field"}}
    resp = getattr(requests, method)(url, headers=shopify_headers(), json=payload)
    print(f"    Shopify {method.upper()} response: HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json()


# ─── Aramex tracking ──────────────────────────────────────────────────────────
def check_aramex_tracking(tracking_number: str) -> str:
    """
    Scrapes the PUBLIC Aramex tracking page — no credentials needed.
    Tries 5 methods in order until one works.
    """
    tracking_url = (
        "https://www.aramex.com/us/en/track/track-results-new"
        f"?type=EXP&ShipmentNumber={tracking_number}"
    )
    print(f"    URL: {tracking_url}")

    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        print(f"    Sending GET request to Aramex...")
        resp = requests.get(tracking_url, headers=hdrs, timeout=20)
        print(f"    Aramex HTTP: {resp.status_code} | Size: {len(resp.text):,} bytes")
        resp.raise_for_status()

        html  = resp.text
        soup  = BeautifulSoup(html, "html.parser")
        lower = html.lower()

        # Method 1 — Next.js __NEXT_DATA__ JSON
        print(f"    [Method 1] Looking for __NEXT_DATA__ JSON...")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag and tag.string:
            print(f"    __NEXT_DATA__ found ({len(tag.string):,} chars) — parsing...")
            try:
                data   = _json.loads(tag.string)
                as_str = _json.dumps(data).lower()
                found  = None
                for stage in STAGES:
                    if stage.lower() in as_str:
                        print(f"    Found stage in JSON: '{stage}'")
                        found = stage
                if found:
                    print(f"    [Method 1] SUCCESS → '{found}'")
                    return found
                print(f"    [Method 1] No stage labels in JSON")
            except Exception as je:
                print(f"    [Method 1] JSON parse error: {je}")
        else:
            print(f"    [Method 1] __NEXT_DATA__ not present")

        # Method 2 — CSS active/current class
        print(f"    [Method 2] Scanning CSS active/current classes...")
        for cls_kw in ["active", "current", "selected", "highlighted"]:
            els = soup.find_all(
                class_=lambda c, k=cls_kw: c and k in " ".join(c).lower()
            )
            if els:
                print(f"    Found {len(els)} elements with '{cls_kw}'")
            for el in els:
                text = el.get_text(separator=" ", strip=True).lower()
                for stage in reversed(STAGES):
                    if stage.lower() in text:
                        print(f"    [Method 2] SUCCESS → '{stage}'")
                        return stage
        print(f"    [Method 2] No active stage found")

        # Method 3 — Exception keywords
        print(f"    [Method 3] Scanning exception keywords...")
        for kw, status in EXCEPTIONS.items():
            if kw in lower:
                print(f"    [Method 3] SUCCESS → '{status}'")
                return status
        print(f"    [Method 3] No exception keywords found")

        # Method 4 — Full text stage scan
        print(f"    [Method 4] Full text scan for stage labels...")
        found = None
        for stage in STAGES:
            if stage.lower() in lower:
                print(f"    Found: '{stage}'")
                found = stage
        if found:
            print(f"    [Method 4] SUCCESS → '{found}'")
            return found
        print(f"    [Method 4] No stage labels found")

        # Method 5 — Latest Update section text
        print(f"    [Method 5] Looking for Latest Update text...")
        for t in soup.find_all(string=lambda s: s and "latest update" in s.lower()):
            parent = t.find_parent()
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    txt = sibling.get_text(strip=True)
                    if txt:
                        print(f"    [Method 5] SUCCESS → '{txt[:60]}'")
                        return txt[:80]
        print(f"    [Method 5] Nothing found")

        print(f"    All 5 methods exhausted — defaulting to In transit")
        return "In transit"

    except requests.exceptions.Timeout:
        print(f"    ERROR: Timed out after 20s")
        return "Tracking error"
    except Exception as e:
        print(f"    ERROR: {e}")
        return "Tracking error"


# ─── Professional Courier tracking ────────────────────────────────────────────
def check_professional_courier_tracking(tracking_number: str) -> str:
    """
    Check Professional Courier (TPC India) tracking status.
    Tries tpcindia.com official site and trackcourier.io aggregator.
    """
    print(f"    Checking Professional Courier: {tracking_number}")

    # Method 1 — TPC India official API
    try:
        print(f"    [Method 1] TPC India JSON API...")
        url  = "https://www.tpcindia.com/api/tracking"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://www.tpcindia.com/",
        }
        resp = requests.post(url, json={"docketno": tracking_number},
                             headers=hdrs, timeout=15)
        print(f"    TPC API HTTP: {resp.status_code}")
        print(f"    TPC response: {resp.text[:300]}")
        if resp.status_code == 200 and resp.text.strip():
            data   = resp.json()
            status = (data.get("status") or data.get("Status") or
                      data.get("current_status") or "")
            if status:
                print(f"    [Method 1] SUCCESS → '{status}'")
                return map_professional_courier_status(str(status))
    except Exception as e:
        print(f"    [Method 1] Error: {e}")

    # Method 2 — TPC India website scrape
    try:
        print(f"    [Method 2] TPC India website scrape...")
        url  = f"https://www.tpcindia.com/track-shipment/?docketno={tracking_number}"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=hdrs, timeout=15)
        print(f"    TPC website HTTP: {resp.status_code}")
        if resp.status_code == 200:
            soup       = BeautifulSoup(resp.text, "html.parser")
            page_lower = resp.text.lower()
            if "delivered" in page_lower:
                print(f"    [Method 2] SUCCESS → 'Delivered'")
                return "Delivered"
            if "out for delivery" in page_lower:
                return "Out for delivery"
            if "in transit" in page_lower:
                return "In transit"
            if "picked up" in page_lower or "collected" in page_lower:
                return "Collected"
            for cls in ["status", "tracking-status", "shipment-status", "current-status"]:
                el = soup.find(class_=cls)
                if el:
                    txt = el.get_text(strip=True)
                    if txt:
                        print(f"    [Method 2] Found: '{txt}'")
                        return map_professional_courier_status(txt)
    except Exception as e:
        print(f"    [Method 2] Error: {e}")

    # Method 3 — trackcourier.io aggregator
    try:
        print(f"    [Method 3] trackcourier.io API...")
        url  = f"https://trackcourier.io/api/track/professional-courier/{tracking_number}"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=hdrs, timeout=15)
        print(f"    trackcourier HTTP: {resp.status_code}")
        print(f"    trackcourier body: {resp.text[:300]}")
        if resp.status_code == 200 and resp.text.strip():
            data   = resp.json()
            status = (data.get("status") or data.get("Status") or
                      data.get("delivery_status") or "")
            if status:
                print(f"    [Method 3] SUCCESS → '{status}'")
                return map_professional_courier_status(str(status))
    except Exception as e:
        print(f"    [Method 3] Error: {e}")

    print(f"    All methods failed — defaulting to In transit")
    return "In transit"


def map_professional_courier_status(raw: str) -> str:
    """Map Professional Courier status text to standard stage labels."""
    r = raw.lower().strip()
    if "delivered" in r:                          return "Delivered"
    if "out for delivery" in r:                   return "Out for delivery"
    if "arrived" in r or "destination" in r:      return "Arrived at destination"
    if "transit" in r:                            return "In transit"
    if "departed" in r or "dispatched" in r:      return "Departed"
    if "picked" in r or "collected" in r:         return "Collected"
    if "booked" in r or "created" in r:           return "Created"
    if "attempt" in r:                            return "Delivery attempted"
    if "hold" in r:                               return "On hold"
    if "return" in r:                             return "Returned to sender"
    return raw[:50] if raw else "In transit"


# ─── Carrier detection ────────────────────────────────────────────────────────
def detect_carrier(tracking_number: str) -> str:
    tn = str(tracking_number).strip()
    # Aramex: 11-digit numeric
    if tn.isdigit() and len(tn) == 11:
        print(f"    Carrier auto-detected: Aramex (11-digit)")
        return "aramex"
    # Professional Courier (TPC India): 7-digit numeric
    if tn.isdigit() and len(tn) == 7:
        print(f"    Carrier auto-detected: Professional Courier (7-digit)")
        return "professional_courier"
    print(f"    Carrier unknown — format: '{tn}'")
    return "unknown"


# ─── Core logic ───────────────────────────────────────────────────────────────
def run_tracking_check():
    print("\n" + "=" * 60)
    print("TRACKING CHECK STARTED")
    print("=" * 60)

    results = {"checked": 0, "updated": 0, "skipped": 0, "errors": []}

    try:
        orders = get_fulfilled_undelivered_orders()
    except Exception as e:
        print(f"FATAL: Could not fetch orders — {e}")
        logger.error(f"Order fetch failed: {e}")
        results["errors"].append(f"Failed to fetch orders: {e}")
        return results

    if not orders:
        print("No fulfilled orders found. Nothing to do.")
        return results

    print(f"\n── STEP 2: Processing {len(orders)} orders ──")

    for i, order in enumerate(orders, 1):
        order_id     = order["id"]
        order_name   = order.get("name", str(order_id))
        fulfillments = order.get("fulfillments", [])

        print(f"\n[{i}/{len(orders)}] Order {order_name}  (id: {order_id})")

        if not fulfillments:
            print(f"  No fulfillments — skipping")
            results["skipped"] += 1
            continue

        print(f"  Reading current delivery_status metafield...")
        current_mf     = get_order_metafield(order_id, "custom", "delivery_status")
        current_status = current_mf["value"] if current_mf else ""

        if current_status:
            print(f"  Stored status: '{current_status}'")
        else:
            print(f"  No status stored yet — first time")

        if current_status in TERMINAL_STATES:
            print(f"  Terminal state '{current_status}' — skipping permanently")
            results["skipped"] += 1
            continue

        last_fulfillment = fulfillments[-1]
        tracking_number  = last_fulfillment.get("tracking_number")
        tracking_company = (last_fulfillment.get("tracking_company") or "").lower()

        print(f"  Tracking number : {tracking_number}")
        print(f"  Carrier (stored): {tracking_company or '(not specified)'}")

        if not tracking_number:
            print(f"  No tracking number — skipping")
            results["skipped"] += 1
            continue

        results["checked"] += 1

        # Detect carrier: stored name first, then auto-detect by number format
        if "aramex" in tracking_company:
            carrier = "aramex"
        elif "professional" in tracking_company or "tpc" in tracking_company:
            carrier = "professional_courier"
        else:
            carrier = detect_carrier(tracking_number)
        print(f"  Carrier resolved: {carrier}")

        if carrier == "aramex":
            print(f"  Calling Aramex tracker...")
            new_status = check_aramex_tracking(tracking_number)
        elif carrier == "professional_courier":
            print(f"  Calling Professional Courier tracker...")
            new_status = check_professional_courier_tracking(tracking_number)
        else:
            new_status = f"Manual check needed ({tracking_company or 'unknown carrier'})"
            print(f"  Unknown carrier — status: '{new_status}'")

        print(f"  Tracking result: '{new_status}'")

        if new_status == current_status:
            print(f"  No change — Shopify NOT updated")
        else:
            print(f"  Status changed: '{current_status}' → '{new_status}'")
            print(f"  Updating Shopify metafield...")
            # try:
            #     set_order_metafield(order_id, "custom", "delivery_status", new_status)
            #     print(f"  Shopify updated ✓")
            #     logger.info(f"{order_name}: '{current_status}' → '{new_status}'")
            #     results["updated"] += 1
            # except Exception as e:
            #     err = f"{order_name}: update failed — {e}"
            #     print(f"  ERROR: {err}")
            #     logger.error(err)
            #     results["errors"].append(err)

    print("\n" + "=" * 60)
    print("TRACKING CHECK COMPLETE")
    print(f"  Orders found : {len(orders)}")
    print(f"  Checked      : {results['checked']}")
    print(f"  Updated      : {results['updated']}")
    print(f"  Skipped      : {results['skipped']}")
    print(f"  Errors       : {len(results['errors'])}")
    if results["errors"]:
        for err in results["errors"]:
            print(f"    ✗ {err}")
    print("=" * 60 + "\n")
    return results


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    print("GET / — health check")
    return jsonify({"status": "Delivery Tracker running", "store": SHOPIFY_STORE})


@app.route("/check-tracking", methods=["POST"])
def check_tracking():
    print("\n>>> POST /check-tracking — triggered by Shopify Flow")
    if FLOW_WEBHOOK_SECRET:
        provided = request.headers.get("X-Flow-Secret", "")
        if provided != FLOW_WEBHOOK_SECRET:
            print(">>> REJECTED — X-Flow-Secret does not match")
            logger.warning("Unauthorized call to /check-tracking")
            return jsonify({"error": "Unauthorized"}), 401
        print(">>> Secret verified ✓")
    else:
        print(">>> No secret set — accepting all requests")
    results = run_tracking_check()
    return jsonify({"ok": True, "results": results})


@app.route("/check-tracking/manual", methods=["GET"])
def manual_check():
    print("\n>>> GET /check-tracking/manual — manual browser trigger")
    results = run_tracking_check()
    return jsonify({"ok": True, "results": results})


@app.route("/test-aramex/<tracking_number>", methods=["GET"])
def test_aramex(tracking_number):
    """Test a single Aramex tracking number — use this before running all orders."""
    print(f"\n>>> Testing Aramex: {tracking_number}")
    result = check_aramex_tracking(tracking_number)
    return jsonify({"tracking_number": tracking_number, "status": result})


@app.route("/test-professional/<tracking_number>", methods=["GET"])
def test_professional(tracking_number):
    """Test a single Professional Courier tracking number."""
    print(f"\n>>> Testing Professional Courier: {tracking_number}")
    result = check_professional_courier_tracking(tracking_number)
    return jsonify({"tracking_number": tracking_number, "status": result})


@app.route("/auth")
def auth():
    shop     = request.args.get("shop")
    auth_url = (
        f"https://{shop}/admin/oauth/authorize"
        f"?client_id={SHOPIFY_API_KEY}"
        f"&scope={SCOPES}"
        f"&redirect_uri={REDIRECT_URI}"
    )
    return redirect(auth_url)


@app.route("/auth/callback")
def callback():
    print(f"Callback params: {request.args}")
    code = request.args.get("code")
    shop = request.args.get("shop")
    print(f"Shop: {shop}, Code: {code}")
    if not shop or not code:
        return f"Missing params! shop={shop}, code={code}", 400
    response = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        data={
            "client_id":     SHOPIFY_API_KEY,
            "client_secret": SHOPIFY_API_SECRET,
            "code":          code,
        }
    )
    token = response.json().get("access_token")
    print(f"ACCESS TOKEN: {token}")
    return f"Token received: {token}"


@app.route("/test-scopes")
def test_scopes():
    shop    = "devfragrantsouq.myshopify.com"
    token   = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    url     = f"https://{shop}/admin/oauth/access_scopes.json"
    headers = {"X-Shopify-Access-Token": token}
    resp    = requests.get(url, headers=headers)
    return resp.json()


@app.route("/test-orders")
def test_orders():
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shop  = os.getenv("SHOPIFY_STORE")
    url   = f"https://{shop}/admin/api/2026-04/orders.json?fulfillment_status=fulfilled&limit=5"
    hdrs  = {"X-Shopify-Access-Token": token}
    resp  = requests.get(url, headers=hdrs)
    return {
        "status_code": resp.status_code,
        "order_count": len(resp.json().get("orders", [])),
        "raw":         resp.json()
    }


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
