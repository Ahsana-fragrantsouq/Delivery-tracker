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


# ─── 17track universal tracker (free, supports Aramex + Professional Courier) ──
def check_17track(tracking_number: str, carrier_code: int = 0) -> str:
    """
    Uses 17track.net free API to track any shipment.
    Supports Aramex (code 190) and Professional Courier (code 2151).
    Register free at https://www.17track.net/en/apiregistr
    carrier_code=0 means auto-detect.
    """
    api_key = os.getenv("TRACK17_API_KEY", "")
    if not api_key:
        print(f"    17track: No API key set — skipping")
        return ""

    hdrs = {
        "17token": api_key,
        "Content-Type": "application/json",
    }

    # Step 1 — Register the tracking number
    try:
        print(f"    17track: Registering {tracking_number}...")
        body = [{"number": tracking_number}]
        if carrier_code:
            body[0]["carrier"] = carrier_code
        resp = requests.post(
            "https://api.17track.net/track/v2.2/register",
            json=body, headers=hdrs, timeout=15
        )
        print(f"    Register HTTP: {resp.status_code} | Body: {resp.text[:200]}")
    except Exception as e:
        print(f"    17track register error: {e}")

    # Step 2 — Get tracking info
    try:
        print(f"    17track: Getting tracking info...")
        resp = requests.post(
            "https://api.17track.net/track/v2.2/gettrackinfo",
            json=[{"number": tracking_number}],
            headers=hdrs, timeout=15
        )
        print(f"    Getinfo HTTP: {resp.status_code} | Body: {resp.text[:500]}")
        if resp.status_code == 200:
            data     = resp.json()
            accepted = data.get("data", {}).get("accepted", [])
            if accepted:
                track    = accepted[0].get("track", {})
                e1       = track.get("e", 0)  # status code
                z0       = track.get("z0", {})  # latest event
                desc     = z0.get("z", "") if z0 else ""
                print(f"    17track status code: {e1}, latest: {desc}")
                # 17track status codes
                STATUS_MAP_17 = {
                    0:  "Not found",
                    10: "In transit",
                    20: "Expired",
                    30: "Delivery attempted",
                    35: "Out for delivery",
                    40: "Delivered",
                    50: "Returned to sender",
                    60: "On hold",
                }
                status = STATUS_MAP_17.get(e1, "")
                if status and status != "Not found":
                    print(f"    17track SUCCESS → '{status}'")
                    return status
                if desc:
                    print(f"    17track desc → '{desc[:50]}'")
                    return desc[:50]
            rejected = data.get("data", {}).get("rejected", [])
            if rejected:
                print(f"    17track rejected: {rejected}")
    except Exception as e:
        print(f"    17track getinfo error: {e}")

    return ""


# ─── Aramex tracking ──────────────────────────────────────────────────────────
def check_aramex_tracking(tracking_number: str) -> str:
    """
    Uses Aramex official tracking API at ws.aramex.net.
    Credentials from environment variables (ARAMEX_USERNAME etc.)
    Falls back to scraping if credentials not set.
    """
    # ── Try official API first (most reliable) ────────────────────────────────
    aramex_user   = os.getenv("ARAMEX_USERNAME", "")
    aramex_pass   = os.getenv("ARAMEX_PASSWORD", "")
    aramex_num    = os.getenv("ARAMEX_ACCOUNT_NUM", "")
    aramex_pin    = os.getenv("ARAMEX_ACCOUNT_PIN", "")
    aramex_entity = os.getenv("ARAMEX_ACCOUNT_ENTITY", "DXB")
    aramex_country= os.getenv("ARAMEX_ACCOUNT_COUNTRY", "AE")

    CODE_MAP = {
        "SH001":"Created","SH002":"Created","SH003":"Created",
        "SH040":"Collected","SH041":"Collected","SH045":"Collected",
        "SH060":"Departed","SH061":"Departed","SH062":"Departed","SH065":"Departed",
        "SH015":"In transit","SH016":"In transit","SH017":"In transit",
        "SH050":"In transit","SH051":"In transit","SH055":"In transit",
        "SH020":"Arrived at destination","SH021":"Arrived at destination","SH022":"Arrived at destination",
        "SH010":"Out for delivery","SH011":"Out for delivery",
        "SH005":"Delivered","SH007":"Delivered",
        "SH006":"Delivery attempted","SH008":"Delivery attempted","SH009":"Delivery attempted",
        "SH025":"On hold","SH026":"On hold",
        "SH030":"Returned to sender","SH031":"Returned to sender","SH032":"Returned to sender",
        "SH035":"Cancelled","SH070":"Lost",
    }

    # ── Try 17track first (free, no Aramex credentials needed) ─────────────────
    result_17 = check_17track(tracking_number, carrier_code=190)  # 190 = Aramex
    if result_17:
        print(f"    17track result: '{result_17}'")
        return result_17

    # ── No credentials — return direct Aramex tracking URL ──────────────────
    if not aramex_user:
        tracking_link = f"https://www.aramex.com/us/en/track/track-results-new?type=EXP&ShipmentNumber={tracking_number}"
        print(f"    Returning direct tracking URL: {tracking_link}")
        return tracking_link

    if aramex_user and aramex_pass:
        print(f"    [API] Using Aramex official API with credentials...")
        try:
            api_url = "https://ws.aramex.net/ShippingAPI.V2/Tracking/Service_1_0.svc/json/TrackShipments"
            payload = {
                "ClientInfo": {
                    "UserName": aramex_user,
                    "Password": aramex_pass,
                    "Version": "v1.0",
                    "AccountNumber": aramex_num,
                    "AccountPin": aramex_pin,
                    "AccountEntity": aramex_entity,
                    "AccountCountryCode": aramex_country,
                    "Source": 24,
                },
                "Shipments": [tracking_number],
                "GetLastTrackingUpdateOnly": True,
            }
            resp = requests.post(api_url, json=payload,
                                 headers={"Content-Type": "application/json"}, timeout=15)
            print(f"    Aramex API HTTP: {resp.status_code}")
            print(f"    Aramex API body: {resp.text[:300]}")
            if resp.status_code == 200 and resp.text.strip():
                data    = resp.json()
                results = data.get("TrackingResults", [])
                if results:
                    updates = results[0].get("Value", [])
                    if updates:
                        latest = updates[-1]
                        code   = latest.get("UpdateCode", "")
                        desc   = latest.get("UpdateDescription", "")
                        mapped = CODE_MAP.get(code, desc or "In transit")
                        print(f"    API result: code={code} → '{mapped}'")
                        return mapped
                print(f"    API: no tracking updates in response")
        except Exception as e:
            print(f"    API error: {e}")
    else:
        print(f"    No Aramex credentials in env — trying web scrape...")

    # ── Fallback: try web scrape ───────────────────────────────────────────────
    tracking_url = (
        "https://www.aramex.com/us/en/track/track-results-new"
        f"?type=EXP&ShipmentNumber={tracking_number}"
    )
    print(f"    Trying web scrape: {tracking_url}")

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
        resp = requests.get(tracking_url, headers=hdrs, timeout=20)
        print(f"    Aramex HTTP: {resp.status_code} | Size: {len(resp.text):,} bytes")
        if resp.status_code == 403:
            print(f"    403 blocked — credentials needed for reliable tracking")
            return "Tracking error"
        resp.raise_for_status()

        html  = resp.text
        soup  = BeautifulSoup(html, "html.parser")
        lower = html.lower()

        tag = soup.find("script", id="__NEXT_DATA__")
        if tag and tag.string:
            try:
                data   = _json.loads(tag.string)
                as_str = _json.dumps(data).lower()
                found  = None
                for stage in STAGES:
                    if stage.lower() in as_str:
                        found = stage
                if found:
                    print(f"    Web scrape SUCCESS → '{found}'")
                    return found
            except Exception:
                pass

        for stage in reversed(STAGES):
            if stage.lower() in lower:
                print(f"    Text scan SUCCESS → '{stage}'")
                return stage

        return "In transit"

    except Exception as e:
        print(f"    Scrape error: {e}")
        return "Tracking error"


# ─── Professional Courier tracking ────────────────────────────────────────────
def check_professional_courier_tracking(tracking_number: str) -> str:
    """
    Check Professional Courier (TPC India) tracking status.
    Tries tpcindia.com official site and trackcourier.io aggregator.
    """
    print(f"    Checking Professional Courier: {tracking_number}")

    # ── Try 17track first (supports Professional Courier) ───────────────────
    result_17 = check_17track(tracking_number, carrier_code=2151)  # 2151 = Professional Courier
    if result_17:
        print(f"    17track result: '{result_17}'")
        return map_professional_courier_status(result_17)

    hdrs_browser = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Method 1 — TPC India TrackConsignment page (correct URL)
    try:
        print(f"    [Method 1] TPC India TrackConsignment page...")
        url  = f"https://www.tpcindia.com/TrackConsignment/Default.aspx?docket={tracking_number}"
        resp = requests.get(url, headers=hdrs_browser, timeout=15)
        print(f"    TPC HTTP: {resp.status_code} | Size: {len(resp.text)} bytes")
        if resp.status_code == 200:
            soup       = BeautifulSoup(resp.text, "html.parser")
            page_lower = resp.text.lower()
            print(f"    TPC page snippet: {resp.text[500:800]}")
            if "delivered" in page_lower:
                print(f"    [Method 1] SUCCESS → 'Delivered'")
                return "Delivered"
            if "out for delivery" in page_lower:
                return "Out for delivery"
            if "in transit" in page_lower or "intransit" in page_lower:
                return "In transit"
            if "picked" in page_lower or "booked" in page_lower:
                return "Collected"
            # Find any table row with status text
            for td in soup.find_all("td"):
                txt = td.get_text(strip=True)
                if txt and len(txt) > 3 and len(txt) < 100:
                    mapped = map_professional_courier_status(txt)
                    if mapped != txt[:50]:
                        print(f"    [Method 1] Found in table: '{txt}' → '{mapped}'")
                        return mapped
    except Exception as e:
        print(f"    [Method 1] Error: {e}")

    # Method 2 — TPC India POST form tracking
    try:
        print(f"    [Method 2] TPC India POST form...")
        url  = "https://www.tpcindia.com/TrackConsignment/Default.aspx"
        hdrs_form = {**hdrs_browser, "Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(url,
                             data={"txtdocket": tracking_number, "btntrack": "Track"},
                             headers=hdrs_form, timeout=15)
        print(f"    TPC POST HTTP: {resp.status_code} | Size: {len(resp.text)} bytes")
        if resp.status_code == 200:
            page_lower = resp.text.lower()
            print(f"    TPC POST snippet: {resp.text[500:800]}")
            if "delivered" in page_lower:
                return "Delivered"
            if "out for delivery" in page_lower:
                return "Out for delivery"
            if "in transit" in page_lower:
                return "In transit"
    except Exception as e:
        print(f"    [Method 2] Error: {e}")

    # Method 3 — AfterShip tracking (aggregator that supports Professional Courier)
    try:
        print(f"    [Method 3] AfterShip tracking page scrape...")
        url  = f"https://track.aftership.com/professional-courier/{tracking_number}"
        resp = requests.get(url, headers=hdrs_browser, timeout=15)
        print(f"    AfterShip HTTP: {resp.status_code} | Size: {len(resp.text)} bytes")
        if resp.status_code == 200:
            page_lower = resp.text.lower()
            if "delivered" in page_lower:
                return "Delivered"
            if "out for delivery" in page_lower:
                return "Out for delivery"
            if "in transit" in page_lower:
                return "In transit"
            # Try Next.js data
            soup = BeautifulSoup(resp.text, "html.parser")
            tag  = soup.find("script", id="__NEXT_DATA__")
            if tag and tag.string:
                try:
                    data   = _json.loads(tag.string)
                    as_str = _json.dumps(data).lower()
                    for stage in reversed(STAGES):
                        if stage.lower() in as_str:
                            print(f"    [Method 3] SUCCESS → '{stage}'")
                            return stage
                except:
                    pass
    except Exception as e:
        print(f"    [Method 3] Error: {e}")

    # ── All methods failed — return direct TPC tracking URL ─────────────────
    tracking_link = f"https://www.tpcindia.com/Track/TrackConsignment.aspx?docket_no={tracking_number}"
    print(f"    Returning direct TPC URL: {tracking_link}")
    return tracking_link


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

        # ── FETCH-ONLY MODE (metafield write disabled) ──────────────────────────
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

        # Detect carrier
        if "aramex" in tracking_company:
            carrier = "aramex"
        elif "professional" in tracking_company or "tpc" in tracking_company:
            carrier = "professional_courier"
        else:
            carrier = detect_carrier(tracking_number)
        print(f"  Carrier: {carrier}")

        if carrier == "aramex":
            new_status = check_aramex_tracking(tracking_number)
        elif carrier == "professional_courier":
            new_status = check_professional_courier_tracking(tracking_number)
        else:
            new_status = f"Manual ({tracking_company or 'unknown'})"

        print(f"  ✓ STATUS: '{new_status}'")
        results["statuses"] = results.get("statuses", [])
        results["statuses"].append({
            "order": order_name,
            "tracking": tracking_number,
            "carrier": carrier,
            "status": new_status,
        })

    print("\n" + "=" * 60)
    print("TRACKING FETCH COMPLETE — no writes, fetch only")
    print(f"  Orders found : {len(orders)}")
    print(f"  Checked      : {results['checked']}")
    print(f"  Skipped      : {results['skipped']}")
    print(f"  Errors       : {len(results['errors'])}")
    print()
    print("── STATUSES FETCHED ──")
    for item in results.get("statuses", []):
        print(f"  {item['order']:8} | {item['carrier']:20} | {item['tracking']:15} | {item['status']}")
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
    import threading
    thread = threading.Thread(target=run_tracking_check)
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True, "message": "Tracking check started in background."})


@app.route("/check-tracking/manual", methods=["GET"])
def manual_check():
    print("\n>>> GET /check-tracking/manual — manual browser trigger")
    import threading
    thread = threading.Thread(target=run_tracking_check)
    thread.daemon = True
    thread.start()
    return jsonify({"ok": True, "message": "Tracking check started in background."})


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
