import os
import logging
import json as _json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify,redirect
from dotenv import load_dotenv
import hashlib, hmac, os

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
SCOPES = "read_orders,read_fulfillments"
REDIRECT_URI = "https://delivery-tracker-m427.onrender.com/auth/callback"

print("=" * 60)
print("DELIVERY TRACKER — STARTING UP")
print(f"  Store       : {SHOPIFY_STORE}")
print(f"  API Key     : {SHOPIFY_API_KEY}")
print(f"  Access Token: {'SET ✓' if ACCESS_TOKEN else 'MISSING ✗'}")
print(f"  Flow Secret : {'SET ✓' if FLOW_WEBHOOK_SECRET else 'not set (open endpoint)'}")
print("=" * 60)

# ─── Aramex stage labels ───────────────────────────────────────────────────────
STAGES = [
    "Created",
    "Collected",
    "Departed",
    "In transit",
    "Arrived at destination",
    "Out for delivery",
    "Delivered",
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
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")  # Read fresh every time
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }


def shopify_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/2026-04/{path}"



def get_fulfilled_undelivered_orders():
    print("\n── STEP 1: Fetching fulfilled orders from Shopify ──")
    logger.info("Calling Shopify Admin API → GET /orders.json")

    orders = []
    url = shopify_url("orders.json")
    params = {
        
        "fulfillment_status": "fulfilled",
        "limit": 250,
        "fields": "id,name,fulfillments,metafields",
    }
    page = 1

    while url:
        print(f"  Fetching page {page}...")
        print(f"  Store URL    : {url}")
        print(f"  Params       : {params}")
        print(f"  Token starts : {ACCESS_TOKEN[:12] if ACCESS_TOKEN else 'MISSING'}")
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

        link = resp.headers.get("Link", "")
        url  = None
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


# ─── Aramex public page scraper ────────────────────────────────────────────────
def check_aramex_tracking(tracking_number: str) -> str:
    """
    Scrapes the PUBLIC Aramex tracking page — no credentials needed.
    Same URL you open in your browser.
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
        print(f"    Aramex HTTP response: {resp.status_code} | "
              f"Page size: {len(resp.text):,} bytes")
        resp.raise_for_status()

        html  = resp.text
        soup  = BeautifulSoup(html, "html.parser")
        lower = html.lower()

        # Method 1 — Next.js __NEXT_DATA__ JSON
        print(f"    [Method 1] Looking for __NEXT_DATA__ JSON...")
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag and tag.string:
            print(f"    __NEXT_DATA__ found ({len(tag.string):,} chars) — parsing JSON...")
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
                print(f"    [Method 1] No stage labels found in JSON data")
            except Exception as je:
                print(f"    [Method 1] JSON parse error: {je}")
        else:
            print(f"    [Method 1] __NEXT_DATA__ not present in page")

        # Method 2 — CSS active/current class
        print(f"    [Method 2] Scanning for active/current CSS classes...")
        for cls_kw in ["active", "current", "selected", "highlighted"]:
            els = soup.find_all(
                class_=lambda c, k=cls_kw: c and k in " ".join(c).lower()
            )
            if els:
                print(f"    Found {len(els)} elements with class containing '{cls_kw}'")
            for el in els:
                text = el.get_text(separator=" ", strip=True).lower()
                for stage in reversed(STAGES):
                    if stage.lower() in text:
                        print(f"    [Method 2] SUCCESS → '{stage}' via class '{cls_kw}'")
                        return stage
        print(f"    [Method 2] No active stage found via CSS classes")

        # Method 3 — Exception keywords
        print(f"    [Method 3] Scanning for exception keywords...")
        for kw, status in EXCEPTIONS.items():
            if kw in lower:
                print(f"    [Method 3] SUCCESS → keyword '{kw}' → '{status}'")
                return status
        print(f"    [Method 3] No exception keywords found")

        # Method 4 — Full text stage scan
        print(f"    [Method 4] Scanning full page text for stage labels...")
        found = None
        for stage in STAGES:
            if stage.lower() in lower:
                print(f"    Stage label found in page: '{stage}'")
                found = stage
        if found:
            print(f"    [Method 4] SUCCESS → '{found}' (furthest stage in text)")
            return found
        print(f"    [Method 4] No stage labels found in page text")

        # Method 5 — Latest Update section
        print(f"    [Method 5] Looking for 'Latest Update' text...")
        for t in soup.find_all(string=lambda s: s and "latest update" in s.lower()):
            parent = t.find_parent()
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    txt = sibling.get_text(strip=True)
                    if txt:
                        print(f"    [Method 5] SUCCESS → '{txt[:60]}'")
                        return txt[:80]
        print(f"    [Method 5] No 'Latest Update' text found")

        print(f"    All 5 methods exhausted — defaulting to 'In transit'")
        return "In transit"

    except requests.exceptions.Timeout:
        print(f"    ERROR: Request timed out after 20s")
        logger.error(f"Aramex timeout: {tracking_number}")
        return "Tracking error"
    except Exception as e:
        print(f"    ERROR: {e}")
        logger.error(f"Aramex scraping error for {tracking_number}: {e}")
        return "Tracking error"


def detect_carrier(tracking_number: str) -> str:
    tn = str(tracking_number).strip()
    if tn.isdigit() and len(tn) == 11:
        print(f"    Carrier auto-detected: Aramex (11-digit numeric)")
        return "aramex"
    print(f"    Carrier unknown — tracking number format: '{tn}'")
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

        print(f"  Reading current delivery_status metafield from Shopify...")
        current_mf     = get_order_metafield(order_id, "custom", "delivery_status")
        current_status = current_mf["value"] if current_mf else ""

        if current_status:
            print(f"  Stored status: '{current_status}'")
        else:
            print(f"  No status stored yet — first time checking this order")

        if current_status in TERMINAL_STATES:
            print(f"  Terminal state — will never change — skipping permanently")
            logger.info(f"{order_name}: skipping terminal '{current_status}'")
            results["skipped"] += 1
            continue

        last_fulfillment = fulfillments[-1]
        tracking_number  = last_fulfillment.get("tracking_number")
        tracking_company = (last_fulfillment.get("tracking_company") or "").lower()

        print(f"  Tracking number : {tracking_number}")
        print(f"  Carrier (stored): {tracking_company or '(not specified in Shopify)'}")

        if not tracking_number:
            print(f"  No tracking number found — skipping")
            results["skipped"] += 1
            continue

        results["checked"] += 1

        carrier = "aramex" if "aramex" in tracking_company else detect_carrier(tracking_number)
        print(f"  Carrier resolved: {carrier}")

        if carrier == "aramex":
            print(f"  Calling Aramex scraper...")
            new_status = check_aramex_tracking(tracking_number)
        else:
            new_status = f"Manual check needed ({tracking_company or 'unknown carrier'})"
            print(f"  Non-Aramex — status set to: '{new_status}'")

        print(f"  Result from Aramex: '{new_status}'")

        if new_status == current_status:
            print(f"  No change (still '{current_status}') — Shopify NOT updated")
        else:
            print(f"  Status changed: '{current_status}' → '{new_status}'")
            print(f"  Updating Shopify metafield...")
            try:
                set_order_metafield(order_id, "custom", "delivery_status", new_status)
                print(f"  Shopify updated ✓")
                logger.info(f"{order_name}: '{current_status}' → '{new_status}'")
                results["updated"] += 1
            except Exception as e:
                err = f"{order_name}: update failed — {e}"
                print(f"  ERROR: {err}")
                logger.error(err)
                results["errors"].append(err)

    print("\n" + "=" * 60)
    print("TRACKING CHECK COMPLETE")
    print(f"  Orders found   : {len(orders)}")
    print(f"  Checked        : {results['checked']}")
    print(f"  Updated        : {results['updated']}")
    print(f"  Skipped        : {results['skipped']}")
    print(f"  Errors         : {len(results['errors'])}")
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


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)



@app.route("/auth")
def auth():
    shop = request.args.get("shop")
    auth_url = f"https://{shop}/admin/oauth/authorize?client_id={SHOPIFY_API_KEY}&scope={SCOPES}&redirect_uri={REDIRECT_URI}"
    return redirect(auth_url)

@app.route("/auth/callback")
def callback():
    code = request.args.get("code")
    shop = request.args.get("shop")
    
    # Exchange code for access token
    response = requests.post(f"https://{shop}/admin/oauth/access_token", json={
        "client_id": SHOPIFY_API_KEY,
        "client_secret": SHOPIFY_API_SECRET,
        "code": code
    })
    
    token = response.json().get("access_token")  # This is your shpat_ token!
    print(f"ACCESS TOKEN: {token}")  # Check Render logs for this
    
    # Save token to environment or database
    return f"Token received: {token}"

@app.route("/test-scopes")
def test_scopes():
    import requests
    
    shop = "devfragrantsouq.myshopify.com"
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    
    url = f"https://{shop}/admin/oauth/access_scopes.json"
    headers = {"X-Shopify-Access-Token": token}
    
    response = requests.get(url, headers=headers)
    return response.json()
