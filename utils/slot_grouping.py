"""
utils/slot_grouping.py
Shared duration-aware slot-grouping logic — used by both the web booking page
(routers/booking_session.py's /slots-for-services) and the WhatsApp-native
booking flow (routers/booking.py). Groups consecutive same-staff grid slots
into runs long enough to cover a multi-service booking's total duration.
"""

from datetime import datetime, timedelta, timezone

from google.cloud.firestore import FieldFilter

from database import get_db, Collections

BUFFER_MINUTES = 10  # mandatory cleaning/sanitization buffer after every service


def compute_slot_groups(client_id: str, service_ids: list[str], staff_filter: str = "") -> dict:
    """
    Returns:
    {
      "total_duration_min": int,
      "buffer_min": int,
      "service_names": [...],
      "slot_groups": [{"staff_name", "start_datetime", "end_datetime", "slot_ids"}, ...],
      "stylist_status": [{"name", "status", "waiting_time_minutes"}, ...],
      "suggested_next_slot": {...} | None,
    }

    staff_filter: if non-empty, only returns groups for that exact staff_name
    (used when the WhatsApp customer picked a specific stylist rather than "Any").
    """
    db  = get_db()
    now = datetime.now(timezone.utc)

    services_ref = db.collection(Collections.CLIENTS).document(client_id).collection(Collections.SERVICES)
    total_duration = 0
    service_names  = []
    for sid in service_ids:
        doc = services_ref.document(sid).get()
        if not doc.exists:
            raise ValueError(f"Service not found: {sid}")
        data = doc.to_dict()
        total_duration += int(data.get("duration_min", 30))
        service_names.append(data.get("name", ""))

    total_block_min = total_duration + BUFFER_MINUTES

    slot_end = now + timedelta(days=14)
    slot_docs = (
        db.collection(Collections.CLIENTS).document(client_id)
        .collection(Collections.SLOTS)
        .where(filter=FieldFilter("status", "==", "available"))
        .where(filter=FieldFilter("slot_datetime", ">=", now))
        .where(filter=FieldFilter("slot_datetime", "<=", slot_end))
        .order_by("slot_datetime")
        .limit(500)
        .get()
    )

    by_staff: dict[str, list[dict]] = {}
    for d in slot_docs:
        data = d.to_dict()
        staff = data.get("staff_name", "") or "_no_staff_"
        if staff_filter and staff != staff_filter:
            continue
        by_staff.setdefault(staff, []).append({
            "slot_id"      : d.id,
            "slot_datetime": data.get("slot_datetime"),
            "duration_min" : data.get("duration_min", 30),
            "staff_name"   : data.get("staff_name", ""),
        })

    groups = []
    for staff, slots in by_staff.items():
        slots.sort(key=lambda s: s["slot_datetime"])
        i = 0
        while i < len(slots):
            run = [slots[i]]
            run_minutes = run[0]["duration_min"]
            j = i + 1
            while run_minutes < total_block_min and j < len(slots):
                prev_end = run[-1]["slot_datetime"] + timedelta(minutes=run[-1]["duration_min"])
                if slots[j]["slot_datetime"] == prev_end:
                    run.append(slots[j])
                    run_minutes += slots[j]["duration_min"]
                    j += 1
                else:
                    break
            if run_minutes >= total_block_min:
                groups.append({
                    "staff_name"    : staff if staff != "_no_staff_" else "",
                    "start_datetime": run[0]["slot_datetime"],
                    "end_datetime"  : run[0]["slot_datetime"] + timedelta(minutes=total_block_min),
                    "slot_ids"      : [s["slot_id"] for s in run],
                })
            i += 1  # slide by one grid-slot so every possible start is considered

    groups.sort(key=lambda g: g["start_datetime"])

    stylist_docs = db.collection(Collections.CLIENTS).document(client_id).collection(Collections.STYLISTS).get()
    stylist_status = []
    for d in stylist_docs:
        data = d.to_dict()
        if staff_filter and data.get("name", "") != staff_filter:
            continue
        entry = {"name": data.get("name", ""), "status": data.get("status", "available")}
        if data.get("status") == "busy" and data.get("busy_until"):
            entry["waiting_time_minutes"] = max(0, int((data["busy_until"] - now).total_seconds() // 60))
        else:
            entry["waiting_time_minutes"] = 0
        stylist_status.append(entry)

    return {
        "total_duration_min" : total_duration,
        "buffer_min"          : BUFFER_MINUTES,
        "service_names"       : service_names,
        "slot_groups"         : groups[:20],
        "stylist_status"      : stylist_status,
        "suggested_next_slot" : groups[0] if groups else None,
    }