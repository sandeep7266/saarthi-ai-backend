"""
routers/stylist_availability.py
Real-time stylist availability, waiting-time calculation, and CRUD for the
`stylists` sub-collection — this is a SEPARATE concept from the pre-generated
time-grid `slots` collection (utils/slot_generator.py) and from the login
"staff" role (routers/auth.py CreateStaffRequest). A "stylist" here is a real
service provider whose live status (available/busy) is tracked in real time
as bookings actually start and finish.

Stylists get marked "busy" not at booking-creation time, but when the
appointment's actual start time arrives (see utils/staff_release.py's
activate_bookings_starting_now), and "available" again once the service
duration + buffer has elapsed (or the stylist confirms completion early via
WhatsApp button).
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from database import get_db, Collections
from routers.auth import require_active_tenant, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Stylist Availability"])

BUFFER_MINUTES = 10          # mandatory cleaning/sanitization buffer after every service
FALLBACK_SCAN_INCREMENT_MIN = 15
FALLBACK_SCAN_MAX_HOURS = 6  # don't scan forever looking for a free slot


# ── Stylist CRUD (used by the dashboard to register real stylists) ─────────────

class StylistRequest(BaseModel):
    name : str
    phone: str = ""  # E.164 — used to send the 5-min-before-end WhatsApp nudge


@router.get("/stylists")
async def list_stylists(client_id: str = Query(...), current_user: dict = Depends(require_active_tenant)):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    db = get_db()
    docs = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.STYLISTS).get()
    )
    return {"stylists": [{"stylist_id": d.id, **d.to_dict()} for d in docs]}


@router.post("/stylists", status_code=201)
async def create_stylist(
    client_id: str = Query(...), body: StylistRequest = ..., admin: dict = Depends(require_admin)
):
    if admin["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    db  = get_db()
    now = datetime.now(timezone.utc)
    ref = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.STYLISTS)
        .add({
            "name"              : body.name,
            "phone"             : body.phone,
            "status"            : "available",
            "current_booking_id": None,
            "busy_until"        : None,
            "created_at"        : now,
        })
    )
    return {"stylist_id": ref[1].id, "name": body.name, "status": "available"}


# ── Availability + waiting time ─────────────────────────────────────────────────

@router.get("/stylist-availability")
async def get_stylist_availability(
    client_id      : str = Query(...),
    service_id     : str = Query(...),
    staff_id       : str = Query(""),
    requested_time : str = Query(""),  # ISO 8601, optional — triggers fallback scan
    current_user   : dict = Depends(require_active_tenant),
):
    if current_user["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    db  = get_db()
    now = datetime.now(timezone.utc)

    service_doc = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.SERVICES).document(service_id).get()
    )
    if not service_doc.exists:
        raise HTTPException(status_code=404, detail="Service not found.")
    duration_min = service_doc.to_dict().get("duration_min", 30)
    total_block_min = duration_min + BUFFER_MINUTES

    stylists_ref = db.collection(Collections.CLIENTS).document(client_id).collection(Collections.STYLISTS)
    stylist_docs = [stylists_ref.document(staff_id).get()] if staff_id else list(stylists_ref.get())
    stylist_docs = [d for d in stylist_docs if d.exists]

    if staff_id and not stylist_docs:
        raise HTTPException(status_code=404, detail="Stylist not found.")

    results = []
    for doc in stylist_docs:
        data = doc.to_dict()
        entry = {"stylist_id": doc.id, "name": data.get("name", ""), "status": data.get("status", "available")}

        if data.get("status") == "busy" and data.get("busy_until"):
            busy_until = data["busy_until"]
            waiting_minutes = max(0, int((busy_until - now).total_seconds() // 60))
            entry["waiting_time_minutes"] = waiting_minutes
            entry["free_at"] = busy_until.isoformat()
        else:
            entry["waiting_time_minutes"] = 0
            entry["free_at"] = now.isoformat()

        results.append(entry)

    response = {
        "service_id"     : service_id,
        "duration_min"   : duration_min,
        "buffer_min"     : BUFFER_MINUTES,
        "stylists"       : sorted(results, key=lambda r: r["waiting_time_minutes"]),
    }

    # ── Fallback scan: customer requested a specific time, find next free slot ──
    if requested_time:
        try:
            req_dt = datetime.fromisoformat(requested_time.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="requested_time must be ISO 8601.")

        suggested = _find_next_available_slot(stylist_docs, req_dt, total_block_min, now)
        response["requested_time"] = requested_time
        response["requested_time_available"] = suggested is not None and suggested["start"] == req_dt
        response["suggested_next_slot"] = suggested

    return response


def _find_next_available_slot(stylist_docs, start_from: datetime, block_minutes: int, now: datetime):
    """
    Scans forward in FALLBACK_SCAN_INCREMENT_MIN steps from start_from, checking
    each candidate stylist's busy_until, and returns the first stylist+time that
    can accommodate the full service block (duration + buffer).
    """
    candidate = max(start_from, now)
    deadline  = start_from + timedelta(hours=FALLBACK_SCAN_MAX_HOURS)

    while candidate <= deadline:
        for doc in stylist_docs:
            data = doc.to_dict()
            busy_until = data.get("busy_until")
            is_free_at_candidate = (
                data.get("status") != "busy"
                or not busy_until
                or candidate >= busy_until
            )
            if is_free_at_candidate:
                return {
                    "stylist_id": doc.id,
                    "name"      : data.get("name", ""),
                    "start"     : candidate,
                    "start_iso" : candidate.isoformat(),
                    "end_iso"   : (candidate + timedelta(minutes=block_minutes)).isoformat(),
                }
        candidate += timedelta(minutes=FALLBACK_SCAN_INCREMENT_MIN)

    return None