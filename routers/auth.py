"""
routers/auth.py
JWT management, Staff/Admin login handlers for Saarthi-AI.
"""

import os
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import httpx
from fastapi import APIRouter, HTTPException, Depends, Request, status
from utils.rate_limiter import limiter, LIMIT_STRICT, LIMIT_NORMAL
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr

from database import get_db, Collections

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

META_ACCESS_TOKEN   = os.getenv("META_ACCESS_TOKEN", "")
META_API_VERSION    = os.getenv("META_API_VERSION", "v19.0")
COMPANY_WHATSAPP_PHONE_ID = os.getenv("COMPANY_WHATSAPP_PHONE_ID", "")
OTP_TTL_MINUTES     = 10


JWT_SECRET      = os.getenv("JWT_SECRET", "CHANGE_ME_IN_PRODUCTION_USE_256_BIT_SECRET")
JWT_ALGORITHM   = "HS256"
ACCESS_TOKEN_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "12"))

bearer_scheme = HTTPBearer()


# ── Pydantic Models ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    client_id: str
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    client_id: str
    tenant_status: str

class CreateStaffRequest(BaseModel):
    client_id: str
    name: str
    email: EmailStr
    password: str
    role: str  # "admin" | "staff"

class PlatformLoginRequest(BaseModel):
    email: str
    password: str

class PlatformTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "platform_admin"

class ForgotPasswordRequest(BaseModel):
    client_id: str
    email: str

class ResetPasswordRequest(BaseModel):
    client_id: str
    email: str
    otp: str
    new_password: str


# ── JWT Utilities ──────────────────────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=ACCESS_TOKEN_TTL_HOURS))
    payload.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> dict:
    """Dependency: decode JWT and return claims dict."""
    return decode_token(credentials.credentials)


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Dependency: enforce admin role."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current_user


def require_platform_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Dependency: enforce Saarthi-AI's own platform_admin role — completely
    separate from tenant "admin"/"staff" roles. A tenant admin token will
    never satisfy this, since it carries role="admin" and a client_id scope,
    not role="platform_admin".
    """
    if current_user.get("role") != "platform_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform admin access required.")
    return current_user


def require_active_tenant(current_user: dict = Depends(get_current_user)) -> dict:
    """Dependency: block expired/inactive tenants at the API layer."""
    status_val = current_user.get("tenant_status", "inactive")
    if status_val in ("expired", "inactive"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Tenant subscription is {status_val}. Please renew to continue.",
        )
    return current_user


# ── Helpers ────────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _get_tenant_status(client_id: str) -> str:
    """Live tenant status lookup from Firestore (used during login)."""
    db = get_db()
    doc = db.collection(Collections.CLIENTS).document(client_id).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    return doc.to_dict().get("status", "inactive")


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
@limiter.limit(LIMIT_STRICT)
async def login(request: Request, body: LoginRequest):
    """
    Authenticate a staff or admin user.
    Returns a signed JWT carrying role + tenant_status claims.
    """
    db = get_db()
    users_ref = (
        db.collection(Collections.USERS)
        .where("client_id", "==", body.client_id)
        .where("email", "==", body.email)
        .limit(1)
        .get()
    )

    if not users_ref:
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    user_doc = users_ref[0].to_dict()

    if not verify_password(body.password, user_doc.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    if not user_doc.get("is_active", False):
        raise HTTPException(status_code=403, detail="Account disabled. Contact your administrator.")

    # Always fetch live tenant status so hard-lock propagates instantly
    tenant_status = _get_tenant_status(body.client_id)

    token_data = {
        "sub"           : user_doc["email"],
        "user_id"       : users_ref[0].id,
        "client_id"     : body.client_id,
        "role"          : user_doc.get("role", "staff"),
        "tenant_status" : tenant_status,
        "business_name" : user_doc.get("business_name", ""),
    }

    access_token = create_access_token(token_data)

    logger.info("Login success: %s | role=%s | tenant=%s", body.email, token_data["role"], tenant_status)

    return TokenResponse(
        access_token=access_token,
        role=token_data["role"],
        client_id=body.client_id,
        tenant_status=tenant_status,
    )


@router.post("/platform-login", response_model=PlatformTokenResponse)
@limiter.limit(LIMIT_STRICT)
async def platform_login(request: Request, body: PlatformLoginRequest):
    """
    Authenticate a Saarthi-AI platform admin (Sandeep / internal team).
    Completely separate from tenant login — looked up in Collections.PLATFORM_ADMINS,
    never Collections.USERS, so tenant accounts can never satisfy this even by accident.
    """
    db = get_db()
    admins_ref = (
        db.collection(Collections.PLATFORM_ADMINS)
        .where("email", "==", body.email)
        .limit(1)
        .get()
    )

    if not admins_ref:
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    admin_doc = admins_ref[0].to_dict()

    if not verify_password(body.password, admin_doc.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    if not admin_doc.get("is_active", False):
        raise HTTPException(status_code=403, detail="Account disabled.")

    token_data = {
        "sub" : admin_doc["email"],
        "user_id": admins_ref[0].id,
        "role": "platform_admin",
    }
    access_token = create_access_token(token_data)
    logger.info("Platform admin login: %s", body.email)

    return PlatformTokenResponse(access_token=access_token)


@router.post("/forgot-password")
@limiter.limit(LIMIT_STRICT)
async def forgot_password(request: Request, body: ForgotPasswordRequest):
    """
    Sends a 6-digit OTP to the client's owner_phone via WhatsApp (no email
    service exists in this stack yet, so WhatsApp is the reset channel).
    Always returns a generic success message regardless of whether the
    account exists, to avoid leaking which emails are registered.
    """
    db = get_db()
    users_ref = (
        db.collection(Collections.USERS)
        .where("client_id", "==", body.client_id)
        .where("email", "==", body.email)
        .limit(1)
        .get()
    )

    generic_response = {"message": "Agar account exist karta hai, OTP WhatsApp par bhej diya gaya hai."}

    if not users_ref:
        logger.info("Password reset requested for unknown user: %s / %s", body.client_id, body.email)
        return generic_response

    user_ref = users_ref[0].reference
    otp = f"{secrets.randbelow(1000000):06d}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES)

    user_ref.update({
        "reset_otp"        : otp,
        "reset_otp_expires": expires_at,
    })

    client_doc = db.collection(Collections.CLIENTS).document(body.client_id).get()
    owner_phone = client_doc.to_dict().get("owner_phone", "") if client_doc.exists else ""

    if owner_phone and COMPANY_WHATSAPP_PHONE_ID:
        await _send_otp_whatsapp(owner_phone, otp)
    else:
        logger.error("Could not send reset OTP — missing owner_phone or COMPANY_WHATSAPP_PHONE_ID for client %s",
                     body.client_id)

    return generic_response


@router.post("/reset-password")
@limiter.limit(LIMIT_STRICT)
async def reset_password(request: Request, body: ResetPasswordRequest):
    db = get_db()
    users_ref = (
        db.collection(Collections.USERS)
        .where("client_id", "==", body.client_id)
        .where("email", "==", body.email)
        .limit(1)
        .get()
    )

    if not users_ref:
        raise HTTPException(status_code=400, detail="Invalid OTP or account.")

    user_doc = users_ref[0]
    user_data = user_doc.to_dict()

    stored_otp = user_data.get("reset_otp", "")
    expires_at = user_data.get("reset_otp_expires")

    if not stored_otp or stored_otp != body.otp:
        raise HTTPException(status_code=400, detail="Invalid OTP.")

    if not expires_at or datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=400, detail="OTP expired. Please request a new one.")

    if len(body.new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    user_doc.reference.update({
        "password_hash"    : hash_password(body.new_password),
        "reset_otp"         : "",
        "reset_otp_expires" : None,
    })
    logger.info("Password reset successful: %s / %s", body.client_id, body.email)

    return {"message": "Password reset successful. Aap ab naye password se login kar sakte hain."}


async def _send_otp_whatsapp(to: str, otp: str) -> None:
    url = f"https://graph.facebook.com/{META_API_VERSION}/{COMPANY_WHATSAPP_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to"  : to,
        "type": "text",
        "text": {"body": f"Aapka Saarthi-AI password reset OTP hai: *{otp}*\n\n"
                          f"Ye {OTP_TTL_MINUTES} minute mein expire ho jaayega. Kisi ke saath share na karein."},
    }
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to send reset OTP via WhatsApp → %s: %s", to, e)


@router.post("/create-staff", status_code=201)
@limiter.limit(LIMIT_NORMAL)
async def create_staff(request: Request, body: CreateStaffRequest, admin: dict = Depends(require_admin)):
    """
    Admin-only: create a staff or sub-admin account under the same tenant.
    """
    if admin["client_id"] != body.client_id:
        raise HTTPException(status_code=403, detail="Cannot create staff for a different tenant.")

    if body.role not in ("admin", "staff"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'staff'.")

    db = get_db()

    # Prevent duplicate emails within the same tenant
    existing = (
        db.collection(Collections.USERS)
        .where("client_id", "==", body.client_id)
        .where("email", "==", body.email)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered for this tenant.")

    new_user = {
        "client_id"     : body.client_id,
        "name"          : body.name,
        "email"         : body.email,
        "password_hash" : hash_password(body.password),
        "role"          : body.role,
        "is_active"     : True,
        "created_at"    : datetime.now(timezone.utc),
        "created_by"    : admin["user_id"],
    }

    ref = db.collection(Collections.USERS).add(new_user)
    logger.info("Staff created: %s | role=%s | client=%s", body.email, body.role, body.client_id)

    return {"message": "Staff account created.", "user_id": ref[1].id}


@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Return decoded JWT claims for the requesting user."""
    return current_user


@router.post("/refresh")
async def refresh_token(current_user: dict = Depends(get_current_user)):
    """
    Issue a fresh token with an updated tenant_status (reflects any live plan changes).
    Flutter app calls this on foreground resume.
    """
    tenant_status = _get_tenant_status(current_user["client_id"])

    token_data = {
        "sub"           : current_user["sub"],
        "user_id"       : current_user["user_id"],
        "client_id"     : current_user["client_id"],
        "role"          : current_user["role"],
        "tenant_status" : tenant_status,
        "business_name" : current_user.get("business_name", ""),
    }

    access_token = create_access_token(token_data)
    return {"access_token": access_token, "tenant_status": tenant_status}


# ── Staff Management Endpoints (missing — Flutter app calls these) ─────────────

@router.get("/staff")
async def get_staff_list(
    client_id   : str  = None,
    admin       : dict = Depends(require_admin),
):
    """
    Admin-only: list all staff members for the tenant.
    Flutter StaffScreen calls GET /api/v1/auth/staff?client_id=xxx
    """
    cid = client_id or admin.get("client_id")
    if cid != admin["client_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    db   = get_db()
    docs = (
        db.collection(Collections.USERS)
        .where("client_id", "==", cid)
        .order_by("created_at", direction="DESCENDING")
        .get()
    )

    staff = []
    for doc in docs:
        data         = doc.to_dict()
        data["_id"]  = doc.id
        # Never expose password hash
        data.pop("password_hash", None)
        staff.append(data)

    return {"staff": staff, "count": len(staff)}


@router.patch("/staff/{user_id}")
async def toggle_staff_active(
    user_id  : str,
    body     : dict,
    admin    : dict = Depends(require_admin),
):
    """
    Admin-only: enable or disable a staff account.
    Flutter StaffScreen calls PATCH /api/v1/auth/staff/{user_id}
    """
    db  = get_db()
    ref = db.collection(Collections.USERS).document(user_id)
    doc = ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Staff member not found.")

    staff_data = doc.to_dict()

    # Ensure admin can only manage staff from their own tenant
    if staff_data.get("client_id") != admin["client_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    # Prevent admin from disabling themselves
    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot disable your own account.")

    is_active = body.get("is_active")
    if not isinstance(is_active, bool):
        raise HTTPException(status_code=422, detail="is_active must be true or false.")

    ref.update({
        "is_active" : is_active,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": admin["user_id"],
    })

    logger.info(
        "Staff %s %s by admin %s",
        user_id,
        "enabled" if is_active else "disabled",
        admin["sub"],
    )

    return {
        "message" : f"Staff account {'enabled' if is_active else 'disabled'}.",
        "user_id" : user_id,
        "is_active": is_active,
    }


@router.delete("/staff/{user_id}")
async def delete_staff(
    user_id: str,
    admin  : dict = Depends(require_admin),
):
    """Admin-only: permanently delete a staff account."""
    db  = get_db()
    ref = db.collection(Collections.USERS).document(user_id)
    doc = ref.get()

    if not doc.exists:
        raise HTTPException(status_code=404, detail="Staff member not found.")

    if doc.to_dict().get("client_id") != admin["client_id"]:
        raise HTTPException(status_code=403, detail="Access denied.")

    if user_id == admin["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")

    ref.delete()
    logger.info("Staff %s deleted by admin %s", user_id, admin["sub"])
    return {"message": "Staff member deleted.", "user_id": user_id}