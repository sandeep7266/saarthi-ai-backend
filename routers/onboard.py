"""
routers/onboard.py
Paid B2B vendor onboarding — creates Firestore tenant doc + Razorpay payment link.

Supports five business verticals: salon, parlour, clinic, cafe, restaurant.
Each vertical gets a tailored Gemini bot persona and default service categories.
"""

import os
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from enum import Enum

import razorpay
from fastapi import APIRouter, HTTPException, Request
from utils.rate_limiter import limiter, LIMIT_STRICT
from pydantic import BaseModel, EmailStr, Field

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


class BusinessType(str, Enum):
    """Supported business verticals — shown as a dropdown in Swagger UI."""
    salon      = "salon"
    parlour    = "parlour"
    clinic     = "clinic"
    cafe       = "cafe"
    restaurant = "restaurant"


class BillingCycle(str, Enum):
    monthly = "monthly"
    yearly  = "yearly"


class PlanTier(str, Enum):
    basic   = "basic"
    premium = "premium"


# Default persona language/tone tweaks per vertical — used when configuring
# the Gemini/Groq bot profile after payment activation (see payments.py).
BUSINESS_TYPE_DEFAULTS = {
    BusinessType.salon: {
        "service_noun": "appointment",
        "default_categories": ["hair", "spa", "nails"],
    },
    BusinessType.parlour: {
        "service_noun": "appointment",
        "default_categories": ["hair", "skin", "makeup"],
    },
    BusinessType.clinic: {
        "service_noun": "consultation",
        "default_categories": ["consultation", "checkup", "therapy"],
    },
    BusinessType.cafe: {
        "service_noun": "order",
        "default_categories": ["beverage", "food", "dessert"],
    },
    BusinessType.restaurant: {
        "service_noun": "reservation",
        "default_categories": ["starter", "main_course", "dessert", "beverage"],
    },
}


# ── Pydantic Models ────────────────────────────────────────────────────────────

class OnboardRequest(BaseModel):
    business_name  : str = Field(..., examples=["Sharma Hair Salon"])
    owner_name     : str = Field(..., examples=["Ramesh Sharma"])
    owner_phone    : str = Field(..., description="E.164 format with country code", examples=["+919876543210"])
    owner_email    : EmailStr = Field(..., examples=["ramesh@example.com"])
    business_type  : BusinessType = Field(..., description="One of: salon, parlour, clinic, cafe, restaurant")
    city           : str = Field(..., examples=["Ranchi"])
    address        : str = Field(..., examples=["Shop 5, Main Market, Ranchi"])
    plan           : PlanTier = Field(..., description="basic (₹999/mo) or premium (₹1999/mo)")
    billing_cycle  : BillingCycle
    whatsapp_phone_id: str = Field(..., description="Meta WhatsApp Cloud API Phone Number ID for this business")

class OnboardResponse(BaseModel):
    client_id     : str
    payment_link  : str
    message       : str


# ── Route ──────────────────────────────────────────────────────────────────────

@router.post(
    "/create-pending-vendor",
    response_model=OnboardResponse,
    status_code=201,
    summary="Register a new business (salon, parlour, clinic, cafe, or restaurant)",
    description=(
        "Step 1 of B2B onboarding. Creates an inactive Firestore tenant document "
        "and a Razorpay payment link for the setup fee + first billing cycle. "
        "The tenant is activated automatically once payment is confirmed via the "
        "Razorpay webhook (see /api/v1/payments/razorpay-webhook)."
    ),
)
@limiter.limit(LIMIT_STRICT)
async def create_pending_vendor(request: Request, body: OnboardRequest):
    """
    HTTP entry point (rate-limited). Delegates to the shared core function so
    that internal callers (e.g. the WhatsApp Master Onboarding Bot) can reuse
    the exact same logic without going through slowapi's Request dependency.
    """
    return await _create_pending_vendor_core(body)


async def _create_pending_vendor_core(body: OnboardRequest) -> OnboardResponse:
    """
    Step 1 of B2B onboarding (shared core, no rate-limiting/Request dependency):
      1. Validate input & check for duplicate phone
      2. Create Firestore client doc with status='inactive'
      3. Generate Razorpay Payment Link for the setup fee
      4. Return payment link to redirect the prospect

    Called by:
      - POST /api/v1/onboard/create-pending-vendor (rate-limited HTTP route)
      - routers/master_onboarding.py (WhatsApp-driven onboarding, internal call)
    """

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

    # ── Vertical-specific bot persona defaults ─────────────────────────────────
    defaults = BUSINESS_TYPE_DEFAULTS.get(body.business_type, {})
    service_noun = defaults.get("service_noun", "appointment")

    # ── Create Firestore tenant document ──────────────────────────────────────
    now = datetime.now(timezone.utc)
    client_doc = {
        "business_name"        : body.business_name,
        "owner_name"           : body.owner_name,
        "owner_phone"          : body.owner_phone,
        "owner_email"          : body.owner_email,
        "business_type"        : body.business_type.value,
        "city"                 : body.city,
        "address"              : body.address,
        "plan"                 : body.plan.value,
        "billing_cycle"        : body.billing_cycle.value,
        "whatsapp_phone_id"    : body.whatsapp_phone_id,
        "whatsapp_business_number": "",  # E.164 dialable number for QR (set later via /connect-whatsapp)
        "status"               : "inactive",
        "razorpay_sub_id"      : None,
        "razorpay_payment_link_id": None,
        "subscription_end_date": None,
        "grace_period_end"     : None,
        "default_categories"   : defaults.get("default_categories", []),
        "gemini_bot_profile"   : {
            "persona_name" : "Priya",
            "language"     : "hi-en",   # Hinglish default
            "business_type": body.business_type.value,
            "welcome_msg"  : (
                f"Namaste! Main Priya hoon, {body.business_name} ki virtual receptionist. "
                f"{service_noun.title()} ke liye madad kar sakti hoon. 😊"
            ),
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

    plan_amount  = PLAN_PRICING[body.plan.value][body.billing_cycle.value]
    total_amount = SETUP_FEE_PAISE + plan_amount

    payment_link_payload = {
        "amount"      : total_amount,
        "currency"    : "INR",
        "accept_partial": False,
        "description" : f"Saarthi-AI Setup + First {body.billing_cycle.value.title()} — {body.plan.value.title()} Plan ({body.business_name})",
        "customer"    : {
            "name"   : body.owner_name,
            "email"  : body.owner_email,
            "contact": body.owner_phone,
        },
        "notify"      : {"sms": True, "email": True, "whatsapp": True},
        "reminder_enable": True,
        "notes"       : {
            "client_id"    : client_id,
            "plan"         : body.plan.value,
            "billing_cycle": body.billing_cycle.value,
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


# ── Connect Client's Own WhatsApp Business Number ──────────────────────────────

class ConnectWhatsAppRequest(BaseModel):
    whatsapp_phone_id: str = Field(..., description="Meta WhatsApp Cloud API Phone Number ID (used for sending/receiving via API)")
    whatsapp_business_number: str = Field(..., description="Actual E.164 dialable number linked to that Phone Number ID, e.g. +919876543210 (used for the customer-facing QR code)")


@router.patch(
    "/{client_id}/connect-whatsapp",
    summary="Connect the client's own Meta WhatsApp Business number",
    description=(
        "Called once a client has registered their own number with Meta "
        "(post-onboarding). Updates the tenant's routing ID (whatsapp_phone_id) "
        "and the dialable number used for their QR code, then regenerates the "
        "QR code to point at their own number instead of the fallback owner_phone."
    ),
)
async def connect_whatsapp_number(client_id: str, body: ConnectWhatsAppRequest):
    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    client_doc = client_ref.get()

    if not client_doc.exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    client_data = client_doc.to_dict()

    client_ref.update({
        "whatsapp_phone_id"        : body.whatsapp_phone_id,
        "whatsapp_business_number" : body.whatsapp_business_number,
        "updated_at"               : datetime.now(timezone.utc),
    })

    # ── Regenerate QR so it now points at the client's own number ──────────────
    from utils.qr_generator import generate_client_qr # type: ignore

    qr_url = ""
    try:
        qr_url = generate_client_qr(
            client_id=client_id,
            business_name=client_data.get("business_name", ""),
            whatsapp_number=body.whatsapp_business_number,
        )
        if qr_url:
            client_ref.update({"qr_code_url": qr_url})
    except Exception as e:
        logger.error("QR regeneration failed for client %s: %s", client_id, e)

    return {
        "success": True,
        "client_id": client_id,
        "qr_code_url": qr_url,
        "message": "WhatsApp number connected. QR code updated to point at your business number.",
    }