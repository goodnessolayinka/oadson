"""
api/auth.py — OADSON V3 Auth Router.

Google OAuth. Session creation. Logout.
Nothing else lives here.
"""

import base64
import json
import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from dependency import get_db, get_current_user
from core.google_oauth import exchange_auth_code, GoogleOAuthError

logger = logging.getLogger("oadson.api.auth")

router = APIRouter()
security = HTTPBearer(auto_error=False)

SESSION_TTL_DAYS = 30
ALLOWED_EMAILS = {
    *[e.strip() for e in os.getenv("OADSON_ALLOWED_EMAILS", "").split(",") if e.strip()],
    "dev@oadson.local",
}

# ── OAuth redirect-flow config ──
# Replaces Google Identity Services (google.accounts.id/.oauth2), which
# doesn't run reliably inside Android WebView-wrapped apps (WebIntoApp,
# Web2APK, Appilix, Median, etc). login.html now redirects the browser to
# Google directly instead of using the GIS JS SDK; Google redirects back
# here with an authorization code.
#
# GOOGLE_OAUTH_REDIRECT_URI must be added to this OAuth client's
# "Authorized redirect URIs" list in Google Cloud Console, and must match
# this value exactly (scheme, host, path — no trailing slash).
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI",
    "http://localhost:8000/api/v3/auth/google/callback",
)
# Where the callback sends the browser back to (session token in the
# query string, or an error code) — login.html reads these on load.
LOGIN_PAGE_PATH = os.getenv("LOGIN_PAGE_PATH", "http://localhost:3000/login")

# Return targets a mobile app is allowed to receive a login on.
# Anything not in this set falls back to the web login page.
MOBILE_RETURN_URLS = {"oadsonmobile://auth"}


def _decode_oauth_state(state: Optional[str]) -> dict:
    """Decode the base64url JSON state sent by the mobile app. Returns {} if absent or malformed."""
    if not state:
        return {}
    try:
        padded = state + "=" * (-len(state) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _login_return_base(state: Optional[str]) -> str:
    """Where to send the browser after the Google callback.

    Mobile logins go back to the app's deep link. Everything else keeps
    the existing web login page.
    """
    st = _decode_oauth_state(state)
    if st.get("client") == "mobile" and st.get("return_to") in MOBILE_RETURN_URLS:
        return st["return_to"]
    return LOGIN_PAGE_PATH


class GoogleAuthRequest(BaseModel):
    token: str
    # Preferred: the one-time serverAuthCode from
    # @react-native-google-signin/google-signin. Backend exchanges it once
    # for an access_token + refresh_token pair.
    server_auth_code: Optional[str] = None
    # Legacy/back-compat: a raw refresh_token if the client already has one.
    refresh_token: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {"token": "<Google id_token from GIS sign-in>"}
        }
    }


class AuthResponse(BaseModel):
    session_token: str
    user_id: str
    email: str
    name: str
    user_name: str  # duplicate of `name` — login.html reads `user_name`, not `name`
    picture: str
    whitelisted: bool


class GoogleExchangeRequest(BaseModel):
    code: str

    model_config = {
        "json_schema_extra": {
            "example": {"code": "<one-time code from Google's code client popup>"}
        }
    }


# ── PRIVATE HELPERS ──

def _verify_google_identity(token: str) -> dict:
    """Verify a Google id_token and return its decoded claims.

    Purpose: wraps google-auth's verify_oauth2_token with our own error
    handling so route handlers don't touch the Google SDK directly.
    Inputs: raw id_token from GIS sign-in.
    Returns: decoded token claims dict. Raises HTTPException(401/500).
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(500, "GOOGLE_CLIENT_ID not configured.")

    try:
        return id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
    except Exception as e:
        logger.warning(f"Google token verification failed: {e}")
        raise HTTPException(401, "Invalid Google token.")


def _validate_whitelist(email: str) -> None:
    """Raise 403 if the email isn't in ALLOWED_EMAILS.

    Purpose: single choke point for whitelist enforcement in this router.
    Inputs: email string.
    Returns: None. Raises HTTPException(403) if not whitelisted.
    """
    if email not in ALLOWED_EMAILS:
        raise HTTPException(403, "Email not whitelisted. Contact admin.")


async def _fetch_google_tokens(server_auth_code: Optional[str], refresh_token: Optional[str], email: str) -> dict:
    """Resolve Google API tokens (access + refresh) to store on the user.

    Purpose: consolidates the two ways a client can hand us Google API
    credentials during /auth/google — a fresh serverAuthCode to exchange,
    or (legacy) an already-obtained refresh_token.
    Inputs: optional server_auth_code, optional refresh_token, email (for
    logging only).
    Returns: dict of fields to merge into the user update. Never raises —
    a failed exchange is logged and login proceeds without Google access.
    """
    update: dict = {}
    now = datetime.utcnow()

    if server_auth_code:
        try:
            token_data = await exchange_auth_code(server_auth_code)
            if token_data.get("access_token"):
                update["google_access_token"] = token_data["access_token"]
                expires_in = token_data.get("expires_in", 3600)
                update["google_token_expiry"] = now + timedelta(seconds=expires_in)
            # refresh_token is only sent by Google on first consent — don't
            # clobber a previously stored one if this exchange didn't get one.
            if token_data.get("refresh_token"):
                update["google_refresh_token"] = token_data["refresh_token"]
        except GoogleOAuthError as e:
            logger.warning(f"Google auth code exchange failed for {email}: {e}")
            # Don't fail login over this — Google skills just won't work
            # until the client re-sends a valid server_auth_code.
    elif refresh_token:
        update["google_refresh_token"] = refresh_token

    return update


async def _upsert_user(db, email: str, name: str, picture: str, google_token_fields: dict) -> dict:
    """Create or update the user document for this email.

    Purpose: single upsert call combining identity fields and any Google
    API token fields resolved this request.
    Inputs: db handle, identity fields, google_token_fields from
    _fetch_google_tokens.
    Returns: the user document after upsert.

    FIX 2026-08-07: 'picture' used to be in $set unconditionally, so
    EVERY Google login/re-auth overwrote whatever picture the user had
    set via POST /settings/profile/picture with their Google account
    photo — indistinguishable from "the picture keeps resetting itself"
    from the user's side, at whatever cadence they end up re-authing.
    Google's photo is now only used as the one-time default for a
    brand-new user ($setOnInsert); once a user exists, their picture is
    never touched by login again — only the explicit picture-upload
    endpoint changes it after that point.
    """
    now = datetime.utcnow()
    user_update: dict = {
        "email": email,
        "name": name,
        "last_login": now,
        **google_token_fields,
    }

    await db["users"].update_one(
        {"email": email},
        {
            "$set": user_update,
            "$setOnInsert": {"created_at": now, "picture": picture},
        },
        upsert=True,
    )

    return await db["users"].find_one({"email": email})


async def _create_session(db, user_id) -> str:
    """Create a session document and return its token.

    Purpose: single place that mints session tokens and TTL, so the
    expiry policy (SESSION_TTL_DAYS) only lives in one spot.
    Inputs: db handle, user's _id.
    Returns: the new session_token string.
    """
    session_token = str(uuid.uuid4())
    now = datetime.utcnow()
    await db["sessions"].insert_one({
        "session_token": session_token,
        "user_id": user_id,
        "created_at": now,
        "expires_at": now + timedelta(days=SESSION_TTL_DAYS),
    })
    return session_token


def _build_auth_response(session_token: str, user: dict, email: str, name: str, picture: str) -> AuthResponse:
    """Assemble the AuthResponse payload.

    Purpose: keeps the response shape (including the user_name/name
    duplication login.html relies on) defined in one place.
    Inputs: session_token, user doc, identity fields.
    Returns: AuthResponse.
    """
    return AuthResponse(
        session_token=session_token,
        user_id=str(user["_id"]),
        email=email,
        name=name,
        user_name=name,
        picture=picture,
        whitelisted=True,
    )


# ── ROUTES ──

@router.post(
    "/auth/google",
    response_model=AuthResponse,
    summary="Sign in with Google (identity only)",
    description=(
        "Verifies a Google id_token (GIS sign-in) and issues an OADSON "
        "session. This does NOT grant Gmail/Calendar/Drive access — call "
        "POST /auth/google/exchange afterward for that."
    ),
)
async def google_auth(body: GoogleAuthRequest, db=Depends(get_db)):
    info = _verify_google_identity(body.token)

    email = info.get("email", "")
    name = info.get("name", "")
    picture = info.get("picture", "")

    _validate_whitelist(email)

    google_token_fields = await _fetch_google_tokens(body.server_auth_code, body.refresh_token, email)
    user = await _upsert_user(db, email, name, picture, google_token_fields)
    session_token = await _create_session(db, user["_id"])

    logger.info(f"Auth: {email}")

    # Use the picture actually stored on the user doc (their custom
    # upload, if any) rather than the fresh Google photo just fetched —
    # see _upsert_user's fix note. Falls back to the Google photo only
    # for a brand-new user where they're the same value anyway.
    stored_picture = user.get("picture") or picture
    return _build_auth_response(session_token, user, email, name, stored_picture)


@router.get(
    "/auth/google/callback",
    summary="OAuth redirect callback (Authorization Code flow)",
    description=(
        "Browser-facing OAuth callback. Mobile sign-ins carry a state with "
        "return_to=oadsonmobile://auth and are sent back to the app; web "
        "sign-ins are sent to the login page as before."
    ),
)
async def google_auth_callback(
    code: Optional[str] = None,
    error: Optional[str] = None,
    state: Optional[str] = None,
    db=Depends(get_db),
):
    # Mobile sign-ins send a state with client=mobile and return_to set to
    # the app's deep link. Web sign-ins send no state and keep going to
    # LOGIN_PAGE_PATH.
    base = _login_return_base(state)

    # User declined consent, or Google sent back an error instead of a code.
    if error or not code:
        if error:
            logger.info(f"Google OAuth redirect returned an error: {error}")
        return RedirectResponse(f"{base}?error=denied")

    # Exchange the code using the *redirect* URI, not "postmessage" — this
    # must match exactly what was sent in the original authorization
    # request.
    try:
        token_data = await exchange_auth_code(code, redirect_uri=GOOGLE_OAUTH_REDIRECT_URI)
    except GoogleOAuthError as e:
        logger.warning(f"Google auth code exchange failed (redirect flow): {e}")
        return RedirectResponse(f"{base}?error=exchange_failed")

    id_tok = token_data.get("id_token")
    if not id_tok:
        # Fail closed rather than proceed with no verified identity.
        logger.warning("Google token response missing id_token (redirect flow).")
        return RedirectResponse(f"{base}?error=exchange_failed")

    try:
        info = _verify_google_identity(id_tok)
    except HTTPException:
        return RedirectResponse(f"{base}?error=invalid_token")

    email = info.get("email", "")
    name = info.get("name", "")
    picture = info.get("picture", "")

    if email not in ALLOWED_EMAILS:
        logger.info(f"Google OAuth redirect: non-whitelisted email attempted login ({email}).")
        return RedirectResponse(f"{base}?error=denied")

    # Identity-only flow — no Gmail/Calendar/Drive scopes requested here.
    user = await _upsert_user(db, email, name, picture, {})
    session_token = await _create_session(db, user["_id"])

    logger.info(f"Auth (redirect flow): {email} -> {base.split('?')[0]}")

    # Use the actually stored picture (custom upload survives), not the fresh Google one.
    stored_picture = user.get("picture") or picture

    params = urlencode({
        "session_token": session_token,
        "name": name,
        "picture": stored_picture,
        "email": email,
    })
    return RedirectResponse(f"{base}?{params}")


@router.post(
    "/auth/google/exchange",
    summary="Grant Google scopes (Gmail/Calendar/Drive/etc.)",
    description=(
        "Step 2 of login — call after POST /auth/google succeeds, "
        "authenticated with the session token from that response. Exchanges "
        "a one-time code (from Google's code-client popup requesting Gmail/"
        "Calendar/Drive/Tasks scopes) for an access_token + refresh_token, "
        "stored so Google-integration skills work."
    ),
)
async def google_exchange(
    body: GoogleExchangeRequest,
    db=Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """
    Step 2 of login.html's two-step flow: after identity sign-in (POST
    /auth/google) succeeds, the client separately requests Gmail/Calendar/
    Drive/Tasks scopes via Google's code client and posts the resulting
    one-time `code` here, authenticated as the user from step 1. Exchanges
    it for an access_token + refresh_token and stores both on the user.

    Non-fatal on failure — the client proceeds to chat either way and
    Google skills just won't work until a valid code is exchanged.
    """
    try:
        token_data = await exchange_auth_code(body.code)
    except GoogleOAuthError as e:
        logger.warning(f"Google scope exchange failed for {user.get('email')}: {e}")
        raise HTTPException(400, str(e))

    if not token_data.get("access_token"):
        raise HTTPException(400, "Google did not return an access token.")

    now = datetime.utcnow()
    expires_in = token_data.get("expires_in", 3600)
    update: dict = {
        "google_access_token": token_data["access_token"],
        "google_token_expiry": now + timedelta(seconds=expires_in),
    }
    # refresh_token is only sent by Google on first consent — don't clobber
    # a previously stored one if this exchange didn't get one.
    if token_data.get("refresh_token"):
        update["google_refresh_token"] = token_data["refresh_token"]

    await db["users"].update_one({"_id": user["_id"]}, {"$set": update})

    logger.info(f"Google scopes granted: {user.get('email')}")
    return {"status": "ok", "message": "Google scopes granted."}


@router.post("/auth/logout", summary="Invalidate the current session")
async def logout(
    db=Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
    user: dict = Depends(get_current_user),
):
    """
    Was previously a no-op — it validated the token but never deleted the
    session, so sessions never actually got invalidated server-side.
    """
    if credentials and credentials.credentials:
        await db["sessions"].delete_one({"session_token": credentials.credentials})
    return {"status": "ok", "message": "Logged out."}


@router.delete("/auth/logout")
async def logout_by_token(token: str, db=Depends(get_db)):
    """Direct token deletion. Frontend passes token explicitly."""
    result = await db["sessions"].delete_one({"session_token": token})
    if result.deleted_count == 0:
        raise HTTPException(404, "Session not found.")
    return {"status": "ok"}
