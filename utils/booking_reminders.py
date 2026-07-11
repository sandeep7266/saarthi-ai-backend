"""
utils/booking_reminders.py
Sends WhatsApp reminders to customers for upcoming confirmed bookings.
Runs every 15 minutes via APScheduler — finds bookings whose slot is between
REMINDER_WINDOW_HOURS from now and REMINDER_WINDOW_HOURS - 0.25 (one job
interval) from now, so each booking gets exactly one reminder as it enters
the window, without needing a separate "already sent" scan every time.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from google.cloud.firestore import FieldFilter

from database import get_db, Collections

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION  = os.getenv("META_API_VERSION", "v19.0")

REMINDER_WINDOW_HOURS = 3  # send reminder ~3 hours before the appointment
IST = ZoneInfo("Asia/Kolkata")


def send_upcoming_booking_reminders() -> dict:
    """
    Scans ALL tenant booking sub-collections for 'confirmed' bookings whose
    slot_datetime falls within the reminder window, and haven't had a
    reminder sent yet. Sends a WhatsApp text and marks reminder_sent=True.

    Called by APScheduler every 15 minutes. Returns summary dict for logging.
    """
    db  = get_db()
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=REMINDER_WINDOW_HOURS)

    sent   = 0
    errors = 0

    clients = (
        db.collection(Collections.CLIENTS)
        .where(filter=FieldFilter("status", "in", ["active", "grace"]))
        .get()
    )

    for client_doc in clients:
        client_id   = client_doc.id
        client_data = client_doc.to_dict()
        phone_number_id = client_data.get("whatsapp_phone_id", "")
        business_name   = client_data.get("business_name", "")

        if not phone_number_id:
            continue  # can't send reminders without a connected WhatsApp number

        try:
            due_bookings = (
                db.collection(Collections.CLIENTS)
                .document(client_id)
                .collection(Collections.BOOKINGS)
                .where(filter=FieldFilter("status", "==", "confirmed"))
                .where(filter=FieldFilter("slot_datetime", "<=", window_end))
                .where(filter=FieldFilter("slot_datetime", ">=", now))
                .where(filter=FieldFilter("reminder_sent", "==", False))
                .get()
            )

            for booking_doc in due_bookings:
                booking_data   = booking_doc.to_dict()
                customer_phone = booking_data.get("customer_phone", "")
                if not customer_phone:
                    continue

                slot_dt    = booking_data.get("slot_datetime")
                time_label = slot_dt.astimezone(IST).strftime("%d %b, %I:%M %p") if hasattr(slot_dt, "strftime") else str(slot_dt)
                service_name = booking_data.get("service_name", "your appointment")
                staff_name   = booking_data.get("staff_name", "")

                message = (
                    f"⏰ *Reminder!* Aapki appointment aane wali hai:\n\n"
                    f"📍 *{business_name}*\n"
                    f"💇 {service_name}"
                    + (f" with {staff_name}" if staff_name else "") +
                    f"\n🗓 {time_label}\n\n"
                    f"Miss na karein! Kisi wajah se aana na ho toh reschedule/cancel karne "
                    f"ke liye humein message karein. 🙏"
                )

                _send_whatsapp_text(phone_number_id, customer_phone, message)
                booking_doc.reference.update({
                    "reminder_sent"   : True,
                    "reminder_sent_at": now,
                })
                sent += 1

        except Exception as e:
            logger.error("Reminder scan failed for client %s: %s", client_id, e)
            errors += 1

    logger.info("Booking reminders: sent=%d errors=%d", sent, errors)
    return {"sent": sent, "errors": errors}


def _send_whatsapp_text(phone_number_id: str, to: str, message: str) -> None:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "text",
        "text": {"body": message},
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Reminder WhatsApp send failed for %s: %s", to, e)