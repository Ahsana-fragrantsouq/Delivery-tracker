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
# Shopify REST API has no direct filter for Delivery Status (shipment_status).
# So we fetch fulfilled orders, then filter in Python for:
#   - shipment_status != 'delivered'  →  Delivery Status = 'Tracking added'
#   - tracking_company == 'Other'     →  Professional Courier
#   - tracking_number is not empty

def get_orders_needing_delivery_check():
    print("\n[SHOPIFY] Fetching orders where Delivery Status = Tracking added...")
    all_orders = []
    url    = shopify("/orders.json")
    params = {
        "fulfillment_status": "shipped",  # only fulfilled orders
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

        # Filter: keep only orders with at least one fulfillment that
        # needs a delivery check (not yet delivered + carrier Other + has AWB)
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

        # Cursor-based pagination via Link header
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
# Reads ONLY the "Current Status" table cell for the specific AWB.
# Never reads whole page — prevents false positives.

def check_courier(tracking_number: str) -> dict:
    TRACKING_URL = "https://professionalcourier.ae/tracking/"
    print(f"    [COURIER] Checking AWB {tracking_number}...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer":         "https://professionalcourier.ae/",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Step 1 — load page (grab hidden CSRF fields)
    try:
        page = session.get(TRACKING_URL, timeout=20)
        page.raise_for_status()
        print(f"    [COURIER] Page loaded OK (status {page.status_code})")
    except Exception as e:
        print(f"    [COURIER] ✗ Site unreachable: {e}")
        return {"is_delivered": False, "status": "unreachable", "error": str(e)}

    soup      = BeautifulSoup(page.text, "html.parser")
    form_data = {}
    form      = soup.find("form")
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            if inp.get("name"):
                form_data[inp["name"]] = inp.get("value", "")
        print(f"    [COURIER] Form found, hidden fields: {list(form_data.keys())}")
    else:
        print(f"    [COURIER] No form found on page")

    # Try every common field name the form might use
    for field in ("tracknumber", "TrackNo", "track_no", "awb_no",
                  "awbno", "awb", "tracking_number", "number"):
        form_data[field] = tracking_number

    # Step 2 — POST the tracking form
    try:
        resp = session.post(TRACKING_URL, data=form_data, timeout=20)
        resp.raise_for_status()
        print(f"    [COURIER] POST response: {resp.status_code} ({len(resp.text)} chars)")
    except Exception as e:
        print(f"    [COURIER] ✗ POST failed: {e}")
        return {"is_delivered": False, "status": "post_failed", "error": str(e)}

    result_soup = BeautifulSoup(resp.text, "html.parser")
    page_text   = result_soup.get_text(" ", strip=True)

    # Step 3 — verify tracking number appears in the result
    if tracking_number not in page_text:
        print(f"    [COURIER] ✗ Tracking number not found in result page")
        return {"is_delivered": False, "status": "not_found"}

    print(f"    [COURIER] ✓ Tracking number found in result")

    # Step 4 — find "Current Status" column in summary table
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
                        print(f"    [COURIER] Current Status cell: '{status_text}'")
            except (ValueError, IndexError) as e:
                print(f"    [COURIER] Table parse error: {e}")
            break

    # Step 5 — fallback: scan text near the tracking number only
    if not status_text:
        idx = page_text.find(tracking_number)
        if idx != -1:
            nearby = page_text[idx: idx + 400].lower()
            for k in ["delivered", "out for delivery", "in transit",
                      "dispatched", "picked up", "processing", "pending"]:
                if k in nearby:
                    status_text = k.title()
                    print(f"    [COURIER] Fallback status found: '{status_text}'")
                    break

    # Step 6 — exact match only (never partial/page-wide match)
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

    # Fetch only orders that actually need checking
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

            # ── Condition 1: Delivery Status must not be delivered ────────────
            if shipment_status == "delivered":
                msg = "skip — already delivered"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            # ── Condition 2: Carrier must be "Other" (Professional Courier) ───
            if tracking_company.lower() != "other":
                msg = f"skip — carrier is '{tracking_company}' not 'Other'"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            # ── No tracking number ────────────────────────────────────────────
            if not tracking_number:
                msg = "skip — no tracking number"
                print(f"  → {msg}")
                detail["action"] = msg
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            # ── Both conditions met → check courier ───────────────────────────
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
    """Called by Shopify Flow at 9am and 6pm IST."""
    print(f"\n>>> POST /check-tracking — triggered")
    summary = run_tracking()
    return jsonify({"ok": True, "summary": summary}), 200


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
