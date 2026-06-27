"""
routers/booking_session.py
Customer Web Booking flow:
1. WhatsApp bot customer ka naam/number leta hai
2. Ek session token banta hai (short-lived, secure)
3. Customer ko web app link milta hai: /book/{client_id}?session={token}
4. Web app session se customer details fetch karta hai
5. Customer service+staff+slot select karta hai → payment → booking confirm
"""
from google.cloud.firestore import FieldFilter
from fastapi import APIRouter, HTTPException, Query, Request # <-- Add Request here
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from database import get_db, Collections
from utils.rate_limiter import limiter, LIMIT_NORMAL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/booking-session", tags=["Booking Session"])

SESSION_TTL_MINUTES = 30  # Session 30 min tak valid rahega


# ── Models ─────────────────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    client_id      : str
    customer_phone : str
    customer_name  : str = ""


class CreateSessionResponse(BaseModel):
    session_token : str
    booking_url   : str
    expires_at    : str


# ── Create Session (called internally by WhatsApp bot) ────────────────────────

def create_booking_session(
    client_id: str,
    customer_phone: str,
    customer_name: str = "",
) -> dict:
    """
    WhatsApp bot ke andar se call hota hai jab customer
    naam+number de deta hai. Session token generate karta hai
    aur Firestore mein store karta hai.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=SESSION_TTL_MINUTES)

    token = secrets.token_urlsafe(24)

    session_data = {
        "client_id"     : client_id,
        "customer_phone": customer_phone,
        "customer_name" : customer_name,
        "status"        : "pending",   # pending -> completed -> expired
        "created_at"    : now,
        "expires_at"    : expires_at,
        "booking_id"    : None,
    }

    db.collection("booking_sessions").document(token).set(session_data)

    app_base_url = os.getenv("APP_BASE_URL", "https://saarthi-ai.in")
    booking_url  = f"{app_base_url}/book?session={token}"

    logger.info(
        "Booking session created: client=%s customer=%s token=%s...",
        client_id, customer_phone, token[:8]
    )

    return {
        "session_token": token,
        "booking_url"  : booking_url,
        "expires_at"   : expires_at.isoformat(),
    }


# ── Get Session Details (called by Web Booking App) ───────────────────────────

@router.get("/{session_token}")
@limiter.limit(LIMIT_NORMAL)
async def get_session(request: Request, session_token: str):
    """
    Web booking app yeh call karta hai page load pe.
    Session valid hai to client_id + customer details return karta hai,
    saath mein client ka business_name, services, available slots.
    """
    db = get_db()
    session_ref = db.collection("booking_sessions").document(session_token)
    session_doc = session_ref.get()

    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    session = session_doc.to_dict()
    now = datetime.now(timezone.utc)

    expires_at = session.get("expires_at")
    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at and now > expires_at:
        raise HTTPException(status_code=410, detail="Session has expired. Please request a new link.")

    if session.get("status") == "completed":
        raise HTTPException(status_code=409, detail="This booking session is already completed.")

    client_id = session["client_id"]

    # Client business info fetch karo
    client_doc = db.collection(Collections.CLIENTS).document(client_id).get()
    if not client_doc.exists:
        raise HTTPException(status_code=404, detail="Business not found.")
    client_data = client_doc.to_dict()

    # Active services fetch karo
    services_docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .where("is_active", "==", True)
        .get()
    )
    services = [{"service_id": d.id, **d.to_dict()} for d in services_docs]

    # Available slots fetch karo (next 14 days)
    slot_start = now
    slot_end   = now + timedelta(days=14)
    slots_docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .where("status", "==", "available")
        .where("slot_datetime", ">=", slot_start)
        .where("slot_datetime", "<=", slot_end)
        .order_by("slot_datetime")
        .limit(100)
        .get()
    )
    slots = []
    for d in slots_docs:
        data = d.to_dict()
        data["slot_id"] = d.id
        if hasattr(data.get("slot_datetime"), "isoformat"):
            data["slot_datetime"] = data["slot_datetime"].isoformat()
        slots.append(data)

    return {
        "session_token" : session_token,
        "client_id"      : client_id,
        "business_name"  : client_data.get("business_name", ""),
        "business_type"  : client_data.get("business_type", ""),
        "customer_name"  : session.get("customer_name", ""),
        "customer_phone" : session.get("customer_phone", ""),
        "services"       : services,
        "slots"          : slots,
    }


# ── Update Session With Selection (called by Web App before payment) ─────────

class UpdateSessionRequest(BaseModel):
    service_id : str
    slot_id    : str


@router.patch("/{session_token}/select")
@limiter.limit(LIMIT_NORMAL)
async def select_service_slot(request: Request, session_token: str, body: UpdateSessionRequest):
    """
    Customer ne service + slot choose kar liya web app mein.
    Yeh slot ko temporarily lock karega aur booking record banayega
    (status: pending_payment), phir Razorpay link generate karega.
    """
    from routers.booking import _initiate_booking  # reuse existing logic

    db = get_db()
    session_ref = db.collection("booking_sessions").document(session_token)
    session_doc = session_ref.get()

    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found.")

    session = session_doc.to_dict()
    client_id = session["client_id"]

    client_doc = db.collection(Collections.CLIENTS).document(client_id).get()
    client_data = client_doc.to_dict()

    # Service fetch karo
    service_doc = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .document(body.service_id)
        .get()
    )
    if not service_doc.exists:
        raise HTTPException(status_code=404, detail="Service not found.")
    service_data = service_doc.to_dict()

    # Slot fetch karo by ID
    slot_doc = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .document(body.slot_id)
        .get()
    )
    if not slot_doc.exists:
        raise HTTPException(status_code=404, detail="Slot not found.")
    slot_data = slot_doc.to_dict()
    slot_data["id"] = body.slot_id

    # Booking initiate karo (existing reusable function)
    result = await _initiate_booking(
        client_id=client_id,
        client_data=client_data,
        customer_phone=session["customer_phone"],
        slot_info=slot_data,
        service_info={
            "name" : service_data.get("name", ""),
            "price": service_data.get("price", 0),
            "staff": slot_data.get("staff_name", ""),
        },
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=409,
            detail="This slot was just taken. Please choose another slot."
        )

    # Session update karo
    session_ref.update({
        "booking_id": result["booking_id"],
        "status"    : "awaiting_payment",
    })

    return {
        "booking_id"  : result["booking_id"],
        "payment_link": result["payment_link"],
    }


# ── Mark Session Completed (called internally by payments webhook) ───────────

def mark_session_completed(booking_id: str) -> None:
    """payments.py se call hota hai booking confirm hone ke baad."""
    db = get_db()
    sessions = (
        db.collection("booking_sessions")
        .where("booking_id", "==", booking_id)
        .limit(1)
        .get()
    )
    for s in sessions:
        s.reference.update({"status": "completed"})