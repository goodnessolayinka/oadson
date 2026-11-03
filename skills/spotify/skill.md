# Spotify

## What It Does
Searches Spotify for tracks, albums, artists, and playlists. Returns playback links, audio features, and track metadata. Requires Spotify OAuth or Client Credentials flow.

## When to Use It
- User says "search Spotify for", "find this song", "play X on Spotify"
- User wants music recommendations or playlist info
- User wants track metadata (BPM, key, energy, danceability)
- User looking up an artist's discography

## When NOT to Use It
- User wants to control active playback (skip, pause) — requires Spotify Premium + device
- No Spotify credentials configured — return auth error
- User wants YouTube music → use youtube skill

## Calling Convention
Single-purpose skill — no `action` parameter. Required: `query`.

## Output Format
```json
{
  "tracks": [
    {
      "name": "Essence",
      "artist": "Wizkid ft. Tems",
      "album": "Made in Lagos",
      "url": "https://open.spotify.com/track/...",
      "duration_ms": 214400,
      "popularity": 87,
      "audio_features": {
        "tempo_bpm": 102,
        "energy": 0.72,
        "danceability": 0.84,
        "key": "F# Major"
      }
    }
  ]
}
```

## Constraints
- Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET
- Playback control requires Premium account + active device
- Search returns top 10 results by default
- Audio features available for tracks only (not podcasts)
- Market defaults to NG (Nigeria) — adjust if needed

## Examples
- "Find Afrobeats songs for my focus playlist"
- "Search for 'Gbona' by Burna Boy"
- "What's the energy level of this track?"
