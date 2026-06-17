"""
routers/onboard.py
Paid B2B vendor onboarding — creates Firestore tenant doc + Razorpay payment link.
"""

import os
import hmac
import hashlib
import logging
from datetime import datetime, timezone

import razorpay
from fastapi import APIRouter, HTTPException, Request
from utils.rate_limiter import limiter, LIMIT_STRICT
from pydantic import BaseModel, EmailStr

from database import get_db, Collections

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/onboard", tags=["Onboarding"])

RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
APP_BASE_URL        = os.getenv("APP_BASE_URL", "https://saarthi-ai.in")

PLAN_PRICING = {
    "basic"   : {"monthly": 99900,  "yearly": 999900},   # paise
    "premium" : {"monthly": 199900, "yearly": 1999900},
}
SETUP_FEE_PAISE = 49900  # ₹499 one-time setup fee


# ── Pydantic Models ────────────────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    business_name  : str
    owner_name     : str
    owner_phone    : str   # E.164 format: +919876543210
    owner_email    : EmailStr
    business_type  : str   # "salon" | "cafe"
    city           : str
    address        : str
    plan           : str   # "basic" | "premium"
    billing_cycle  : str   # "monthly" | "yearly"
    whatsapp_phone_id: str  # Meta Phone Number ID for this business

class OnboardResponse(BaseModel):
    client_id     : str
    payment_link  : str
    message       : str


# ── Route ──────────────────────────────────────────────────────────────────────

@router.post("/create-pending-vendor", response_model=OnboardResponse, status_code=201)
@limiter.limit(LIMIT_STRICT)
async def create_pending_vendor(request: Request, body: OnboardRequest):
    """
    Step 1 of B2B onboarding:
      1. Validate input & check for duplicate phone
      2. Create Firestore client doc with status='inactive'
      3. Generate Razorpay Payment Link for the setup fee
      4. Return payment link to redirect the prospect
    """

    if body.business_type not in ("salon", "cafe"):
        raise HTTPException(status_code=422, detail="business_type must be 'salon' or 'cafe'.")
    if body.plan not in PLAN_PRICING:
        raise HTTPException(status_code=422, detail="plan must be 'basic' or 'premium'.")
    if body.billing_cycle not in ("monthly", "yearly"):
        raise HTTPException(status_code=422, detail="billing_cycle must be 'monthly' or 'yearly'.")

    db = get_db()

    # ── Duplicate phone guard ──────────────────────────────────────────────────
    existing = (
        db.collection(Collections.CLIENTS)
        .where("owner_phone", "==", body.owner_phone)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(status_code=409, detail="A vendor with this phone number is already registered.")

    # ── Create Firestore tenant document ──────────────────────────────────────
    now = datetime.now(timezone.utc)
    client_doc = {
        "business_name"        : body.business_name,
        "owner_name"           : body.owner_name,
        "owner_phone"          : body.owner_phone,
        "owner_email"          : body.owner_email,
        "business_type"        : body.business_type,
        "city"                 : body.city,
        "address"              : body.address,
        "plan"                 : body.plan,
        "billing_cycle"        : body.billing_cycle,
        "whatsapp_phone_id"    : body.whatsapp_phone_id,
        "status"               : "inactive",
        "razorpay_sub_id"      : None,
        "razorpay_payment_link_id": None,
        "subscription_end_date": None,
        "grace_period_end"     : None,
        "gemini_bot_profile"   : {
            "persona_name" : "Priya",
            "language"     : "hi-en",   # Hinglish default
            "welcome_msg"  : f"Namaste! Main Priya hoon, {body.business_name} ki virtual receptionist. Aapki kya madad kar sakti hoon? 😊",
        },
        "created_at"           : now,
        "updated_at"           : now,
    }

    _, client_ref = db.collection(Collections.CLIENTS).add(client_doc)
    client_id = client_ref.id
    logger.info("Pending vendor created: %s (%s)", body.business_name, client_id)

    # ── Razorpay Payment Link ─────────────────────────────────────────────────
    # Agar Razorpay keys nahi hain (testing mode) to Firestore doc save karo
    # aur dummy payment link return karo
    if not RAZORPAY_KEY_ID or RAZORPAY_KEY_ID == "dummy":
        logger.warning("Razorpay not configured — returning test mode response.")
        return OnboardResponse(
            client_id=client_id,
            payment_link=f"{APP_BASE_URL}/pay/{client_id}",
            message=(
                f"[TEST MODE] Vendor registered: {body.business_name}. "
                "Add real Razorpay keys to generate actual payment link."
            ),
        )

    rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

    plan_amount  = PLAN_PRICING[body.plan][body.billing_cycle]
    total_amount = SETUP_FEE_PAISE + plan_amount

    payment_link_payload = {
        "amount"      : total_amount,
        "currency"    : "INR",
        "accept_partial": False,
        "description" : f"Saarthi-AI Setup + First {body.billing_cycle.title()} — {body.plan.title()} Plan ({body.business_name})",
        "customer"    : {
            "name"   : body.owner_name,
            "email"  : body.owner_email,
            "contact": body.owner_phone,
        },
        "notify"      : {"sms": True, "email": True, "whatsapp": True},
        "reminder_enable": True,
        "notes"       : {
            "client_id"    : client_id,
            "plan"         : body.plan,
            "billing_cycle": body.billing_cycle,
        },
        "callback_url"    : f"{APP_BASE_URL}/onboard/success",
        "callback_method" : "get",
    }

    try:
        plink = rz_client.payment_link.create(payment_link_payload)
    except Exception as e:
        logger.error("Razorpay payment link creation failed: %s", e)
        # Document rakhna hai — sirf error log karo
        # Admin manually payment le sakta hai ya baad mein link generate kar sakta hai
        client_ref.update({
            "razorpay_error": str(e),
            "updated_at"    : __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        })
        return OnboardResponse(
            client_id=client_id,
            payment_link="",
            message=(
                f"Vendor {body.business_name} registered successfully. "
                f"Payment link generation failed: {str(e)}. "
                "Contact admin for manual payment processing."
            ),
        )

    # Persist Razorpay link ID back to the doc
    client_ref.update({
        "razorpay_payment_link_id": plink["id"],
        "updated_at"              : datetime.now(timezone.utc),
    })

    logger.info("Payment link created for client %s: %s", client_id, plink["short_url"])

    return OnboardResponse(
        client_id=client_id,
        payment_link=plink["short_url"],
        message=(
            f"Vendor registration initiated for {body.business_name}. "
            "Complete payment to activate your Saarthi-AI subscription."
        ),
    )