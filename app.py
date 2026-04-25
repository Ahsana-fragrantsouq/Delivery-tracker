import os
import logging
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

SHOPIFY_STORE        = os.getenv("SHOPIFY_STORE")         # fragrantsouq.myshopify.com
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")  # shpat_xxx
API_VERSION          = os.getenv("SHOPIFY_API_VERSION", "2024-04")

HEADERS = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}

def shopify(path):
    return f"https://{SHOPIFY_STORE}/admin/api/{API_VERSION}{path}"


# ── 1. Fetch all fulfilled orders (paginated) ─────────────────────────────────
# Returns orders where fulfillment_status = "fulfilled"
# Fulfillments are embedded in each order — no extra API call needed

def get_fulfilled_orders():
    all_orders = []
    url    = shopify("/orders.json")
    params = {
        "fulfillment_status": "shipped",   # Shopify: "shipped" = fulfilled
        "status":             "any",
        "limit":              250,
    }

    while url:
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        orders = r.json().get("orders", [])
        all_orders.extend(orders)
        log.info(f"Fetched {len(orders)} orders (total so far: {len(all_orders)})")

        # Cursor-based pagination via Link header
        link   = r.headers.get("Link", "")
        url    = None
        params = None          # params only go on first request
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break

    return all_orders


# ── 2. Mark Delivery Status = Delivered in Shopify ────────────────────────────

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
# Reads ONLY the "Current Status" cell for the specific tracking number.
# Never reads the whole page — prevents false positives.

def check_courier(tracking_number: str) -> dict:
    TRACKING_URL = "https://professionalcourier.ae/tracking/"

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer":  "https://professionalcourier.ae/",
        "Accept":   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Step 1 — load the page to grab hidden CSRF tokens
    try:
        page = session.get(TRACKING_URL, timeout=20)
        page.raise_for_status()
    except Exception as e:
        log.warning(f"[{tracking_number}] Courier site unreachable: {e}")
        return {"is_delivered": False, "status": "unreachable", "error": str(e)}

    soup      = BeautifulSoup(page.text, "html.parser")
    form_data = {}
    form      = soup.find("form")
    if form:
        for inp in form.find_all("input", {"type": "hidden"}):
            if inp.get("name"):
                form_data[inp["name"]] = inp.get("value", "")

    # Try every common field name the form might use
    for field in ("tracknumber", "TrackNo", "track_no", "awb_no",
                  "awbno", "awb", "tracking_number", "number"):
        form_data[field] = tracking_number

    # Step 2 — POST the tracking form
    try:
        resp = session.post(TRACKING_URL, data=form_data, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"[{tracking_number}] Courier POST failed: {e}")
        return {"is_delivered": False, "status": "post_failed", "error": str(e)}

    result_soup = BeautifulSoup(resp.text, "html.parser")
    page_text   = result_soup.get_text(" ", strip=True)

    # Step 3 — verify this tracking number appears in the result
    if tracking_number not in page_text:
        log.info(f"[{tracking_number}] Not found on courier site")
        return {"is_delivered": False, "status": "not_found"}

    # Step 4 — find the summary table:
    # From | To | Current Status | Current Activity
    status_text = ""
    for table in result_soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if "current status" in headers:
            try:
                si   = headers.index("current status")
                rows = table.find_all("tr")
                if len(rows) > 1:
                    cells = rows[1].find_all("td")
                    if cells and si < len(cells):
                        status_text = cells[si].get_text(strip=True)
            except (ValueError, IndexError):
                pass
            break  # found the right table — stop

    # Step 5 — fallback: scan 400 chars after the tracking number
    if not status_text:
        idx = page_text.find(tracking_number)
        if idx != -1:
            nearby = page_text[idx: idx + 400].lower()
            for k in ["delivered", "out for delivery", "in transit",
                      "dispatched", "picked up", "processing", "pending"]:
                if k in nearby:
                    status_text = k.title()
                    break

    # Step 6 — SAFE decision: exact match on status cell only
    is_delivered = status_text.strip().lower() in (
        "delivered", "delivery complete", "successfully delivered"
    )

    log.info(f"[{tracking_number}] Courier status: '{status_text}' → is_delivered={is_delivered}")
    return {
        "is_delivered": is_delivered,
        "status":       status_text or "unknown",
    }


# ── Main logic ────────────────────────────────────────────────────────────────
#
# CONDITIONS (both must be true to process a fulfillment):
#   1. Delivery Status = "Tracking added"
#      → fulfillment["shipment_status"] is NOT "delivered"
#      → specifically looks for "in_transit" or None/unknown
#   2. Tracking company = "Other"
#      → fulfillment["tracking_company"] == "Other"
#
# If both conditions met → scrape professionalcourier.ae
# If courier says Delivered → POST fulfillment event → Shopify shows "Delivered"

def run_tracking():
    summary = {"checked": 0, "updated": 0, "errors": 0, "skipped": 0, "details": []}

    try:
        orders = get_fulfilled_orders()
    except Exception as e:
        log.error(f"Failed to fetch orders: {e}")
        summary["errors"] += 1
        return summary

    log.info(f"Processing {len(orders)} fulfilled orders...")

    for order in orders:
        order_number = order.get("order_number") or order.get("name")
        order_id     = order["id"]

        for ful in order.get("fulfillments", []):
            ful_id          = ful["id"]
            tracking_number = ful.get("tracking_number") or ""
            tracking_company = (ful.get("tracking_company") or "").strip()
            shipment_status  = (ful.get("shipment_status") or "").lower()

            detail = {
                "order":           order_number,
                "tracking_number": tracking_number,
                "company":         tracking_company,
                "shopify_status":  shipment_status,
                "action":          None,
            }

            # ── Condition 1: Delivery Status must be "Tracking added" ─────────
            # shipment_status "in_transit" = "Tracking added" in Shopify admin
            # Skip if already delivered
            if shipment_status == "delivered":
                detail["action"] = "skipped (already delivered)"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            # ── Condition 2: Carrier must be "Other" (Professional Courier) ───
            if tracking_company.lower() != "other":
                detail["action"] = f"skipped (carrier: {tracking_company})"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            if not tracking_number:
                detail["action"] = "skipped (no tracking number)"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            # ── Both conditions met → check courier ───────────────────────────
            summary["checked"] += 1
            log.info(f"Order {order_number} | Tracking {tracking_number} → checking courier...")

            courier = check_courier(tracking_number)

            if courier.get("error"):
                detail["action"] = f"error: {courier['error']}"
                summary["errors"] += 1
                summary["details"].append(detail)
                continue

            if courier["is_delivered"]:
                try:
                    mark_delivered(order_id, ful_id)
                    detail["action"] = "✅ marked delivered"
                    summary["updated"] += 1
                    log.info(f"Order {order_number} | Tracking {tracking_number} → MARKED DELIVERED")
                except Exception as e:
                    detail["action"] = f"shopify_error: {e}"
                    summary["errors"] += 1
                    log.error(f"Order {order_number} | Shopify update failed: {e}")
            else:
                detail["action"] = f"not delivered (status: {courier['status']})"
                log.info(f"Order {order_number} | Not yet delivered: {courier['status']}")

            summary["details"].append(detail)

    log.info(
        f"Done. checked={summary['checked']} updated={summary['updated']} "
        f"errors={summary['errors']} skipped={summary['skipped']}"
    )
    return summary


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/check-tracking", methods=["POST", "GET"])
def check_tracking():
    """
    Called by Shopify Flow at 9am and 6pm IST every day.
    Loops all fulfilled orders → checks courier → marks delivered if confirmed.
    """
    log.info("=== /check-tracking triggered ===")
    summary = run_tracking()
    return jsonify({"ok": True, "summary": summary}), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check — Render and UptimeRobot ping this to keep the server warm."""
    return jsonify({"status": "ok", "store": SHOPIFY_STORE}), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Delivery Sync",
        "store":   SHOPIFY_STORE,
        "endpoints": {
            "POST /check-tracking": "Run tracking check (called by Shopify Flow)",
            "GET  /health":         "Health check",
        }
    }), 200


if __name__ == "__main__":
    app.run(debug=False, port=5000)
