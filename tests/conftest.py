import os

# Must run before any `app` import: Settings requires these and the DB engine is built at import.
os.environ.setdefault("API_KEY_HMAC_SECRET", "test-only-hmac-secret-0123456789abcdef0123")
os.environ.setdefault("ADMIN_TOKEN", "test-only-admin-token")
