"""
personal.py — password gate for the private tier.

Single password (PIN-style). Hashed via stdlib hashlib.scrypt with a
per-install salt. Unlock issues a HMAC-signed token in a HttpOnly cookie
that expires after `personal_unlock_minutes` (default 30).

This is local-laptop security, not adversarial. The threat model is
"someone else opens the dashboard on my laptop" — not "nation-state
attacks the brain." Strength is enough to deter casual peeking, not
much more.

Public API:
    is_password_set(con) -> bool
    set_password(con, password) -> None         # first-time setup OR change
    verify_password(con, password) -> bool
    create_unlock_token(con, *, ttl_minutes=30) -> str
    validate_token(con, token) -> bool
    purge_expired_tokens(con) -> int
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path



_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN  = 32


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN,
    )


def is_password_set(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM personal_auth WHERE id = 1"
    ).fetchone()
    return row is not None


def set_password(con: sqlite3.Connection, password: str) -> None:
    """Set or replace the personal password. The server_secret is rotated
    on every set — invalidating any outstanding unlock tokens."""
    if not password or len(password) < 4:
        raise ValueError("password too short (min 4 characters)")
    salt = secrets.token_bytes(16)
    h = _hash(password, salt)
    server_secret = secrets.token_hex(32)
    now = _iso(_now())
    if is_password_set(con):
        con.execute(
            """
            UPDATE personal_auth
            SET password_hash = ?, password_salt = ?, server_secret = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (h.hex(), salt.hex(), server_secret, now),
        )
        # Rotating the secret kills outstanding tokens.
        con.execute("DELETE FROM personal_unlock_token")
    else:
        con.execute(
            """
            INSERT INTO personal_auth(id, password_hash, password_salt,
                                       server_secret, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (h.hex(), salt.hex(), server_secret, now, now),
        )


def verify_password(con: sqlite3.Connection, password: str) -> bool:
    row = con.execute(
        "SELECT password_hash, password_salt FROM personal_auth WHERE id = 1"
    ).fetchone()
    if not row:
        return False
    salt = bytes.fromhex(row["password_salt"])
    expected = bytes.fromhex(row["password_hash"])
    actual = _hash(password, salt)
    return hmac.compare_digest(expected, actual)


def _server_secret(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        "SELECT server_secret FROM personal_auth WHERE id = 1"
    ).fetchone()
    return row["server_secret"] if row else None


def create_unlock_token(con: sqlite3.Connection, *,
                        ttl_minutes: int = 30) -> str:
    """Issue a signed token. The signature ties the token to this
    install's server_secret; rotating the secret (e.g., on password
    change) invalidates everything outstanding."""
    secret = _server_secret(con)
    if not secret:
        raise RuntimeError("no password set")
    raw = secrets.token_hex(16)
    expires = _now() + timedelta(minutes=ttl_minutes)
    expires_iso = _iso(expires)
    sig = hmac.new(secret.encode("utf-8"),
                   f"{raw}|{expires_iso}".encode("utf-8"),
                   hashlib.sha256).hexdigest()[:16]
    token = f"{raw}.{sig}"
    con.execute(
        """
        INSERT INTO personal_unlock_token(token, expires_at, created_at)
        VALUES (?, ?, ?)
        """,
        (token, expires_iso, _iso(_now())),
    )
    return token


def validate_token(con: sqlite3.Connection, token: str | None) -> bool:
    if not token:
        return False
    row = con.execute(
        "SELECT expires_at FROM personal_unlock_token WHERE token = ?",
        (token,),
    ).fetchone()
    if not row:
        return False
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if _now() > expires:
        return False
    return True


def purge_expired_tokens(con: sqlite3.Connection) -> int:
    cur = con.execute(
        "DELETE FROM personal_unlock_token WHERE expires_at < ?",
        (_iso(_now()),),
    )
    return cur.rowcount or 0
