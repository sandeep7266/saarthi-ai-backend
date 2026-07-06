"""
routers/booking.py
WhatsApp incoming webhook — NEW FLOW:
1. Customer message karta hai → Gemini greeting + intent detect karta hai
2. Agar booking intent hai → naam (agar pehle se nahi hai) puchta hai
3. Naam mil jaane par → Web Booking App link bhejta hai (session token ke saath)
4. Customer web app pe service+staff+slot+payment complete karta hai
5. Payment confirm hone par → booking ID + invoice WhatsApp pe wapas aata hai

Yeh Gemini ko slot/service selection ki complexity se free karta hai —
ab AI sirf conversational layer hai, asli booking web app mein hoti hai.
"""

import os
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
import re
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi import APIRouter, HTTPException, Query, Request, BackgroundTasks
from database import get_db, Collections

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Booking"])

# ── Environment Config ─────────────────────────────────────────────────────────
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "saarthi_verify_token")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION      = os.getenv("META_API_VERSION", "v19.0")
GROQ_API_KEY          = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL            = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
# llama-3.3-70b-versatile is being shut down by Groq on 08/16/26 — switched to
# qwen3.6-27b (verified against console.groq.com/docs/deprecations, July 2026).
APP_BASE_URL          = os.getenv("APP_BASE_URL", "https://saarthi-ai.in")


# ── Meta Webhook Verification (GET) ───────────────────────────────────────────

@router.get("/whatsapp")
async def whatsapp_verify(
    hub_mode        : str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge   : str = Query(..., alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified successfully.")
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification token mismatch.")


# ── Main Incoming Message Handler (POST) ──────────────────────────────────────

@router.post("/whatsapp")
#async def whatsapp_incoming(request: Request, @router.post("/whatsapp")
async def whatsapp_incoming(request: Request, background_tasks: BackgroundTasks):
    """
    Ingestion gateway for Meta WhatsApp Cloud API messages.
    NEW FLOW: Naam collect karke Web Booking App link bhejta hai.
    """
    payload = await request.json()

    try:
        entry   = payload["entry"][0]
        changes = entry["changes"][0]["value"]
        phone_number_id = changes.get("metadata", {}).get("phone_number_id", "")

        messages = changes.get("messages", [])
        if not messages:
            return {"status": "ok"}

        msg         = messages[0]
        from_number = msg.get("from", "")
        msg_type    = msg.get("type", "")
        msg_body    = ""
        interactive_id = ""
        media_id    = ""
        media_type  = ""

        if msg_type == "text":
            msg_body = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                msg_body       = interactive["button_reply"]["title"]
                interactive_id = interactive["button_reply"]["id"]
            elif interactive.get("type") == "list_reply":
                msg_body       = interactive["list_reply"]["title"]
                interactive_id = interactive["list_reply"]["id"]
        elif msg_type in ("image", "document"):
            media_id   = msg.get(msg_type, {}).get("id", "")
            media_type = msg_type
            msg_body   = f"[{msg_type} uploaded]"  # placeholder so it isn't treated as empty
        else:
            _send_whatsapp_text(phone_number_id, from_number,
                "Sorry, main abhi sirf text messages samajh sakti hoon. 😊")
            return {"status": "ok"}

        if not msg_body:
            return {"status": "ok"}

        logger.info("Incoming WhatsApp | phone_id=%s | from=%s | msg=%s",
                    phone_number_id, from_number, msg_body[:80])

        # ── Multi-Tenant Isolation ──────────────────────────────────────────
        client_data, client_id = _resolve_tenant(phone_number_id)
        if not client_data:
            from routers.master_onboarding import handle_onboarding_message
            
            # Ye task ab background mein chalega, aur server turant reply dega
            background_tasks.add_task(
                handle_onboarding_message,
                phone_number_id=phone_number_id,
                from_number=from_number,
                msg_body=msg_body,
                interactive_id=interactive_id,
                media_id=media_id,
                media_type=media_type,
            )
            return {"status": "ok"}
        if client_data.get("status") != "active":
            return {"status": "ok"}

        # ── Customer profile fetch karo (naam already pata hai ya nahi) ────
        customer_profile = _get_or_create_customer_profile(client_id, from_number)

        # ── Conversation history ────────────────────────────────────────────
        conversation_history = _get_conversation_history(client_id, from_number)

        # business_type top-level client field se lo (always reliable) —
        # gemini_bot_profile incomplete ho sakta hai agar client manually
        # activate kiya gaya ho (Razorpay webhook se nahi guzra).
        bot_profile = dict(client_data.get("gemini_bot_profile", {}))
        bot_profile["business_type"] = client_data.get("business_type", bot_profile.get("business_type", "salon"))

        # ── Cancel — works while waiting for name (only WhatsApp-side state) ────
        # Once the customer moves to book.html, abandoning there is already
        # handled by the 15-min stale-booking auto-expiry (utils/booking_expiry.py).
        cancel_keywords = {"cancel", "cancel karo", "band karo", "ruk jao", "stop",
                           "roko", "nahi karna", "chodo", "chhod do", "exit", "quit"}
        if msg_body.strip().lower() in cancel_keywords and _is_awaiting_name(client_id, from_number):
            _clear_awaiting_name(client_id, from_number)
            _send_whatsapp_text(phone_number_id, from_number,
                "Theek hai, booking cancel kar diya. 🙏 Jab bhi chahein, dobara message kar dein.")
            return {"status": "ok"}

        # ── Gemini se sirf conversational reply + intent lo ────────────────
        ai_response = await _invoke_gemini(
            user_message=msg_body,
            bot_profile=bot_profile,
            conversation_history=conversation_history,
            customer_name=customer_profile.get("name", ""),
        )

        ai_text = ai_response.get("reply_text", "")
        intent  = ai_response.get("intent", "conversation")

        # ── Intent: booking chahiye ──────────────────────────────────────────
        if intent == "want_booking":
            if not customer_profile.get("name"):
                # Naam nahi pata — pehle naam pucho
                ai_text = (
                    f"{ai_text}\n\n"
                    f"Booking ke liye, aapka naam bata dein? 😊"
                )
                _set_awaiting_name(client_id, from_number)
            else:
                # Naam pata hai — seedha booking link bhej do
                ai_text = _generate_booking_link_message(
                    client_id=client_id,
                    customer_phone=from_number,
                    customer_name=customer_profile["name"],
                    business_name=client_data.get("business_name", ""),
                )

        # ── Intent: naam de raha hai (awaiting_name state mein) ────────────
        elif _is_awaiting_name(client_id, from_number):
            extracted_name = _extract_name_from_message(msg_body)
            if extracted_name:
                _save_customer_name(client_id, from_number, extracted_name)
                _clear_awaiting_name(client_id, from_number)
                ai_text = _generate_booking_link_message(
                    client_id=client_id,
                    customer_phone=from_number,
                    customer_name=extracted_name,
                    business_name=client_data.get("business_name", ""),
                )
            else:
                ai_text = "Maafi chahti hoon, aapka naam samajh nahi paayi. Phir se bata dein? 🙏"

        # ── Store conversation turn ──────────────────────────────────────────
        _store_conversation_turn(client_id, from_number, msg_body, ai_text)

        if ai_text:
            _send_whatsapp_text(phone_number_id, from_number, ai_text)

    except (KeyError, IndexError) as e:
        logger.debug("Webhook payload parse skip (likely non-message event): %s", e)

    return {"status": "ok"}


# ── Tenant Resolution ──────────────────────────────────────────────────────────

def _resolve_tenant(phone_number_id: str) -> tuple[Optional[dict], Optional[str]]:
    db = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .where("whatsapp_phone_id", "==", phone_number_id)
        .where("status", "==", "active")
        .limit(1)
        .get()
    )
    if docs:
        return docs[0].to_dict(), docs[0].id
    return None, None


# ── Customer Profile Management ────────────────────────────────────────────────

def _get_or_create_customer_profile(client_id: str, customer_phone: str) -> dict:
    """Customer ka naam aur preferences yaad rakhne ke liye."""
    db = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("customers")
        .document(customer_phone)
    )
    doc = ref.get()
    if doc.exists:
        return doc.to_dict()

    profile = {
        "phone"        : customer_phone,
        "name"         : "",
        "awaiting_name": False,
        "created_at"   : datetime.now(timezone.utc),
    }
    ref.set(profile)
    return profile


def _set_awaiting_name(client_id: str, customer_phone: str) -> None:
    db = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("customers")
        .document(customer_phone)
    )
    ref.update({"awaiting_name": True})


def _is_awaiting_name(client_id: str, customer_phone: str) -> bool:
    db = get_db()
    doc = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("customers")
        .document(customer_phone)
        .get()
    )
    return doc.exists and doc.to_dict().get("awaiting_name", False)


def _clear_awaiting_name(client_id: str, customer_phone: str) -> None:
    db = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("customers")
        .document(customer_phone)
    )
    ref.update({"awaiting_name": False})


def _save_customer_name(client_id: str, customer_phone: str, name: str) -> None:
    db = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("customers")
        .document(customer_phone)
    )
    ref.update({"name": name, "updated_at": datetime.now(timezone.utc)})


def _extract_name_from_message(msg: str) -> str:
    """Simple heuristic: pehla 2-3 words jo letters hain, naam maan lo."""
    cleaned = msg.strip()
    # Common prefixes hata do
    for prefix in ["mera naam", "my name is", "naam", "name is", "main"]:
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    cleaned = cleaned.strip(".,!- ")
    words = cleaned.split()[:3]
    name = " ".join(words).title()
    return name if len(name) >= 2 else ""


# ── Booking Link Generator ──────────────────────────────────────────────────────

def _generate_booking_link_message(
    client_id: str,
    customer_phone: str,
    customer_name: str,
    business_name: str,
) -> str:
    """
    Booking session banao aur customer ko Web App link bhejo.
    """
    from routers.booking_session import create_booking_session

    session = create_booking_session(
        client_id=client_id,
        customer_phone=customer_phone,
        customer_name=customer_name,
    )

    return (
        f"Dhanyavaad, {customer_name}! 😊\n\n"
        f"Neeche diye link pe jaake apni booking complete karein:\n"
        f"👉 {session['booking_url']}\n\n"
        f"Yahan se aap service, staff aur time slot choose kar sakte hain "
        f"aur payment bhi kar sakte hain. Link 30 minute tak valid hai. 🙏"
    )


# ── Conversation Memory ────────────────────────────────────────────────────────

def _get_conversation_history(client_id: str, customer_phone: str) -> list[dict]:
    db = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("conversations")
        .document(customer_phone)
        .collection("turns")
        .order_by("timestamp", direction="DESCENDING")
        .limit(10)
        .get()
    )
    turns = [d.to_dict() for d in docs]
    turns.reverse()
    return turns


def _store_conversation_turn(client_id: str, customer_phone: str, user_msg: str, ai_reply: str):
    db = get_db()
    turns_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection("conversations")
        .document(customer_phone)
        .collection("turns")
    )
    turns_ref.add({
        "user"     : user_msg,
        "assistant": ai_reply,
        "timestamp": datetime.now(timezone.utc),
    })


# ── Groq AI — Simplified (sirf conversation + intent) ─────────────────────────

async def _invoke_gemini(
    user_message: str,
    bot_profile: dict,
    conversation_history: list[dict],
    customer_name: str = "",
) -> dict:
    """
    AI ab aur bhi strictly front-loaded classification ke sath kaam karega
    taaki loops aur false links na bheje.
    """
    persona_name  = bot_profile.get("persona_name", "Priya")
    business_type = bot_profile.get("business_type", "salon")

    history_text = ""
    for turn in conversation_history[-6:]:
        history_text += f"Customer: {turn.get('user','')}\n{persona_name}: {turn.get('assistant','')}\n"

    name_context = f"Customer ka naam {customer_name} hai." if customer_name else "Customer ka naam abhi pata nahi hai."

    # 🧠 NEW UPGRADED STRICT PROMPT
    system_prompt = f"""You are {persona_name}, a warm AI receptionist for a {business_type} business.
Communicate in Hinglish (Hindi-English mix). Keep replies SHORT (2-3 lines max).
{name_context}

CRITICAL INSTRUCTION — YOU MUST START YOUR RESPONSE WITH ONE OF THESE TWO PREFIXES:
1. Start with '[BOOKING]' ONLY if the customer explicitly says they want to book an appointment, ask for a booking link, or wants to block a slot right now.
2. Start with '[CHAT]' for everything else (greetings like hi/hello, asking about timing/prices, cancellations, general questions, or casual talk).

EXAMPLES:
- User: Hi -> Response: [CHAT] Hello! Kaise hain aap? Kaise madad karu aapki?
- User: Mujhe booking karni hai -> Response: [BOOKING] Sure! Main aapki booking ka link share karti hoon.
- User: Cancel kar do -> Response: [CHAT] Koi baat nahi, aapka session cancel kar diya hai. Jab sahi lage tab batana!

CONVERSATION HISTORY:
{history_text}

Never invent specific slot times."""

    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not configured.")
        return {
            "reply_text": "Maafi chahti hoon, abhi thodi technical dikkat aa rahi hai. Thodi der mein try karein. 🙏",
            "intent"    : "error",
        }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type" : "application/json",
                },
                json={
                    "model"      : GROQ_MODEL,
                    "messages"   : [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                    "temperature": 0.1,  # Strict temperature for logical accuracy
                    "max_tokens" : 1024,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
            # 1. Poore closed think tags udao (Case-insensitive (?i) ke sath)
            raw_text = re.sub(r'(?i)<think>.*?</think>', '', raw_text, flags=re.DOTALL)
            # 2. Agar token khatam hone se tag adhoora reh gaya ho, toh use bhi udao
            raw_text = re.sub(r'(?i)<think>.*', '',raw_text, flags=re.DOTALL).strip()
    except Exception as e:
        logger.error("Groq API error: %s", e)
        return {
            "reply_text": "Maafi chahti hoon, abhi thodi technical dikkat aa rahi hai. Thodi der mein try karein. 🙏",
            "intent"    : "error",
        }

    # 🛠️ SMART PARSING LOGIC BASED ON PREFIX
    intent = "conversation"
    reply_text = raw_text

    if raw_text.startswith("[BOOKING]"):
        intent = "want_booking"
        reply_text = raw_text.replace("[BOOKING]", "").strip()
    elif raw_text.startswith("[CHAT]"):
        intent = "conversation"
        reply_text = raw_text.replace("[CHAT]", "").strip()
    else:
        # Safe fallback agar AI prefix bhool jaye
        if "INTENT:want_booking" in raw_text:
            intent = "want_booking"
            reply_text = raw_text.replace("INTENT:want_booking", "").strip()

    return {"reply_text": reply_text, "intent": intent}
    intent = "conversation"
    reply_text = raw_text

    if "INTENT:want_booking" in raw_text:
        intent     = "want_booking"
        reply_text = raw_text.replace("INTENT:want_booking", "").strip()

    return {"reply_text": reply_text, "intent": intent}


# ── Booking Initiation (reused by booking_session.py) ─────────────────────────

async def _initiate_booking(
    client_id: str,
    client_data: dict,
    customer_phone: str,
    slot_info: dict,
    services_info: list[dict],
) -> dict:
    """
    Lock slot as pending_payment, create booking doc, generate Razorpay deposit link.
    Called from booking_session.py after customer selects via Web App.

    services_info: list of {"service_id", "name", "price"} — supports multiple
    services booked together against a single slot+staff (cart-style booking).
    """
    import razorpay

    RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
    DEPOSIT_PERCENT      = 0.25

    db = get_db()
    booking_id = str(uuid.uuid4())[:8].upper()
    now        = datetime.now(timezone.utc)

    total_price    = sum(int(s.get("price", 0)) for s in services_info)
    deposit_amount = int(total_price * DEPOSIT_PERCENT)
    deposit_paise  = max(deposit_amount * 100, 100)

    service_names_joined = ", ".join(s.get("name", "") for s in services_info)

    slot_id = slot_info["id"]

    slot_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .document(slot_id)
    )

    from firebase_admin import firestore as _firestore

    @_firestore.transactional
    def lock_slot_txn(transaction):
        slot_doc = slot_ref.get(transaction=transaction)
        if not slot_doc.exists or slot_doc.to_dict().get("status") != "available":
            raise ValueError("Slot no longer available")
        transaction.update(slot_ref, {"status": "pending_payment", "locked_at": now})

    try:
        transaction = db.transaction()
        lock_slot_txn(transaction)
    except ValueError:
        return {"success": False, "reason": "slot_taken"}
    except Exception as e:
        logger.error("Slot lock transaction failed: %s", e)
        return {"success": False, "reason": "transaction_error"}

    booking_doc = {
        "booking_id"    : booking_id,
        "client_id"     : client_id,
        "customer_phone": customer_phone,
        "slot_id"       : slot_id,
        "slot_datetime" : slot_info.get("slot_datetime"),
        "staff_name"    : slot_info.get("staff_name", ""),
        "services"      : [
            {"service_id": s.get("service_id", ""), "name": s.get("name", ""), "price": int(s.get("price", 0))}
            for s in services_info
        ],
        "service_name"  : service_names_joined,  # backward-compat (PDF invoice, analytics)
        "service_price" : total_price,           # backward-compat (PDF invoice, analytics)
        "deposit_amount": deposit_amount,
        "status"        : "pending_payment",
        "created_at"    : now,
        "updated_at"    : now,
    }

    booking_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .document(booking_id)
    )
    booking_ref.set(booking_doc)

    if not RAZORPAY_KEY_ID or RAZORPAY_KEY_ID == "dummy":
        # Test mode — dummy link
        slot_ref.update({"status": "pending_payment"})
        return {
            "success"     : True,
            "payment_link": f"{APP_BASE_URL}/pay-test/{booking_id}",
            "booking_id"  : booking_id,
        }

    rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    business_name = client_data.get("business_name", "")

    try:
        plink = rz_client.payment_link.create({
            "amount"        : deposit_paise,
            "currency"      : "INR",
            "accept_partial": False,
            "description"   : f"25% Advance — {service_names_joined} @ {business_name}",
            "customer"      : {"contact": customer_phone},
            "notify"        : {"sms": False, "email": False, "whatsapp": False},
            "reminder_enable": False,
            "expire_by"     : int(now.timestamp() + 900),
            "notes"         : {"booking_id": booking_id, "client_id": client_id},
            "callback_url"  : f"{APP_BASE_URL}/api/v1/webhook/booking-success",
            "callback_method": "get",
        })
        return {"success": True, "payment_link": plink["short_url"], "booking_id": booking_id}
    except Exception as e:
        logger.error("Razorpay deposit link creation failed: %s", e)
        slot_ref.update({"status": "available", "locked_at": None})
        booking_ref.delete()
        return {"success": False, "reason": f"razorpay_error: {e}"}


# ── WhatsApp Sender ────────────────────────────────────────────────────────────

def _send_whatsapp_text(phone_number_id: str, to: str, message: str) -> None:
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
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("WhatsApp send failed → %s: %s", to, e)


# ── Payment Success Redirect Page (customer booking deposit) ───────────────────

@router.get("/booking-success", response_class=HTMLResponse)
async def booking_payment_success(request: Request):
    """
    Razorpay redirects the customer's browser here after paying their booking
    deposit. Purely a UX landing page — the actual booking confirmation is
    handled server-to-server by /razorpay-webhook (payments.py), independent
    of whether the browser follows this redirect.
    """
    params = dict(request.query_params)
    is_paid = params.get("razorpay_payment_link_status", "") == "paid"

    heading = "Booking Confirmed! 🎉" if is_paid else "Payment Received"
    sub = "Confirmation aur booking details WhatsApp par bhej diye gaye hain." if is_paid \
        else "Aapki payment process ho rahi hai. Confirmation WhatsApp par milega."

    html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Saarthi-AI — Booking Status</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#0b1f17; color:#fff;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:24px; }}
  .card {{ max-width:420px; text-align:center; background:#122c20; border-radius:20px; padding:36px 28px; }}
  .icon {{ font-size:56px; margin-bottom:12px; }}
  h1 {{ font-size:22px; margin:0 0 10px; }}
  p {{ color:#9fb8ac; font-size:15px; line-height:1.5; margin:0; }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">{'✅' if is_paid else '⏳'}</div>
    <h1>{heading}</h1>
    <p>{sub}</p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)