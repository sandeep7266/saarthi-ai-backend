"""
routers/platform_admin.py
Saarthi-AI's own internal admin endpoints — completely separate from tenant
dashboards. Gated by require_platform_admin (routers/auth.py), which checks
role == "platform_admin" from a token issued by /api/v1/auth/platform-login.

Tenant "admin"/"staff" tokens can NEVER access these routes — they carry
role="admin"/"staff", not "platform_admin", so require_platform_admin rejects
them with 403 regardless of which client they belong to.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from database import get_db, Collections
from routers.auth import require_platform_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/platform", tags=["Platform Admin"])


# ── List all clients (with KYC review flags, status, plan) ─────────────────────

@router.get("/clients")
async def list_all_clients(admin: dict = Depends(require_platform_admin)):
    db = get_db()
    docs = db.collection(Collections.CLIENTS).stream()

    clients = []
    for doc in docs:
        d = doc.to_dict()
        clients.append({
            "client_id"        : doc.id,
            "business_name"    : d.get("business_name", ""),
            "business_type"    : d.get("business_type", ""),
            "owner_name"       : d.get("owner_name", ""),
            "owner_phone"      : d.get("owner_phone", ""),
            "status"           : d.get("status", ""),
            "plan"             : d.get("plan", ""),
            "billing_cycle"    : d.get("billing_cycle", ""),
            "subscription_end_date": d.get("subscription_end_date"),
            "kyc_review_needed": d.get("kyc_review_needed", False),
            "whatsapp_phone_id": d.get("whatsapp_phone_id", ""),
            "created_at"       : d.get("created_at"),
        })

    return {"count": len(clients), "clients": clients}


# ── Single client detail ────────────────────────────────────────────────────────

@router.get("/clients/{client_id}")
async def get_client_detail(client_id: str, admin: dict = Depends(require_platform_admin)):
    db = get_db()
    doc = db.collection(Collections.CLIENTS).document(client_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Client not found.")
    return {"client_id": client_id, **doc.to_dict()}


@router.get("/clients/pending-whatsapp-connect")
async def list_pending_whatsapp_connect(admin: dict = Depends(require_platform_admin)):
    """
    Clients who paid and are active but haven't had their dedicated WhatsApp
    Business number set up yet — the team's call-list for the white-glove
    connect-whatsapp workflow.
    """
    db = get_db()
    docs = db.collection(Collections.CLIENTS).where("status", "==", "active").stream()

    pending = []
    for doc in docs:
        d = doc.to_dict()
        if not d.get("whatsapp_phone_id"):
            pending.append({
                "client_id"    : doc.id,
                "business_name": d.get("business_name", ""),
                "owner_name"   : d.get("owner_name", ""),
                "owner_phone"  : d.get("owner_phone", ""),
                "created_at"   : d.get("created_at"),
            })

    return {"count": len(pending), "clients": pending}


# ── Manually set a client's status (e.g. resolve a KYC mismatch review) ────────

class StatusUpdateRequest(BaseModel):
    status: str  # "active" | "inactive" | "suspended" | "expired"
    reason: str = ""


@router.patch("/clients/{client_id}/status")
async def update_client_status(
    client_id: str, body: StatusUpdateRequest, admin: dict = Depends(require_platform_admin)
):
    valid_statuses = {"active", "inactive", "suspended", "expired"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=422, detail=f"status must be one of {valid_statuses}")

    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    if not client_ref.get().exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    client_ref.update({
        "status"        : body.status,
        "updated_at"    : datetime.now(timezone.utc),
        "status_note"   : body.reason,
        "status_set_by" : admin.get("sub", "platform_admin"),
    })
    logger.info("Platform admin %s set client %s status to %s (%s)",
                admin.get("sub"), client_id, body.status, body.reason)

    return {"success": True, "client_id": client_id, "status": body.status}


# ── Clear a KYC manual-review flag once verified by a human ─────────────────────

@router.patch("/clients/{client_id}/kyc-reviewed")
async def mark_kyc_reviewed(client_id: str, admin: dict = Depends(require_platform_admin)):
    db = get_db()
    client_ref = db.collection(Collections.CLIENTS).document(client_id)
    if not client_ref.get().exists:
        raise HTTPException(status_code=404, detail="Client not found.")

    client_ref.update({
        "kyc_review_needed": False,
        "kyc_reviewed_by"  : admin.get("sub", "platform_admin"),
        "kyc_reviewed_at"  : datetime.now(timezone.utc),
    })
    return {"success": True, "client_id": client_id}