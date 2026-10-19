import hashlib
import hmac
import secrets
from dataclasses import dataclass

from app.core.config import get_settings

KEY_TAG = "rf"
PREFIX_BYTES = 6  # -> 12 hex characters
SECRET_BYTES = 32  # -> 43 url-safe characters


@dataclass(frozen=True)
class GeneratedKey:
    raw: str  # shown to the caller exactly once
    prefix: str  # stored in plain text, indexed for lookup
    key_hash: str  # stored; the only thing used to verify the key later


def hash_api_key(raw_key: str) -> str:
    """HMAC-SHA256 of the full key with a server-side secret.

    API keys carry 256 bits of randomness, so a slow password hash (bcrypt, argon2)
    adds latency to every request without adding security. The HMAC secret means a
    leaked database alone cannot be used to check guesses.
    """
    secret = get_settings().api_key_hmac_secret.encode()
    return hmac.new(secret, raw_key.encode(), hashlib.sha256).hexdigest()


def generate_api_key() -> GeneratedKey:
    prefix = secrets.token_hex(PREFIX_BYTES)
    raw = f"{KEY_TAG}_{prefix}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return GeneratedKey(raw=raw, prefix=prefix, key_hash=hash_api_key(raw))


def parse_key_prefix(raw_key: str) -> str | None:
    """Return the lookup prefix of a well-formed key, or None.

    The prefix is hex, so it never contains '_'; the secret part may, hence maxsplit=2.
    """
    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_TAG:
        return None
    prefix = parts[1]
    if len(prefix) != PREFIX_BYTES * 2 or any(c not in "0123456789abcdef" for c in prefix):
        return None
    return prefix


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def verify_admin_token(supplied: str) -> bool:
    return constant_time_equals(supplied, get_settings().admin_token)
