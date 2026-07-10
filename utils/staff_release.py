"""
utils/staff_release.py
Real-time stylist status lifecycle, run by three separate APScheduler jobs:

1. activate_bookings_starting_now()  — every 1 min. Confirmed bookings whose
   appointment time has arrived flip to 'in_progress', and the matching
   stylist (looked up by name against clients/{id}/stylists) is marked busy.

2. send_pre_end_warnings()           — every 1 min. ~5 minutes before an
   in-progress booking's estimated_end_time, sends the stylist a WhatsApp
   "Completed?" button prompt (routers/booking.py handles the reply webhook).

3. check_and_release_staff()         — every 1 min. Hybrid auto-release: if
   the stylist hasn't replied by the time estimated_end_time (+buffer) has
   passed, they're released automatically regardless.

All three are safe to run frequently and idempotently — each only acts on
bookings/stylists in the specific state it's looking for, and immediately
flips that state so repeat runs are no-ops.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from google.cloud.firestore import FieldFilter

from database import get_db, Collections

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION  = os.getenv("META_API_VERSION", "v19.0")

BUFFER_MINUTES     = 10
PRE_END_WARNING_MIN = 5


def activate_bookings_starting_now() -> dict:
    db  = get_db()
    now = datetime.now(timezone.utc)
    activated, errors = 0, 0

    clients = db.collection(Collections.CLIENTS).where(
        filter=FieldFilter("status", "in", ["active", "grace"])
    ).get()

    for client_doc in clients:
        client_id = client_doc.id
        try:
            due = (
                db.collection(Collections.CLIENTS).document(client_id)
                .collection(Collections.BOOKINGS)
                .where(filter=FieldFilter("status", "==", "confirmed"))
                .where(filter=FieldFilter("slot_datetime", "<=", now))
                .get()
            )
            for booking_doc in due:
                booking_data = booking_doc.to_dict()
                duration_min = booking_data.get("duration_min", 30)
                busy_until   = now + timedelta(minutes=duration_min + BUFFER_MINUTES)

                booking_doc.reference.update({
                    "status"           : "in_progress",
                    "estimated_end_time": busy_until,
                    "pre_end_warning_sent": False,
                })

                stylist_ref = _find_stylist_by_name(db, client_id, booking_data.get("staff_name", ""))
                if stylist_ref:
                    stylist_ref.update({
                        "status"             : "busy",
                        "current_booking_id" : booking_doc.id,
                        "busy_until"         : busy_until,
                    })
                activated += 1
        except Exception as e:
            logger.error("activate_bookings_starting_now failed for client %s: %s", client_id, e)
            errors += 1

    logger.info("Bookings activated: %d (errors=%d)", activated, errors)
    return {"activated": activated, "errors": errors}


def send_pre_end_warnings() -> dict:
    db  = get_db()
    now = datetime.now(timezone.utc)
    warn_by = now + timedelta(minutes=PRE_END_WARNING_MIN)
    sent, errors = 0, 0

    clients = db.collection(Collections.CLIENTS).where(
        filter=FieldFilter("status", "in", ["active", "grace"])
    ).get()

    for client_doc in clients:
        client_id       = client_doc.id
        phone_number_id = client_doc.to_dict().get("whatsapp_phone_id", "")
        if not phone_number_id:
            continue
        try:
            due = (
                db.collection(Collections.CLIENTS).document(client_id)
                .collection(Collections.BOOKINGS)
                .where(filter=FieldFilter("status", "==", "in_progress"))
                .where(filter=FieldFilter("estimated_end_time", "<=", warn_by))
                .where(filter=FieldFilter("pre_end_warning_sent", "==", False))
                .get()
            )
            for booking_doc in due:
                booking_data = booking_doc.to_dict()
                stylist_ref = _find_stylist_by_name(db, client_id, booking_data.get("staff_name", ""))
                if not stylist_ref:
                    booking_doc.reference.update({"pre_end_warning_sent": True})  # avoid re-checking forever
                    continue
                stylist_doc = stylist_ref.get()
                stylist_phone = stylist_doc.to_dict().get("phone", "") if stylist_doc.exists else ""
                if not stylist_phone:
                    booking_doc.reference.update({"pre_end_warning_sent": True})
                    continue

                _send_stylist_buttons(
                    phone_number_id, stylist_phone,
                    f"⏰ {booking_data.get('service_name','Service')} khatam hone wali hai "
                    f"(~{PRE_END_WARNING_MIN} min). Complete ho gaya?",
                    booking_doc.id,
                )
                booking_doc.reference.update({"pre_end_warning_sent": True})
                sent += 1
        except Exception as e:
            logger.error("send_pre_end_warnings failed for client %s: %s", client_id, e)
            errors += 1

    logger.info("Pre-end warnings sent: %d (errors=%d)", sent, errors)
    return {"sent": sent, "errors": errors}


def check_and_release_staff() -> dict:
    """Hybrid auto-release fallback — releases anyone the staff-reply webhook missed."""
    db  = get_db()
    now = datetime.now(timezone.utc)
    released, errors = 0, 0

    clients = db.collection(Collections.CLIENTS).where(
        filter=FieldFilter("status", "in", ["active", "grace"])
    ).get()

    for client_doc in clients:
        client_id = client_doc.id
        try:
            due = (
                db.collection(Collections.CLIENTS).document(client_id)
                .collection(Collections.BOOKINGS)
                .where(filter=FieldFilter("status", "==", "in_progress"))
                .where(filter=FieldFilter("estimated_end_time", "<=", now))
                .get()
            )
            for booking_doc in due:
                booking_data = booking_doc.to_dict()
                booking_doc.reference.update({"status": "service_completed", "completed_at": now})

                stylist_ref = _find_stylist_by_name(db, client_id, booking_data.get("staff_name", ""))
                if stylist_ref:
                    stylist_ref.update({
                        "status"             : "available",
                        "current_booking_id" : None,
                        "busy_until"         : None,
                    })
                released += 1
        except Exception as e:
            logger.error("check_and_release_staff failed for client %s: %s", client_id, e)
            errors += 1

    logger.info("Staff auto-released: %d (errors=%d)", released, errors)
    return {"released": released, "errors": errors}


# ── Helpers ──────────────────────────────────────────────────────────────────────

def _find_stylist_by_name(db, client_id: str, staff_name: str):
    """Bridges the slot-grid's plain staff_name string to a real stylist doc, if registered."""
    if not staff_name:
        return None
    docs = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.STYLISTS)
        .where(filter=FieldFilter("name", "==", staff_name))
        .limit(1)
        .get()
    )
    for doc in docs:
        return doc.reference
    return None


def _send_stylist_buttons(phone_number_id: str, to: str, body_text: str, booking_id: str) -> None:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"stylist_completed:{booking_id}", "title": "✅ Completed"}},
                    {"type": "reply", "reply": {"id": f"stylist_need_more_time:{booking_id}", "title": "⏳ Need 10 more min"}},
                ]
            },
        },
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Stylist button send failed for %s: %s", to, e)