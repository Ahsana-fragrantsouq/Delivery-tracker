import os
import logging
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

SHOPIFY_STORE        = os.getenv("SHOPIFY_STORE")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION          = os.getenv("SHOPIFY_API_VERSION", "2024-04")

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}

def shopify(path):
    return f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}{path}"

# ── Startup banner ────────────────────────────────────────────────────────────
print("=" * 60)
print("  DELIVERY TRACKER — STARTING")
print(f"  Store : {SHOPIFY_STORE}")
print(f"  Token : {'SET ✓' if SHOPIFY_ACCESS_TOKEN else 'MISSING ✗'}")
print("=" * 60)


# ── 1. Fetch only orders that need delivery check ─────────────────────────────
def get_orders_needing_delivery_check():
    print("\n[SHOPIFY] Fetching orders where Delivery Status = Tracking added...")
    all_orders = []
    url    = shopify("/orders.json")
    params = {
        "fulfillment_status": "shipped",
        "status":             "any",
        "limit":              250,
    }

    page = 0
    while url:
        page += 1
        print(f"  → Page {page}: GET {url}")
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()

        orders = r.json().get("orders", [])
        print(f"    Fetched {len(orders)} orders from Shopify")

        for order in orders:
            needs_check = any(
                (ful.get("shipment_status") or "") != "delivered"
                and (ful.get("tracking_company") or "").strip().lower() == "other"
                and (ful.get("tracking_number") or "").strip()
                for ful in order.get("fulfillments", [])
            )
            if needs_check:
                all_orders.append(order)

        print(f"    {len(all_orders)} orders need delivery check so far")

        link   = r.headers.get("Link", "")
        url    = None
        params = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break

    print(f"[SHOPIFY] Orders needing delivery check: {len(all_orders)}\n")
    return all_orders


# ── 2. Mark Delivery Status = Delivered in Shopify ───────────────────────────

def mark_delivered(order_id, fulfillment_id):
    r = requests.post(
        shopify(f"/orders/{order_id}/fulfillments/{fulfillment_id}/events.json"),
        headers=HEADERS,
        json={"event": {"status": "delivered"}},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("fulfillment_event", {})


# ── 3. Scrape professionalcourier.ae ─────────────────────────────────────────
# Confirmed from browser DevTools:
#   Form action : https://professionalcourier.ae/tracking  (POST)
#   Field name  : trackno
# Flow: GET first to obtain session cookie → POST with trackno=AWB

def check_courier(tracking_number: str) -> dict:
    TRACKING_URL = "https://professionalcourier.ae/tracking"
    print(f"    [COURIER] Checking AWB {tracking_number}...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # ── Step 1: GET the page to obtain session cookies ────────────────────────
    try:
        get_resp = session.get(TRACKING_URL, timeout=20)
        get_resp.raise_for_status()
        print(f"    [COURIER] GET OK — {len(get_resp.text)} chars | "
              f"cookies: {list(session.cookies.keys())}")
    except Exception as e:
        print(f"    [COURIER] ✗ GET failed: {e}")
        return {"is_delivered": False, "status": "unreachable", "error": str(e)}

    # ── Step 2: POST with correct field name "trackno" ────────────────────────
    try:
        resp = session.post(
            TRACKING_URL,
            data={"trackno": tracking_number},
            headers={
                "Referer":      TRACKING_URL,
                "Origin":       "https://professionalcourier.ae",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
        )
        resp.raise_for_status()
        print(f"    [COURIER] POST {resp.status_code} — {len(resp.text)} chars")
    except Exception as e:
        print(f"    [COURIER] ✗ POST failed: {e}")
        return {"is_delivered": False, "status": "post_failed", "error": str(e)}

    result_soup = BeautifulSoup(resp.text, "html.parser")
    page_text   = result_soup.get_text(" ", strip=True)

    # ── Step 3: Verify tracking number appears in result ─────────────────────
    if tracking_number not in page_text:
        print(f"    [COURIER] ✗ Tracking number not found in result")
        # Print snippet for debugging
        print(f"    [COURIER] Page snippet: {page_text[:200]}")
        return {"is_delivered": False, "status": "not_found"}

    print(f"    [COURIER] ✓ Tracking number found in result")

    # ── Step 4: Find "Current Status" column in summary table ─────────────────
    # Table structure: From | To | Current Status | Current Activity
    status_text = ""
    for table in result_soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        print(f"    [COURIER] Table headers: {headers}")
        if "current status" in headers:
            try:
                si   = headers.index("current status")
                rows = table.find_all("tr")
                if len(rows) > 1:
                    cells = rows[1].find_all("td")
                    if cells and si < len(cells):
                        status_text = cells[si].get_text(strip=True)
                        print(f"    [COURIER] Current Status: '{status_text}'")
            except (ValueError, IndexError) as e:
                print(f"    [COURIER] Table parse error: {e}")
            break

    # ── Step 5: Fallback — scan text near tracking number only ────────────────
    if not status_text:
        idx = page_text.find(tracking_number)
        if idx != -1:
            nearby = page_text[idx: idx + 400].lower()
            for k in ["delivered", "out for delivery", "in transit",
                      "dispatched", "picked up", "processing", "pending"]:
                if k in nearby:
                    status_text = k.title()
                    print(f"    [COURIER] Fallback status: '{status_text}'")
                    break

    # ── Step 6: Exact match only — never whole-page match ────────────────────
    is_delivered = status_text.strip().lower() in (
        "delivered", "delivery complete", "successfully delivered"
    )

    print(f"    [COURIER] Final → status='{status_text}' is_delivered={is_delivered}")
    return {"is_delivered": is_delivered, "status": status_text or "unknown"}


# ── Main logic ────────────────────────────────────────────────────────────────

def run_tracking():
    print("\n" + "=" * 60)
    print("  RUN TRACKING STARTED")
    print("=" * 60)

    summary = {
        "checked": 0, "updated": 0,
        "errors":  0, "skipped": 0,
        "details": []
    }

    try:
        orders = get_orders_needing_delivery_check()
    except Exception as e:
        print(f"[ERROR] Failed to fetch orders: {e}")
        summary["errors"] += 1
        return summary

    print(f"[PROCESSING] {len(orders)} orders to check...\n")

    for order in orders:
        order_number = order.get("order_number") or order.get("name")
        order_id     = order["id"]

        for ful in order.get("fulfillments", []):
            ful_id           = ful["id"]
            tracking_number  = (ful.get("tracking_number") or "").strip()
            tracking_company = (ful.get("tracking_company") or "").strip()
            shipment_status  = (ful.get("shipment_status") or "").lower()

            print(f"\n  Order #{order_number} | AWB: {tracking_number} | "
                  f"Carrier: {tracking_company} | Status: {shipment_status}")

            detail = {
                "order":   order_number,
                "awb":     tracking_number,
                "carrier": tracking_company,
                "status":  shipment_status,
                "action":  None,
            }

            if shipment_status == "delivered":
                msg = "skip — already delivered"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            if tracking_company.lower() != "other":
                msg = f"skip — carrier is '{tracking_company}' not 'Other'"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            if not tracking_number:
                msg = "skip — no tracking number"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            print(f"  → ✓ Conditions met — checking professionalcourier.ae...")
            summary["checked"] += 1

            courier = check_courier(tracking_number)

            if courier.get("error"):
                msg = f"error: {courier['error']}"
                print(f"  → ✗ {msg}")
                detail["action"] = msg
                summary["errors"] += 1
                summary["details"].append(detail)
                continue

            if courier["is_delivered"]:
                try:
                    mark_delivered(order_id, ful_id)
                    msg = "✅ MARKED DELIVERED in Shopify"
                    print(f"  → {msg}")
                    detail["action"] = msg
                    summary["updated"] += 1
                except Exception as e:
                    msg = f"Shopify update failed: {e}"
                    print(f"  → ✗ {msg}")
                    detail["action"] = msg
                    summary["errors"] += 1
            else:
                msg = f"not delivered yet (courier: {courier['status']})"
                print(f"  → {msg}")
                detail["action"] = msg

            summary["details"].append(detail)

    print("\n" + "=" * 60)
    print(f"  RUN COMPLETE")
    print(f"  Checked : {summary['checked']}")
    print(f"  Updated : {summary['updated']}")
    print(f"  Skipped : {summary['skipped']}")
    print(f"  Errors  : {summary['errors']}")
    print("=" * 60 + "\n")

    return summary


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/check-tracking", methods=["POST", "GET"])
def check_tracking():
    """
    Called by Shopify Flow at 9am and 6pm IST.
    Responds immediately to avoid Flow's 30-second timeout.
    Tracking runs in background — check Render logs for results.
    """
    import threading
    print(f"\n>>> /check-tracking triggered — starting background job")
    thread = threading.Thread(target=run_tracking, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Tracking job started in background"}), 200


@app.route("/health", methods=["GET"])
def health():
    """Ping this every 14 min from UptimeRobot to keep Render awake."""
    print(">>> GET /health — OK")
    return jsonify({"status": "ok", "store": SHOPIFY_STORE}), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service":   "Delivery Sync — Fragrant Souq",
        "endpoints": {
            "POST /check-tracking": "Run tracking (called by Shopify Flow)",
            "GET  /health":         "Health check for uptime monitors",
        }
    }), 200


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
