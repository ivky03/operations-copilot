"""Password hashing and session tokens.

- Passwords are hashed with Argon2id, a deliberately slow, memory-hard algorithm, so a stolen
  hash is expensive to brute-force. The plain password is never stored or logged.
- Session tokens are 256-bit random values. Only their SHA-256 hash is stored in the database.
"""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

# Verified against when the email doesn't exist, so "unknown email" and "wrong password" take
# the same time. Otherwise response timing would reveal which emails have accounts.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-password")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    """URL-safe random token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time comparison, so timing doesn't leak how many characters matched."""
    return hmac.compare_digest(a.encode(), b.encode())
