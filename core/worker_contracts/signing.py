"""
Signed requests between the owner (Render) and the worker (Railway).

Standard library only, so both sides can use it without extra packages.

Signature = HMAC-SHA256(INTERNAL_SECRET, canonical), hex-encoded, where

    canonical = METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + sha256_hex(BODY)

Requests older or newer than MAX_SKEW_S seconds are rejected, which limits
how long a captured request is worth anything. Comparison is constant-time.
"""
import hashlib
import hmac
import time

MAX_SKEW_S = 300
TS_HEADER = "X-Oadson-Timestamp"
SIG_HEADER = "X-Oadson-Signature"


def _canonical(method: str, path: str, timestamp: int, body: bytes) -> bytes:
    return "\n".join([
        method.upper(),
        path,
        str(timestamp),
        hashlib.sha256(body).hexdigest(),
    ]).encode()


def _signature(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), _canonical(method, path, timestamp, body),
                    hashlib.sha256).hexdigest()


def sign(secret: str, method: str, path: str, body: bytes, now: float | None = None) -> dict:
    """Headers to attach to an outgoing request."""
    ts = int(now if now is not None else time.time())
    return {TS_HEADER: str(ts), SIG_HEADER: _signature(secret, method, path, ts, body)}


def verify_any(secrets: list, method: str, path: str, body: bytes,
               timestamp: str | None, signature: str | None,
               now: float | None = None) -> tuple[bool, str]:
    """Accept a request signed with any of the given secrets (used during rotation)."""
    usable = [x for x in secrets if x]
    if not usable:
        return False, "this service has no INTERNAL_SECRET set, so it refuses all requests"
    last = (False, "signature does not match")
    for secret in usable:
        ok, why = verify(secret, method, path, body, timestamp, signature, now=now)
        if ok:
            return True, "ok"
        last = (False, why)
    return last


def verify(secret: str, method: str, path: str, body: bytes,
           timestamp: str | None, signature: str | None,
           now: float | None = None) -> tuple[bool, str]:
    """(ok, reason). Fails closed: an empty secret rejects everything."""
    if not secret:
        return False, "this service has no INTERNAL_SECRET set, so it refuses all requests"
    if not timestamp or not signature:
        return False, "missing signature headers"
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "timestamp is not a whole number"
    current = int(now if now is not None else time.time())
    if abs(current - ts) > MAX_SKEW_S:
        return False, "timestamp is outside the allowed window (check the clocks)"
    expected = _signature(secret, method, path, ts, body)
    if not hmac.compare_digest(expected, signature):
        return False, "signature does not match (check INTERNAL_SECRET is identical on both sides)"
    return True, "ok"
