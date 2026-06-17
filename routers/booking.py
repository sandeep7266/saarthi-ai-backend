"""
routers/booking.py
WhatsApp incoming webhook message parser + Gemini AI booking execution engine.
Multi-tenant isolation, slot locking, Razorpay deposit link generation.
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import razorpay
from fastapi import APIRouter, HTTPException, Query, Request
from google import generativeai as genai

from database import get_db, Collections

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhook", tags=["WhatsApp Booking"])

# ── Environment Config ─────────────────────────────────────────────────────────
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "saarthi_verify_token")
META_ACCESS_TOKEN     = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION      = os.getenv("META_API_VERSION", "v19.0")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY", "")
RAZORPAY_KEY_ID       = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET   = os.getenv("RAZORPAY_KEY_SECRET", "")
APP_BASE_URL          = os.getenv("APP_BASE_URL", "https://saarthi-ai.in")

DEPOSIT_PERCENT = 0.25   # 25% deposit required to hold slot

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ── Meta Webhook Verification (GET) ───────────────────────────────────────────

@router.get("/whatsapp")
async def whatsapp_verify(
    hub_mode       : str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge  : str = Query(..., alias="hub.challenge"),
):
    """Meta webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified successfully.")
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification token mismatch.")


# ── Main Incoming Message Handler (POST) ──────────────────────────────────────

@router.post("/whatsapp")
async def whatsapp_incoming(request: Request):
    """
    Ingestion gateway for Meta WhatsApp Cloud API messages.
    Performs multi-tenant isolation → Gemini AI routing → booking execution.
    """
    payload = await request.json()

    try:
        entry   = payload["entry"][0]
        changes = entry["changes"][0]["value"]

        # Extract the incoming phone number ID to identify the tenant
        phone_number_id = changes.get("metadata", {}).get("phone_number_id", "")

        messages = changes.get("messages", [])
        if not messages:
            # Delivery receipts / status updates — acknowledge silently
            return {"status": "ok"}

        msg         = messages[0]
        from_number = msg.get("from", "")   # Customer's WhatsApp number
        msg_type    = msg.get("type", "")
        msg_body    = ""

        if msg_type == "text":
            msg_body = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            # Handle quick-reply buttons
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                msg_body = interactive["button_reply"]["title"]
        else:
            # Unsupported message type
            _send_whatsapp_text(phone_number_id, from_number,
                "Sorry, main abhi sirf text messages samajh sakti hoon. 😊")
            return {"status": "ok"}

        if not msg_body:
            return {"status": "ok"}

        logger.info("Incoming WhatsApp | phone_id=%s | from=%s | msg=%s",
                    phone_number_id, from_number, msg_body[:80])

        # ── Multi-Tenant Isolation: resolve client by phone_number_id ──────────
        client_data, client_id = _resolve_tenant(phone_number_id)
        if not client_data:
            logger.warning("No active tenant found for phone_number_id: %s", phone_number_id)
            return {"status": "ok"}   # Unknown number, silently drop

        if client_data.get("status") != "active":
            # Bot is shut down for expired/inactive tenants
            return {"status": "ok"}

        # ── Fetch business context for Gemini ─────────────────────────────────
        services      = _fetch_services(client_id)
        available_slots = _fetch_available_slots(client_id)
        bot_profile   = client_data.get("gemini_bot_profile", {})

        # ── Build conversation history from Firestore ─────────────────────────
        conversation_history = _get_conversation_history(client_id, from_number)

        # ── Call Gemini AI ────────────────────────────────────────────────────
        ai_response = await _invoke_gemini(
            client_id=client_id,
            customer_phone=from_number,
            user_message=msg_body,
            services=services,
            available_slots=available_slots,
            bot_profile=bot_profile,
            conversation_history=conversation_history,
        )

        # ── Parse AI intent and act ────────────────────────────────────────────
        ai_text   = ai_response.get("reply_text", "")
        intent    = ai_response.get("intent", "conversation")
        slot_info = ai_response.get("slot_to_book")
        service_info = ai_response.get("service_to_book")

        if intent == "book_slot" and slot_info and service_info:
            # Lock slot + create pending booking + generate deposit link
            result = await _initiate_booking(
                client_id=client_id,
                client_data=client_data,
                customer_phone=from_number,
                slot_info=slot_info,
                service_info=service_info,
            )
            if result.get("success"):
                ai_text = (
                    f"{ai_text}\n\n"
                    f"💳 *25% Advance Payment Link (Valid 15 min):*\n{result['payment_link']}\n\n"
                    f"Payment ke baad aapki booking confirm ho jayegi! ✅"
                )
            else:
                ai_text = ai_text or "Maafi chahti hoon, yeh slot abhi available nahi hai. Doosra time chuniye? 🙏"

        # ── Store conversation turn ────────────────────────────────────────────
        _store_conversation_turn(client_id, from_number, msg_body, ai_text)

        # ── Send reply via WhatsApp ────────────────────────────────────────────
        if ai_text:
            _send_whatsapp_text(phone_number_id, from_number, ai_text)

    except (KeyError, IndexError) as e:
        logger.debug("Webhook payload parse skip (likely non-message event): %s", e)

    return {"status": "ok"}


# ── Tenant Resolution ──────────────────────────────────────────────────────────

def _resolve_tenant(phone_number_id: str) -> tuple[Optional[dict], Optional[str]]:
    """Find the active tenant document by Meta phone_number_id."""
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


# ── Business Data Fetchers ─────────────────────────────────────────────────────

def _fetch_services(client_id: str) -> list[dict]:
    db = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .where("is_active", "==", True)
        .get()
    )
    return [{"id": d.id, **d.to_dict()} for d in docs]


def _fetch_available_slots(client_id: str) -> list[dict]:
    db = get_db()
    now = datetime.now(timezone.utc)
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .where("status", "==", "available")
        .where("slot_datetime", ">=", now)
        .order_by("slot_datetime")
        .limit(30)
        .get()
    )
    return [{"id": d.id, **d.to_dict()} for d in docs]


# ── Conversation Memory ────────────────────────────────────────────────────────

def _get_conversation_history(client_id: str, customer_phone: str) -> list[dict]:
    """Retrieve last 10 conversation turns for context window."""
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
    turns.reverse()  # Chronological order
    return turns


def _store_conversation_turn(client_id: str, customer_phone: str, user_msg: str, ai_reply: str):
    """Persist a conversation turn for multi-turn context."""
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


# ── Gemini AI Engine ───────────────────────────────────────────────────────────

async def _invoke_gemini(
    client_id: str,
    customer_phone: str,
    user_message: str,
    services: list[dict],
    available_slots: list[dict],
    bot_profile: dict,
    conversation_history: list[dict],
) -> dict:
    """
    Builds context-rich prompt and invokes Gemini.
    Returns structured dict: { reply_text, intent, slot_to_book, service_to_book }.
    """
    persona_name  = bot_profile.get("persona_name", "Priya")
    business_type = bot_profile.get("business_type", "salon")
    welcome_msg   = bot_profile.get("welcome_msg", "")

    # Format services
    services_text = "\n".join([
        f"- {s.get('name')} | ₹{s.get('price')} | {s.get('duration_min', 30)} min"
        for s in services
    ]) or "Services list loading..."

    # Format slots (limit to 10 for prompt efficiency)
    slots_text = "\n".join([
        f"- SlotID:{s['id']} | {_format_slot_datetime(s.get('slot_datetime'))} | Staff:{s.get('staff_name','Any')}"
        for s in available_slots[:10]
    ]) or "No slots available today."

    # Format conversation history
    history_text = ""
    for turn in conversation_history[-6:]:
        history_text += f"Customer: {turn.get('user','')}\n{persona_name}: {turn.get('assistant','')}\n"

    system_prompt = f"""You are {persona_name}, the AI WhatsApp receptionist for this {business_type} business.
Communicate warmly in Hinglish (mix of Hindi and English). Keep replies SHORT and conversational (2-4 lines max).
Never break character. You can understand English, Hindi, and Hinglish.

SERVICES AVAILABLE:
{services_text}

AVAILABLE APPOINTMENT SLOTS:
{slots_text}

INSTRUCTIONS:
1. Help customers discover services, check availability, and book appointments.
2. When a customer wants to book, confirm: which service, which date/time, which staff (if preference).
3. When you are confident about slot + service selection, output a JSON block (and only in that case):
   BOOKING_JSON:{{\"intent\":\"book_slot\",\"slot_id\":\"<exact SlotID from list>\",\"service_name\":\"<service name>\",\"service_price\":<price as integer>,\"staff_name\":\"<staff name>\"}}
4. For general questions, inquiry, or greetings: output only conversational text.
5. If a requested slot is NOT in the available list, politely say it's taken and suggest alternatives.
6. Never make up slots or services not listed above.

CONVERSATION HISTORY:
{history_text}"""

    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=system_prompt,
        )
        response = model.generate_content(
            user_message,
            generation_config=genai.types.GenerationConfig(
                temperature=0.4,
                max_output_tokens=512,
            ),
        )
        raw_text = response.text.strip()
    except Exception as e:
        logger.error("Gemini API error: %s", e)
        return {
            "reply_text": "Maafi chahti hoon, abhi thodi technical dikkat aa rahi hai. Thodi der mein try karein. 🙏",
            "intent"    : "error",
        }

    # Parse booking intent if Gemini signaled it
    if "BOOKING_JSON:" in raw_text:
        parts      = raw_text.split("BOOKING_JSON:", 1)
        reply_text = parts[0].strip()
        json_str   = parts[1].strip()

        # Clean up any trailing text after the JSON
        brace_end = json_str.rfind("}") + 1
        json_str  = json_str[:brace_end]

        try:
            booking_data = json.loads(json_str)
            slot_id = booking_data.get("slot_id", "")

            # Validate slot_id exists in our available list
            valid_slot_ids = {s["id"] for s in available_slots}
            if slot_id not in valid_slot_ids:
                logger.warning("Gemini returned invalid slot_id: %s", slot_id)
                return {"reply_text": reply_text or "Woh slot available nahi hai. Koi aur time chuniye?", "intent": "conversation"}

            slot_obj = next(s for s in available_slots if s["id"] == slot_id)

            return {
                "reply_text"     : reply_text,
                "intent"         : "book_slot",
                "slot_to_book"   : slot_obj,
                "service_to_book": {
                    "name" : booking_data.get("service_name", ""),
                    "price": booking_data.get("service_price", 0),
                    "staff": booking_data.get("staff_name", ""),
                },
            }
        except (json.JSONDecodeError, StopIteration) as e:
            logger.error("Failed to parse Gemini BOOKING_JSON: %s | raw=%s", e, json_str)
            return {"reply_text": raw_text, "intent": "conversation"}

    return {"reply_text": raw_text, "intent": "conversation"}


def _format_slot_datetime(slot_dt) -> str:
    if slot_dt is None:
        return "TBD"
    if hasattr(slot_dt, "strftime"):
        return slot_dt.strftime("%d %b %Y %I:%M %p")
    return str(slot_dt)


# ── Booking Initiation ─────────────────────────────────────────────────────────

async def _initiate_booking(
    client_id: str,
    client_data: dict,
    customer_phone: str,
    slot_info: dict,
    service_info: dict,
) -> dict:
    """
    Lock slot as pending_payment, create booking doc, generate Razorpay deposit link.
    """
    db = get_db()
    booking_id = str(uuid.uuid4())[:8].upper()
    now        = datetime.now(timezone.utc)

    service_price   = int(service_info.get("price", 0))
    deposit_amount  = int(service_price * DEPOSIT_PERCENT)   # 25%
    deposit_paise   = deposit_amount * 100                    # Razorpay uses paise

    if deposit_paise < 100:   # Minimum ₹1
        deposit_paise = 100

    slot_id = slot_info["id"]

    # ── Atomic slot lock (prevent race conditions) ─────────────────────────────
    slot_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .document(slot_id)
    )

    @db.transactional
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

    # ── Create pending booking document ────────────────────────────────────────
    booking_doc = {
        "booking_id"      : booking_id,
        "client_id"       : client_id,
        "customer_phone"  : customer_phone,
        "slot_id"         : slot_id,
        "slot_datetime"   : slot_info.get("slot_datetime"),
        "staff_name"      : service_info.get("staff") or slot_info.get("staff_name", ""),
        "service_name"    : service_info.get("name", ""),
        "service_price"   : service_price,
        "deposit_amount"  : deposit_amount,
        "status"          : "pending_payment",
        "created_at"      : now,
        "updated_at"      : now,
    }

    booking_ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .document(booking_id)
    )
    booking_ref.set(booking_doc)

    # ── Razorpay Deposit Link ──────────────────────────────────────────────────
    if not RAZORPAY_KEY_ID:
        return {"success": False, "reason": "razorpay_not_configured"}

    rz_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    business_name = client_data.get("business_name", "")

    try:
        plink = rz_client.payment_link.create({
            "amount"      : deposit_paise,
            "currency"    : "INR",
            "accept_partial": False,
            "description" : f"25% Advance — {service_info['name']} @ {business_name}",
            "customer"    : {"contact": customer_phone},
            "notify"      : {"sms": False, "email": False, "whatsapp": False},
            "reminder_enable": False,
            "expire_by"   : int((now.timestamp()) + 900),   # 15 minutes
            "notes"       : {
                "booking_id": booking_id,
                "client_id" : client_id,
            },
            "callback_url"   : f"{APP_BASE_URL}/booking/success",
            "callback_method": "get",
        })
        return {"success": True, "payment_link": plink["short_url"], "booking_id": booking_id}
    except Exception as e:
        logger.error("Razorpay deposit link creation failed: %s", e)
        # Release slot lock on failure
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
        "text": {"body": message, "preview_url": False},
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
