"""Secret generation and verification.

Agent tokens look like ``ahk_<agent_id>_<secret>``. The agent id lets the hub
find the row without searching by hash; the secret is 256 bits from
``secrets``. Because the secret is high-entropy, a single SHA-256 is enough
(a slow KDF only helps against guessable secrets). Comparison is always
constant-time.

Enrollment codes are short and human-typeable (they get read aloud or pasted
between terminals). They are protected by a 15-minute expiry, single use, and
rate limits, and are also stored only as a hash.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_PREFIX = "ahk_"
# Crockford-style alphabet without look-alikes (no 0/O, 1/I/L).
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 12  # ~59 bits


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_agent_id() -> str:
    return "ag" + secrets.token_hex(6)


def new_token(agent_id: str) -> str:
    return f"{TOKEN_PREFIX}{agent_id}_{secrets.token_urlsafe(32)}"


def parse_token(token: str) -> tuple[str, str] | None:
    """Split a token into (agent_id, secret); None if malformed."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    agent_id, sep, secret = token[len(TOKEN_PREFIX) :].partition("_")
    if not sep or not agent_id or not secret:
        return None
    return agent_id, secret


def hash_token(token: str) -> str:
    return sha256_hex(token)


def verify_token(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def new_enrollment_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + 4] for i in range(0, CODE_LENGTH, 4))


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(code: str) -> str:
    return sha256_hex("enroll:" + normalize_code(code))
