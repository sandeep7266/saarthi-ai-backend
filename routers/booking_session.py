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
BUFFER_MINUTES = 10  # mandatory cleaning/sanitization buffer, matches routers/stylist_availability.py


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
        "pending_booking_id": session.get("booking_id") if session.get("status") == "awaiting_payment" else None,
    }


# ── Duration-Aware Slot Groups (multi-service booking) ─────────────────────────

@router.get("/{session_token}/slots-for-services")
async def get_slots_for_services(session_token: str, service_ids: str = Query(...)):
    """
    Customer ne 1+ services choose kar liye — total duration nikaal ke,
    fixed 30-min-grid slots ko CONSECUTIVE runs mein group karta hai (same
    staff, back-to-back) taaki total duration cover ho sake.

    Bhi return karta hai: har busy stylist ka live waiting_time_minutes
    (agar stylist registered hai clients/{id}/stylists mein), aur agar
    koi bhi valid consecutive-run nahi milta, ek suggested_next_slot.

    service_ids: comma-separated service_id list, e.g. "svc1,svc2"
    """
    db = get_db()
    session_ref = db.collection("booking_sessions").document(session_token)
    session_doc = session_ref.get()
    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    session   = session_doc.to_dict()
    client_id = session["client_id"]
    now       = datetime.now(timezone.utc)

    ids = [s.strip() for s in service_ids.split(",") if s.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="At least one service_id required.")

    services_ref = db.collection(Collections.CLIENTS).document(client_id).collection(Collections.SERVICES)
    total_duration = 0
    service_names  = []
    for sid in ids:
        doc = services_ref.document(sid).get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail=f"Service not found: {sid}")
        data = doc.to_dict()
        total_duration += int(data.get("duration_min", 30))
        service_names.append(data.get("name", ""))

    total_block_min = total_duration + BUFFER_MINUTES

    slot_end = now + timedelta(days=14)
    slot_docs = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.SLOTS)
        .where(filter=FieldFilter("status", "==", "available"))
        .where(filter=FieldFilter("slot_datetime", ">=", now))
        .where(filter=FieldFilter("slot_datetime", "<=", slot_end))
        .order_by("slot_datetime")
        .limit(500)
        .get()
    )

    by_staff: dict[str, list[dict]] = {}
    for d in slot_docs:
        data = d.to_dict()
        staff = data.get("staff_name", "") or "_no_staff_"
        by_staff.setdefault(staff, []).append({
            "slot_id"      : d.id,
            "slot_datetime": data.get("slot_datetime"),
            "duration_min" : data.get("duration_min", 30),
            "staff_name"   : data.get("staff_name", ""),
        })

    groups = []
    for staff, slots in by_staff.items():
        slots.sort(key=lambda s: s["slot_datetime"])
        i = 0
        while i < len(slots):
            run = [slots[i]]
            run_minutes = run[0]["duration_min"]
            j = i + 1
            while run_minutes < total_block_min and j < len(slots):
                prev_end = run[-1]["slot_datetime"] + timedelta(minutes=run[-1]["duration_min"])
                if slots[j]["slot_datetime"] == prev_end:
                    run.append(slots[j])
                    run_minutes += slots[j]["duration_min"]
                    j += 1
                else:
                    break
            if run_minutes >= total_block_min:
                groups.append({
                    "staff_name"    : staff if staff != "_no_staff_" else "",
                    "start_datetime": run[0]["slot_datetime"].isoformat(),
                    "end_datetime"  : (run[0]["slot_datetime"] + timedelta(minutes=total_block_min)).isoformat(),
                    "slot_ids"      : [s["slot_id"] for s in run],
                })
            i += 1  # slide by one grid-slot so every possible start is considered

    groups.sort(key=lambda g: g["start_datetime"])

    # ── Live stylist waiting-time (only for stylists who are formally registered) ──
    stylist_docs = db.collection(Collections.CLIENTS).document(client_id).collection(Collections.STYLISTS).get()
    stylist_status = []
    for d in stylist_docs:
        data = d.to_dict()
        entry = {"name": data.get("name", ""), "status": data.get("status", "available")}
        if data.get("status") == "busy" and data.get("busy_until"):
            entry["waiting_time_minutes"] = max(0, int((data["busy_until"] - now).total_seconds() // 60))
        else:
            entry["waiting_time_minutes"] = 0
        stylist_status.append(entry)

    suggested_next = groups[0] if groups else None

    return {
        "total_duration_min": total_duration,
        "buffer_min"        : BUFFER_MINUTES,
        "service_names"     : service_names,
        "slot_groups"       : groups[:20],
        "stylist_status"    : stylist_status,
        "suggested_next_slot": suggested_next,
    }


# ── Update Session With Selection (called by Web App before payment) ─────────

class UpdateSessionRequest(BaseModel):
    service_ids : list[str]
    slot_id     : str               # primary/first slot (backward-compat, single-service)
    slot_ids    : list[str] = []    # full consecutive run for multi-service bookings (optional)


@router.patch("/{session_token}/select")
@limiter.limit(LIMIT_NORMAL)
async def select_service_slot(request: Request, session_token: str, body: UpdateSessionRequest):
    """
    Customer ne service(s) + slot choose kar liya web app mein.
    Yeh slot ko temporarily lock karega aur booking record banayega
    (status: pending_payment), phir Razorpay link generate karega.

    Multi-service (cart-style) support: customer ek slot ke against
    multiple services select kar sakta hai (e.g. haircut + shave,
    ya cafe mein 2 items).
    """
    from routers.booking import _initiate_booking  # reuse existing logic

    if not body.service_ids:
        raise HTTPException(status_code=400, detail="At least one service must be selected.")

    db = get_db()
    session_ref = db.collection("booking_sessions").document(session_token)
    session_doc = session_ref.get()

    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found.")

    session = session_doc.to_dict()
    client_id = session["client_id"]

    client_doc = db.collection(Collections.CLIENTS).document(client_id).get()
    client_data = client_doc.to_dict()

    # Sab selected services fetch karo
    services_info = []
    services_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
    )
    for service_id in body.service_ids:
        service_doc = services_ref.document(service_id).get()
        if not service_doc.exists:
            raise HTTPException(status_code=404, detail=f"Service not found: {service_id}")
        service_data = service_doc.to_dict()
        services_info.append({
            "service_id"  : service_id,
            "name"        : service_data.get("name", ""),
            "price"       : service_data.get("price", 0),
            "duration_min": service_data.get("duration_min", 30),
        })

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
        services_info=services_info,
        extra_slot_ids=body.slot_ids,
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


# ── Cancel a pending (unpaid) booking on this session ─────────────────────────

@router.patch("/{session_token}/cancel")
@limiter.limit(LIMIT_NORMAL)
async def cancel_pending_booking(request: Request, session_token: str):
    """
    Customer picked a slot + clicked Pay, then abandoned Razorpay checkout and
    came back. Rather than making them wait for the 15-minute auto-expiry
    (utils/booking_expiry.py), this releases the slot immediately so they can
    pick a different one right away.
    """
    db = get_db()
    session_ref = db.collection("booking_sessions").document(session_token)
    session_doc = session_ref.get()

    if not session_doc.exists:
        raise HTTPException(status_code=404, detail="Session not found.")

    session = session_doc.to_dict()
    booking_id = session.get("booking_id")

    if not booking_id or session.get("status") != "awaiting_payment":
        return {"success": True, "message": "No pending booking to cancel."}

    client_id = session["client_id"]
    booking_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .document(booking_id)
    )
    booking_doc = booking_ref.get()

    if booking_doc.exists and booking_doc.to_dict().get("status") == "pending_payment":
        booking_data = booking_doc.to_dict()
        slot_ids = booking_data.get("slot_ids") or [booking_data.get("slot_id")]
        slot_ids = [sid for sid in slot_ids if sid]
        now = datetime.now(timezone.utc)

        batch = db.batch()
        batch.update(booking_ref, {
            "status"      : "cancelled",
            "cancelled_at": now,
            "cancelled_by": "customer",
        })
        for slot_id in slot_ids:
            slot_ref = (
                db.collection(Collections.CLIENTS)
                .document(client_id)
                .collection(Collections.SLOTS)
                .document(slot_id)
            )
            slot_doc = slot_ref.get()
            if slot_doc.exists and slot_doc.to_dict().get("status") == "pending_payment":
                batch.update(slot_ref, {
                    "status"     : "available",
                    "booking_id" : None,
                    "locked_at"  : None,
                    "updated_at" : now,
                })
        batch.commit()

    session_ref.update({"booking_id": None, "status": "in_progress"})

    return {"success": True, "message": "Booking cancelled. Slot released."}


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