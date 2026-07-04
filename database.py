"""
database.py
Firebase Admin SDK initialization for Saarthi-AI Multi-Tenant Platform.
"""

import os
import logging

import firebase_admin
from firebase_admin import credentials, firestore, storage

logger = logging.getLogger(__name__)

_firebase_app = None
_db = None
_bucket = None


def initialize_firebase() -> None:
    global _firebase_app, _db, _bucket
    if _firebase_app is not None:
        return

    storage_bucket = os.getenv("FIREBASE_STORAGE_BUCKET", "saarthi-ai.appspot.com")

    # Support base64-encoded service account (for Railway/Docker where file upload is hard)
    sa_base64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_BASE64", "")
    if sa_base64:
        import base64
        import json as _json
        import tempfile
        sa_json = base64.b64decode(sa_base64).decode("utf-8")
        sa_dict = _json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)
        logger.info("Firebase: using base64-encoded service account.")
    else:
        # Fall back to file path
        service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "serviceAccountKey.json")
        if not os.path.exists(service_account_path):
            raise FileNotFoundError(
                f"Firebase credentials not found. Either set FIREBASE_SERVICE_ACCOUNT_BASE64 "
                f"or place serviceAccountKey.json at: {service_account_path}"
            )
        cred = credentials.Certificate(service_account_path)
        logger.info("Firebase: using service account file.")

    _firebase_app = firebase_admin.initialize_app(cred, {"storageBucket": storage_bucket})
    _db = firestore.client()
    _bucket = storage.bucket()
    logger.info("Firebase Admin SDK initialized.")


def get_db():
    if _db is None:
        raise RuntimeError("Firestore not initialized. Call initialize_firebase() first.")
    return _db


def get_bucket():
    if _bucket is None:
        raise RuntimeError("Storage bucket not initialized. Call initialize_firebase() first.")
    return _bucket


class Collections:
    CLIENTS    = "clients"
    BOOKINGS   = "bookings"
    USERS      = "users"
    SERVICES   = "services"
    SLOTS      = "slots"
    INVOICES   = "invoices"
    AUDIT_LOGS = "audit_logs"
    PLATFORM_ADMINS = "platform_admins"  # Saarthi-AI's own team — separate from tenant users