"""
Reelz Backend — FastAPI + Supabase PostgreSQL
==============================================
4 endpoints only. Webhook is source of truth.
Local-first: app caches everything, backend is only hit:
  1. On sign-in (POST /auth/google)
  2. After payment (GET /subscription/status)
  3. On Paystack webhook (POST /webhook/paystack)
  4. On payment init (POST /payments/init)
"""

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reelz")

# ── Env ───────────────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID        = os.environ["GOOGLE_CLIENT_ID"]        # Your Android client ID
PAYSTACK_SECRET_KEY     = os.environ["PAYSTACK_SECRET_KEY"]     # sk_live_... or sk_test_...
DATABASE_URL            = os.environ["DATABASE_URL"]            # postgres://... from Supabase
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", PAYSTACK_SECRET_KEY)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Reelz API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    """
    Open a fresh connection per request.
    Render's free tier keeps the process alive, so a connection pool would
    work, but fresh connections are simpler and safe given low traffic.
    """
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    """Create tables if they don't exist. Runs at startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    google_sub   TEXT UNIQUE NOT NULL,
                    email        TEXT NOT NULL,
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id                   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status                    TEXT NOT NULL DEFAULT 'expired',
                    expires_at                TIMESTAMPTZ,
                    paystack_customer_code    TEXT,
                    paystack_subscription_code TEXT,
                    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
                CREATE INDEX IF NOT EXISTS idx_users_google_sub     ON users(google_sub);
            """)
        conn.commit()
    log.info("DB tables ensured")


@app.on_event("startup")
async def startup():
    init_db()


# ── Pydantic models ───────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    id_token: str


class PaymentInitRequest(BaseModel):
    user_id: str          # UUID from the app's local session
    plan: str             # "monthly" | "yearly"
    email: str            # Shown on Paystack checkout (from Google, not trusted for auth)


class SubscriptionStatusRequest(BaseModel):
    user_id: str


# ── Helper: verify Google ID token ───────────────────────────────────────────

async def verify_google_token(id_token: str) -> dict:
    """
    Calls Google's tokeninfo endpoint. Returns the decoded claims or raises.
    For production volume, use google-auth library instead.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": id_token},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    claims = resp.json()

    # Validate audience — must match your Android client ID
    aud = claims.get("aud", "")
    if GOOGLE_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        log.warning("Token audience mismatch: %s", aud)
        raise HTTPException(status_code=401, detail="Token audience mismatch")

    # Token must not be expired
    exp = int(claims.get("exp", 0))
    if datetime.now(timezone.utc).timestamp() > exp:
        raise HTTPException(status_code=401, detail="Token expired")

    return claims


# ── Helper: read subscription ─────────────────────────────────────────────────

def _get_subscription(cur, user_id: str) -> dict | None:
    cur.execute(
        "SELECT * FROM subscriptions WHERE user_id = %s ORDER BY updated_at DESC LIMIT 1",
        (user_id,),
    )
    return cur.fetchone()


def _subscription_response(sub: dict | None) -> dict:
    """
    Normalise to the shape the app expects.
    Fail SAFE toward free: any ambiguity returns premium=false.
    """
    if sub is None:
        return {"premium": False, "status": "none", "expires_at": None}

    now = datetime.now(timezone.utc)
    expires_at: datetime | None = sub.get("expires_at")
    status = sub.get("status", "expired")

    # Grace period = 24 hours after expiry
    in_grace = (
        expires_at is not None
        and expires_at < now
        and (now - expires_at) < timedelta(hours=24)
    )

    is_premium = (
        status == "active"
        and expires_at is not None
        and (expires_at > now or in_grace)
    )

    return {
        "premium":    is_premium,
        "status":     status,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 1 — POST /auth/google
# Called once per sign-in. Returns premium status from DB.
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/auth/google")
async def auth_google(body: GoogleAuthRequest):
    """
    1. Verify token with Google (source of truth for identity).
    2. Upsert user row by google_sub (never by email — email can change).
    3. Return user_id + current subscription status.

    The app saves this locally. It will NOT call this endpoint again until
    the user explicitly signs in again.
    """
    claims = await verify_google_token(body.id_token)

    google_sub: str = claims["sub"]   # permanent, never changes
    email: str = claims.get("email", "").lower().strip()

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Upsert user — use google_sub as the unique key
            cur.execute("""
                INSERT INTO users (google_sub, email)
                VALUES (%s, %s)
                ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email
                RETURNING id
            """, (google_sub, email))
            user = cur.fetchone()
            user_id = str(user["id"])

            # Read latest subscription
            sub = _get_subscription(cur, user_id)
        conn.commit()

    log.info("auth/google: sub=%s user_id=%s", google_sub[:8], user_id[:8])
    return {"user_id": user_id, **_subscription_response(sub)}


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 2 — POST /payments/init
# Creates a Paystack transaction and returns the checkout URL.
# ═════════════════════════════════════════════════════════════════════════════

# Prices in kobo (1 NGN = 100 kobo)
PLAN_AMOUNTS = {
    "monthly": 150_000,   # ₦1,500
    "yearly":  1_200_000, # ₦12,000
}

@app.post("/payments/init")
async def payments_init(body: PaymentInitRequest):
    """
    Initialises a Paystack transaction server-side.
    The app opens the returned authorization_url in an in-app browser.
    The webhook (not this response) is the source of truth.
    """
    if body.plan not in PLAN_AMOUNTS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {body.plan}")

    amount = PLAN_AMOUNTS[body.plan]

    payload = {
        "email":     body.email,
        "amount":    amount,
        "currency":  "NGN",
        "metadata": {
            "user_id":    body.user_id,
            "plan":       body.plan,
            "custom_fields": [
                {"display_name": "Plan",    "variable_name": "plan",    "value": body.plan},
                {"display_name": "User ID", "variable_name": "user_id", "value": body.user_id},
            ],
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json=payload,
        )

    if resp.status_code != 200:
        log.error("Paystack init failed: %s", resp.text)
        raise HTTPException(status_code=502, detail="Payment provider error")

    data = resp.json()
    if not data.get("status"):
        raise HTTPException(status_code=502, detail=data.get("message", "Paystack error"))

    return {
        "authorization_url": data["data"]["authorization_url"],
        "reference":         data["data"]["reference"],
    }


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 3 — GET /subscription/status
# Called in background: on app launch (if >24h since last check) and after payment.
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/subscription/status")
async def subscription_status(user_id: str):
    """
    Lightweight read-only endpoint.
    The app calls this at most:
      - Once at launch (only if >24h since last check, checked locally)
      - Once after the user pays
    Never called per-screen.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Verify user exists
            cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="User not found")
            sub = _get_subscription(cur, user_id)

    return _subscription_response(sub)


# ═════════════════════════════════════════════════════════════════════════════
# ENDPOINT 4 — POST /webhook/paystack
# Paystack calls this directly. This is the ONLY place subscriptions activate.
# ═════════════════════════════════════════════════════════════════════════════

@app.post("/webhook/paystack")
async def webhook_paystack(request: Request):
    """
    Source of truth for all payment events.
    Verifies the Paystack HMAC signature before touching the DB.
    Returns 200 immediately — Paystack retries on non-200.
    """
    body_bytes = await request.body()
    signature  = request.headers.get("x-paystack-signature", "")

    # ── 1. Verify HMAC signature ──────────────────────────────────────────
    expected = hmac.new(
        PAYSTACK_WEBHOOK_SECRET.encode(),
        body_bytes,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        log.warning("Webhook signature mismatch — ignoring")
        # Return 200 so Paystack doesn't keep retrying a bad secret
        return Response(status_code=200)

    # ── 2. Parse event ────────────────────────────────────────────────────
    try:
        event = json.loads(body_bytes)
    except json.JSONDecodeError:
        log.error("Webhook: invalid JSON")
        return Response(status_code=200)

    event_type = event.get("event", "")
    data       = event.get("data", {})
    log.info("Webhook received: %s", event_type)

    # ── 3. Handle charge.success ──────────────────────────────────────────
    if event_type == "charge.success":
        await _handle_charge_success(data)

    # ── 4. Handle subscription.disable / cancel ───────────────────────────
    elif event_type in ("subscription.disable", "subscription.not_renew"):
        await _handle_subscription_cancel(data)

    return Response(status_code=200)


async def _handle_charge_success(data: dict):
    """Activate or renew the subscription for this payment."""
    metadata = data.get("metadata", {})
    user_id  = metadata.get("user_id", "")
    plan     = metadata.get("plan", "monthly")

    # Fall back: try custom_fields list
    if not user_id:
        for field in metadata.get("custom_fields", []):
            if field.get("variable_name") == "user_id":
                user_id = field.get("value", "")
            if field.get("variable_name") == "plan":
                plan = field.get("value", "monthly")

    if not user_id:
        log.error("charge.success: no user_id in metadata — %s", metadata)
        return

    # Calculate expiry
    plan_days = 365 if plan == "yearly" else 31
    expires_at = datetime.now(timezone.utc) + timedelta(days=plan_days)

    customer_code      = data.get("customer", {}).get("customer_code", "")
    subscription_code  = data.get("subscription_code", "")  # present on recurring charges
    reference          = data.get("reference", "")

    log.info(
        "Activating premium: user_id=%s plan=%s expires=%s ref=%s",
        user_id[:8], plan, expires_at.date(), reference,
    )

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Verify user exists before writing
                cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
                if cur.fetchone() is None:
                    log.error("charge.success: unknown user_id=%s", user_id)
                    return

                # Upsert subscription
                cur.execute("""
                    INSERT INTO subscriptions
                        (user_id, status, expires_at, paystack_customer_code,
                         paystack_subscription_code, updated_at)
                    VALUES (%s, 'active', %s, %s, %s, now())
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        status                     = 'active',
                        expires_at                 = EXCLUDED.expires_at,
                        paystack_customer_code     = EXCLUDED.paystack_customer_code,
                        paystack_subscription_code = EXCLUDED.paystack_subscription_code,
                        updated_at                 = now()
                """, (user_id, expires_at, customer_code, subscription_code))

            conn.commit()
        log.info("Premium activated for user_id=%s", user_id[:8])
    except Exception as exc:
        log.exception("DB error activating subscription: %s", exc)


# Add UNIQUE constraint helper (idempotent)
# NOTE: Run this migration once in Supabase SQL editor:
#   ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_user_id_unique UNIQUE (user_id);
# The ON CONFLICT clause above requires it.


async def _handle_subscription_cancel(data: dict):
    """Mark subscription as cancelled when Paystack disables it."""
    subscription_code = data.get("subscription_code", "")
    if not subscription_code:
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE subscriptions SET status = 'cancelled', updated_at = now()
                    WHERE paystack_subscription_code = %s
                """, (subscription_code,))
            conn.commit()
        log.info("Subscription cancelled: %s", subscription_code)
    except Exception as exc:
        log.exception("DB error cancelling subscription: %s", exc)


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True}
