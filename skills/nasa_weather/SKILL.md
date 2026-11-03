# nasa_weather

## What It Does
Fetches **historical daily climate data** — temperature, precipitation,
and solar radiation — for a specific latitude/longitude and date range,
via NASA's POWER (Prediction Of Worldwide Energy Resources) API.

**This is NOT real-time weather, solar flares, asteroids, or APOD** —
none of those are covered by this skill. For a real-time forecast, use
the separate `weather_forecast`/`weather` skill instead. There is no
action for space weather, near-Earth objects, or astronomy picture of
the day anywhere in this skill.

## Calling Convention
No `action` parameter — every call needs all four:

| Param | Type | Notes |
|---|---|---|
| `latitude` | number | required |
| `longitude` | number | required |
| `start_date` | string | required, `YYYYMMDD` format |
| `end_date` | string | required, `YYYYMMDD` format |

```
nasa_weather latitude=6.5244 longitude=3.3792 start_date=20260101 end_date=20260107
```

## When to Use It
- User asks about historical temperature, rainfall, or solar radiation
  for a location and date range
- Agricultural/climate-planning style questions ("what was rainfall like
  in X during planting season last year")

## When NOT to Use It
- Current or forecast weather → `weather_forecast`
- Anything about the sun's activity, space weather, solar flares,
  asteroids, or astronomy imagery — this skill has no such capability,
  don't imply it does
