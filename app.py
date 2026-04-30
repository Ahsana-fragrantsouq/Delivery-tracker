import os
import logging
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

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

print("=" * 60)
print("  DELIVERY TRACKER — STARTING")
print(f"  Store : {SHOPIFY_STORE}")
print(f"  Token : {'SET ✓' if SHOPIFY_ACCESS_TOKEN else 'MISSING ✗'}")
print("=" * 60)


# ── 1. Fetch only orders needing delivery check ───────────────────────────────

def get_orders_needing_delivery_check():
    print("\n[SHOPIFY] Fetching orders where Delivery Status = Tracking added...")
    all_orders = []
    url    = shopify("/orders.json")
    params = {"fulfillment_status": "shipped", "status": "any", "limit": 250}

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


# ── 2. Mark Delivery Status = Delivered ──────────────────────────────────────

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
# The site is WordPress-based. The tracking result is loaded via JavaScript
# (AJAX) after page load — BeautifulSoup cannot see JS-rendered content.
# Fix: POST directly to wp-admin/admin-ajax.php with common action names,
# OR try GET with the tracking number in the URL query string,
# OR try the REST API endpoint pattern.

def check_courier(tracking_number: str) -> dict:
    BASE = "https://professionalcourier.ae"

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer":         f"{BASE}/tracking/",
        "Accept":          "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
    })

    print(f"    [COURIER] Checking AWB {tracking_number}...")

    # ── Attempt 1: WordPress admin-ajax.php with common action names ──────────
    ajax_url = f"{BASE}/wp-admin/admin-ajax.php"
    actions  = [
        "track_shipment", "shipment_tracking", "get_tracking",
        "tracking_status", "track_order", "courier_tracking",
        "get_shipment_status", "track", "awb_tracking",
    ]

    for action in actions:
        try:
            payload = {"action": action, "awb": tracking_number,
                       "tracknumber": tracking_number, "TrackNo": tracking_number}
            r = session.post(ajax_url, data=payload, timeout=15)
            print(f"    [COURIER] admin-ajax action={action} → {r.status_code} ({len(r.text)} chars)")
            if r.status_code == 200 and tracking_number in r.text and len(r.text) > 100:
                print(f"    [COURIER] ✓ Got result from admin-ajax action={action}")
                result = _parse_response(r.text, tracking_number)
                if result["status"] != "unknown":
                    return result
        except Exception as e:
            print(f"    [COURIER] admin-ajax error: {e}")

    # ── Attempt 2: WordPress REST API ─────────────────────────────────────────
    rest_urls = [
        f"{BASE}/wp-json/tracking/v1/status/{tracking_number}",
        f"{BASE}/wp-json/courier/v1/track/{tracking_number}",
        f"{BASE}/wp-json/shipment/v1/track?awb={tracking_number}",
    ]
    for rest_url in rest_urls:
        try:
            r = session.get(rest_url, timeout=15)
            print(f"    [COURIER] REST {rest_url} → {r.status_code}")
            if r.status_code == 200 and tracking_number in r.text:
                result = _parse_response(r.text, tracking_number)
                if result["status"] != "unknown":
                    return result
        except Exception as e:
            print(f"    [COURIER] REST error: {e}")

    # ── Attempt 3: GET tracking page with AWB in query string ─────────────────
    get_urls = [
        f"{BASE}/tracking/?tracknumber={tracking_number}",
        f"{BASE}/tracking/?awb={tracking_number}",
        f"{BASE}/tracking/?TrackNo={tracking_number}",
        f"{BASE}/tracking/{tracking_number}/",
    ]
    for get_url in get_urls:
        try:
            r = session.get(get_url, timeout=20)
            print(f"    [COURIER] GET {get_url} → {r.status_code} ({len(r.text)} chars)")
            if r.status_code == 200 and tracking_number in r.text:
                print(f"    [COURIER] ✓ Tracking number found via GET")
                result = _parse_response(r.text, tracking_number)
                if result["status"] != "unknown":
                    return result
        except Exception as e:
            print(f"    [COURIER] GET error: {e}")

    # ── Attempt 4: Classic POST to /tracking/ (original method) ──────────────
    # Load the page first to get any CSRF tokens, then POST
    try:
        page = session.get(f"{BASE}/tracking/", timeout=20)
        soup = BeautifulSoup(page.text, "html.parser")
        form_data = {}
        form = soup.find("form")
        if form:
            for inp in form.find_all("input", {"type": "hidden"}):
                if inp.get("name"):
                    form_data[inp["name"]] = inp.get("value", "")
            # Get the actual form action URL if set
            action_url = form.get("action") or f"{BASE}/tracking/"
            if not action_url.startswith("http"):
                action_url = BASE + action_url
        else:
            action_url = f"{BASE}/tracking/"

        # Fill all possible field names
        for field in ("tracknumber", "TrackNo", "track_no", "awb_no",
                      "awbno", "awb", "tracking_number", "number"):
            form_data[field] = tracking_number

        print(f"    [COURIER] POST to {action_url} with fields: {list(form_data.keys())}")
        # Remove X-Requested-With for this request (it's a form POST, not AJAX)
        post_headers = {k: v for k, v in session.headers.items()
                        if k != "X-Requested-With"}
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"

        resp = session.post(action_url, data=form_data,
                            headers=post_headers, timeout=20)
        print(f"    [COURIER] POST → {resp.status_code} ({len(resp.text)} chars)")

        if tracking_number in resp.text:
            print(f"    [COURIER] ✓ Tracking number found in POST response")
            return _parse_response(resp.text, tracking_number)
        else:
            print(f"    [COURIER] ✗ Tracking number NOT in POST response")
            # Save a snippet of the response for debugging
            snippet = resp.text[:500].replace('\n', ' ')
            print(f"    [COURIER] Response snippet: {snippet}")

    except Exception as e:
        print(f"    [COURIER] POST error: {e}")

    print(f"    [COURIER] ✗ All attempts failed for AWB {tracking_number}")
    return {"is_delivered": False, "status": "not_found"}


def _parse_response(html_or_json: str, tracking_number: str) -> dict:
    """
    Parse courier site response.
    Handles both JSON responses (from AJAX/REST) and HTML responses.
    Reads ONLY the 'Current Status' cell — never the whole page.
    """
    # ── Try JSON first ────────────────────────────────────────────────────────
    try:
        import json
        data = json.loads(html_or_json)
        text = str(data).lower()
        print(f"    [COURIER] JSON response: {str(data)[:200]}")
        is_delivered = "delivered" in text
        status = "Delivered" if is_delivered else "In Transit"
        return {"is_delivered": is_delivered, "status": status}
    except Exception:
        pass

    # ── Parse as HTML ─────────────────────────────────────────────────────────
    soup      = BeautifulSoup(html_or_json, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    status_text = ""

    # Find the summary table: From | To | Current Status | Current Activity
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        print(f"    [COURIER] Table headers found: {headers}")
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

    # Fallback: scan 400 chars after tracking number only
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

    summary = {"checked": 0, "updated": 0, "errors": 0, "skipped": 0, "details": []}

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

            detail = {"order": order_number, "awb": tracking_number,
                      "carrier": tracking_company, "status": shipment_status,
                      "action": None}

            if shipment_status == "delivered":
                detail["action"] = "skip — already delivered"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            if tracking_company.lower() != "other":
                detail["action"] = f"skip — carrier '{tracking_company}'"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            if not tracking_number:
                detail["action"] = "skip — no tracking number"
                summary["skipped"] += 1
                summary["details"].append(detail)
                continue

            print(f"  → ✓ Conditions met — checking professionalcourier.ae...")
            summary["checked"] += 1

            courier = check_courier(tracking_number)

            if courier.get("error"):
                detail["action"] = f"error: {courier['error']}"
                summary["errors"] += 1
            elif courier["is_delivered"]:
                try:
                    mark_delivered(order_id, ful_id)
                    detail["action"] = "✅ MARKED DELIVERED in Shopify"
                    summary["updated"] += 1
                    print(f"  → ✅ MARKED DELIVERED")
                except Exception as e:
                    detail["action"] = f"Shopify update failed: {e}"
                    summary["errors"] += 1
                    print(f"  → ✗ Shopify update failed: {e}")
            else:
                detail["action"] = f"not delivered yet (courier: {courier['status']})"
                print(f"  → not delivered yet: {courier['status']}")

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
    print(f"\n>>> /check-tracking triggered")
    summary = run_tracking()
    return jsonify({"ok": True, "summary": summary}), 200


@app.route("/health", methods=["GET"])
def health():
    print(">>> GET /health — OK")
    return jsonify({"status": "ok", "store": SHOPIFY_STORE}), 200


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service":   "Delivery Sync — Fragrant Souq",
        "endpoints": {
            "POST /check-tracking": "Run tracking (called by Shopify Flow)",
            "GET  /health":         "Health check",
        }
    }), 200


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
