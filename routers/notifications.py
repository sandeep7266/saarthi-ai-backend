"""
routers/notifications.py
FCM push notification endpoints.
- Register device tokens
- Send booking alerts to admins
- Broadcast renewal reminders
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db, Collections
from routers.auth import require_active_tenant, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")  # Firebase Cloud Messaging server key


# ── Models ─────────────────────────────────────────────────────────────────────

class RegisterTokenRequest(BaseModel):
    client_id: str
    user_id  : str
    fcm_token: str
    platform : str = "android"   # "android" | "ios"


class SendNotificationRequest(BaseModel):
    client_id: str
    title    : str
    body     : str
    data     : dict = {}
    target   : str  = "all"      # "all" | "admin" | specific user_id


# ── Token registration ─────────────────────────────────────────────────────────

@router.post("/register-token", status_code=200)
async def register_fcm_token(
    body        : RegisterTokenRequest,
    current_user: dict = Depends(require_active_tenant),
):
    """
    Flutter app calls this on login / token refresh.
    Stores FCM token against user_id in Firestore.
    """
    if current_user["client_id"] != body.client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    now = datetime.now(timezone.utc)

    # Upsert token — one user may have multiple devices
    token_ref = (
        db.collection(Collections.CLIENTS)
        .document(body.client_id)
        .collection("fcm_tokens")
        .document(body.user_id)
    )

    token_ref.set({
        "user_id"    : body.user_id,
        "fcm_token"  : body.fcm_token,
        "platform"   : body.platform,
        "updated_at" : now,
    }, merge=True)

    logger.info(
        "FCM token registered: user=%s client=%s platform=%s",
        body.user_id, body.client_id, body.platform
    )
    return {"message": "Token registered."}


# ── Send push notification ─────────────────────────────────────────────────────

@router.post("/send")
async def send_notification(
    body : SendNotificationRequest,
    admin: dict = Depends(require_admin),
):
    """
    Admin-triggered manual push notification.
    Used for testing or broadcasting important messages.
    """
    if admin["client_id"] != body.client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    tokens = _get_target_tokens(body.client_id, body.target)
    if not tokens:
        return {"message": "No registered devices found.", "sent": 0}

    sent = await _send_fcm_multicast(tokens, body.title, body.body, body.data)
    return {"message": f"Notification sent to {sent} device(s).", "sent": sent}


# ── Internal helpers (called by payments.py + cron_jobs.py) ───────────────────

def _get_target_tokens(client_id: str, target: str = "admin") -> list[str]:
    """Fetch FCM tokens for a tenant. target: 'all' | 'admin' | user_id."""
    db   = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("fcm_tokens")
        .get()
    )
    tokens = []
    for doc in docs:
        data = doc.to_dict()
        if target == "all":
            tokens.append(data.get("fcm_token", ""))
        elif target == "admin":
            # Check if this user is admin
            user_doc = db.collection(Collections.USERS).document(doc.id).get()
            if user_doc.exists and user_doc.to_dict().get("role") == "admin":
                tokens.append(data.get("fcm_token", ""))
        else:
            if doc.id == target:
                tokens.append(data.get("fcm_token", ""))

    return [t for t in tokens if t]


async def send_booking_notification_to_admin(
    client_id  : str,
    booking_id : str,
    service    : str,
    customer   : str,
    slot_time  : str,
) -> None:
    """
    Called by payments.py when a booking is confirmed.
    Sends push to admin devices immediately.
    """
    tokens = _get_target_tokens(client_id, "admin")
    if not tokens:
        return

    await _send_fcm_multicast(
        tokens,
        title= "💇 New Booking Confirmed!",
        body = f"{service} for {customer} at {slot_time}",
        data = {
            "type"      : "new_booking",
            "booking_id": booking_id,
            "client_id" : client_id,
        },
    )


async def send_renewal_push_to_admin(
    client_id    : str,
    business_name: str,
    days_left    : int,
    renewal_url  : str,
) -> None:
    """Called by cron_jobs.py for renewal reminders."""
    tokens = _get_target_tokens(client_id, "admin")
    if not tokens:
        return

    await _send_fcm_multicast(
        tokens,
        title= "⚠️ Subscription Expiring Soon",
        body = f"{business_name} subscription expires in {days_left} days. Tap to renew.",
        data = {
            "type"       : "renewal_reminder",
            "client_id"  : client_id,
            "renewal_url": renewal_url,
            "days_left"  : str(days_left),
        },
    )


async def _send_fcm_multicast(
    tokens: list[str],
    title : str,
    body  : str,
    data  : dict,
) -> int:
    """
    Send FCM multicast message via HTTP v1 API.
    Returns count of successfully sent messages.
    """
    if not FCM_SERVER_KEY:
        logger.warning("FCM_SERVER_KEY not set — skipping push notification.")
        return 0

    import httpx

    # Batch into chunks of 500 (FCM limit)
    sent = 0
    for i in range(0, len(tokens), 500):
        chunk = tokens[i:i + 500]
        payload = {
            "registration_ids": chunk,
            "notification"    : {"title": title, "body": body},
            "data"            : {k: str(v) for k, v in data.items()},
            "android"         : {
                "priority"    : "high",
                "notification": {
                    "channel_id": "saarthi_bookings"
                        if data.get("type") == "new_booking"
                        else "saarthi_renewal",
                    "sound"     : "default",
                },
            },
            "apns": {
                "headers": {"apns-priority": "10"},
                "payload": {"aps": {"sound": "default", "badge": 1}},
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://fcm.googleapis.com/fcm/send",
                    json   = payload,
                    headers= {
                        "Authorization": f"key={FCM_SERVER_KEY}",
                        "Content-Type" : "application/json",
                    },
                )
                resp_data = resp.json()
                sent += resp_data.get("success", 0)
                logger.info(
                    "FCM batch sent: success=%d failure=%d",
                    resp_data.get("success", 0),
                    resp_data.get("failure", 0),
                )
        except Exception as e:
            logger.error("FCM send error: %s", e)

    return sent
