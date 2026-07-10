"""
utils/slot_generator.py
Recurring weekly-schedule-based slot auto-generation.

Instead of a client manually creating slots every single day, they set up
a template ONCE (which days they're open, hours, slot length, staff), and:
  1. Saving the template immediately backfills the next 14 days (bootstrap).
  2. A daily cron job (main.py) rolls the window forward by generating
     exactly the day that's newly entering the 14-day booking window,
     so clients never have to think about it again.

Template shape (stored on the client doc as `schedule_template`):
{
  "enabled": true,
  "slot_duration_min": 30,
  "open_time": "10:00",   # 24h HH:MM, client's local (Asia/Kolkata) time
  "close_time": "18:00",
  "open_days": ["mon", "tue", "wed", "thu", "fri", "sat"],  # lowercase
  "staff": ["Rahul", "Aradhya"]  # empty list = no staff assignment (solo business)
}
"""

import logging
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from database import get_db, Collections

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ROLLING_WINDOW_DAYS = 14


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def generate_slots_for_date(client_id: str, target_date_ist) -> int:
    """
    Generates slots for ONE calendar date (IST) based on the client's
    schedule_template, skipping any (staff, datetime) combo that already
    exists — safe to call repeatedly without creating duplicates.
    Returns the number of slots created.
    """
    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    client_doc = client_ref.get()
    if not client_doc.exists:
        return 0

    template = client_doc.to_dict().get("schedule_template")
    if not template or not template.get("enabled"):
        return 0

    weekday_key = WEEKDAY_KEYS[target_date_ist.weekday()]
    if weekday_key not in template.get("open_days", []):
        return 0

    try:
        open_t  = _parse_hhmm(template["open_time"])
        close_t = _parse_hhmm(template["close_time"])
        duration = int(template.get("slot_duration_min", 30))
    except Exception as e:
        logger.error("Invalid schedule_template for client %s: %s", client_id, e)
        return 0

    staff_list = template.get("staff") or [""]  # [""] = single unnamed slot per time

    # Check existing slots for this date to avoid duplicates
    day_start = datetime.combine(target_date_ist, open_t, tzinfo=IST).astimezone(timezone.utc)
    day_end   = datetime.combine(target_date_ist, close_t, tzinfo=IST).astimezone(timezone.utc)
    existing_docs = (
        client_ref.collection(Collections.SLOTS)
        .where("slot_datetime", ">=", day_start)
        .where("slot_datetime", "<", day_end)
        .get()
    )
    existing_keys = set()
    for doc in existing_docs:
        d = doc.to_dict()
        dt = d.get("slot_datetime")
        if hasattr(dt, "isoformat"):
            existing_keys.add((d.get("staff_name", ""), dt.isoformat()))

    now = datetime.now(timezone.utc)
    batch = db.batch()
    created = 0

    for staff_name in staff_list:
        cursor_ist = datetime.combine(target_date_ist, open_t, tzinfo=IST)
        end_ist    = datetime.combine(target_date_ist, close_t, tzinfo=IST)
        while cursor_ist < end_ist:
            slot_dt_utc = cursor_ist.astimezone(timezone.utc)
            key = (staff_name, slot_dt_utc.isoformat())
            if key not in existing_keys:
                ref = client_ref.collection(Collections.SLOTS).document()
                batch.set(ref, {
                    "staff_name"   : staff_name,
                    "slot_datetime": slot_dt_utc,
                    "duration_min" : duration,
                    "status"       : "available",
                    "booking_id"   : None,
                    "auto_generated": True,
                    "created_at"   : now,
                    "updated_at"   : now,
                })
                created += 1
            cursor_ist += timedelta(minutes=duration)

    if created:
        batch.commit()
    return created


def bootstrap_slots_for_client(client_id: str) -> int:
    """Called right after a client saves/enables their template — fills the
    full rolling window immediately instead of waiting for the daily cron."""
    today_ist = datetime.now(IST).date()
    total = 0
    for i in range(ROLLING_WINDOW_DAYS):
        total += generate_slots_for_date(client_id, today_ist + timedelta(days=i))
    logger.info("Bootstrapped %d auto-generated slots for client %s", total, client_id)
    return total


def daily_slot_rollforward() -> dict:
    """
    Runs once a day (main.py cron). For every active client with an enabled
    schedule_template, generates slots for exactly the day newly entering
    the rolling 14-day window — existing days already have slots, so this
    keeps the window full without regenerating everything each time.
    """
    db = get_db()
    target_date_ist = (datetime.now(IST) + timedelta(days=ROLLING_WINDOW_DAYS)).date()

    clients = (
        db.collection(Collections.CLIENTS)
        .where("status", "in", ["active", "grace"])
        .get()
    )

    processed = 0
    created_total = 0
    for client_doc in clients:
        template = client_doc.to_dict().get("schedule_template")
        if not template or not template.get("enabled"):
            continue
        try:
            created = generate_slots_for_date(client_doc.id, target_date_ist)
            created_total += created
            processed += 1
        except Exception as e:
            logger.error("Slot rollforward failed for client %s: %s", client_doc.id, e)

    logger.info(
        "Daily slot rollforward: clients=%d, slots_created=%d, date=%s",
        processed, created_total, target_date_ist.isoformat(),
    )
    return {"clients_processed": processed, "slots_created": created_total}