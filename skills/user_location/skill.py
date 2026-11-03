"""
skills/user_location/skill.py — reverse-geocode the user's OWN current
coordinates into a readable place (city/region/country).

Added 2026-08-26 — "add a location feature". This is deliberately
separate from ip_geolocation (that skill investigates ARBITRARY IPs for
network recon/VPN detection — see its skill.md; conflating "where is
this IP" with "where is the user right now" was the wrong fix here).

This skill NEVER guesses or looks anything up on its own — it only ever
converts real coordinates the user has already shared into something
readable. Where those coordinates come from is surface-specific and
handled upstream, before this skill is ever called:
  - WhatsApp: a locationMessage share is turned into a synthetic user
    message ("My current location: lat X, lon Y") by api/whatsapp.py.
  - Telegram: same treatment for message.location, in api/telegram.py.
  - Website/App: a "Share location" action in the message composer uses
    the browser's real Geolocation API and sends the same synthetic text.

The model is expected to just ask in plain conversation when it needs
location and doesn't have coordinates yet ("Can you share your
location?") — no separate "request" mechanism needed; the user's next
message, once they've shared it from whichever surface they're on,
naturally contains coordinates for the model to notice and act on by
calling this skill. See skill.md for the exact trigger conditions.
"""

import httpx

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# Nominatim's usage policy requires a real, non-stock User-Agent
# identifying the application — a default httpx/requests User-Agent is
# explicitly disallowed and can get the calling IP banned. This is a
# single-user personal app making occasional, human-paced requests
# (never bulk/systematic queries), which is exactly the use case their
# free public policy is meant to accommodate.
_HEADERS = {"User-Agent": "OADSON-Personal-Assistant/1.0 (single-user; http://localhost:3000)"}


async def _reverse_geocode(lat: float, lon: float) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                NOMINATIM_URL,
                params={"format": "json", "lat": lat, "lon": lon, "zoom": 14, "addressdetails": 1},
                headers=_HEADERS,
                timeout=15,
            )
        if response.status_code != 200:
            return {"status": "error", "reason": f"Reverse geocoding failed (HTTP {response.status_code})"}

        data = response.json()
        if "error" in data:
            return {"status": "error", "reason": data.get("error", "No location data for these coordinates")}

        addr = data.get("address", {}) or {}
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb")
        region = addr.get("state") or addr.get("region")
        country = addr.get("country")

        readable = ", ".join(p for p in (city, region, country) if p) or data.get("display_name", "Unknown location")

        return {
            "status": "success",
            "readable_location": readable,
            "city": city,
            "region": region,
            "country": country,
            "country_code": addr.get("country_code"),
            "latitude": lat,
            "longitude": lon,
            "maps_link": f"https://maps.google.com/?q={lat},{lon}",
            "message": f"You're in {readable}.",
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def run(payload):
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if action != "from_coordinates":
        return {
            "status": "error",
            "reason": "This skill only supports action='from_coordinates', called with the "
                      "latitude/longitude the user already shared in their message. If you "
                      "don't have real coordinates yet, ask the user to share their location "
                      "instead of calling this skill.",
        }

    lat, lon = args.get("latitude"), args.get("longitude")
    if lat is None or lon is None:
        return {"status": "error", "reason": "from_coordinates requires both 'latitude' and 'longitude'"}

    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"status": "error", "reason": "latitude/longitude must be numbers"}

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"status": "error", "reason": "latitude/longitude out of valid range"}

    result = await _reverse_geocode(lat, lon)

    # PERSISTENCE 2026-08-26 — "I thought it should use my last saved
    # location, or ask every time". Every earlier call just resolved
    # coordinates and forgot them the instant the turn ended — the next
    # conversation, even minutes later, had to ask again from scratch.
    # Stored here via the same MemoryVault every other skill already uses
    # (skills/memory_manager/skill.py's own "save" action does exactly
    # this) rather than inventing a separate mechanism. This feeds
    # straight into the semantic memory search
    # (memory.vault.MemoryVault.search, called from orchestrator.py as
    # `vault.search(objective, ...)`) that's already injected into every
    # system prompt as the memory block — so a future "what's the
    # weather near me" CAN surface this without a fresh share, without
    # this skill or the model needing any special-case logic for it.
    # Deliberately doesn't stop the model from asking again when it
    # judges the stored location might be stale (traveling, long gap
    # since last update, etc.) — both behaviors the user asked for
    # ("use last saved" and "ask every time") stay genuinely available;
    # this just makes the first one possible at all, which it never was.
    if result.get("status") == "success":
        db = payload.get("db")
        user_id = payload.get("user_id")
        if db is not None and user_id:
            try:
                from memory.vault import MemoryVault
                vault = MemoryVault(db, user_id)
                await vault.store(
                    "current_location",
                    f"{result['readable_location']} (lat {lat}, lon {lon}, as of "
                    f"{__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})",
                    category="location",
                )
            except Exception as e:
                logger.warning(f"user_location: could not persist to memory — {e}")

    return result
