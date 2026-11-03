"""
core/google_oauth.py — OADSON V3 Google OAuth helper.

Previously the backend stored a `google_refresh_token` on login and never
used it. `google_access_token` (the thing skills actually need to call
Gmail/Calendar/Drive) was never written anywhere. Every Google-integration
skill therefore always received `google_token: None`.

This module closes that gap:
  - exchange_auth_code(): one-time exchange of the mobile app's
    `serverAuthCode` (from @react-native-google-signin/google-signin) for
    an access_token + refresh_token pair.
  - refresh_access_token(): use a stored refresh_token to mint a new
    short-lived access_token when the old one has expired.
  - get_valid_access_token(): the single entry point orchestrator/skills
    should call. Returns a live access token, refreshing + persisting to
    the DB transparently if the cached one is stale or missing.

Requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars (OAuth client
credentials from Google Cloud Console — the same client used for the
Android/iOS sign-in, with a "Web application" client secret configured for
server-side code exchange).
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("oadson.google_oauth")

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh a little early so we never hand a skill a token that expires
# mid-request.
EXPIRY_SAFETY_MARGIN = timedelta(minutes=2)

# Native app flow (google-signin) exchanges the code server-side with no
# redirect — "postmessage" is Google's documented value for this case.
NATIVE_REDIRECT_URI = "postmessage"


class GoogleOAuthError(Exception):
    pass


def _get_client_credentials() -> tuple[str, str]:
    """Read and validate the OAuth client credentials from the environment.

    Purpose: shared guard used by every function that talks to Google's
    token endpoint, so the "not configured" check and error message only
    live in one place.
    Inputs: none (reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars).
    Returns: (client_id, client_secret) tuple. Raises GoogleOAuthError if
    either is missing.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise GoogleOAuthError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not configured.")
    return client_id, client_secret


async def _post_token_request(data: dict) -> dict:
    """POST to Google's token endpoint and parse the response.

    Purpose: shared HTTP + error-handling path for both the auth-code
    exchange and the refresh-token exchange — same endpoint, same failure
    shape, only the payload differs.
    Inputs: form-encoded request body for the token endpoint.
    Returns: parsed JSON response. Raises GoogleOAuthError on non-200.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(GOOGLE_TOKEN_URL, data=data)

    if res.status_code != 200:
        logger.warning(f"Google token request failed: {res.status_code} {res.text}")
        raise GoogleOAuthError(f"Google token request failed: {res.text}")

    return res.json()


def _compute_expiry(token_data: dict, now: Optional[datetime] = None) -> datetime:
    """Compute the absolute expiry timestamp for a token response.

    Purpose: centralizes the expires_in → datetime conversion so callers
    don't each redo `now + timedelta(seconds=...)` with their own default.
    Inputs: raw Google token response, optional reference time.
    Returns: datetime when the access_token expires.
    """
    now = now or datetime.utcnow()
    expires_in = token_data.get("expires_in", 3600)
    return now + timedelta(seconds=expires_in)


async def exchange_auth_code(auth_code: str, redirect_uri: str = NATIVE_REDIRECT_URI) -> dict:
    """
    Exchange a one-time authorization code for access_token + refresh_token
    (+ id_token, when the original scope included "openid").

    redirect_uri must match exactly what was used in the original
    authorization request:
      - Mobile app / google-signin serverAuthCode: NATIVE_REDIRECT_URI
        ("postmessage") — this is the default, so every existing caller
        (POST /auth/google, POST /auth/google/exchange) is unaffected.
      - Browser OAuth redirect flow (login.html -> GET
        /auth/google/callback): pass the actual https callback URL that's
        registered as an Authorized redirect URI in Google Cloud Console.

    Returns the raw Google token response. refresh_token is only returned
    on first consent — callers must not overwrite an existing stored
    refresh_token with None.
    """
    client_id, client_secret = _get_client_credentials()
    return await _post_token_request({
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })


async def refresh_access_token(refresh_token: str) -> dict:
    """Use a stored refresh_token to mint a fresh access_token."""
    client_id, client_secret = _get_client_credentials()
    return await _post_token_request({
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })


async def get_valid_access_token(user: dict, db) -> Optional[str]:
    """
    Return a live Google access token for this user, refreshing and
    persisting to the DB if the cached one is missing/expired.
    Returns None if the user never connected Google (no refresh token on
    file) — callers should treat that as "Google skills unavailable."
    """
    access_token = user.get("google_access_token")
    expiry = user.get("google_token_expiry")
    refresh_token = user.get("google_refresh_token")

    now = datetime.utcnow()
    if access_token and expiry and expiry - EXPIRY_SAFETY_MARGIN > now:
        return access_token

    if not refresh_token:
        return None

    try:
        token_data = await refresh_access_token(refresh_token)
    except GoogleOAuthError as e:
        logger.error(f"Could not refresh Google token for user {user.get('_id')}: {e}")
        return None

    new_access_token = token_data.get("access_token")
    if not new_access_token:
        return None

    new_expiry = _compute_expiry(token_data, now)

    await db["users"].update_one(
        {"_id": user["_id"]},
        {"$set": {
            "google_access_token": new_access_token,
            "google_token_expiry": new_expiry,
        }},
    )

    # Keep the in-memory user dict consistent for the rest of this request.
    user["google_access_token"] = new_access_token
    user["google_token_expiry"] = new_expiry

    return new_access_token


async def get_valid_google_token(user_id: str, db) -> str:
    """
    Compatibility wrapper for the (user_id, db) call signature every
    Google skill (gmail, google_drive, google_calendar, google_tasks,
    google_keep, google_docs) already calls directly — this name was
    referenced but never defined anywhere, so every one of those skills
    raised `NameError: name 'get_valid_google_token' is not defined` on
    first use, orchestrator-level fix or not (they don't read
    payload["google_token"] at all, they call this themselves).

    Looks up the user doc, delegates to get_valid_access_token(), and
    raises instead of returning None so callers get a clear error instead
    of silently passing None as a bearer token.
    """
    from bson import ObjectId

    try:
        query_id = user_id if isinstance(user_id, ObjectId) else ObjectId(user_id)
    except Exception:
        raise GoogleOAuthError(f"Malformed user_id: '{user_id}'")

    user = await db["users"].find_one({"_id": query_id})
    if not user:
        raise GoogleOAuthError(f"User '{user_id}' not found.")

    token = await get_valid_access_token(user, db)
    if not token:
        raise GoogleOAuthError(
            "No Google access token available for this user — "
            "they need to grant Google scopes via /auth/google/exchange first."
        )
    return token
