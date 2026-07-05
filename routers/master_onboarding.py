"""
routers/master_onboarding.py
Master Onboarding Bot — runs on the SAME WhatsApp number/webhook as customer
booking. When an incoming message's phone_number_id does NOT match any
existing active client, this HYBRID conversation takes over:

  - Free-text fields (owner name, business name, email, address, city, GST)
    are collected naturally via Groq (Llama 3.3 70B) — the user can type
    however they like, in any order, and the model extracts structured data.

  - The 3 fields that directly control pricing/routing/UI-mode — business_type,
    plan, and billing_cycle — are NEVER interpreted from free text. They are
    always asked via WhatsApp buttons/list, so there is zero risk of the model
    misclassifying "barbershop" vs "parlour" or mixing up Basic/Premium. A
    final "Confirm & Pay" / "Edit" button gate replaces free-text confirmation
    parsing too.

This keeps the conversation feeling natural for the easy stuff, while making
the fields that can break billing or the dashboard's vertical-specific UI
mode fully deterministic.

On payment success (payments.py), the client's own customer-facing QR code
is generated and two WhatsApp messages are sent: one to the new client
(welcome + QR + bill) and one to the company admin (new client notification).

This module is called from routers/booking.py's whatsapp_incoming() handler
whenever _resolve_tenant() returns no match.
"""

import base64
import difflib
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx

from database import get_db
from utils.invoice_generator import PLAN_FEATURES

logger = logging.getLogger(__name__)

META_ACCESS_TOKEN   = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION    = os.getenv("META_API_VERSION", "v19.0")
COMPANY_ADMIN_PHONE = os.getenv("COMPANY_ADMIN_PHONE", "")  # where "new client" alerts go
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL          = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")
# qwen3.6-27b is multimodal (text + vision), so the same model covers both the
# onboarding conversation and KYC document OCR — one model to keep an eye on
# instead of two. Verified against console.groq.com/docs/deprecations (July 2026):
# llama-3.3-70b-versatile shuts down 08/16/26, llama-4-scout shuts down 07/17/26.
# Re-check that page if either of these calls starts failing again.
GROQ_VISION_MODEL   = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

ONBOARDING_SESSIONS = "onboarding_sessions"  # Firestore collection (top-level)

MAX_HISTORY_TURNS = 12  # keep prompt small

# ── Button-only fields (never AI-interpreted) ──────────────────────────────────

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
BILLING_OPTIONS = [
    ("monthly", "Monthly"),
    ("yearly",  "Yearly (2 months free)"),
]
CONFIRM_OPTIONS = [
    ("confirm_yes",  "✅ Confirm & Pay"),
    ("confirm_edit", "✏️ Edit Details"),
]

# Fields the AI is allowed to collect from free text
AI_FIELDS = ["owner_name", "business_name", "owner_email", "address", "city"]
# GST is optional, collected by AI too but never blocks progress
ALL_REQUIRED_FOR_PAYMENT = AI_FIELDS + ["business_type", "plan", "billing_cycle"]

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
GST_PATTERN   = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d]$")
PHONE_DIGITS_PATTERN = re.compile(r"^\d{10,15}$")

SYSTEM_PROMPT_TEMPLATE = """Tum Saarthi-AI ki onboarding assistant ho, jo WhatsApp par naye business owners ko \
register karti ho. Tumhara tone warm, friendly Hinglish hai. Chhote, natural messages likho (2-3 lines).

TUMHARA KAAM: SIRF neeche diye fields ko conversation se collect karna — ek saath sab mat pucho, 1-2 cheez ek \
baar mein pucho. Jo already collected hain unhe dobara mat pucho.

- owner_name: Business owner ka naam
- business_name: Shop/business ka naam
- owner_email: Valid email address
- address: Shop ka full address
- city: Shop kis city mein hai
- gst_number: Optional — agar user "nahi hai"/"skip"/"no" bole toh empty string "" set kar do, aage mat pucho

ZAROORI: Business type, plan, aur billing cycle TUM MAT PUCHO — system separately buttons se poochega, tumhara \
kaam nahi hai. Agar upar diye saare fields (gst ke alawa) mil chuke hain, ek chhota sa message do jaise \
"Bas thoda aur baaki hai!" — aage system khud sambhal lega.

CURRENT COLLECTED DATA (isse merge karo, purana data preserve karo):
{collected_json}

TUMHE SIRF EK VALID JSON OBJECT SE REPLY KARNA HAI, KUCH AUR NAHI (no markdown, no preamble). Exact schema:
{{
  "reply": "<WhatsApp par bhejne wala message, Hinglish, warm>",
  "collected": {{
    "owner_name": "", "business_name": "", "owner_email": "", "address": "", "city": "", "gst_number": ""
  }}
}}"""


# ── Entry point — called from booking.py when tenant not found ────────────────

async def handle_onboarding_message(
    phone_number_id: str,
    from_number: str,
    msg_body: str,
    interactive_id: str = "",
    media_id: str = "",
    media_type: str = "",
) -> None:
    db = get_db()
    session_ref = db.collection(ONBOARDING_SESSIONS).document(from_number)
    session_doc = session_ref.get()

    if not session_doc.exists:
        session = _create_session(session_ref)
    else:
        session = session_doc.to_dict()

    if session.get("status") == "completed":
        await _send_text(
            phone_number_id, from_number,
            "Aapka registration already ho chuka hai! Payment link check karein upar. "
            "Koi help chahiye toh support@saarthi-ai.in pe likhein. 🙏"
        )
        return

    collected      = session.get("collected", {})
    pending_choice = session.get("pending_choice", "")

    # ── Path 1a: We're waiting on a KYC document upload ─────────────────────────
    if pending_choice in ("upload_aadhaar", "upload_pan"):
        # WhatsApp can deliver "front" and "back" (or any two quick uploads) as
        # near-simultaneous, separate webhook calls. Without a lock, both could
        # read the same pending_choice and both process as the same document,
        # duplicating messages and skipping straight past PAN. Claim the slot
        # atomically first — only the request that wins the transaction proceeds;
        # the other is a no-op (its image is simply not needed).
        claimed = _claim_kyc_slot(db, session_ref, pending_choice)
        if not claimed:
            return  # a concurrent request already claimed this step — ignore silently
        await _handle_kyc_upload(
            phone_number_id, from_number, session_ref, collected, pending_choice, media_id
        )
        return

    # ── Path 1b: We're waiting on a button tap (business_type/plan/billing/confirm) ──
    if pending_choice:
        handled = await _handle_pending_choice(
            phone_number_id, from_number, session_ref, session, pending_choice, interactive_id
        )
        if handled:
            return
        # handled == False means invalid tap; the nudge + buttons were already resent.
        return

    # ── Path 2: Free-text turn — let Groq collect the easy fields ──────────────────
    history = session.get("history", [])
    history.append({"role": "user", "content": interactive_id or msg_body})

    ai_result = await _call_groq_onboarding(collected, history)
    reply_text = (ai_result.get("reply") or "").strip()
    collected  = ai_result.get("collected", collected)

    history.append({"role": "assistant", "content": reply_text or ""})
    history = history[-MAX_HISTORY_TURNS:]

    session_ref.update({
        "history"   : history,
        "collected" : collected,
        "updated_at": datetime.now(timezone.utc),
    })
    session["collected"] = collected
    session["history"]   = history

    if reply_text:
        await _send_text(phone_number_id, from_number, reply_text)

    await _advance(phone_number_id, from_number, session_ref, collected)


def _create_session(session_ref) -> dict:
    now = datetime.now(timezone.utc)
    session = {
        "status"        : "in_progress",
        "created_at"    : now,
        "updated_at"    : now,
        "history"       : [],
        "collected"     : {},
        "pending_choice": "",
    }
    session_ref.set(session)
    return session


# ── KYC document upload handling ────────────────────────────────────────────────

def _claim_kyc_slot(db, session_ref, expected_pending_choice: str) -> bool:
    """
    Atomically claims the current KYC upload step so that two near-simultaneous
    webhook calls (e.g. WhatsApp delivering front+back of Aadhaar as separate
    messages within milliseconds of each other) can't both process as the same
    document. Only the request that wins the transaction gets True; the other
    gets False and should silently no-op.
    """
    from firebase_admin import firestore as _firestore

    @_firestore.transactional
    def _txn(transaction):
        snapshot = session_ref.get(transaction=transaction)
        current = (snapshot.to_dict() or {}).get("pending_choice", "")
        if current != expected_pending_choice:
            return False
        transaction.update(session_ref, {"pending_choice": f"{expected_pending_choice}:claimed"})
        return True

    try:
        return _txn(db.transaction())
    except Exception as e:
        logger.error("KYC slot claim transaction failed: %s", e)
        return False


async def _handle_kyc_upload(
    phone_number_id, to, session_ref, collected, pending_choice, media_id
) -> None:
    doc_label = "Aadhaar card" if pending_choice == "upload_aadhaar" else "PAN card"

    def _retry(msg: str):
        # Restore pending_choice (the claim lock moved it to a temp value) so
        # the user's next upload attempt is still routed here correctly.
        session_ref.update({"pending_choice": pending_choice})
        return _send_text(phone_number_id, to, msg)

    if not media_id:
        await _retry(f"Kripya {doc_label} ki ek photo bhejein (image ya PDF ke roop mein) 📄")
        return

    image_bytes = await _download_whatsapp_media(media_id)
    if not image_bytes:
        await _retry(f"{doc_label} download nahi ho payi. Ek baar phir bhejne ki koshish karein 🙏")
        return

    doc_type = "aadhaar" if pending_choice == "upload_aadhaar" else "pan"
    extracted = await _extract_id_document(image_bytes, doc_type)

    if extracted is None:
        await _retry(f"{doc_label} clearly padh nahi payi — kripya saaf, achi roshni mein photo bhejein 🙏")
        return

    if doc_type == "aadhaar":
        aadhaar_number = extracted.get("id_number", "").replace(" ", "")
        if len(aadhaar_number) < 4:
            await _retry("Aadhaar number clearly nahi dikha. Ek baar phir saaf photo bhejein 🙏")
            return
        collected["aadhaar_last4"] = aadhaar_number[-4:]
    else:
        pan_number = extracted.get("id_number", "").replace(" ", "").upper()
        if len(pan_number) != 10:
            await _retry("PAN number clearly nahi dikha. Ek baar phir saaf photo bhejein 🙏")
            return
        collected["pan_number"] = pan_number

    # ── Name matching against owner_name (fuzzy, case-insensitive) ─────────────
    doc_name   = (extracted.get("name") or "").strip().lower()
    owner_name = str(collected.get("owner_name", "")).strip().lower()
    if doc_name and owner_name:
        similarity = difflib.SequenceMatcher(None, doc_name, owner_name).ratio()
        name_matches = similarity >= 0.6
        # Only downgrade the flag, never upgrade — one mismatched doc should
        # still show the warning even if the other doc matched fine.
        collected["kyc_name_match"] = collected.get("kyc_name_match", True) and name_matches

    session_ref.update({"collected": collected, "pending_choice": ""})
    await _send_text(phone_number_id, to, f"{doc_label} mil gaya, dhanyavaad! ✅")
    await _advance(phone_number_id, to, session_ref, collected)


async def _download_whatsapp_media(media_id: str) -> bytes:
    """Meta's media API is two-step: fetch a short-lived URL, then fetch the bytes."""
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            meta_resp = await client.get(
                f"https://graph.facebook.com/{META_API_VERSION}/{media_id}", headers=headers
            )
            meta_resp.raise_for_status()
            media_url = meta_resp.json().get("url", "")
            if not media_url:
                return b""

            file_resp = await client.get(media_url, headers=headers)
            file_resp.raise_for_status()
            return file_resp.content
    except Exception as e:
        logger.error("WhatsApp media download failed (media_id=%s): %s", media_id, e)
        return b""


async def _extract_id_document(image_bytes: bytes, doc_type: str) -> dict | None:
    """
    Uses a Groq vision model to OCR the ID card and extract just the name and
    ID number as JSON. Returns None on any failure (caller re-prompts the user
    rather than guessing).
    """
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not configured — cannot run KYC OCR.")
        return None

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    id_hint = "12-digit Aadhaar number" if doc_type == "aadhaar" else "10-character alphanumeric PAN number"
    prompt = (
        f"Ye ek {doc_type.upper()} card ki photo hai. Isse SIRF ye JSON extract karo, kuch aur nahi "
        f'(no markdown, no code fences, no preamble — just raw JSON): '
        f'{{"name": "<card par likha poora naam>", "id_number": "<{id_hint}>"}}. '
        "Agar clearly padh nahi paa rahe ho, dono fields empty string rakho."
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type" : "application/json",
                },
                json={
                    "model": GROQ_VISION_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                        ],
                    }],
                    "temperature": 0.1,
                    "max_tokens": 200,
                    "reasoning_effort": "none",  # disable <think> output — breaks our JSON parsing
                    # NOTE: response_format=json_object is deliberately omitted here —
                    # several Groq vision models return 400 when JSON mode is combined
                    # with image content. We rely on the prompt instructions instead,
                    # plus markdown-fence-stripping below as a safety net.
                },
            )
            resp.raise_for_status()
            raw_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        error_detail = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail = e.response.text[:500]
            except Exception:
                pass
        logger.error("Groq vision OCR failed (%s): %s | response=%s", doc_type, e, error_detail)
        return None

    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        logger.error("Groq OCR JSON parse failed: %s | raw=%s", e, raw_text[:200])
        return None

    if not parsed.get("id_number"):
        return None
    return parsed


# ── Button-response handling ────────────────────────────────────────────────────

async def _handle_pending_choice(
    phone_number_id, to, session_ref, session, pending_choice, interactive_id
) -> bool:
    collected = session.get("collected", {})

    if pending_choice == "confirm":
        if interactive_id == "confirm_yes":
            session_ref.update({"pending_choice": ""})
            ok = _validate_collected(collected)
            if ok:
                await _finalize_and_send_payment_link(phone_number_id, to, collected)
            else:
                logger.warning("Confirmed but server validation failed: %s", collected)
                await _send_text(
                    phone_number_id, to,
                    "Kuch detail adhuri lag rahi hai, dobara shuru karte hain thoda sa. 🙏"
                )
                await _advance(phone_number_id, to, session_ref, collected)
            return True
        elif interactive_id == "confirm_edit":
            # Simple, predictable reset: re-ask business_type onward. Free-text
            # fields already collected stay as-is.
            session_ref.update({"pending_choice": ""})
            for f in ("business_type", "plan", "billing_cycle"):
                collected.pop(f, None)
            session_ref.update({"collected": collected})
            await _advance(phone_number_id, to, session_ref, collected)
            return True
        else:
            await _send_text(phone_number_id, to, "Kripya neeche diye button mein se ek choose karein 🙏")
            await _send_buttons(phone_number_id, to, _confirm_summary_text(collected), _as_buttons(CONFIRM_OPTIONS))
            return False

    valid_map = {
        "business_type": dict(BUSINESS_TYPES),
        "plan"          : dict(PLAN_OPTIONS),
        "billing_cycle" : dict(BILLING_OPTIONS),
    }.get(pending_choice, {})

    if interactive_id in valid_map:
        collected[pending_choice] = interactive_id
        session_ref.update({"collected": collected, "pending_choice": ""})
        await _advance(phone_number_id, to, session_ref, collected)
        return True
    else:
        await _send_text(phone_number_id, to, "Kripya neeche diye options mein se ek tap karein 🙏")
        await _resend_choice(phone_number_id, to, pending_choice)
        return False


async def _resend_choice(phone_number_id, to, choice) -> None:
    if choice == "business_type":
        await _send_list(
            phone_number_id, to, "Business Type",
            "Aapka business kis type ka hai?",
            "Choose Type", BUSINESS_TYPES,
        )
    elif choice == "plan":
        await _send_buttons(phone_number_id, to, _plan_comparison_text(), _as_buttons(PLAN_OPTIONS))
    elif choice == "billing_cycle":
        await _send_buttons(phone_number_id, to, "Billing cycle choose karein:", _as_buttons(BILLING_OPTIONS))


# ── Orchestration: decide what's still missing and ask for it ──────────────────

async def _advance(phone_number_id, to, session_ref, collected: dict) -> None:
    for field in AI_FIELDS:
        if not str(collected.get(field, "")).strip():
            return  # AI still collecting free-text fields; nothing more to do this turn

    email = str(collected.get("owner_email", "")).strip()
    if not EMAIL_PATTERN.match(email):
        collected["owner_email"] = ""
        session_ref.update({"collected": collected})
        await _send_text(
            phone_number_id, to,
            f"'{email}' ek valid email nahi lag raha. Kripya sahi email address bhejein 🙏"
        )
        return

    gst = str(collected.get("gst_number", "")).strip().upper()
    if gst and not GST_PATTERN.match(gst):
        collected["gst_number"] = ""
        session_ref.update({"collected": collected})
        await _send_text(
            phone_number_id, to,
            f"'{gst}' ek valid GST number nahi lag raha (15 characters hone chahiye, jaise "
            f"27AAPFU0939F1ZV). Kripya sahi GST number bhejein, ya 'skip' likhein 🙏"
        )
        return
    elif gst:
        collected["gst_number"] = gst  # store normalized (uppercase) form

    if not collected.get("business_type"):
        session_ref.update({"pending_choice": "business_type"})
        await _send_list(
            phone_number_id, to, "Business Type",
            "Bahut badhiya! Ab bataiye — aapka business kis type ka hai?",
            "Choose Type", BUSINESS_TYPES,
        )
        return

    if not collected.get("plan"):
        session_ref.update({"pending_choice": "plan"})
        await _send_buttons(phone_number_id, to, _plan_comparison_text(), _as_buttons(PLAN_OPTIONS))
        return

    if not collected.get("billing_cycle"):
        session_ref.update({"pending_choice": "billing_cycle"})
        await _send_buttons(phone_number_id, to, "Billing cycle choose karein:", _as_buttons(BILLING_OPTIONS))
        return

    if not collected.get("aadhaar_last4"):
        session_ref.update({"pending_choice": "upload_aadhaar"})
        await _send_text(
            phone_number_id, to,
            "KYC ke liye Aadhaar card ki photo bhejein 📄 (front side, saaf aur clear).\n"
            "Note: Hum sirf verification ke liye use karte hain — poora Aadhaar number save nahi hota, "
            "sirf last 4 digits."
        )
        return

    if not collected.get("pan_number"):
        session_ref.update({"pending_choice": "upload_pan"})
        await _send_text(phone_number_id, to, "Ab PAN card ki photo bhejein 📄 (saaf aur clear).")
        return

    # Everything present — final confirmation gate
    session_ref.update({"pending_choice": "confirm"})
    await _send_buttons(phone_number_id, to, _confirm_summary_text(collected), _as_buttons(CONFIRM_OPTIONS))


def _confirm_summary_text(collected: dict) -> str:
    business_type_label = dict(BUSINESS_TYPES).get(collected.get("business_type", ""), collected.get("business_type", ""))
    plan_label = dict(PLAN_OPTIONS).get(collected.get("plan", ""), collected.get("plan", ""))
    billing_label = dict(BILLING_OPTIONS).get(collected.get("billing_cycle", ""), collected.get("billing_cycle", ""))
    kyc_line = f"🪪 Aadhaar (last 4): {collected.get('aadhaar_last4','')} | PAN: {collected.get('pan_number','')}"
    if not collected.get("kyc_name_match", True):
        kyc_line += "\n⚠️ Naam thoda match nahi hua documents se — hamari team manually verify karegi."
    return (
        "Ek baar confirm kar lete hain:\n\n"
        f"👤 {collected.get('owner_name','')}\n"
        f"🏪 {collected.get('business_name','')} ({business_type_label})\n"
        f"📍 {collected.get('address','')}, {collected.get('city','')}\n"
        f"✉️ {collected.get('owner_email','')}\n"
        f"💳 {plan_label} — {billing_label}\n"
        f"{kyc_line}\n\n"
        "Sab sahi hai?"
    )


def _plan_comparison_text() -> str:
    basic_feats   = "\n".join(f"• {f}" for f in PLAN_FEATURES.get("basic", []))
    premium_feats = "\n".join(f"• {f}" for f in PLAN_FEATURES.get("premium", []))
    return (
        "Kaunsa plan chahiye? Dono ke features neeche hain:\n\n"
        f"*💠 Basic — ₹999/mo*\n{basic_feats}\n\n"
        f"*✨ Premium — ₹1999/mo*\n{premium_feats}"
    )


def _as_buttons(options: list[tuple[str, str]]) -> list[dict]:
    return [{"id": opt_id, "title": title} for opt_id, title in options]


def _validate_collected(collected: dict) -> bool:
    for field in ALL_REQUIRED_FOR_PAYMENT:
        if not str(collected.get(field, "")).strip():
            return False
    if collected.get("business_type") not in dict(BUSINESS_TYPES):
        return False
    if collected.get("plan") not in dict(PLAN_OPTIONS):
        return False
    if collected.get("billing_cycle") not in dict(BILLING_OPTIONS):
        return False
    email = str(collected.get("owner_email", "")).strip()
    if not EMAIL_PATTERN.match(email):
        return False
    gst = str(collected.get("gst_number", "")).strip().upper()
    if gst and not GST_PATTERN.match(gst):
        return False
    # KYC docs must have been processed (even if name mismatch — that's a
    # manual-review flag, not a hard block), but the fields must exist.
    if not str(collected.get("aadhaar_last4", "")).strip():
        return False
    if not str(collected.get("pan_number", "")).strip():
        return False
    return True


# ── Groq call (free-text fields only) ───────────────────────────────────────────

async def _call_groq_onboarding(collected: dict, history: list[dict]) -> dict:
    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY not configured.")
        return {
            "reply": "Maafi chahti hoon, abhi thodi technical dikkat aa rahi hai. Thodi der mein try karein. 🙏",
            "collected": collected,
        }

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(collected_json=json.dumps(collected, ensure_ascii=False))
    system_prompt += (
        "\n\nIMPORTANT: Reply with ONLY a raw JSON object, no markdown, no code fences, no preamble."
    )
    messages = [{"role": "system", "content": system_prompt}] + history

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type" : "application/json",
                },
                json={
                    "model"           : GROQ_MODEL,
                    "messages"        : messages,
                    "temperature"     : 0.4,
                    "max_tokens"      : 400,
                    "reasoning_effort": "none",  # Qwen3.6 thinks by default, wrapping output in <think> tags
                    # that break JSON parsing — disable it, we don't need chain-of-thought here.
                    # NOTE: response_format=json_object deliberately omitted — this
                    # model has returned 400 with JSON mode enabled. Relying on the
                    # prompt instruction + fence-stripping below instead.
                },
            )
            resp.raise_for_status()
            raw_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        error_detail = ""
        if hasattr(e, "response") and e.response is not None:
            try:
                error_detail = e.response.text[:500]
            except Exception:
                pass
        logger.error("Groq API error (onboarding): %s | response=%s", e, error_detail)
        return {
            "reply": "Maafi chahti hoon, abhi thodi technical dikkat aa rahi hai. Thodi der mein try karein. 🙏",
            "collected": collected,
        }

    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        logger.error("Groq onboarding JSON parse failed: %s | raw=%s", e, raw_text[:300])
        return {
            "reply": "Sorry, samajh nahi paayi. Ek baar phir bata sakte hain? 🙏",
            "collected": collected,
        }

    merged = {**collected, **(parsed.get("collected") or {})}
    parsed["collected"] = merged
    return parsed


# ── Finalize: create tenant + Razorpay payment link ────────────────────────────

async def _finalize_and_send_payment_link(phone_number_id: str, to: str, collected: dict) -> None:
    from routers.onboard import (
        OnboardRequest, BusinessType, PlanTier, BillingCycle,
        _create_pending_vendor_core,
    )

    owner_phone = to if to.startswith("+") else f"+{to}"
    phone_digits = re.sub(r"\D", "", owner_phone)
    if not PHONE_DIGITS_PATTERN.match(phone_digits):
        logger.error("Onboarding finalize: invalid phone digits: %s", phone_digits)
        await _send_text(
            phone_number_id, to,
            "Maafi chahti hoon, aapka phone number format sahi nahi lag raha. "
            "Support se contact karein: support@saarthi-ai.in 🙏"
        )
        return

    try:
        body = OnboardRequest(
            business_name     = collected["business_name"],
            owner_name        = collected["owner_name"],
            owner_phone       = owner_phone,
            owner_email       = collected["owner_email"],
            business_type     = BusinessType(collected["business_type"]),
            city              = collected.get("city", ""),
            address           = collected["address"],
            plan              = PlanTier(collected["plan"]),
            billing_cycle     = BillingCycle(collected["billing_cycle"]),
            whatsapp_phone_id = "",  # set later by client via /connect-whatsapp
            aadhaar_last4     = collected.get("aadhaar_last4", ""),
            pan_number        = collected.get("pan_number", ""),
            kyc_name_match    = collected.get("kyc_name_match", True),
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

    db = get_db()
    db.collection(ONBOARDING_SESSIONS).document(to).update({
        "client_id": result.client_id,
        "status"   : "completed",
    })

    await _send_text(
        phone_number_id, to,
        f"Shaandar, {collected['owner_name']}! 🎉\n\n"
        f"*{collected['business_name']}* ({collected['business_type'].title()}) "
        f"register ho gaya hai.\n\n"
        f"Payment complete karein activate karne ke liye:\n"
        f"👉 {result.payment_link}\n\n"
        f"Payment ke baad aapka QR code aur bill WhatsApp pe milega. 🙏"
    )

    if COMPANY_ADMIN_PHONE:
        kyc_flag = "\n⚠️ KYC NAME MISMATCH — please review manually." if not collected.get("kyc_name_match", True) else ""
        await _send_text(
            phone_number_id, COMPANY_ADMIN_PHONE,
            f"🆕 New client started onboarding:\n"
            f"{collected['business_name']} ({collected['business_type']})\n"
            f"Owner: {collected['owner_name']} | {owner_phone}\n"
            f"Aadhaar (last4): {collected.get('aadhaar_last4','')} | PAN: {collected.get('pan_number','')}"
            f"{kyc_flag}\n"
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
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Onboarding bot send (text) failed → %s: %s", to, e)


async def _send_buttons(phone_number_id: str, to: str, body_text: str, buttons: list[dict]) -> None:
    """buttons: list of {"id","title"} — Meta allows max 3 per message."""
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
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Onboarding bot send (buttons) failed → %s: %s", to, e)


async def _send_list(phone_number_id: str, to: str, header: str, body_text: str,
                      button_label: str, options: list[tuple[str, str]]) -> None:
    """
    WhatsApp 'list' message — supports up to 10 rows in one message, unlike
    buttons which cap at 3. Used for business_type (5 options).
    """
    url = f"https://graph.facebook.com/{META_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": header},
            "body": {"text": body_text},
            "action": {
                "button": button_label,
                "sections": [{
                    "title": header,
                    "rows": [{"id": opt_id, "title": title} for opt_id, title in options],
                }],
            },
        },
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Onboarding bot send (list) failed → %s: %s", to, e)