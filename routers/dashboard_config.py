"""
routers/dashboard_config.py
Serves the vendor dashboard its business profile + vertical-specific UI config.

dashboard.html calls GET /api/v1/dashboard-config?client_id=... on load and
expects a shape like:

{
  "business_name": "...",
  "city": "...",
  "plan": "basic",
  "status": "active",
  "qr_code_url": "...",
  "whatsapp_phone_id": "...",
  "whatsapp_business_number": "...",
  "default_categories": [...],
  "config": {
      "display_name": "Salon / Barbershop",
      "icon": "💇",
      "service_label": "Services",
      "service_label_singular": "Service",
      "staff_label": "Staff",
      "staff_label_singular": "Staff Member",
      "slot_label": "Schedule",
      "slot_screen_type": "staff_grid" | "table_grid",
      "booking_noun": "Appointment"
  }
}
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_db, Collections
from routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Dashboard"])


# ── Vertical-specific UI config (mirrors onboard.py's BUSINESS_TYPE_DEFAULTS) ──

VERTICAL_UI_CONFIG = {
    "salon": {
        "display_name": "Salon / Barbershop",
        "icon": "💇",
        "service_label": "Services",
        "service_label_singular": "Service",
        "staff_label": "Staff",
        "staff_label_singular": "Staff Member",
        "slot_label": "Schedule",
        "slot_screen_type": "staff_grid",
        "booking_noun": "Appointment",
    },
    "parlour": {
        "display_name": "Beauty Parlour",
        "icon": "💅",
        "service_label": "Services",
        "service_label_singular": "Service",
        "staff_label": "Staff",
        "staff_label_singular": "Staff Member",
        "slot_label": "Schedule",
        "slot_screen_type": "staff_grid",
        "booking_noun": "Appointment",
    },
    "clinic": {
        "display_name": "Clinic",
        "icon": "🩺",
        "service_label": "Treatments",
        "service_label_singular": "Treatment",
        "staff_label": "Doctors",
        "staff_label_singular": "Doctor",
        "slot_label": "Schedule",
        "slot_screen_type": "staff_grid",
        "booking_noun": "Appointment",
    },
    "cafe": {
        "display_name": "Café",
        "icon": "☕",
        "service_label": "Menu Items",
        "service_label_singular": "Menu Item",
        "staff_label": "Tables",
        "staff_label_singular": "Table",
        "slot_label": "Tables",
        "slot_screen_type": "table_grid",
        "booking_noun": "Order",
    },
    "restaurant": {
        "display_name": "Restaurant",
        "icon": "🍽️",
        "service_label": "Menu Items",
        "service_label_singular": "Menu Item",
        "staff_label": "Tables",
        "staff_label_singular": "Table",
        "slot_label": "Tables",
        "slot_screen_type": "table_grid",
        "booking_noun": "Reservation",
    },
}

DEFAULT_VERTICAL_CONFIG = VERTICAL_UI_CONFIG["salon"]


@router.get("/dashboard-config")
async def get_dashboard_config(
    client_id: str = Query(..., description="Client (tenant) ID"),
    current_user: dict = Depends(get_current_user),
):
    """
    Returns the vendor's business profile + vertical-specific UI labels for
    the dashboard shell (sidebar labels, slot-grid mode, QR code, etc.)
    """
    # ── Tenant isolation: token's client_id must match the requested one ──────
    if current_user.get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="Not authorized for this client.")

    db = get_db()
    client_doc = db.collection(Collections.CLIENTS).document(client_id).get()

    if not client_doc.exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    client_data = client_doc.to_dict()
    business_type = client_data.get("business_type", "salon")
    vertical_cfg = VERTICAL_UI_CONFIG.get(business_type, DEFAULT_VERTICAL_CONFIG)

    return {
        "business_name"            : client_data.get("business_name", ""),
        "city"                     : client_data.get("city", ""),
        "plan"                     : client_data.get("plan", ""),
        "status"                   : client_data.get("status", ""),
        "qr_code_url"              : client_data.get("qr_code_url", ""),
        "whatsapp_phone_id"        : client_data.get("whatsapp_phone_id", ""),
        "whatsapp_business_number" : client_data.get("whatsapp_business_number", ""),
        "default_categories"       : client_data.get("default_categories", []),
        "config"                   : vertical_cfg,
    }