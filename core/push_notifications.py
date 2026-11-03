"""
core/push_notifications.py — sends push notifications via OneSignal's
REST API, targeting a specific user by their external_id.

WHY THIS EXISTS
================
Added 2026-08-25 per explicit request: "notifications for when OADSON is
done" — specifically for the Median-wrapped Android app, where a turn
that takes a while (a long tool-calling loop, a slow skill) currently
gives no signal once the user has backgrounded the app or locked their
phone. OneSignal (connected via Median's App Studio plugin, not raw Web
Push — WebViews don't support the Web Push API natively, confirmed via
Median's own docs) is the actual delivery mechanism; this module is the
server-side half that tells OneSignal to actually send one.

WEB PUSH 2026-10-08 — the frontend built its own, separate Web Push path
(a real service worker + chat page, independent of Median's native
Android wrapper) and specified the exact payload shape this function
needs to add — see BACKEND_PUSH_PATCH.md, applied below. `web_url`,
`data.session_id`, and `web_buttons` are the three fields that only
matter to the WEB push path; Median's native Android app ignores
web_buttons entirely (it uses OneSignal's separate native `buttons`
field, which has no text-input/quick-send support — not something this
function adds, per the frontend dev's own note).

TARGETING
=========
OneSignal notifications here are sent via `include_aliases` with an
`external_id` — the same identifier median.onesignal.login(userId) (for
the native app) or OneSignal.login(userId) (for the web/PWA build, per
BACKEND_PUSH_PATCH.md) registers a device under. CONFIRMED identifier:
email, not the Mongo _id (see core/response_pipeline.py's call site
comment for why). If a user was never registered client-side, sending
to their external_id simply reaches zero devices — OneSignal's API
call still succeeds, it just has no one to deliver to. This module
never raises for that case; see send_completion_notification's
docstring for the full non-blocking contract.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("oadson.push_notifications")

ONESIGNAL_API_URL = "https://onesignal.com/api/v1/notifications"


async def send_completion_notification(
    user_id: str,
    title: str,
    message: str,
    url: str | None = None,
    session_id: str | None = None,
    quick_reply_text: str | None = None,
) -> bool:
    """
    Sends a push notification to a specific user's registered device(s)
    via OneSignal, targeted by external_id (== user_id — same value
    median.onesignal.login() / OneSignal.login() registers client-side).

    Returns True if OneSignal accepted the send request, False on any
    failure (missing config, network error, non-2xx response). NEVER
    raises — this is fire-and-forget background signal, not something
    that should ever break or slow down the actual turn completing.
    Callers should not await this in the hot path of returning a
    response to the user; see core/response_pipeline.py's _finalize for
    where this gets called via asyncio.create_task, same fire-and-forget
    pattern already used there for style-profile learning.

    title/message: keep these SHORT — this is a phone notification, not
    a chat reply. A one-line summary of what finished, not the full
    response text (which may contain markdown, code blocks, or be long
    enough to be unreadable as a notification body anyway).

    url: relative deep link path (e.g. "/chat/abc123") — used to build
    BOTH the native app's `data.targetUrl` (Median path, unchanged
    behavior) AND the absolute `web_url` the frontend's Web Push buttons
    need (OneSignal requires a full URL there, not a relative path — see
    BACKEND_PUSH_PATCH.md). Pass None for no specific deep link.

    session_id: ADDED 2026-10-08, per BACKEND_PUSH_PATCH.md — the
    frontend's service worker reads this directly from `data.session_id`
    (separate from the URL string) to know which chat a quick-reply
    button should send to, without having to parse it back out of a
    path. Pass whenever url/session routing is relevant; omitted (None)
    means no session-specific buttons are offered (falls back to a
    plain notification with no actions), which is correct for a
    notification that isn't about any particular chat.

    quick_reply_text: ADDED 2026-10-08 — the label AND the literal text
    sent if the user taps the quick-reply button (per the frontend
    spec's "qr:<text> is sent verbatim as the user's message" contract).
    Caller picks this per notification type — e.g. "Continue" for a
    finished task, "Yes" for a yes/no question the agent asked — per
    BACKEND_PUSH_PATCH.md's own guidance that different pushes should
    carry different buttons. None means no quick-reply button is
    offered (e.g. nothing sensible to auto-reply with); Reply-only in
    that case rather than guessing a label that might not fit.
    """
    app_id = os.getenv("ONESIGNAL_APP_ID", "")
    api_key = os.getenv("ONESIGNAL_REST_API_KEY", "")
    if not app_id or not api_key:
        logger.debug("Push notification skipped — ONESIGNAL_APP_ID/ONESIGNAL_REST_API_KEY not set.")
        return False

    payload = {
        "app_id": app_id,
        "include_aliases": {"external_id": [str(user_id)]},
        "target_channel": "push",
        "headings": {"en": title[:65]},  # OneSignal/Android practical heading limit
        "contents": {"en": message[:180]},
    }

    if url:
        # BUGFIX 2026-10-08 — REAL PRODUCTION BUG, confirmed and fixed.
        # targetUrl was being sent as the bare relative path passed in
        # (e.g. "/chat/abc123"), but Median's own integration docs are
        # explicit this MUST be a full absolute URL:
        # https://documentation.onesignal.com/docs/en/median-integration
        # — "data": {"targetUrl": "https://example.com/page"}. A relative
        # path isn't a resolvable URI for Median's native deep-link
        # handler, which was producing "app not installed" on EVERY
        # notification tap in the Median app — nothing to do with the
        # installed APK version (re-downloading Median would not have
        # fixed this; the payload itself was malformed). web_url already
        # got correct absolute-URL treatment below; targetUrl never did,
        # despite needing the exact same fix. Built once, upfront, and
        # reused for both fields instead of duplicating the absolute-URL
        # construction or fixing only one of the two places that needed it.
        frontend_base = os.getenv("FRONTEND_BASE_URL", "")
        absolute_url = f"{frontend_base.rstrip('/')}{url}" if frontend_base else None

        data_block = {"targetUrl": absolute_url or url}
        if session_id:
            data_block["session_id"] = session_id
        payload["data"] = data_block

        # FRONTEND_BASE_URL must be set to the real deployed domain
        # (e.g. https://oadson-site.example.com) for targetUrl/web_url to
        # resolve correctly. If it's missing, targetUrl falls back to the
        # original relative path (same broken behavior as before this
        # fix, logged clearly below) rather than silently sending an
        # empty or malformed value.
        if absolute_url:
            payload["web_url"] = absolute_url

            # BUGFIX (caught by testing before deploy) — web_buttons was
            # being built and sent regardless of whether web_url was
            # actually set, which meant a missing FRONTEND_BASE_URL
            # produced a payload with Reply/quick-reply buttons but NO
            # valid absolute URL for them to open — buttons that would
            # tap and do nothing useful. Nested inside the frontend_base
            # check now, so buttons are only ever sent alongside a real,
            # valid web_url.
            if session_id:
                # Chrome on Android shows at most 2 buttons (frontend
                # dev's own confirmed limit) — Reply is always offered
                # when there's a session to reply to; quick_reply_text is
                # the second, caller-chosen slot, omitted entirely (not a
                # blank button) when the caller has nothing sensible to
                # suggest.
                buttons = [{"id": "reply", "text": "Reply"}]
                if quick_reply_text:
                    buttons.append({"id": f"qr:{quick_reply_text}", "text": quick_reply_text[:20]})
                payload["web_buttons"] = buttons
        else:
            logger.warning(
                "FRONTEND_BASE_URL not set — targetUrl falls back to a RELATIVE path "
                "(will likely break Median's native deep-link / cause 'app not installed' "
                "on tap), and web_url/web_buttons are omitted entirely. Set "
                "FRONTEND_BASE_URL to the real deployed domain to fix both."
            )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                ONESIGNAL_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Key {api_key}",
                    "Content-Type": "application/json",
                },
            )
        if r.status_code >= 400:
            # Logged, not raised — a bad OneSignal response (e.g. app_id
            # typo, no subscribers under this external_id yet) should
            # never surface as a user-facing error for the turn that
            # triggered it.
            logger.warning(f"OneSignal send failed ({r.status_code}) for user {user_id}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"OneSignal send raised (non-fatal) for user {user_id}: {e}")
        return False
