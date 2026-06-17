"""
utils/rate_limiter.py
slowapi-based rate limiting configuration.
Applied per-endpoint and globally to prevent abuse.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

# Global limiter instance — imported by main.py and routers
limiter = Limiter(
    key_func        = get_remote_address,
    default_limits  = ["200/minute"],       # Global default
    storage_uri     = "memory://",          # In-memory for single instance
    # For multi-instance: use "redis://localhost:6379"
)

# ── Limit presets (used as decorators on routes) ──────────────────────────────
#
# Usage in a route:
#   from utils.rate_limiter import limiter
#   @limiter.limit("5/minute")
#   async def my_endpoint(request: Request, ...):
#
# Presets:
#   STRICT  = "5/minute"    — login, onboarding
#   NORMAL  = "30/minute"   — authenticated CRUD
#   RELAXED = "200/minute"  — webhooks (Meta sends bursts)

LIMIT_STRICT  = "5/minute"
LIMIT_NORMAL  = "30/minute"
LIMIT_WEBHOOK = "300/minute"
LIMIT_CRON    = "10/hour"
