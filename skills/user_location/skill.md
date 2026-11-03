# User Location

## What It Does
Converts the user's OWN real GPS coordinates — already shared by them —
into a readable place: city, region, country. Nothing more. It never
looks up, guesses, or estimates location on its own initiative.

## When to Use It
- The user's message already contains real coordinates they just shared
  (e.g. "My current location: latitude 6.52, longitude 3.37" — this is
  how a location share from the website, app, WhatsApp, or Telegram
  shows up once the user has actually shared it)
- Immediately after that, to answer whatever they originally asked
  ("what's the weather here", "restaurants near me", etc.)

## When NOT to Use It
- The user asked something location-dependent but you don't have real
  coordinates yet — do NOT call this skill with guessed, remembered, or
  IP-based coordinates. Just ask them in plain conversation to share
  their location (the website/app has a "Share location" option in the
  message composer; WhatsApp and Telegram both have a native
  location-share feature under the attachment/paperclip icon). Wait for
  their next message — once shared, it will contain real coordinates you
  can then call this skill with.
- User wants to look up a DIFFERENT ip/location (not their own) → use
  ip_geolocation instead — that skill is for investigating arbitrary
  third-party IPs (VPN detection, network recon), not "where is the
  user right now."

## How to Call It
```
User's message: "My current location: latitude 6.5244, longitude 3.3792"
→ call user_location(action="from_coordinates", latitude=6.5244, longitude=3.3792)
```

## Output Format
```json
{
  "status": "success",
  "readable_location": "Lagos, Lagos, Nigeria",
  "city": "Lagos",
  "region": "Lagos",
  "country": "Nigeria",
  "latitude": 6.5244,
  "longitude": 3.3792,
  "maps_link": "https://maps.google.com/?q=6.5244,3.3792"
}
```

## Constraints
- Requires real coordinates already present in the conversation — this
  skill has no way to obtain them itself
- Reverse-geocoding accuracy is neighborhood/city-level, not exact-address
- Rural or sparsely-mapped coordinates may return a larger/less precise
  region

## Examples
- "What's the weather like where I am?" → ask for location if not
  already shared → once shared, call this skill, then answer the
  weather question using the resolved city
- "Any good restaurants near me?" → same pattern
