"""
routers/payments.py
Cryptographically verified Razorpay Webhook listener.
Handles B2B onboarding confirmation and B2C booking deposit confirmations.
"""

import os
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, timezone

import razorpay
from fastapi import APIRouter, HTTPException, Request, Header
from google.cloud import firestore as fs

from database import get_db, Collections
from utils.invoice_generator import generate_booking_invoice

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])

RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
META_API_VERSION        = os.getenv("META_API_VERSION", "v19.0")
META_ACCESS_TOKEN       = os.getenv("META_ACCESS_TOKEN", "")

PLAN_DURATIONS = {
    "monthly": 30,
    "yearly" : 365,
}


# ── Signature Verification ─────────────────────────────────────────────────────

def _verify_razorpay_signature(raw_body: bytes, signature: str) -> bool:
    """HMAC-SHA256 verification of Razorpay webhook payload."""
    if not RAZORPAY_WEBHOOK_SECRET:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET not set.")
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── WhatsApp Message Sender ────────────────────────────────────────────────────

def _send_whatsapp_text(phone_number_id: str, to: str, message: str) -> None:
    """Send a plain text WhatsApp message via Meta Cloud API."""
    import httpx
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"               : to,
        "type"             : "text",
        "text"             : {"body": message},
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("WhatsApp send failed for %s: %s", to, e)


def _send_whatsapp_document(phone_number_id: str, to: str, doc_url: str, filename: str, caption: str) -> None:
    """Send a document (PDF invoice) via WhatsApp."""
    import httpx
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"               : to,
        "type"             : "document",
        "document"         : {"link": doc_url, "filename": filename, "caption": caption},
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("WhatsApp document send failed for %s: %s", to, e)


# ── Main Webhook Endpoint ──────────────────────────────────────────────────────

@router.post("/razorpay-webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(..., alias="X-Razorpay-Signature"),
):
    """
    Single Razorpay webhook endpoint.
    Routes to appropriate handler based on event type.
    """
    raw_body = await request.body()

    # ── Signature verification (MUST happen before any business logic) ─────────
    if not _verify_razorpay_signature(raw_body, x_razorpay_signature):
        logger.warning("Razorpay webhook: invalid signature rejected.")
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    payload    = await request.json()
    event      = payload.get("event", "")
    entity     = payload.get("payload", {})
    event_id   = payload.get("id", "")  # Razorpay unique event ID

    logger.info("Razorpay webhook received: event=%s id=%s", event, event_id)

    # ── Idempotency guard: skip already-processed events ──────────────────────
    if event_id:
        db = get_db()
        already_processed = (
            db.collection("webhook_events")
            .document(event_id)
            .get()
            .exists
        )
        if already_processed:
            logger.info("Duplicate webhook event skipped: %s", event_id)
            return {"status": "already_processed"}

        # Mark as processed (TTL via Firestore TTL policy on expires_at field)
        import datetime as _dt
        db.collection("webhook_events").document(event_id).set({
            "event"     : event,
            "received_at": _dt.datetime.now(_dt.timezone.utc),
            "expires_at" : _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=7),
        })

    # ── Route by event type ────────────────────────────────────────────────────
    if event == "payment_link.paid":
        await _handle_b2b_onboarding_payment(entity)

    elif event == "payment.captured":
        await _handle_b2c_booking_payment(entity)

    # Return 200 quickly so Razorpay doesn't retry
    return {"status": "ok"}


# ── B2B: Activate Vendor Subscription ─────────────────────────────────────────

async def _handle_b2b_onboarding_payment(entity: dict) -> None:
    """
    Fires on 'payment_link.paid'.
    Activates vendor, sets subscription window, configures Gemini bot profile.
    """
    payment_link = entity.get("payment_link", {}).get("payload", {})
    notes        = payment_link.get("notes", {})
    client_id    = notes.get("client_id")
    plan         = notes.get("plan", "basic")
    billing_cycle= notes.get("billing_cycle", "monthly")

    if not client_id:
        logger.error("B2B webhook: missing client_id in notes. Payload: %s", entity)
        return

    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    client_doc = client_ref.get()

    if not client_doc.exists:
        logger.error("B2B webhook: client_id %s not found in Firestore.", client_id)
        return

    client_data = client_doc.to_dict()

    # Idempotency guard — don't double-activate
    if client_data.get("status") == "active":
        logger.info("B2B webhook: client %s already active, skipping.", client_id)
        return

    now = datetime.now(timezone.utc)
    days = PLAN_DURATIONS.get(billing_cycle, 30)
    sub_end = now + timedelta(days=days)
    grace_end = sub_end + timedelta(days=3)

    # Configure Gemini persona based on business type
    business_type = client_data.get("business_type", "salon")
    business_name = client_data.get("business_name", "")
    persona = {
        "persona_name": "Priya",
        "language"    : "hi-en",
        "business_type": business_type,
        "welcome_msg" : (
            f"Namaste! Main Priya hoon, {business_name} ki AI receptionist. "
            f"Appointment book karna hai ya kuch puchna hai? 😊"
        ),
        "plan"        : plan,
        "max_daily_bookings": 50 if plan == "premium" else 20,
    }

    client_ref.update({
        "status"               : "active",
        "plan"                 : plan,
        "billing_cycle"        : billing_cycle,
        "subscription_end_date": sub_end,
        "grace_period_end"     : grace_end,
        "gemini_bot_profile"   : persona,
        "activated_at"         : now,
        "updated_at"           : now,
    })

    logger.info(
        "Vendor ACTIVATED: %s | plan=%s | expires=%s",
        client_id, plan, sub_end.isoformat()
    )

    # Send welcome WhatsApp message to owner
    owner_phone      = client_data.get("owner_phone", "")
    phone_number_id  = client_data.get("whatsapp_phone_id", "")
    if owner_phone and phone_number_id:
        _send_whatsapp_text(
            phone_number_id,
            owner_phone,
            f"🎉 Saarthi-AI activated for *{business_name}*!\n\n"
            f"Plan: *{plan.title()} ({billing_cycle})*\n"
            f"Valid till: *{sub_end.strftime('%d %b %Y')}*\n\n"
            f"Aapka AI WhatsApp receptionist ab live hai. Namaskar! 🙏"
        )


# ── B2C: Confirm Customer Booking ─────────────────────────────────────────────

async def _handle_b2c_booking_payment(entity: dict) -> None:
    """
    Fires on 'payment.captured'.
    Confirms customer booking, generates PDF invoice, sends via WhatsApp.
    """
    payment    = entity.get("payment", {}).get("payload", {})
    notes      = payment.get("notes", {})
    booking_id = notes.get("booking_id")
    client_id  = notes.get("client_id")

    if not booking_id or not client_id:
        logger.error("B2C webhook: missing booking_id or client_id. Notes: %s", notes)
        return

    db = get_db()
    booking_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .document(booking_id)
    )
    booking_doc = booking_ref.get()

    if not booking_doc.exists:
        logger.error("B2C webhook: booking %s not found.", booking_id)
        return

    booking_data = booking_doc.to_dict()

    # Idempotency guard
    if booking_data.get("status") == "confirmed":
        logger.info("B2C webhook: booking %s already confirmed.", booking_id)
        return

    # Confirm the slot
    slot_id  = booking_data.get("slot_id")
    slot_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .document(slot_id)
    )

    now = datetime.now(timezone.utc)

    # Atomic batch update: booking + slot
    batch = db.batch()
    batch.update(booking_ref, {
        "status"      : "confirmed",
        "confirmed_at": now,
        "updated_at"  : now,
        "payment_id"  : payment.get("id", ""),
    })
    batch.update(slot_ref, {
        "status"    : "booked",
        "booking_id": booking_id,
        "updated_at": now,
    })
    batch.commit()

    logger.info("Booking CONFIRMED: %s | client=%s", booking_id, client_id)

    # Mark booking session completed (agar Web App se aaya tha)
    try:
        from routers.booking_session import mark_session_completed
        mark_session_completed(booking_id)
    except Exception as e:
        logger.debug("Session mark-complete skip: %s", e)

    # Generate PDF invoice
    client_doc = db.collection(Collections.CLIENTS).document(client_id).get().to_dict()
    invoice_url = generate_booking_invoice(
        booking_id=booking_id,
        booking_data=booking_data,
        client_data=client_doc,
        payment_id=payment.get("id", ""),
    )

    # Send confirmation + invoice via WhatsApp
    customer_phone = booking_data.get("customer_phone", "")
    phone_number_id = client_doc.get("whatsapp_phone_id", "")
    service_name    = booking_data.get("service_name", "Service")
    slot_time       = booking_data.get("slot_datetime", "")
    staff_name      = booking_data.get("staff_name", "")
    business_name   = client_doc.get("business_name", "")

    if customer_phone and phone_number_id:
        confirmation_msg = (
            f"✅ *Booking Confirmed!*\n\n"
            f"📍 *{business_name}*\n"
            f"💇 Service: {service_name}\n"
            f"👤 Staff: {staff_name}\n"
            f"🗓 Date & Time: {slot_time}\n"
            f"🎫 Booking ID: `{booking_id}`\n\n"
            f"Aapka invoice neeche attach hai. Dhanyavaad! 🙏"
        )
        _send_whatsapp_text(phone_number_id, customer_phone, confirmation_msg)

        if invoice_url:
            _send_whatsapp_document(
                phone_number_id,
                customer_phone,
                invoice_url,
                f"invoice_{booking_id}.pdf",
                f"Booking Invoice — {business_name}"
            )