# Shopify Auto Tracking App

Auto-checks Aramex tracking status for fulfilled orders twice daily and updates
the delivery_status metafield on each order. Embedded inside Shopify Admin.

---

## Files

```
app.py              ← Flask app (all logic)
requirements.txt    ← Python dependencies
render.yaml         ← Render deployment config
.env.example        ← Copy to .env and fill in your values
```

---

## Setup Steps

### 1. Partner Dashboard — Create the App

1. Go to https://partners.shopify.com → Apps → Create app → Custom app
2. App name: "Tracking Checker" (or anything you like)
3. App URL: `https://your-app-name.onrender.com`
4. Allowed redirect URLs: `https://your-app-name.onrender.com/auth/callback`
5. Under Configuration → API Scopes, enable:
   - `read_orders`, `write_orders`
   - `read_fulfillments`, `write_fulfillments`
   - `write_metafields`, `read_metafields`
6. Save. Copy the **API Key** and **API Secret**.

### 2. Get Your Access Token

In the Partner Dashboard → your app → API credentials:
- Install the app on your store (fragrantsouq)
- Copy the **Admin API access token** (`shpat_...`)

### 3. Create the Metafield Definition

In your Shopify Admin (fragrantsouq):
1. Settings → Custom data → Orders → Add definition
2. Name: `Delivery Status`
3. Namespace and key: `custom.delivery_status`
4. Type: Single line text
5. Save

This makes the delivery_status appear in your Orders list view.

### 4. Get Aramex API Credentials

1. Go to https://developer.aramex.com
2. Register or log in with your Aramex business account
3. Get: Username, Password, Account Number, Account PIN, Account Entity

### 5. Deploy to Render

1. Push this folder to a GitHub repo
2. Go to https://render.com → New → Web Service → Connect your repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --bind 0.0.0.0:$PORT`
5. Add all environment variables from .env.example in the Render dashboard
6. Deploy. Note your Render URL (e.g. https://shopify-tracking.onrender.com)

### 6. Set Up Shopify Flow

In your Shopify Admin → Apps → Flow → Create workflow:

**Workflow 1 — Morning (8 AM)**
- Trigger: Scheduled time → Every day at 8:00 AM
- Action: Send HTTP request
  - URL: `https://your-app.onrender.com/check-tracking`
  - Method: POST
  - Headers: `X-Flow-Secret: your-secret-from-env`
  - Body: `{}`

**Workflow 2 — Evening (6 PM)**
- Same as above but set time to 6:00 PM

### 7. Add the Embedded App to Shopify Admin

In Partner Dashboard → your app → Extensions → Add:
- Extension type: Admin link
- Label: Tracking Checker
- Link URL: `https://your-app.onrender.com/app`
- Page: Orders

This adds the app as a link inside your Orders page, just like Track123.

---

## Testing

After deploying, test immediately by visiting:
```
https://your-app.onrender.com/check-tracking/manual
```
This runs the full check without waiting for Flow to trigger.

Or use the embedded UI:
```
https://your-app.onrender.com/app
```
Click "Run Check Now".

---

## How It Works

1. Shopify Flow calls `/check-tracking` at 8 AM and 6 PM
2. Flask fetches all fulfilled (but not yet delivered) orders via Shopify Admin API
3. For each order, it reads the tracking number from the fulfillment
4. If the carrier is Aramex, it calls the Aramex Tracking API
5. The returned status (e.g. "Out for delivery", "Delivered") is saved to
   the `custom.delivery_status` metafield on the order
6. This metafield value appears in the Delivery status column in your Orders list
7. Orders already marked "Delivered" are skipped to save API calls

---

## Aramex Status Codes → Display Values

| Aramex Code | Shown as |
|-------------|----------|
| SH005       | Delivered |
| SH006       | Delivery attempted |
| SH010       | Out for delivery |
| SH015       | In transit |
| SH020       | At destination facility |
| SH025       | Shipment on hold |
| SH030       | Returned to sender |
| SH035       | Cancelled |
| SH040       | Picked up |
| SH045       | Processed |
| SH050       | Customs clearance |

---

## Adding More Carriers

In `app.py`, find the `detect_carrier()` function and add your logic.
Then add a new `check_XYZ_tracking()` function following the same pattern
as `check_aramex_tracking()`, and call it in `run_tracking_check()`.
