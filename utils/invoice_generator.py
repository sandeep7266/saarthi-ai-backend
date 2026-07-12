"""
utils/invoice_generator.py
ReportLab PDF invoice generator — uploads to Cloudinary (free tier).
No Firebase Storage upgrade needed.
"""

import io
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.uploader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

logger = logging.getLogger(__name__)

# ── Cloudinary config (loaded from env) ───────────────────────────────────────
cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key    = os.getenv("CLOUDINARY_API_KEY",    ""),
    api_secret = os.getenv("CLOUDINARY_API_SECRET", ""),
    secure     = True,
)

# ── Brand colors ──────────────────────────────────────────────────────────────
BRAND_PURPLE = colors.HexColor("#7C3AED")
IST = ZoneInfo("Asia/Kolkata")
BRAND_DARK   = colors.HexColor("#1E1B4B")
LIGHT_GRAY   = colors.HexColor("#F8F7FF")
MID_GRAY     = colors.HexColor("#E5E7EB")
TEXT_DARK    = colors.HexColor("#111827")
TEXT_GRAY    = colors.HexColor("#6B7280")

# ── Plan features shown on the subscription invoice ────────────────────────────
# NOTE: adjust this list to match your actual plan benefits — these are
# reasonable placeholders based on max_daily_bookings used elsewhere.
PLAN_FEATURES = {
    "basic": [
        "WhatsApp AI booking bot (unlimited customer chats)",
        "Up to 20 bookings/day",
        "Razorpay payment collection & auto-confirmation",
        "Customer-facing QR code",
        "Vendor dashboard access",
        "Email support",
    ],
    "premium": [
        "Everything in Basic, plus:",
        "Up to 50 bookings/day",
        "Priority WhatsApp response speed",
        "Custom branding on QR code & invoices",
        "Booking analytics dashboard",
        "Priority support (faster response time)",
    ],
}


# ── Public entry points ────────────────────────────────────────────────────────

def generate_booking_invoice(
    booking_id  : str,
    booking_data: dict,
    client_data : dict,
    payment_id  : str,
) -> str:
    """
    Generate B2C booking PDF → upload to Cloudinary → return public URL.
    """
    buffer = io.BytesIO()
    _build_booking_pdf(buffer, booking_id, booking_data, client_data, payment_id)
    buffer.seek(0)

    folder    = f"saarthi-ai/invoices/{client_data.get('business_name','client').replace(' ','_')}"
    public_id = f"{folder}/booking_{booking_id}"

    url = _upload_to_cloudinary(buffer, public_id)
    logger.info("Booking invoice uploaded: %s", public_id)
    return url


def generate_subscription_invoice(
    client_id  : str,
    client_data: dict,
    payment_id : str,
    amount     : int,   # paise
) -> str:
    """
    Generate B2B subscription PDF → upload to Cloudinary → return public URL.
    """
    buffer = io.BytesIO()
    _build_subscription_pdf(buffer, client_id, client_data, payment_id, amount)
    buffer.seek(0)

    public_id = f"saarthi-ai/subscriptions/{client_id}/sub_{payment_id}"
    url = _upload_to_cloudinary(buffer, public_id)
    logger.info("Subscription invoice uploaded: %s", public_id)
    return url


# ── Cloudinary upload ──────────────────────────────────────────────────────────

def _upload_to_cloudinary(buffer: io.BytesIO, public_id: str) -> str:
    """Upload PDF buffer to Cloudinary. Returns secure URL."""
    if not cloudinary.config().cloud_name:
        logger.warning("Cloudinary not configured — invoice URL will be empty.")
        return ""

    try:
        result = cloudinary.uploader.upload(
            buffer,
            public_id      = public_id,
            resource_type  = "raw",       # PDF is a raw file
            format         = "pdf",
            overwrite      = True,
            access_mode    = "public",    # Direct URL accessible
        )
        return result.get("secure_url", "")
    except Exception as e:
        logger.error("Cloudinary upload failed (%s): %s", public_id, e)
        return ""


# ── B2C Booking Invoice PDF ───────────────────────────────────────────────────

def _build_booking_pdf(
    buffer      : io.BytesIO,
    booking_id  : str,
    booking     : dict,
    client      : dict,
    payment_id  : str,
) -> None:

    doc   = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )
    story = []
    now   = datetime.now(timezone.utc)
    W, _  = A4

    # Styles
    brand_s  = ParagraphStyle("brand", fontName="Helvetica-Bold",
                               fontSize=22, leading=26, textColor=BRAND_PURPLE)
    tag_s    = ParagraphStyle("tag",   fontName="Helvetica",
                               fontSize=9,  leading=13, textColor=TEXT_GRAY)
    inv_s    = ParagraphStyle("inv",   fontName="Helvetica-Bold",
                               fontSize=14, textColor=BRAND_DARK,
                               alignment=TA_RIGHT)
    meta_v   = ParagraphStyle("mv",    fontName="Helvetica-Bold",
                               fontSize=9,  textColor=TEXT_DARK)
    meta_l   = ParagraphStyle("ml",    fontName="Helvetica",
                               fontSize=9,  textColor=TEXT_GRAY)
    hdr_s    = ParagraphStyle("th",    fontName="Helvetica-Bold",
                               fontSize=9,  textColor=colors.white,
                               alignment=TA_CENTER)
    cell_s   = ParagraphStyle("td",    fontName="Helvetica",
                               fontSize=9,  textColor=TEXT_DARK)
    num_s    = ParagraphStyle("num",   fontName="Helvetica",
                               fontSize=9,  textColor=TEXT_DARK,
                               alignment=TA_RIGHT)
    lbl_s    = ParagraphStyle("lbl",   fontName="Helvetica",
                               fontSize=9,  textColor=TEXT_GRAY,
                               alignment=TA_RIGHT)
    amt_s    = ParagraphStyle("amt",   fontName="Helvetica",
                               fontSize=9,  textColor=TEXT_DARK,
                               alignment=TA_RIGHT)
    tot_l    = ParagraphStyle("tl",    fontName="Helvetica-Bold",
                               fontSize=11, textColor=colors.white,
                               alignment=TA_RIGHT)
    tot_a    = ParagraphStyle("ta",    fontName="Helvetica-Bold",
                               fontSize=11, textColor=colors.white,
                               alignment=TA_RIGHT)
    foot_s   = ParagraphStyle("ft",    fontName="Helvetica",
                               fontSize=8,  textColor=TEXT_GRAY,
                               alignment=TA_CENTER)

    # Header row
    invoice_no  = f"INV-{booking_id}-{now.strftime('%Y%m')}"
    invoice_date= now.strftime("%d %b %Y")
    biz_name    = client.get("business_name", "")
    biz_addr    = client.get("address", "")
    biz_city    = client.get("city", "")

    hdr_data = [[
        Paragraph("⬡ Saarthi-AI", brand_s),
        Paragraph("BOOKING INVOICE", inv_s),
    ]]
    hdr_tbl  = Table(hdr_data, colWidths=[(W-40*mm)*0.6, (W-40*mm)*0.4])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(hdr_tbl)
    story.append(Paragraph("AI-Powered Business Automation", tag_s))
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND_PURPLE))
    story.append(Spacer(1, 4*mm))

    # Meta info
    meta_data = [
        [Paragraph(f"<b>{biz_name}</b>", meta_v),
         Paragraph("Invoice No:", meta_l),
         Paragraph(invoice_no, meta_v)],
        [Paragraph(f"{biz_addr}, {biz_city}", meta_l),
         Paragraph("Date:", meta_l),
         Paragraph(invoice_date, meta_v)],
        [Paragraph(f"Type: {client.get('business_type','').title()}", meta_l),
         Paragraph("Payment ID:", meta_l),
         Paragraph(payment_id or "—", meta_l)],
    ]
    meta_tbl = Table(meta_data, colWidths=[80*mm, 35*mm, 55*mm])
    meta_tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 6*mm))

    # Customer box
    cust_data = [[
        Paragraph("<b>BILL TO</b>",
                  ParagraphStyle("bh", fontName="Helvetica-Bold",
                                 fontSize=8, textColor=colors.white)),
    ],[
        Paragraph(
            f"<b>{booking.get('customer_phone','')}</b><br/>"
            f"Booking ID: <b>{booking_id}</b>",
            ParagraphStyle("cd", fontName="Helvetica",
                           fontSize=9, textColor=TEXT_DARK)),
    ]]
    cust_tbl = Table(cust_data, colWidths=[170*mm])
    cust_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BRAND_PURPLE),
        ("BACKGROUND",    (0,1), (-1,1), LIGHT_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ]))
    story.append(cust_tbl)
    story.append(Spacer(1, 6*mm))

    # Line items
    service_price  = booking.get("service_price", 0)
    deposit_amount = booking.get("deposit_amount", 0)
    balance_due    = service_price - deposit_amount
    slot_dt        = booking.get("slot_datetime", "")
    if hasattr(slot_dt, "astimezone"):
        slot_dt = slot_dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p")

    items_data = [
        [Paragraph("Description", hdr_s),
         Paragraph("Staff", hdr_s),
         Paragraph("Date & Time", hdr_s),
         Paragraph("Amount (Rs.)", hdr_s)],
        [Paragraph(booking.get("service_name", "—"), cell_s),
         Paragraph(booking.get("staff_name",   "—"), cell_s),
         Paragraph(str(slot_dt),                      cell_s),
         Paragraph(f"Rs. {service_price:,.0f}",           num_s)],
    ]
    items_tbl = Table(items_data, colWidths=[70*mm, 35*mm, 45*mm, 20*mm])
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BRAND_DARK),
        ("BACKGROUND",    (0,1), (-1,1), colors.white),
        ("GRID",          (0,0), (-1,-1), 0.5, MID_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 4*mm))

    # Summary
    summary_data = [
        [Paragraph("Subtotal:",     lbl_s), Paragraph(f"Rs. {service_price:,.0f}",  amt_s)],
        [Paragraph("Deposit Paid:", lbl_s), Paragraph(f"- Rs. {deposit_amount:,.0f}", amt_s)],
        [Paragraph("Balance Due:",  tot_l), Paragraph(f"Rs. {balance_due:,.0f}",    tot_a)],
    ]
    sum_tbl = Table(summary_data, colWidths=[140*mm, 30*mm])
    sum_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,2), (-1,2), BRAND_PURPLE),
        ("TOPPADDING",  (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
        ("RIGHTPADDING",(0,0), (-1,-1), 8),
        ("LINEABOVE",   (0,2), (-1,2), 1, BRAND_PURPLE),
    ]))
    story.append(sum_tbl)
    story.append(Spacer(1, 8*mm))

    # Footer
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "Thank you for choosing Saarthi-AI • support@saarthi-ai.in • www.saarthi-ai.in<br/>"
        "This is a computer-generated invoice and does not require a signature.",
        foot_s,
    ))
    doc.build(story)


# ── B2B Subscription Invoice PDF ──────────────────────────────────────────────

def _build_subscription_pdf(
    buffer      : io.BytesIO,
    client_id   : str,
    client      : dict,
    payment_id  : str,
    amount_paise: int,
) -> None:

    doc   = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=20*mm, leftMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm,
    )
    story = []
    now   = datetime.now(timezone.utc)
    W, _  = A4
    amount_inr = amount_paise / 100

    brand_s = ParagraphStyle("b",  fontName="Helvetica-Bold",
                              fontSize=22, leading=26, textColor=BRAND_PURPLE)
    tag_s   = ParagraphStyle("t",  fontName="Helvetica",
                              fontSize=9,  leading=13, textColor=TEXT_GRAY)
    v_s     = ParagraphStyle("v",  fontName="Helvetica-Bold",
                              fontSize=9,  leading=13, textColor=TEXT_DARK)
    l_s     = ParagraphStyle("l",  fontName="Helvetica",
                              fontSize=9,  leading=13, textColor=TEXT_GRAY)
    hdr_s   = ParagraphStyle("h",  fontName="Helvetica-Bold",
                              fontSize=9,  leading=13, textColor=colors.white)
    td_s    = ParagraphStyle("d",  fontName="Helvetica",
                              fontSize=9,  leading=13, textColor=TEXT_DARK)
    nr_s    = ParagraphStyle("n",  fontName="Helvetica",
                              fontSize=9,  leading=13, textColor=TEXT_DARK,
                              alignment=TA_RIGHT)
    foot_s  = ParagraphStyle("f",  fontName="Helvetica",
                              fontSize=8,  leading=12, textColor=TEXT_GRAY,
                              alignment=TA_CENTER)

    story.append(Paragraph("⬡ Saarthi-AI", brand_s))
    story.append(Spacer(1, 1*mm))
    story.append(Paragraph("AI-Powered Business Automation Platform", tag_s))
    story.append(Spacer(1, 3*mm))
    story.append(HRFlowable(width="100%", thickness=2, color=BRAND_PURPLE))
    story.append(Spacer(1, 5*mm))

    invoice_no  = f"SUB-{client_id[:6].upper()}-{now.strftime('%Y%m')}"
    plan        = client.get("plan",          "basic")
    cycle       = client.get("billing_cycle", "monthly")
    sub_end     = client.get("subscription_end_date")
    sub_end_str = sub_end.strftime("%d %b %Y") if sub_end else "—"

    story.append(Table([
        [Paragraph(f"<b>{client.get('business_name','')}</b>", v_s),
         Paragraph("Invoice No:",   l_s), Paragraph(invoice_no,             v_s)],
        [Paragraph(client.get("owner_name",  ""), l_s),
         Paragraph("Invoice Date:", l_s), Paragraph(now.strftime("%d %b %Y"), v_s)],
        [Paragraph(client.get("owner_email", ""), l_s),
         Paragraph("Valid Until:", l_s),  Paragraph(sub_end_str,              v_s)],
        [Paragraph(client.get("city",        ""), l_s),
         Paragraph("Payment ID:",  l_s),  Paragraph(payment_id or "—",        l_s)],
    ], colWidths=[80*mm, 40*mm, 50*mm]))
    story.append(Spacer(1, 6*mm))

    gst_rate  = 0.18
    base_amt  = round(amount_inr / (1 + gst_rate), 2)
    gst_amt   = round(amount_inr - base_amt, 2)

    items_tbl = Table([
        [Paragraph("Description", hdr_s), Paragraph("Plan",   hdr_s),
         Paragraph("Period",      hdr_s), Paragraph("Amount (Rs.)", hdr_s)],
        [Paragraph("Saarthi-AI Subscription", td_s), Paragraph(plan.title(), td_s),
         Paragraph(cycle.title(), td_s), Paragraph(f"Rs. {base_amt:,.2f}", nr_s)],
        [Paragraph("GST @ 18%",   td_s), Paragraph("", td_s),
         Paragraph("",            td_s), Paragraph(f"Rs. {gst_amt:,.2f}", nr_s)],
    ], colWidths=[80*mm, 30*mm, 30*mm, 30*mm])
    items_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), BRAND_DARK),
        ("GRID",          (0,0), (-1,-1), 0.5, MID_GRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ]))
    story.append(items_tbl)
    story.append(Spacer(1, 3*mm))

    tot_l = ParagraphStyle("tl", fontName="Helvetica-Bold",
                            fontSize=12, leading=15, textColor=colors.white, alignment=TA_RIGHT)
    tot_tbl = Table([[
        Paragraph("TOTAL AMOUNT:", tot_l),
        Paragraph(f"Rs. {amount_inr:,.2f}", tot_l),
    ]], colWidths=[140*mm, 30*mm])
    tot_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), BRAND_PURPLE),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ]))
    story.append(tot_tbl)
    story.append(Spacer(1, 8*mm))

    # ── Plan features (what's included) ─────────────────────────────────────────
    feat_title_s = ParagraphStyle("ftt", fontName="Helvetica-Bold",
                                   fontSize=11, leading=14, textColor=BRAND_DARK)
    feat_item_s  = ParagraphStyle("fti", fontName="Helvetica",
                                   fontSize=9,  leading=14, textColor=TEXT_DARK,
                                   leftIndent=4*mm, bulletIndent=0)

    features = PLAN_FEATURES.get(plan, [])
    if features:
        story.append(Paragraph(f"What's included — {plan.title()} Plan", feat_title_s))
        story.append(Spacer(1, 2*mm))
        for feat in features:
            story.append(Paragraph(f"✓ {feat}", feat_item_s))
        story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MID_GRAY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        "Saarthi-AI | GSTIN: 20ABCDE1234F1Z5 | support@saarthi-ai.in | www.saarthi-ai.in<br/>"
        "Computer-generated invoice — no signature required.",
        foot_s,
    ))
    doc.build(story)