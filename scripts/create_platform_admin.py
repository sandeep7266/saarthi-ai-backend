"""
scripts/create_platform_admin.py

One-time bootstrap script to create the FIRST Saarthi-AI platform admin
account (Sandeep / internal team). There is deliberately no public API
endpoint for this — platform_admin is the highest privilege level in the
system (sees every client's data), so account creation is a manual,
run-it-yourself step using the same Firebase credentials Railway uses.

USAGE (run from the project root, with the same env vars Railway uses —
either locally with a .env file, or via `railway run` so it uses Railway's
live Firebase credentials):

    python scripts/create_platform_admin.py

It will prompt for name, email, and password interactively (password input
is hidden). Run it again later with a different email to add more platform
admins — it does not need to be run only once.
"""

import getpass
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")  # allow running from project root

from database import get_db, initialize_firebase, Collections  # noqa: E402
from routers.auth import hash_password  # noqa: E402


def main():
    initialize_firebase()
    db = get_db()

    name  = input("Name: ").strip()
    email = input("Email: ").strip().lower()
    if not name or not email:
        print("Name and email are required.")
        sys.exit(1)

    existing = (
        db.collection(Collections.PLATFORM_ADMINS)
        .where("email", "==", email)
        .limit(1)
        .get()
    )
    if existing:
        print(f"A platform admin with email {email} already exists.")
        sys.exit(1)

    password  = getpass.getpass("Password: ")
    password2 = getpass.getpass("Confirm password: ")
    if password != password2:
        print("Passwords do not match.")
        sys.exit(1)
    if len(password) < 8:
        print("Password should be at least 8 characters.")
        sys.exit(1)

    db.collection(Collections.PLATFORM_ADMINS).add({
        "name"          : name,
        "email"         : email,
        "password_hash" : hash_password(password),
        "is_active"     : True,
        "created_at"    : datetime.now(timezone.utc),
    })

    print(f"\n✅ Platform admin created: {email}")
    print("Login via POST /api/v1/auth/platform-login with this email + password.")


if __name__ == "__main__":
    main()