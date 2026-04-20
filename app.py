import os
import hmac
import hashlib
import base64
import logging
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify, render_template_string, redirect, session
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-me-in-production")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
SHOPIFY_API_KEY    = os.getenv("SHOPIFY_API_KEY")
SHOPIFY_API_SECRET = os.getenv("SHOPIFY_API_SECRET")
SHOPIFY_STORE      = os.getenv("SHOPIFY_STORE")        # e.g. fragrantsouq.myshopify.com
ACCESS_TOKEN       = os.getenv("SHOPIFY_ACCESS_TOKEN") # long-lived token for custom app
FLOW_WEBHOOK_SECRET = os.getenv("FLOW_WEBHOOK_SECRET", "") # optional shared secret

ARAMEX_USERNAME    = os.getenv("ARAMEX_USERNAME", "")
ARAMEX_PASSWORD    = os.getenv("ARAMEX_PASSWORD", "")
ARAMEX_ACCOUNT_NUM = os.getenv("ARAMEX_ACCOUNT_NUM", "")
ARAMEX_ACCOUNT_PIN = os.getenv("ARAMEX_ACCOUNT_PIN", "")
ARAMEX_ACCOUNT_ENTITY   = os.getenv("ARAMEX_ACCOUNT_ENTITY", "")
ARAMEX_ACCOUNT_COUNTRY  = os.getenv("ARAMEX_ACCOUNT_COUNTRY", "AE")

# ─── Aramex tracking status map ───────────────────────────────────────────────
# Maps Aramex UpdateCode → exact stage labels shown on aramex.com tracking page
#
# The 7 stages shown on the Aramex website visual tracker:
#   Created → Collected → Departed → In transit →
#   Arrived at destination → Out for delivery → Delivered
#
# Aramex API UpdateCode reference:
#   Codes starting with "SH" are standard shipment update codes.
#   Below is the full known mapping based on Aramex API documentation.

ARAMEX_STATUS_MAP = {

    # ── Stage 1: Created ──────────────────────────────────────────────────────
    # Shipment record exists in Aramex system, not yet picked up
    "SH001": "Created",
    "SH002": "Created",       # Shipment data received
    "SH003": "Created",       # Label printed / booking confirmed

    # ── Stage 2: Collected ───────────────────────────────────────────────────
    # Aramex courier has physically picked up the package from sender
    "SH040": "Collected",     # Picked up from shipper
    "SH041": "Collected",     # Received at origin facility
    "SH045": "Collected",     # Shipment processed at origin

    # ── Stage 3: Departed ────────────────────────────────────────────────────
    # Package has left the origin city / facility, now moving toward destination
    "SH060": "Departed",      # Departed origin gateway
    "SH061": "Departed",      # Departed origin country
    "SH062": "Departed",      # Departed transit hub
    "SH065": "Departed",      # On vehicle / dispatched from facility

    # ── Stage 4: In transit ──────────────────────────────────────────────────
    # Package is moving between facilities (domestic or international)
    "SH015": "In transit",    # In transit (general)
    "SH016": "In transit",    # Arrived at transit facility
    "SH017": "In transit",    # Departed transit facility
    "SH050": "In transit",    # Customs clearance in progress (still moving)
    "SH051": "In transit",    # Customs cleared
    "SH055": "In transit",    # Import customs processing

    # ── Stage 5: Arrived at destination ──────────────────────────────────────
    # Package has reached the destination city's Aramex facility
    "SH020": "Arrived at destination",   # Arrived at destination gateway
    "SH021": "Arrived at destination",   # Arrived at destination facility
    "SH022": "Arrived at destination",   # Received at destination station

    # ── Stage 6: Out for delivery ─────────────────────────────────────────────
    # Aramex courier is on the way to the customer's address right now
    "SH010": "Out for delivery",         # Out for delivery
    "SH011": "Out for delivery",         # With delivery courier

    # ── Stage 7: Delivered ────────────────────────────────────────────────────
    # Package successfully handed to recipient
    "SH005": "Delivered",                # Delivered successfully
    "SH007": "Delivered",                # Delivered to neighbour / reception

    # ── Exceptions (not part of normal flow but important to show) ────────────
    "SH006": "Delivery attempted",       # Delivery tried, nobody home
    "SH008": "Delivery attempted",       # Second delivery attempt
    "SH009": "Delivery attempted",       # Third delivery attempt
    "SH025": "On hold",                  # Shipment on hold (customer request / address issue)
    "SH026": "On hold",                  # Held at facility (awaiting customer action)
    "SH030": "Returned to sender",       # Return initiated
    "SH031": "Returned to sender",       # Return in progress
    "SH032": "Returned to sender",       # Returned to origin
    "SH035": "Cancelled",                # Shipment cancelled
    "SH070": "Lost",                     # Shipment lost / investigation opened
}

# The 7 normal stage labels in order — used for display and ordering logic
ARAMEX_STAGE_ORDER = [
    "Created",
    "Collected",
    "Departed",
    "In transit",
    "Arrived at destination",
    "Out for delivery",
    "Delivered",
]

# ─── Shopify API helpers ───────────────────────────────────────────────────────
def shopify_headers():
    return {
        "X-Shopify-Access-Token": ACCESS_TOKEN,
        "Content-Type": "application/json",
    }

def shopify_url(path):
    return f"https://{SHOPIFY_STORE}/admin/api/2024-04/{path}"


def get_fulfilled_undelivered_orders():
    """Fetch orders that are fulfilled but delivery_status is not Delivered."""
    orders = []
    url = shopify_url("orders.json")
    params = {
        "status": "open",
        "fulfillment_status": "fulfilled",
        "limit": 250,
        "fields": "id,name,fulfillments,metafields",
    }
    while url:
        resp = requests.get(url, headers=shopify_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()
        orders.extend(data.get("orders", []))
        # Pagination via Link header
        link = resp.headers.get("Link", "")
        url = None
        params = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
    return orders


def get_order_metafield(order_id, namespace, key):
    """Fetch a specific metafield for an order."""
    url = shopify_url(f"orders/{order_id}/metafields.json")
    resp = requests.get(url, headers=shopify_headers(),
                        params={"namespace": namespace, "key": key})
    resp.raise_for_status()
    mfs = resp.json().get("metafields", [])
    return mfs[0] if mfs else None


def set_order_metafield(order_id, namespace, key, value):
    """Create or update a metafield on an order."""
    existing = get_order_metafield(order_id, namespace, key)
    if existing:
        url = shopify_url(f"orders/{order_id}/metafields/{existing['id']}.json")
        method = "put"
        payload = {"metafield": {"id": existing["id"], "value": value, "type": "single_line_text_field"}}
    else:
        url = shopify_url(f"orders/{order_id}/metafields.json")
        method = "post"
        payload = {"metafield": {
            "namespace": namespace,
            "key": key,
            "value": value,
            "type": "single_line_text_field",
        }}
    resp = getattr(requests, method)(url, headers=shopify_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


# ─── Aramex API ───────────────────────────────────────────────────────────────
def check_aramex_tracking(tracking_number: str) -> str:
    """
    Call Aramex Tracking API and return the latest status string.
    Returns 'Unknown' if anything fails.
    """
    url = "https://ws.aramex.net/ShippingAPI.V2/Tracking/Service_1_0.svc/json/TrackShipments"
    payload = {
        "ClientInfo": {
            "UserName": ARAMEX_USERNAME,
            "Password": ARAMEX_PASSWORD,
            "Version": "v1.0",
            "AccountNumber": ARAMEX_ACCOUNT_NUM,
            "AccountPin": ARAMEX_ACCOUNT_PIN,
            "AccountEntity": ARAMEX_ACCOUNT_ENTITY,
            "AccountCountryCode": ARAMEX_ACCOUNT_COUNTRY,
            "Source": 24,
        },
        "Shipments": [tracking_number],
        "GetLastTrackingUpdateOnly": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tracking_results = data.get("TrackingResults", [])
        if not tracking_results:
            return "No tracking data"
        # Each result has a Value list with update entries
        updates = tracking_results[0].get("Value", [])
        if not updates:
            return "No updates"
        latest = updates[-1]  # most recent
        code = latest.get("UpdateCode", "")
        description = latest.get("UpdateDescription", "")
        mapped = ARAMEX_STATUS_MAP.get(code, description or "In transit")
        logger.info(f"Aramex {tracking_number}: {code} → {mapped}")
        return mapped
    except Exception as e:
        logger.error(f"Aramex API error for {tracking_number}: {e}")
        return "Tracking error"


def detect_carrier(tracking_number: str) -> str:
    """Simple heuristic to detect carrier from tracking number format."""
    tn = str(tracking_number).strip()
    # Aramex tracking numbers are typically 11 digits starting with 6
    if tn.isdigit() and len(tn) == 11:
        return "aramex"
    # Add more carriers here as needed (e.g. DHL, FedEx, etc.)
    return "unknown"


# ─── Core check-and-update logic ──────────────────────────────────────────────
def run_tracking_check():
    """
    Main job: fetch fulfilled orders, check each tracking number,
    update delivery_status metafield if changed.
    """
    results = {"checked": 0, "updated": 0, "errors": []}
    try:
        orders = get_fulfilled_undelivered_orders()
        logger.info(f"Found {len(orders)} fulfilled orders to check")
    except Exception as e:
        results["errors"].append(f"Failed to fetch orders: {e}")
        return results

    for order in orders:
        order_id   = order["id"]
        order_name = order.get("name", order_id)
        fulfillments = order.get("fulfillments", [])

        if not fulfillments:
            continue

        # Check current delivery_status metafield
        current_mf = get_order_metafield(order_id, "custom", "delivery_status")
        current_status = current_mf["value"] if current_mf else ""

        # Skip orders that have reached a terminal state — no point re-checking
        TERMINAL_STATES = {"Delivered", "Returned to sender", "Cancelled", "Lost"}
        if current_status in TERMINAL_STATES:
            logger.info(f"{order_name}: terminal status '{current_status}', skipping")
            continue

        # Get the most recent fulfillment's tracking number
        last_fulfillment = fulfillments[-1]
        tracking_number  = last_fulfillment.get("tracking_number")
        tracking_company = (last_fulfillment.get("tracking_company") or "").lower()

        if not tracking_number:
            logger.info(f"{order_name}: no tracking number found")
            continue

        results["checked"] += 1

        # Detect carrier and fetch status
        carrier = "aramex" if "aramex" in tracking_company else detect_carrier(tracking_number)

        if carrier == "aramex":
            new_status = check_aramex_tracking(tracking_number)
        else:
            new_status = f"Manual check needed ({tracking_company or 'unknown carrier'})"

        # Only update if status has changed
        if new_status != current_status:
            try:
                set_order_metafield(order_id, "custom", "delivery_status", new_status)
                logger.info(f"{order_name}: updated '{current_status}' → '{new_status}'")
                results["updated"] += 1
            except Exception as e:
                err = f"{order_name}: failed to update metafield: {e}"
                logger.error(err)
                results["errors"].append(err)

    return results


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return jsonify({"status": "Shopify Tracking App running"})


@app.route("/check-tracking", methods=["POST"])
def check_tracking():
    """
    Called by Shopify Flow twice daily.
    Optionally verify a shared secret in the X-Flow-Secret header.
    """
    if FLOW_WEBHOOK_SECRET:
        provided = request.headers.get("X-Flow-Secret", "")
        if provided != FLOW_WEBHOOK_SECRET:
            logger.warning("Unauthorized /check-tracking call")
            return jsonify({"error": "Unauthorized"}), 401

    logger.info("=== Tracking check triggered ===")
    results = run_tracking_check()
    logger.info(f"Done: {results}")
    return jsonify({"ok": True, "results": results})


@app.route("/check-tracking/manual", methods=["GET"])
def manual_check():
    """GET endpoint so you can trigger manually from browser during testing."""
    results = run_tracking_check()
    return jsonify({"ok": True, "results": results})





if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
