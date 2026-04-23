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
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")

    print(f"Token being used: {token[:20] if token else 'NOT SET!'}")

    print("\n── STEP 1: Fetching fulfilled orders from Shopify ──")
    logger.info("Calling Shopify Admin API → GET /orders.json")

    orders = []
    url = shopify_url("orders.json")
    params = {
        "status": "any",             # any = open + closed/archived
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


# ─── Aramex tracking via JSON API (no bot blocking) ──────────────────────────
def check_aramex_tracking(tracking_number: str) -> str:
    """
    Uses Aramex's official JSON tracking API endpoint.
    This avoids the 403 bot-blocking on the public website.
    No credentials needed for basic tracking status.
    Falls back to multiple alternative methods if blocked.
    """

    # Method 1 — Aramex JSON API (fastest, most reliable)
    print(f"    [Method 1] Trying Aramex JSON API...")
    try:
        api_url = "https://ws.aramex.net/ShippingAPI.V2/Tracking/Service_1_0.svc/json/TrackShipments"
        # Try with guest/demo credentials first
        payload = {
            "ClientInfo": {
                "UserName": "testingapi@aramex.com",
                "Password": "R123456789$r",
                "Version": "v1.0",
                "AccountNumber": "20016",
                "AccountPin": "331421",
                "AccountEntity": "AMM",
                "AccountCountryCode": "JO",
                "Source": 24,
            },
            "Shipments": [tracking_number],
            "GetLastTrackingUpdateOnly": True,
        }
        hdrs = {"Content-Type": "application/json"}
        resp = requests.post(api_url, json=payload, headers=hdrs, timeout=15)
        print(f"    Aramex API response: HTTP {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            tracking_results = data.get("TrackingResults", [])
            if tracking_results:
                value_list = tracking_results[0].get("Value", [])
                if value_list:
                    latest = value_list[-1]
                    code = latest.get("UpdateCode", "")
                    desc = latest.get("UpdateDescription", "")
                    print(f"    API result: code={code}, desc={desc}")
                    # Map code to stage label
                    CODE_MAP = {
                        "SH001": "Created", "SH002": "Created", "SH003": "Created",
                        "SH040": "Collected", "SH041": "Collected", "SH045": "Collected",
                        "SH060": "Departed", "SH061": "Departed", "SH062": "Departed",
                        "SH015": "In transit", "SH016": "In transit", "SH017": "In transit",
                        "SH050": "In transit", "SH051": "In transit",
                        "SH020": "Arrived at destination", "SH021": "Arrived at destination",
                        "SH010": "Out for delivery", "SH011": "Out for delivery",
                        "SH005": "Delivered", "SH007": "Delivered",
                        "SH006": "Delivery attempted", "SH008": "Delivery attempted",
                        "SH025": "On hold", "SH026": "On hold",
                        "SH030": "Returned to sender", "SH031": "Returned to sender",
                        "SH035": "Cancelled", "SH070": "Lost",
                    }
                    mapped = CODE_MAP.get(code, desc or "In transit")
                    print(f"    [Method 1] SUCCESS → '{mapped}'")
                    return mapped
                else:
                    print(f"    [Method 1] No tracking updates found")
            else:
                print(f"    [Method 1] No tracking results in response")
    except Exception as e:
        print(f"    [Method 1] Error: {e}")

    # Method 2 — Alternative Aramex tracking endpoint
    print(f"    [Method 2] Trying alternative Aramex endpoint...")
    try:
        alt_url = f"https://www.aramex.com/api/trackingResults?shipmentNumber={tracking_number}&type=EXP"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.aramex.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = requests.get(alt_url, headers=hdrs, timeout=15)
        print(f"    Alt endpoint HTTP: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    Alt response: {str(data)[:200]}")
            # Try to extract status from response
            status = (
                data.get("status") or
                data.get("Status") or
                data.get("currentStatus") or
                data.get("trackingStatus") or ""
            )
            if status:
                print(f"    [Method 2] SUCCESS → '{status}'")
                return str(status)
    except Exception as e:
        print(f"    [Method 2] Error: {e}")

    # Method 3 — Aramex mobile API
    print(f"    [Method 3] Trying Aramex mobile API...")
    try:
        mob_url = "https://www.aramex.com/us/en/api/tracking"
        hdrs = {
            "User-Agent": "Aramex/1.0 (iPhone; iOS 16.0)",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        resp = requests.post(
            mob_url,
            json={"shipmentNumber": tracking_number},
            headers=hdrs,
            timeout=15
        )
        print(f"    Mobile API HTTP: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    Mobile response: {str(data)[:200]}")
    except Exception as e:
        print(f"    [Method 3] Error: {e}")

    print(f"    All methods failed — defaulting to In transit")
    return "In transit"


def detect_carrier(tracking_number: str) -> str:
    tn = str(tracking_number).strip()
    # Standard Aramex international: 11 digits
    if tn.isdigit() and len(tn) == 11:
        print(f"    Carrier auto-detected: Aramex (11-digit)")
        return "aramex"
    # Aramex local UAE shipments: 7 digits starting with 740
    if tn.isdigit() and len(tn) == 7 and tn.startswith("740"):
        print(f"    Carrier auto-detected: Aramex (7-digit local UAE)")
        return "aramex"
    # Aramex local: other 7-digit formats
    if tn.isdigit() and len(tn) == 7:
        print(f"    Carrier possibly Aramex (7-digit) — will attempt Aramex scrape")
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
    # Debug: print all params received
    print(f"Callback params: {request.args}")
    
    code = request.args.get("code")
    shop = request.args.get("shop")
    
    print(f"Shop: {shop}")
    print(f"Code: {code}")
    
    if not shop or not code:
        return f"Missing params! shop={shop}, code={code}", 400
    
    # Use data= not json=
    response = requests.post(f"https://{shop}/admin/oauth/access_token", data={
        "client_id": SHOPIFY_API_KEY,
        "client_secret": SHOPIFY_API_SECRET,
        "code": code
    })
    
    token = response.json().get("access_token")
    print(f"ACCESS TOKEN: {token}")
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
@app.route("/test-orders")
def test_orders():
    token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shop = os.getenv("SHOPIFY_STORE")
    
    url = f"https://{shop}/admin/api/2026-04/orders.json?fulfillment_status=fulfilled&limit=5"
    headers = {"X-Shopify-Access-Token": token}
    
    resp = requests.get(url, headers=headers)
    return {
        "status_code": resp.status_code,
        "order_count": len(resp.json().get("orders", [])),
        "raw": resp.json()
    }
