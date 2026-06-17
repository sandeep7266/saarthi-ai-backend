"""
routers/cron_jobs.py
Daily subscription renewal checks, expiry lockdowns, and backup trigger.
Called nightly at 00:00 via crontab or Task Scheduler.
"""

import os
import logging
from datetime import datetime, timedelta, timezone

import httpx
import razorpay
from fastapi import APIRouter, Depends, HTTPException, Header

from database import get_db, Collections
from utils.backup_engine import run_firestore_backup

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/cron", tags=["Cron Jobs"])

CRON_SECRET         = os.getenv("CRON_SECRET", "CHANGE_ME_CRON_SECRET")
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
META_ACCESS_TOKEN   = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION    = os.getenv("META_API_VERSION", "v19.0")
APP_BASE_URL        = os.getenv("APP_BASE_URL", "https://saarthi-ai.in")

RENEWAL_ALERT_DAYS  = 3   # Send renewal alert when X days remain
GRACE_PERIOD_DAYS   = 3   # Keep active for X days after expiry before hard-lock


# ── Auth Guard ─────────────────────────────────────────────────────────────────

def _verify_cron_secret(x_cron_secret: str = Header(..., alias="X-Cron-Secret")):
    if x_cron_secret != CRON_SECRET:
        raise HTTPException(status_code=403, detail="Invalid cron secret.")


# ── Main Endpoint ──────────────────────────────────────────────────────────────

@router.post("/run-daily-sync", dependencies=[Depends(_verify_cron_secret)])
async def run_daily_sync():
    """
    Nightly sync engine. Performs:
      A. Renewal alerts (3 days before expiry)
      B. Grace period management + hard-lock for expired tenants
      C. Firestore backup to Firebase Storage
    """
    now     = datetime.now(timezone.utc)
    db      = get_db()
    results = {"alerts_sent": [], "locked": [], "warned": [], "backup": None, "errors": []}

    clients = db.collection(Collections.CLIENTS).get()

    for doc in clients:
        client_id   = doc.id
        client_data = doc.to_dict()
        status      = client_data.get("status", "inactive")
        business    = client_data.get("business_name", client_id)
        owner_phone = client_data.get("owner_phone", "")
        phone_id    = client_data.get("whatsapp_phone_id", "")
        sub_end     = client_data.get("subscription_end_date")
        grace_end   = client_data.get("grace_period_end")

        try:
            # Only process active or grace-period clients
            if status not in ("active", "grace"):
                continue

            if sub_end is None:
                continue

            # Normalize to timezone-aware datetime
            if hasattr(sub_end, "tzinfo") and sub_end.tzinfo is None:
                sub_end = sub_end.replace(tzinfo=timezone.utc)

            days_remaining = (sub_end - now).days

            # ── A. Renewal Alert (3 days before expiry) ────────────────────────
            if 0 < days_remaining <= RENEWAL_ALERT_DAYS and status == "active":
                invoice_url = _create_renewal_payment_link(client_id, client_data)
                _send_renewal_alert_whatsapp(phone_id, owner_phone, business, days_remaining, invoice_url)
                results["alerts_sent"].append({"client_id": client_id, "days_left": days_remaining})
                logger.info("Renewal alert sent: %s | %d days left", business, days_remaining)

            # ── B. Expiry & Grace Period Handling ─────────────────────────────
            elif days_remaining <= 0:
                if grace_end:
                    if hasattr(grace_end, "tzinfo") and grace_end.tzinfo is None:
                        grace_end = grace_end.replace(tzinfo=timezone.utc)

                    if now < grace_end:
                        # Within grace period — warn but keep active
                        if status != "grace":
                            doc.reference.update({
                                "status"    : "grace",
                                "updated_at": now,
                            })
                        grace_days_left = (grace_end - now).days
                        _send_grace_warning_whatsapp(phone_id, owner_phone, business, grace_days_left)
                        results["warned"].append({"client_id": client_id, "grace_days_left": grace_days_left})
                        logger.info("Grace warning sent: %s | %d grace days left", business, grace_days_left)

                    else:
                        # Past grace period — HARD LOCK
                        doc.reference.update({
                            "status"    : "expired",
                            "updated_at": now,
                        })
                        _send_hard_lock_notification(phone_id, owner_phone, business)
                        results["locked"].append({"client_id": client_id})
                        logger.warning("HARD LOCK applied: %s | %s", business, client_id)
                else:
                    # No grace_end set — lock immediately
                    doc.reference.update({
                        "status"    : "expired",
                        "updated_at": now,
                    })
                    results["locked"].append({"client_id": client_id})
                    logger.warning("HARD LOCK (no grace): %s | %s", business, client_id)

        except Exception as e:
            logger.error("Cron error for client %s: %s", client_id, e)
            results["errors"].append({"client_id": client_id, "error": str(e)})

    # ── C. Firestore Backup ────────────────────────────────────────────────────
    try:
        backup_path = run_firestore_backup()
        results["backup"] = {"status": "success", "path": backup_path}
        logger.info("Firestore backup complete: %s", backup_path)
    except Exception as e:
        logger.error("Backup failed: %s", e)
        results["backup"] = {"status": "failed", "error": str(e)}

    logger.info("Daily sync complete: %s", results)
    return {"status": "completed", "timestamp": now.isoformat(), "results": results}


# ── Razorpay Renewal Link ──────────────────────────────────────────────────────

def _create_renewal_payment_link(client_id: str, client_data: dict) -> str:
    """Generate a fresh Razorpay payment link for subscription renewal."""
    if not RAZORPAY_KEY_ID:
        return f"{APP_BASE_URL}/renew/{client_id}"

    plan          = client_data.get("plan", "basic")
    billing_cycle = client_data.get("billing_cycle", "monthly")
    owner_name    = client_data.get("owner_name", "")
    owner_email   = client_data.get("owner_email", "")
    owner_phone   = client_data.get("owner_phone", "")
    business_name = client_data.get("business_name", "")

    PLAN_PRICING = {
        "basic"  : {"monthly": 99900,  "yearly": 999900},
        "premium": {"monthly": 199900, "yearly": 1999900},
    }
    amount = PLAN_PRICING.get(plan, {}).get(billing_cycle, 99900)

    rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

    try:
        plink = rz_client.payment_link.create({
            "amount"      : amount,
            "currency"    : "INR",
            "description" : f"Saarthi-AI Renewal — {plan.title()} ({billing_cycle}) | {business_name}",
            "customer"    : {"name": owner_name, "email": owner_email, "contact": owner_phone},
            "notify"      : {"sms": True, "email": True, "whatsapp": True},
            "reminder_enable": True,
            "notes"       : {"client_id": client_id, "plan": plan, "billing_cycle": billing_cycle},
            "callback_url": f"{APP_BASE_URL}/renew/success",
        })
        return plink["short_url"]
    except Exception as e:
        logger.error("Renewal payment link failed for %s: %s", client_id, e)
        return f"{APP_BASE_URL}/renew/{client_id}"


# ── WhatsApp Notification Helpers ─────────────────────────────────────────────

def _send_whatsapp_text(phone_id: str, to: str, message: str) -> None:
    if not phone_id or not to or not META_ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "text",
        "text": {"body": message},
    }
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type" : "application/json",
    }
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("WhatsApp notify failed → %s: %s", to, e)


def _send_renewal_alert_whatsapp(phone_id: str, to: str, business: str, days: int, link: str):
    msg = (
        f"⚠️ *Saarthi-AI Renewal Alert*\n\n"
        f"Namaste! *{business}* ka Saarthi-AI subscription sirf *{days} din* mein expire hoga.\n\n"
        f"Uninterrupted service ke liye abhi renew karein:\n👉 {link}\n\n"
        f"Help ke liye reply karein. Dhanyavaad! 🙏"
    )
    _send_whatsapp_text(phone_id, to, msg)


def _send_grace_warning_whatsapp(phone_id: str, to: str, business: str, grace_days: int):
    msg = (
        f"🔴 *URGENT: Saarthi-AI Grace Period Active*\n\n"
        f"*{business}* ka subscription expire ho gaya hai.\n"
        f"Aapke paas sirf *{grace_days} din* ka grace period bacha hai.\n\n"
        f"Agar abhi renew nahi kiya, to aapka WhatsApp bot aur vendor app *band* ho jayega!\n\n"
        f"Turant renew karein → {APP_BASE_URL}/renew\n\n"
        f"🚨 Yeh automated alert hai. Please ignore mat karein!"
    )
    _send_whatsapp_text(phone_id, to, msg)


def _send_hard_lock_notification(phone_id: str, to: str, business: str):
    msg = (
        f"🔒 *Saarthi-AI Service Suspended*\n\n"
        f"*{business}* ka Saarthi-AI subscription expire ho gaya aur grace period bhi khatam ho gaya.\n\n"
        f"Aapka:\n"
        f"❌ WhatsApp AI bot — BAND\n"
        f"❌ Vendor App — LOCKED\n\n"
        f"Turant reactivate karein:\n👉 {APP_BASE_URL}/renew\n\n"
        f"Support: support@saarthi-ai.in"
    )
    _send_whatsapp_text(phone_id, to, msg)
