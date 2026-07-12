"""
routers/bookings_api.py
Missing CRUD endpoints for bookings, slots, analytics — called by Flutter app.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from database import get_db, Collections
from routers.auth import get_current_user, require_admin, require_active_tenant
from utils.rate_limiter import limiter, LIMIT_NORMAL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Bookings & Slots"])


# ── Bookings list ──────────────────────────────────────────────────────────────

@router.get("/bookings")
async def get_bookings(
    client_id : str          = Query(...),
    date      : Optional[str]= Query(None),   # YYYY-MM-DD
    start_date: Optional[str]= Query(None),
    end_date  : Optional[str]= Query(None),
    current_user: dict       = Depends(require_active_tenant),
):
    """
    Get bookings for a client.
    Staff see all bookings (no financial filter at API level — Flutter handles display).
    Filtering is by single date OR date range.
    """
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
    )

    # Build date filter
    if date:
        try:
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_end   = day_start + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
        query = (
            ref.where("slot_datetime", ">=", day_start)
               .where("slot_datetime", "<",  day_end)
               .order_by("slot_datetime")
        )
    elif start_date and end_date:
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end   = datetime.strptime(end_date,   "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD")
        query = (
            ref.where("slot_datetime", ">=", start)
               .where("slot_datetime", "<",  end)
               .order_by("slot_datetime")
        )
    else:
        query = ref.order_by("slot_datetime", direction="DESCENDING").limit(50)

    docs     = query.get()
    bookings = []
    for doc in docs:
        data              = doc.to_dict()
        data["booking_id"]= doc.id
        # Serialize datetime
        if hasattr(data.get("slot_datetime"), "isoformat"):
            data["slot_datetime"] = data["slot_datetime"].isoformat()
        bookings.append(data)

    return {"bookings": bookings, "count": len(bookings)}


@router.patch("/bookings/{booking_id}")
async def update_booking_status(
    booking_id  : str,
    client_id   : str   = Query(...),
    body        : dict  = Body(...),
    current_user: dict  = Depends(require_active_tenant),
):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    allowed_statuses = {"confirmed", "cancelled", "no_show", "completed"}
    new_status = body.get("status", "")
    if new_status not in allowed_statuses:
        raise HTTPException(status_code=422, detail=f"status must be one of {allowed_statuses}")

    db  = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .document(booking_id)
    )
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Booking not found.")

    ref.update({"status": new_status, "updated_at": datetime.now(timezone.utc)})
    logger.info("Booking %s status → %s by %s", booking_id, new_status, current_user["sub"])
    return {"message": "Booking updated.", "booking_id": booking_id, "status": new_status}


# ── Slots ──────────────────────────────────────────────────────────────────────

class SlotCreateRequest(BaseModel):
    staff_name   : str
    slot_datetime: str   # ISO 8601
    duration_min : int = 30

class SlotsCreateBulkRequest(BaseModel):
    slots: list[SlotCreateRequest]


@router.get("/slots")
async def get_slots(
    client_id   : str          = Query(...),
    date        : Optional[str]= Query(None),
    current_user: dict         = Depends(require_active_tenant),
):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
    )

    if date:
        try:
            day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day_end   = day_start + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
        docs = (
            ref.where("slot_datetime", ">=", day_start)
               .where("slot_datetime", "<",  day_end)
               .order_by("slot_datetime")
               .get()
        )
    else:
        now  = datetime.now(timezone.utc)
        docs = (
            ref.where("slot_datetime", ">=", now)
               .order_by("slot_datetime")
               .limit(50)
               .get()
        )

    slots = []
    for doc in docs:
        data = doc.to_dict()
        data["slot_id"] = doc.id
        if hasattr(data.get("slot_datetime"), "isoformat"):
            data["slot_datetime"] = data["slot_datetime"].isoformat()
        slots.append(data)

    return {"slots": slots, "count": len(slots)}


# ── Recurring Schedule Template (auto slot generation) ─────────────────────────

class ScheduleTemplateRequest(BaseModel):
    enabled: bool
    slot_duration_min: int = 30
    open_time: str   # "HH:MM", 24h
    close_time: str  # "HH:MM", 24h
    open_days: list[str]  # subset of ["mon","tue","wed","thu","fri","sat","sun"]
    staff: list[str] = []  # empty = no staff assignment (solo business)


@router.get("/schedule-template")
async def get_schedule_template(
    client_id: str = Query(...),
    current_user: dict = Depends(require_active_tenant),
):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db = get_db()
    doc = db.collection(Collections.CLIENTS).document(client_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    return {"schedule_template": doc.to_dict().get("schedule_template")}


@router.put("/schedule-template")
async def save_schedule_template(
    body: ScheduleTemplateRequest,
    client_id: str = Query(...),
    admin: dict = Depends(require_admin),
):
    """
    Saves the client's recurring weekly schedule and immediately backfills
    the next 14 days of slots. From then on, a daily cron job keeps the
    rolling window full automatically — no manual slot creation needed.
    """
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    valid_days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    bad_days = set(body.open_days) - valid_days
    if bad_days:
        raise HTTPException(status_code=422, detail=f"Invalid open_days: {bad_days}")

    try:
        from utils.slot_generator import _parse_hhmm
        _parse_hhmm(body.open_time)
        _parse_hhmm(body.close_time)
    except Exception:
        raise HTTPException(status_code=422, detail="open_time/close_time must be HH:MM.")

    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    if not client_ref.get().exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    template = body.dict()
    client_ref.update({"schedule_template": template})

    created = 0
    if body.enabled:
        from utils.slot_generator import bootstrap_slots_for_client
        created = bootstrap_slots_for_client(client_id)

    return {
        "success": True,
        "schedule_template": template,
        "slots_created": created,
    }


@router.post("/slots", status_code=201)
async def create_slots(
    client_id   : str                  = Query(...),
    body        : SlotsCreateBulkRequest = ...,
    admin       : dict                 = Depends(require_admin),
):
    """Admin-only: bulk create availability slots."""
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db      = get_db()
    batch   = db.batch()
    now     = datetime.now(timezone.utc)
    created = []

    for slot in body.slots:
        try:
            slot_dt = datetime.fromisoformat(slot.slot_datetime).replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid datetime: {slot.slot_datetime}")

        ref = (
            db.collection(Collections.CLIENTS)
            .document(client_id)
            .collection(Collections.SLOTS)
            .document()
        )
        slot_doc = {
            "staff_name"  : slot.staff_name,
            "slot_datetime": slot_dt,
            "duration_min": slot.duration_min,
            "status"      : "available",
            "booking_id"  : None,
            "created_at"  : now,
            "updated_at"  : now,
        }
        batch.set(ref, slot_doc)
        created.append(ref.id)

    batch.commit()
    logger.info("Created %d slots for client %s", len(created), client_id)
    return {"message": f"{len(created)} slots created.", "slot_ids": created}


@router.delete("/slots/{slot_id}")
async def delete_slot(
    slot_id    : str,
    client_id  : str  = Query(...),
    admin      : dict = Depends(require_admin),
):
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SLOTS)
        .document(slot_id)
    )
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Slot not found.")
    if doc.to_dict().get("status") != "available":
        raise HTTPException(status_code=409, detail="Cannot delete a slot that is booked or pending.")

    ref.delete()
    return {"message": "Slot deleted.", "slot_id": slot_id}


# ── Analytics ──────────────────────────────────────────────────────────────────

@router.get("/analytics")
async def get_analytics(
    client_id   : str = Query(...),
    period      : str = Query("week"),  # week | month | year
    admin       : dict= Depends(require_admin),
):
    """Admin-only revenue analytics aggregated from Firestore bookings."""
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    now = datetime.now(timezone.utc)
    if period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    elif period == "year":
        start = now - timedelta(days=365)
    else:
        raise HTTPException(status_code=422, detail="period must be week, month, or year")

    db   = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .where("status", "==", "confirmed")
        .where("slot_datetime", ">=", start)
        .order_by("slot_datetime")
        .get()
    )

    # Aggregate by day
    daily: dict[str, dict] = {}
    staff_map: dict[str, dict] = {}
    total_revenue  = 0
    total_bookings = 0

    for doc in docs:
        data       = doc.to_dict()
        slot_dt    = data.get("slot_datetime")
        day_key    = slot_dt.strftime("%Y-%m-%d") if hasattr(slot_dt, "strftime") else str(slot_dt)[:10]
        price      = int(data.get("service_price", 0))
        staff_name = data.get("staff_name", "Unknown")

        # Daily aggregation
        if day_key not in daily:
            daily[day_key] = {"date": day_key, "total_revenue": 0, "booking_count": 0, "deposit_collected": 0}
        daily[day_key]["total_revenue"]     += price
        daily[day_key]["booking_count"]     += 1
        daily[day_key]["deposit_collected"] += int(data.get("deposit_amount", 0))

        # Staff aggregation
        if staff_name not in staff_map:
            staff_map[staff_name] = {"staff_name": staff_name, "booking_count": 0, "revenue": 0}
        staff_map[staff_name]["booking_count"] += 1
        staff_map[staff_name]["revenue"]       += price

        total_revenue  += price
        total_bookings += 1

    # Sort staff by revenue desc
    staff_list = sorted(staff_map.values(), key=lambda x: x["revenue"], reverse=True)

    return {
        "period"           : period,
        "total_revenue"    : total_revenue,
        "total_bookings"   : total_bookings,
        "daily_revenue"    : sorted(daily.values(), key=lambda x: x["date"]),
        "staff_performance": staff_list,
    }


@router.get("/analytics/staff/{staff_name}")
async def get_staff_analytics(
    staff_name  : str,
    client_id   : str = Query(...),
    period      : str = Query("week"),  # week | month | year
    admin       : dict= Depends(require_admin),
):
    """Per-stylist drill-down: revenue, booking count, and full booking history."""
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    now = datetime.now(timezone.utc)
    if period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    elif period == "year":
        start = now - timedelta(days=365)
    else:
        raise HTTPException(status_code=422, detail="period must be week, month, or year")

    db   = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.BOOKINGS)
        .where("status", "==", "confirmed")
        .where("staff_name", "==", staff_name)
        .where("slot_datetime", ">=", start)
        .order_by("slot_datetime", direction="DESCENDING")
        .get()
    )

    bookings = []
    total_revenue = 0
    for doc in docs:
        data = doc.to_dict()
        price = int(data.get("service_price", 0))
        total_revenue += price
        slot_dt = data.get("slot_datetime")
        bookings.append({
            "booking_id"    : doc.id,
            "slot_datetime" : slot_dt.isoformat() if hasattr(slot_dt, "isoformat") else str(slot_dt),
            "service_name"  : data.get("service_name", ""),
            "customer_phone": data.get("customer_phone", ""),
            "price"         : price,
        })

    return {
        "staff_name"    : staff_name,
        "period"        : period,
        "total_revenue" : total_revenue,
        "total_bookings": len(bookings),
        "bookings"      : bookings,
    }


# ── Services management ────────────────────────────────────────────────────────

class ServiceRequest(BaseModel):
    name        : str
    price       : int
    duration_min: int = 30
    description : str = ""
    category    : str = "general"


@router.get("/services")
async def get_services(
    client_id   : str  = Query(...),
    current_user: dict = Depends(require_active_tenant),
):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db   = get_db()
    docs = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .where("is_active", "==", True)
        .get()
    )
    services = [{"service_id": d.id, **d.to_dict()} for d in docs]
    return {"services": services}


@router.post("/services", status_code=201)
async def create_service(
    client_id: str            = Query(...),
    body     : ServiceRequest = ...,
    admin    : dict           = Depends(require_admin),
):
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    now = datetime.now(timezone.utc)
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .add({
            "name"        : body.name,
            "price"       : body.price,
            "duration_min": body.duration_min,
            "description" : body.description,
            "category"    : body.category,
            "is_active"   : True,
            "created_at"  : now,
            "updated_at"  : now,
        })
    )
    return {"message": "Service created.", "service_id": ref[1].id}


@router.patch("/services/{service_id}")
async def toggle_service(
    service_id : str,
    client_id  : str  = Query(...),
    body       : dict = Body(...),
    admin      : dict = Depends(require_admin),
):
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    ref = (
        db.collection(Collections.CLIENTS)
        .document(client_id)
        .collection(Collections.SERVICES)
        .document(service_id)
    )
    ref.update({**body, "updated_at": datetime.now(timezone.utc)})
    return {"message": "Service updated."}