"""
utils/backup_engine.py
Automated Firestore JSON snapshot → ZIP → uploaded to Cloudinary (free tier).
Called nightly by cron_jobs.py.
"""

import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from typing import Any

import cloudinary
import cloudinary.uploader

from database import get_db, Collections

logger = logging.getLogger(__name__)

TOP_LEVEL_COLLECTIONS = [
    Collections.CLIENTS,
    Collections.USERS,
    Collections.AUDIT_LOGS,
]


def run_firestore_backup() -> str:
    """
    Exports all Firestore collections to JSON,
    compresses into timestamped ZIP,
    uploads to Cloudinary (resource_type=raw).
    Returns the Cloudinary secure URL.
    """
    now       = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    zip_name  = f"saarthi_backup_{timestamp}"

    db     = get_db()
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:

        # Top-level collections
        for collection_name in TOP_LEVEL_COLLECTIONS:
            data       = _export_collection(db, collection_name)
            json_bytes = json.dumps(data, indent=2,
                                    default=_json_serializer).encode("utf-8")
            zf.writestr(f"{collection_name}.json", json_bytes)
            logger.info("Exported '%s': %d docs", collection_name, len(data))

        # Per-client sub-collections
        clients = db.collection(Collections.CLIENTS).get()
        for client_doc in clients:
            cid = client_doc.id
            for sub in ("bookings", "slots", "services", "conversations", "invoices"):
                sub_data = _export_subcollection(db, Collections.CLIENTS, cid, sub)
                if sub_data:
                    fname     = f"clients/{cid}/{sub}.json"
                    json_bytes = json.dumps(sub_data, indent=2,
                                            default=_json_serializer).encode("utf-8")
                    zf.writestr(fname, json_bytes)

        # Manifest
        manifest = {
            "backup_timestamp": now.isoformat(),
            "collections"     : TOP_LEVEL_COLLECTIONS,
            "client_count"    : len(clients),
            "generator"       : "Saarthi-AI Backup Engine v1.1 (Cloudinary)",
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    buffer.seek(0)

    # Upload to Cloudinary
    try:
        result = cloudinary.uploader.upload(
            buffer,
            public_id     = f"saarthi-ai/backups/{zip_name}",
            resource_type = "raw",
            format        = "zip",
            overwrite     = True,
            access_mode   = "authenticated",  # Private — needs signed URL to access
        )
        url = result.get("secure_url", "")
        logger.info(
            "Backup uploaded to Cloudinary: %s | size=%.1f KB",
            zip_name, buffer.getbuffer().nbytes / 1024,
        )
        return url
    except Exception as e:
        logger.error("Cloudinary backup upload failed: %s", e)
        raise


# ── Helpers ────────────────────────────────────────────────────────────────────

def _export_collection(db, collection_name: str) -> list[dict]:
    docs   = db.collection(collection_name).get()
    result = []
    for doc in docs:
        data        = doc.to_dict()
        data["_id"] = doc.id
        result.append(data)
    return result


def _export_subcollection(db, parent_col: str, parent_id: str,
                           sub_col: str) -> list[dict]:
    docs   = (
        db.collection(parent_col)
        .document(parent_id)
        .collection(sub_col)
        .get()
    )
    result = []
    for doc in docs:
        data        = doc.to_dict()
        data["_id"] = doc.id
        result.append(data)
    return result


def _json_serializer(obj: Any) -> str:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
