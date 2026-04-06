from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

# PBKDF2 parameters — tuned for interactive auth on modest hardware
_ITERATIONS = 260_000
_HASH_ALG   = "sha256"
_SALT_BYTES = 16
_KEY_BYTES  = 32
_PREFIX     = "pbkdf2"

# Valid amateur callsign pattern (ITU-R M.1172 approximate)
_CALL_RE = re.compile(
    r"^[A-Z0-9]{1,3}[0-9][A-Z]{1,4}(?:-[0-9]{1,2})?$",
    re.IGNORECASE,
)


def is_valid_call(call: str) -> bool:
    """Return True for plausible amateur callsigns (including SYSOP)."""
    upper = call.strip().upper()
    if upper == "SYSOP":
        return True
    return bool(_CALL_RE.match(upper))


def normalize_call(call: str) -> str:
    return call.strip().upper()


def hash_password(password: str) -> str:
    """Hash *password* and return a storable string."""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        _HASH_ALG,
        password.encode("utf-8"),
        salt,
        _ITERATIONS,
        dklen=_KEY_BYTES,
    )
    return f"{_PREFIX}:{_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def is_password_hash(value: str) -> bool:
    """Return True if *value* looks like a stored password hash."""
    return value.startswith(f"{_PREFIX}:")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison of *password* against *stored* hash."""
    if not is_password_hash(stored):
        return False
    try:
        _, iters_str, salt_hex, dk_hex = stored.split(":", 3)
        iters = int(iters_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.pbkdf2_hmac(
        _HASH_ALG,
        password.encode("utf-8"),
        salt,
        iters,
        dklen=len(expected),
    )
    return hmac.compare_digest(candidate, expected)


def generate_session_token() -> str:
    """Return a cryptographically random URL-safe token."""
    return secrets.token_urlsafe(32)


def generate_sysop_password() -> str:
    """Return a random human-readable initial sysop password."""
    word_a = secrets.choice([
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
        "golf", "hotel", "india", "juliet", "kilo", "lima",
    ])
    word_b = secrets.choice([
        "mike", "november", "oscar", "papa", "quebec", "romeo",
        "sierra", "tango", "uniform", "victor", "whiskey", "xray",
    ])
    digits = secrets.randbelow(9000) + 1000
    return f"{word_a}-{word_b}-{digits}"
