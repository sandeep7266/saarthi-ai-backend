"""
utils/booking_expiry.py
Auto-expire pending bookings after 15 minutes.
Releases locked slots back to 'available'.
Runs every 5 minutes via APScheduler.
"""

import logging
from datetime import datetime, timedelta, timezone

from database import get_db, Collections

logger = logging.getLogger(__name__)

PENDING_TIMEOUT_MINUTES = 15


def expire_stale_pending_bookings() -> dict:
    """
    Scans ALL tenant booking sub-collections for bookings stuck in
    'pending_payment' for more than PENDING_TIMEOUT_MINUTES.
    Releases the slot and marks booking as 'expired'.

    Called by APScheduler every 5 minutes.
    Returns summary dict for logging.
    """
    db         = get_db()
    now        = datetime.now(timezone.utc)
    cutoff     = now - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    expired    = 0
    errors     = 0

    # Get all active client IDs
    clients = (
        db.collection(Collections.CLIENTS)
        .where("status", "in", ["active", "grace"])
        .get()
    )

    for client_doc in clients:
        client_id = client_doc.id

        try:
            # Find stale pending bookings for this tenant
            stale_bookings = (
                db.collection(Collections.CLIENTS)
                .document(client_id)
                .collection(Collections.BOOKINGS)
                .where("status", "==", "pending_payment")
                .where("created_at", "<=", cutoff)
                .get()
            )

            for booking_doc in stale_bookings:
                booking_id   = booking_doc.id
                booking_data = booking_doc.to_dict()
                slot_id      = booking_data.get("slot_id")

                try:
                    # Atomic batch: expire booking + release slot
                    batch = db.batch()

                    booking_ref = (
                        db.collection(Collections.CLIENTS)
                        .document(client_id)
                        .collection(Collections.BOOKINGS)
                        .document(booking_id)
                    )
                    batch.update(booking_ref, {
                        "status"    : "expired",
                        "expired_at": now,
                        "updated_at": now,
                    })

                    if slot_id:
                        slot_ref = (
                            db.collection(Collections.CLIENTS)
                            .document(client_id)
                            .collection(Collections.SLOTS)
                            .document(slot_id)
                        )
                        # Only release if still in pending_payment state
                        slot_doc = slot_ref.get()
                        if slot_doc.exists and slot_doc.to_dict().get("status") == "pending_payment":
                            batch.update(slot_ref, {
                                "status"     : "available",
                                "booking_id" : None,
                                "locked_at"  : None,
                                "updated_at" : now,
                            })

                    batch.commit()
                    expired += 1
                    logger.info(
                        "Expired booking %s | client=%s | slot=%s",
                        booking_id, client_id, slot_id
                    )

                except Exception as e:
                    errors += 1
                    logger.error(
                        "Failed to expire booking %s (client=%s): %s",
                        booking_id, client_id, e
                    )

        except Exception as e:
            errors += 1
            logger.error("Failed to scan client %s for stale bookings: %s", client_id, e)

    result = {
        "expired_count": expired,
        "error_count"  : errors,
        "ran_at"       : now.isoformat(),
    }
    if expired > 0:
        logger.info("Booking expiry job: expired=%d errors=%d", expired, errors)

    return result
