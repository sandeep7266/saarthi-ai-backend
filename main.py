"""
main.py
FastAPI application entry point for Saarthi-AI.
Fixes applied:
  - bookings_api router registered
  - Rate limiting (slowapi)
  - APScheduler for auto-expire bookings
  - Better startup logging
  - Webhook idempotency key tracking
  - Request ID header for tracing
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

load_dotenv()

from database import initialize_firebase, get_db, Collections
from routers import auth, onboard, booking, payments, cron_jobs
from routers.bookings_api import router as bookings_router
from routers.notifications import router as notifications_router
from routers.booking_session import router as booking_session_router
from routers.dashboard_config import router as dashboard_config_router
from routers.platform_admin import router as platform_admin_router
from utils.rate_limiter import limiter
from utils.booking_expiry import expire_stale_pending_bookings
from utils.booking_reminders import send_upcoming_booking_reminders

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt= "%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("saarthi_ai")

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Saarthi-AI starting up…")

    # 1. Firebase
    initialize_firebase()
    logger.info("✅ Firebase initialized.")

    # 2. APScheduler — expire stale bookings every 5 min
    scheduler.add_job(
        expire_stale_pending_bookings,
        trigger  = "interval",
        minutes  = 5,
        id       = "expire_bookings",
        name     = "Expire stale pending bookings",
        replace_existing=True,
    )
    scheduler.add_job(
        send_upcoming_booking_reminders,
        trigger  = "interval",
        minutes  = 15,
        id       = "booking_reminders",
        name     = "Send upcoming booking reminders",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("✅ APScheduler started (booking expiry every 5 min, reminders every 15 min).")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    logger.info("🛑 Saarthi-AI shut down cleanly.")


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Saarthi-AI API",
    description = (
        "Multi-Tenant SaaS Platform — AI WhatsApp Receptionist "
        "for Indian local businesses."
    ),
    version     = "1.1.0",
    lifespan    = lifespan,
    docs_url    = "/docs",
    redoc_url   = "/redoc",
)

# ── Rate limiter ───────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── CORS ───────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,http://localhost:8080,https://saarthi-ai.in"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins     = [o.strip() for o in ALLOWED_ORIGINS],
    allow_credentials = True,
    allow_methods     = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers     = ["*"],
)

# ── Request ID + Timing middleware ─────────────────────────────────────────────
@app.middleware("http")
async def request_middleware(request: Request, call_next) -> Response:
    request_id = str(uuid.uuid4())[:8]
    start      = time.perf_counter()

    # Inject request ID for tracing
    request.state.request_id = request_id

    response = await call_next(request)

    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{duration_ms:.1f}ms"

    logger.info(
        "[%s] %s %s → %d | %.1fms",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# ── Global tenant status middleware ───────────────────────────────────────────
EXEMPT_PATHS = {
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/onboard/create-pending-vendor",
    "/api/v1/payments/razorpay-webhook",
    "/api/v1/webhook/whatsapp",
    "/api/v1/cron/run-daily-sync",
}

@app.middleware("http")
async def tenant_status_middleware(request: Request, call_next) -> Response:
    path = request.url.path

    if path in EXEMPT_PATHS or path.startswith("/api/v1/webhook") or path.startswith("/api/v1/platform"):
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            from jose import jwt as jose_jwt
            JWT_SECRET    = os.getenv("JWT_SECRET", "")
            JWT_ALGORITHM = "HS256"
            claims = jose_jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

            # Platform admins (Saarthi-AI's own team) aren't tenants — there is
            # no subscription concept for them. Never subject them to this check.
            if claims.get("role") == "platform_admin":
                return await call_next(request)

            # Only tenant tokens (issued by /auth/login) carry tenant_status at
            # all. If the claim is simply absent (any other token shape, current
            # or future), don't guess "inactive" — let route-level auth handle it.
            tenant_status = claims.get("tenant_status")
            if tenant_status is None:
                return await call_next(request)

            if tenant_status in ("expired", "inactive"):
                return JSONResponse(
                    status_code=402,
                    content={
                        "detail"       : f"Subscription is '{tenant_status}'. Please renew.",
                        "code"         : "SUBSCRIPTION_INACTIVE",
                        "lock"         : True,
                        "tenant_status": tenant_status,
                    },
                )
        except Exception:
            pass  # Let route handler deal with malformed token

    return await call_next(request)


# ── Routers ────────────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(auth.router)
app.include_router(onboard.router)
app.include_router(booking.router)
app.include_router(payments.router)
app.include_router(cron_jobs.router)
app.include_router(bookings_router)


# ── Customer Web Booking App (serves static HTML) ──────────────────────────────
@app.get("/book", tags=["Booking Web App"])
async def serve_booking_app():
    """
    Customer-facing booking page. WhatsApp bot sends links like:
    https://YOUR_DOMAIN/book?session=xxxxx
    """
    return FileResponse("static/book.html", media_type="text/html")


# ── Client Vendor Dashboard (serves static HTML) ────────────────────────────────
@app.get("/dashboard", tags=["Vendor Dashboard"])
async def serve_vendor_dashboard():
    """Client-facing dashboard — login + bookings/services/settings management."""
    return FileResponse("static/dashboard.html", media_type="text/html")


# ── Internal Ops Console (serves static HTML, platform_admin only) ─────────────
@app.get("/admin", tags=["Ops Console"])
async def serve_ops_console():
    """
    Saarthi-AI's own internal team console (not client-facing). The page itself
    calls /api/v1/auth/platform-login and require_platform_admin-gated endpoints,
    so serving the HTML here is not a security boundary by itself — access
    control happens at the API layer, same as the vendor dashboard.
    """
    return FileResponse("static/platform_admin.html", media_type="text/html")


app.include_router(notifications_router)
app.include_router(booking_session_router)
app.include_router(dashboard_config_router)
app.include_router(platform_admin_router)

# ── Health endpoints ───────────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
async def root():
    return {
        "service": "Saarthi-AI API",
        "status" : "operational",
        "version": "1.1.0",
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Deep health check — verifies Firebase + scheduler."""
    firebase_ok   = False
    scheduler_ok  = scheduler.running

    try:
        db = get_db()
        db.collection(Collections.CLIENTS).limit(1).get()
        firebase_ok = True
    except Exception as e:
        logger.error("Health check Firebase error: %s", e)

    status = "healthy" if (firebase_ok and scheduler_ok) else "degraded"
    return {
        "status"   : status,
        "firebase" : "connected" if firebase_ok  else "error",
        "scheduler": "running"   if scheduler_ok else "stopped",
        "version"  : "1.1.0",
    }