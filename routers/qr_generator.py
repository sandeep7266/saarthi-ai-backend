"""
utils/qr_generator.py
Generates a customer-facing WhatsApp QR code for a client's business number.
Scanning the QR opens WhatsApp with a pre-filled greeting, pointed at the
client's OWN WhatsApp Business number (whatsapp_phone_id's linked phone).

Uploaded to Cloudinary (same free-tier pattern as invoices).
"""

import io
import logging
import os

import qrcode
import cloudinary
import cloudinary.uploader
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key    = os.getenv("CLOUDINARY_API_KEY",    ""),
    api_secret = os.getenv("CLOUDINARY_API_SECRET", ""),
    secure     = True,
)

BRAND_PURPLE = (124, 58, 237)
BRAND_DARK   = (26, 21, 37)
WHITE        = (255, 255, 255)


def generate_client_qr(
    client_id: str,
    business_name: str,
    whatsapp_number: str,   # full E.164 number, e.g. "+919876500001"
) -> str:
    """
    Builds a branded QR code PNG that deep-links to wa.me/<number> with a
    pre-filled "Hi" greeting, uploads it to Cloudinary, and returns the URL.

    whatsapp_number must be the CLIENT's own business WhatsApp number
    (the one their customers will message) — not the company's number.
    """
    clean_number = whatsapp_number.replace("+", "").replace(" ", "").replace("-", "")
    wa_link = f"https://wa.me/{clean_number}?text=Hi"

    # ── Build QR code ────────────────────────────────────────────────────────
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=2,
    )
    qr.add_data(wa_link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=BRAND_DARK, back_color=WHITE).convert("RGB")

    # ── Compose branded card around the QR (logo strip + business name) ──────
    qr_w, qr_h = qr_img.size
    padding_top    = 90
    padding_bottom = 70
    padding_side   = 40

    card_w = qr_w + (padding_side * 2)
    card_h = qr_h + padding_top + padding_bottom

    card = Image.new("RGB", (card_w, card_h), WHITE)
    draw = ImageDraw.Draw(card)

    # Header band
    draw.rectangle([0, 0, card_w, padding_top - 20], fill=BRAND_PURPLE)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_sub   = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        font_brand = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except Exception:
        font_title = ImageFont.load_default()
        font_sub   = ImageFont.load_default()
        font_brand = ImageFont.load_default()

    # "Scan to chat" header text
    header_text = "Scan to Book on WhatsApp"
    bbox = draw.textbbox((0, 0), header_text, font=font_sub)
    text_w = bbox[2] - bbox[0]
    draw.text(((card_w - text_w) / 2, 18), header_text, fill=WHITE, font=font_sub)

    # Paste QR
    card.paste(qr_img, (padding_side, padding_top))

    # Business name below QR
    bbox2 = draw.textbbox((0, 0), business_name, font=font_title)
    name_w = bbox2[2] - bbox2[0]
    draw.text(
        ((card_w - name_w) / 2, padding_top + qr_h + 12),
        business_name,
        fill=BRAND_DARK,
        font=font_title,
    )

    # "Powered by Saarthi-AI" footer
    footer_text = "Powered by Saarthi-AI"
    bbox3 = draw.textbbox((0, 0), footer_text, font=font_brand)
    footer_w = bbox3[2] - bbox3[0]
    draw.text(
        ((card_w - footer_w) / 2, card_h - 32),
        footer_text,
        fill=(150, 150, 150),
        font=font_brand,
    )

    # ── Upload to Cloudinary ──────────────────────────────────────────────────
    buffer = io.BytesIO()
    card.save(buffer, format="PNG")
    buffer.seek(0)

    public_id = f"saarthi-ai/qr-codes/{client_id}"

    try:
        result = cloudinary.uploader.upload(
            buffer,
            public_id     = public_id,
            resource_type = "image",
            format        = "png",
            overwrite     = True,
            access_mode   = "public",
        )
        url = result.get("secure_url", "")
        logger.info("QR code generated for client %s: %s", client_id, url)
        return url
    except Exception as e:
        logger.error("QR code upload failed (%s): %s", client_id, e)
        return ""