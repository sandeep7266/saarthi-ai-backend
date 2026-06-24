"""
routers/master_onboarding.py
Master Onboarding Bot — runs on the SAME WhatsApp number/webhook as customer
booking. When an incoming message's phone_number_id does NOT match any
existing active client, this state-machine takes over and walks a NEW
business owner through registration:

  greet → name → shop_name → business_type (buttons) → KYC (phone/email/
  address/GST) → plan selection (buttons) → Razorpay payment link

On payment success (payments.py), the client's own customer-facing QR code
is generated and two WhatsApp messages are sent: one to the new client
(welcome + QR + bill) and one to the company admin (new client notification).

This module is called from routers/booking.py's whatsapp_incoming() handler
whenever _resolve_tenant() returns no match.
"""

import logging
import os
import re
from datetime import datetime, timezone

import httpx

from database import get_db
from utils.rate_limiter import limiter, LIMIT_NORMAL  # noqa: F401  (kept for parity/future use)

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION  = os.getenv("META_API_VERSION", "v19.0")
COMPANY_ADMIN_PHONE = os.getenv("COMPANY_ADMIN_PHONE", "")  # where "new client" alerts go

ONBOARDING_SESSIONS = "onboarding_sessions"  # Firestore collection (top-level)

BUSINESS_TYPES = [
    ("salon",      "💇 Salon"),
    ("parlour",    "💅 Parlour"),
    ("clinic",     "🩺 Clinic"),
    ("cafe",       "☕ Cafe"),
    ("restaurant", "🍽️ Restaurant"),
]

PLAN_OPTIONS = [
    ("basic",   "Basic — ₹999/mo"),
    ("premium", "Premium — ₹1999/mo"),
]

# Step order — drives the state machine
STEPS = [
    "greet",
    "ask_name",
    "ask_shop_name",
    "ask_business_type",
    "ask_phone",
    "ask_email",
    "ask_address",
    "ask_gst",
    "ask_plan",
    "ask_billing_cycle",
    "confirm_and_pay",
    "done",
]


# ── Entry point — called from booking.py when tenant not found ────────────────

async def handle_onboarding_message(
    phone_number_id: str,
    from_number: str,
    msg_body: str,
    interactive_id: str = "",
) -> None:
    """
    Main dispatcher. Loads (or creates) the onboarding session for this
    WhatsApp number, advances the state machine by one step, and sends
    the next prompt.
    """
    db = get_db()
    session_ref = db.collection(ONBOARDING_SESSIONS).document(from_number)
    session_doc = session_ref.get()

    if not session_doc.exists:
        # Brand new — start the flow
        session = _create_session(session_ref)
        await _send_step_prompt(phone_number_id, from_number, session)
        return

    session = session_doc.to_dict()
    current_step = session.get("step", "greet")

    # Use button reply id if present, else free-text message
    user_input = interactive_id or msg_body

    next_step, updates = _process_step(current_step, user_input, session)
    updates["step"] = next_step
    updates["updated_at"] = datetime.now(timezone.utc)
    session_ref.update(updates)

    session.update(updates)

    if next_step == "confirm_and_pay":
        await _finalize_and_send_payment_link(phone_number_id, from_number, session)
    elif next_step == "done":
        pass  # nothing further to send; payment link already sent
    else:
        await _send_step_prompt(phone_number_id, from_number, session)


def _create_session(session_ref) -> dict:
    now = datetime.now(timezone.utc)
    session = {
        "step"        : "greet",
        "created_at"  : now,
        "updated_at"  : now,
        "owner_name"  : "",
        "business_name": "",
        "business_type": "",
        "owner_phone" : "",
        "owner_email" : "",
        "address"     : "",
        "city"        : "",
        "gst_number"  : "",
        "plan"        : "",
        "billing_cycle": "",
    }
    session_ref.set(session)
    return session


# ── State machine — validates input for current step, returns next step ──────

def _process_step(step: str, user_input: str, session: dict) -> tuple[str, dict]:
    """Returns (next_step, field_updates_dict)."""
    text = (user_input or "").strip()

    if step == "greet":
        return "ask_name", {}

    if step == "ask_name":
        name = _extract_name(text)
        if not name:
            return "ask_name", {}  # re-ask, no update
        return "ask_shop_name", {"owner_name": name}

    if step == "ask_shop_name":
        if len(text) < 2:
            return "ask_shop_name", {}
        return "ask_business_type", {"business_name": text}

    if step == "ask_business_type":
        valid_ids = {bt[0] for bt in BUSINESS_TYPES}
        chosen = text.lower().strip()
        if chosen not in valid_ids:
            return "ask_business_type", {}
        return "ask_phone", {"business_type": chosen}

    if step == "ask_phone":
        phone = _extract_phone(text)
        if not phone:
            return "ask_phone", {}
        return "ask_email", {"owner_phone": phone}

    if step == "ask_email":
        email = _extract_email(text)
        if not email:
            return "ask_email", {}
        return "ask_address", {"owner_email": email}

    if step == "ask_address":
        if len(text) < 5:
            return "ask_address", {}
        # Naive split: last word/phrase after comma = city, else whole thing as address+city
        city = text.split(",")[-1].strip() if "," in text else text
        return "ask_gst", {"address": text, "city": city}

    if step == "ask_gst":
        gst = text if text.lower() not in ("skip", "no", "nahi") else ""
        return "ask_plan", {"gst_number": gst}

    if step == "ask_plan":
        valid_ids = {p[0] for p in PLAN_OPTIONS}
        chosen = text.lower().strip()
        if chosen not in valid_ids:
            return "ask_plan", {}
        return "ask_billing_cycle", {"plan": chosen}

    if step == "ask_billing_cycle":
        chosen = text.lower().strip()
        if chosen not in ("monthly", "yearly"):
            return "ask_billing_cycle", {}
        return "confirm_and_pay", {"billing_cycle": chosen}

    return step, {}


# ── Extraction helpers ──────────────────────────────────────────────────────────

def _extract_name(text: str) -> str:
    cleaned = text.strip().strip(".,!- ")
    for prefix in ["mera naam", "my name is", "naam", "name is", "main"]:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    words = cleaned.split()[:3]
    return " ".join(words).title() if len(cleaned) >= 2 else ""


def _extract_phone(text: str) -> str:
    digits = re.sub(r"[^\d+]", "", text)
    if digits.startswith("+91") and len(digits) == 13:
        return digits
    if digits.startswith("91") and len(digits) == 12:
        return "+" + digits
    if len(digits) == 10:
        return "+91" + digits
    return ""


def _extract_email(text: str) -> str:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    return match.group(0) if match else ""


# ── Prompt sender — picks the right message/buttons for the current step ─────

async def _send_step_prompt(phone_number_id: str, to: str, session: dict) -> None:
    step = session.get("step", "greet")

    if step == "greet":
        await _send_text(
            phone_number_id, to,
            "Namaste! 🙏 Main Saarthi-AI ki onboarding assistant hoon.\n\n"
            "Hum salons, parlours, clinics, cafes aur restaurants ke liye "
            "AI WhatsApp receptionist aur booking system banate hain.\n\n"
            "Aapka business Saarthi-AI se connect karne mein khushi hogi! "
            "Pehle, aapka naam bata dein? 😊"
        )

    elif step == "ask_name":
        await _send_text(phone_number_id, to, "Maafi, naam samajh nahi paayi. Phir se bata dein? 🙏")

    elif step == "ask_shop_name":
        await _send_text(phone_number_id, to, "Dhanyavaad! Aapke shop/business ka naam kya hai?")

    elif step == "ask_business_type":
        await _send_buttons(
            phone_number_id, to,
            "Aapka business kis tarah ka hai?",
            [{"id": bt_id, "title": label} for bt_id, label in BUSINESS_TYPES[:3]],
        )
        # Meta allows max 3 buttons per message — send remaining as a list-style follow-up text
        remaining = BUSINESS_TYPES[3:]
        if remaining:
            opts = " ya ".join(label for _, label in remaining)
            await _send_text(phone_number_id, to, f"(Ya reply karein: {opts})")

    elif step == "ask_phone":
        await _send_text(phone_number_id, to, "Aapka contact number bata dein (jaise: 9876543210)")

    elif step == "ask_email":
        await _send_text(phone_number_id, to, "Email address bata dein (invoice/receipts ke liye)")

    elif step == "ask_address":
        await _send_text(phone_number_id, to, "Business address aur city bata dein")

    elif step == "ask_gst":
        await _send_text(
            phone_number_id, to,
            "GST ya Udyam Registration number hai? Agar hai to bhejein, "
            "nahi to 'skip' likh dein."
        )

    elif step == "ask_plan":
        await _send_buttons(
            phone_number_id, to,
            "Plan select karein:",
            [{"id": pid, "title": label} for pid, label in PLAN_OPTIONS],
        )

    elif step == "ask_billing_cycle":
        await _send_buttons(
            phone_number_id, to,
            "Billing cycle?",
            [{"id": "monthly", "title": "Monthly"}, {"id": "yearly", "title": "Yearly (-17%)"}],
        )


# ── Finalize — create Firestore client doc + Razorpay link ───────────────────

async def _finalize_and_send_payment_link(phone_number_id: str, to: str, session: dict) -> None:
    """
    Reuses the existing onboard.py logic by calling its core function directly,
    avoiding duplicate Razorpay/Firestore code paths.
    """
    from routers.onboard import (
        OnboardRequest, BusinessType, PlanTier, BillingCycle,
        _create_pending_vendor_core,
    )

    try:
        body = OnboardRequest(
            business_name     = session["business_name"],
            owner_name        = session["owner_name"],
            owner_phone       = session["owner_phone"],
            owner_email       = session["owner_email"],
            business_type     = BusinessType(session["business_type"]),
            city              = session.get("city", session.get("address", "")),
            address           = session["address"],
            plan              = PlanTier(session["plan"]),
            billing_cycle     = BillingCycle(session["billing_cycle"]),
            whatsapp_phone_id = "",  # set later by client during their own Meta setup
        )
    except Exception as e:
        logger.error("Onboarding finalize validation failed: %s", e)
        await _send_text(
            phone_number_id, to,
            "Maafi chahti hoon, kuch details mein dikkat aa rahi hai. "
            "Support se contact karein: support@saarthi-ai.in 🙏"
        )
        return

    try:
        result = await _create_pending_vendor_core(body)
    except Exception as e:
        logger.error("create_pending_vendor failed during WhatsApp onboarding: %s", e)
        await _send_text(
            phone_number_id, to,
            "Maafi chahti hoon, registration mein technical dikkat aa rahi hai. "
            "Thodi der mein try karein ya support@saarthi-ai.in pe likhein. 🙏"
        )
        return

    # Persist client_id on the onboarding session for the payments webhook to find later
    db = get_db()
    db.collection(ONBOARDING_SESSIONS).document(to).update({
        "client_id": result.client_id,
        "step"     : "done",
    })

    await _send_text(
        phone_number_id, to,
        f"Shaandar, {session['owner_name']}! 🎉\n\n"
        f"*{session['business_name']}* ({session['business_type'].title()}) "
        f"register ho gaya hai.\n\n"
        f"Payment complete karein activate karne ke liye:\n"
        f"👉 {result.payment_link}\n\n"
        f"Payment ke baad aapka QR code aur bill WhatsApp pe milega. 🙏"
    )

    # Notify company admin
    if COMPANY_ADMIN_PHONE:
        await _send_text(
            phone_number_id, COMPANY_ADMIN_PHONE,
            f"🆕 New client started onboarding:\n"
            f"{session['business_name']} ({session['business_type']})\n"
            f"Owner: {session['owner_name']} | {session['owner_phone']}\n"
            f"Awaiting payment confirmation."
        )


# ── WhatsApp senders ───────────────────────────────────────────────────────────

async def _send_text(phone_number_id: str, to: str, message: str) -> None:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "text",
        "text": {"body": message, "preview_url": True},
    }
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type" : "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Onboarding bot send (text) failed → %s: %s", to, e)


async def _send_buttons(phone_number_id: str, to: str, body_text: str, buttons: list[dict]) -> None:
    """
    buttons: list of {"id": "...", "title": "..."} — Meta allows max 3 per message.
    """
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
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons[:3]
                ]
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type" : "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Onboarding bot send (buttons) failed → %s: %s", to, e)