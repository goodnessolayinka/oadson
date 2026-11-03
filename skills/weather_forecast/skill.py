"""
weather_forecast — fixed, twice now.

Bugs found in this file (chronological):

1. (earlier fix, kept) Used `requests` synchronously inside async def
   run() — froze the whole FastAPI process per call. Fixed: httpx.AsyncClient.

2. (earlier fix, kept) "hours ahead" math ignored OpenWeather's 3-hour
   granularity — hours=5 silently returned the next 15 hours.

3. (earlier fix, kept) No real "target a specific day" logic, and no
   clear timestamp labeling — near-term forecast data could easily read
   as "current conditions."

4. (this fix) `_fmt()` referenced `wind_flag` in its f-string, which was
   NEVER DEFINED — only `rain_flag` existed. This is a NameError on
   every single call that reached this line, meaning the "full
   analysis" formatting path was completely broken in production; the
   caller either got a bare exception (status=error) or, if the model
   fell back to describing the raw returned dict itself, an
   AI-generated summary instead of real numbers — which matches "it
   just summarizes instead of giving me the real numbers." Fixed by
   removing the dangling reference.

5. (this fix) No wind DIRECTION was ever extracted — only wind_speed_ms.
   Added compass direction (N/NE/E/etc.) computed from OpenWeather's
   `wind.deg`, plus the raw degrees, per entry.

6. (this fix) No precipitation VOLUME (mm) was surfaced, only percent
   chance of rain. Added rain_mm / snow_mm from OpenWeather's
   `rain.3h` / `snow.3h` fields when present.

7. (this fix) No way to ask for a SPECIFIC hour ("what's it doing at
   3pm") — only "next N hours" or "next N days" as a block. Added
   `at_hour` (0-23, local time) + optional `day_offset` (0=today,
   1=tomorrow, ...) to pick the single closest 3-hour bucket to that
   local time and return just that one, clearly labeled. Local time is
   computed from OpenWeather's own `city.timezone` offset in the
   forecast response — NOT the server's timezone — so this is accurate
   for whatever city was requested, not just Nigeria.

   Honesty note baked into the response: OpenWeather's free /forecast
   endpoint is only 3-HOUR resolution. Asking for 15:00 doesn't get a
   real 15:00 reading — it gets whichever 3-hour bucket (e.g. 15:00 or
   18:00, whichever is nearer) is closest, and the response says so
   explicitly rather than pretending it's exact.
"""

import os
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("oadson.skill.weather_forecast")

_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass_dir(deg) -> str:
    if deg is None:
        return "?"
    return _COMPASS[int((deg / 22.5) + 0.5) % 16]


def _label_entry(entry: dict, now_utc: datetime, tz_offset_secs: int) -> dict:
    dt_txt = entry.get("dt_txt")  # e.g. "2026-07-15 15:00:00", UTC
    local_time_str = "unknown"
    when_label = "unknown"
    try:
        entry_time_utc = datetime.strptime(dt_txt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        hours_from_now = round((entry_time_utc - now_utc).total_seconds() / 3600, 1)
        when_label = f"{hours_from_now}h from now" if hours_from_now >= 0 else "past"
        local_time = entry_time_utc + timedelta(seconds=tz_offset_secs)
        local_time_str = local_time.strftime("%a %d %b, %H:%M local")
    except (TypeError, ValueError):
        pass

    main = entry.get("main", {})
    weather = (entry.get("weather") or [{}])[0]
    wind = entry.get("wind", {})
    rain_mm = (entry.get("rain") or {}).get("3h")
    snow_mm = (entry.get("snow") or {}).get("3h")

    return {
        "time_utc": dt_txt,
        "local_time": local_time_str,
        "when": when_label,
        "temp_c": main.get("temp"),
        "feels_like_c": main.get("feels_like"),
        "condition": weather.get("description"),
        "humidity_pct": main.get("humidity"),
        "wind_speed_ms": wind.get("speed"),
        "wind_deg": wind.get("deg"),
        "wind_direction": _compass_dir(wind.get("deg")),
        "chance_of_rain_pct": round((entry.get("pop") or 0) * 100),
        "rain_mm_last_3h": rain_mm,
        "snow_mm_last_3h": snow_mm,
        "pressure_hpa": main.get("pressure"),
    }


def _fmt(e: dict) -> str:
    rain_pct = e.get("chance_of_rain_pct", 0)
    rain_flag = " 🌧" if rain_pct >= 50 else (" 🌦" if rain_pct >= 30 else "")
    wind_speed = e.get("wind_speed_ms")
    wind_dir = e.get("wind_direction", "?")
    wind_str = f", wind {wind_speed}m/s {wind_dir} ({e.get('wind_deg')}°)" if wind_speed is not None else ""
    precip_str = ""
    if e.get("rain_mm_last_3h"):
        precip_str = f", {e['rain_mm_last_3h']}mm rain/3h"
    elif e.get("snow_mm_last_3h"):
        precip_str = f", {e['snow_mm_last_3h']}mm snow/3h"

    return (
        f"{e['local_time']}  ({e['when']})\n"
        f"    {e['temp_c']}°C (feels {e['feels_like_c']}°C){rain_flag} — {e['condition']}\n"
        f"    Humidity: {e['humidity_pct']}%  |  Rain chance: {rain_pct}%{precip_str}\n"
        f"    Wind: {wind_speed}m/s {wind_dir} ({e.get('wind_deg')}°)  |  Pressure: {e.get('pressure_hpa')}hPa"
    )


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    city = (args.get("city") or "").strip()
    hours = args.get("hours")
    days = args.get("days")
    at_hour = args.get("at_hour")
    day_offset = args.get("day_offset", 0)
    api_key = os.getenv("OPENWEATHER_API_KEY")

    if not city:
        return {"status": "error", "reason": "city is required"}
    if not api_key:
        return {"status": "error", "reason": "OPENWEATHER_API_KEY not configured"}
    if at_hour is not None and not (0 <= int(at_hour) <= 23):
        return {"status": "error", "reason": "at_hour must be 0-23"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            geo_res = await client.get(
                "https://api.openweathermap.org/geo/1.0/direct",
                params={"q": city, "limit": 1, "appid": api_key},
            )
            if geo_res.status_code != 200:
                return {"status": "error", "reason": f"Geocoding API returned {geo_res.status_code}"}

            geo_data = geo_res.json()
            if not geo_data:
                return {"status": "error", "reason": f"City '{city}' not found"}

            lat, lon = geo_data[0]["lat"], geo_data[0]["lon"]

            forecast_res = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={"lat": lat, "lon": lon, "appid": api_key, "units": "metric"},
            )
            if forecast_res.status_code != 200:
                return {"status": "error", "reason": f"Forecast API returned {forecast_res.status_code}"}

            forecast_data = forecast_res.json()

        forecast_items = forecast_data.get("list", [])
        if not forecast_items:
            return {"status": "error", "reason": "No forecast data returned"}

        tz_offset_secs = forecast_data.get("city", {}).get("timezone", 0)
        now_utc = datetime.now(timezone.utc)
        city_name = geo_data[0].get("name", city)

        all_labeled = [_label_entry(e, now_utc, tz_offset_secs) for e in forecast_items]

        # ── Specific-hour request: find the closest 3-hour bucket to
        # the requested LOCAL hour, on the requested local day. ──
        if at_hour is not None:
            target_local_date = (now_utc + timedelta(seconds=tz_offset_secs)).date() + timedelta(days=int(day_offset))

            def _local_dt(e):
                return datetime.strptime(e["time_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) + timedelta(seconds=tz_offset_secs)

            same_day = [e for e in all_labeled if _local_dt(e).date() == target_local_date]
            pool = same_day or all_labeled
            if not pool:
                return {"status": "error", "reason": "No forecast data in range for that day."}

            best = min(pool, key=lambda e: abs(_local_dt(e).hour - int(at_hour)))
            actual_hour = _local_dt(best).hour
            granularity_note = (
                f"OpenWeather's free forecast is only 3-hour resolution — you asked for "
                f"{int(at_hour):02d}:00, the closest available reading is {actual_hour:02d}:00 local."
                if actual_hour != int(at_hour) else
                f"Exact match available for {actual_hour:02d}:00 local."
            )
            return {
                "status": "success",
                "city": city_name,
                "scope": f"closest reading to {int(at_hour):02d}:00 local, {target_local_date.isoformat()}",
                "granularity_note": granularity_note,
                "forecast": [best],
                "message": f"Weather for {city_name} — {granularity_note}\n\n{_fmt(best)}",
            }

        # ── Block request: next N hours / N days / default window ──
        if days:
            entries_needed = max(1, int(days) * 8)
            scope = f"next {days} day(s)"
        elif hours:
            entries_needed = max(1, -(-int(hours) // 3))  # ceil division
            scope = f"next {hours} hour(s)"
        else:
            entries_needed = 5
            scope = "nearest available (~15 hours) — no specific hours/days requested"

        results = all_labeled[:entries_needed]
        hourly_summary = "\n\n".join(_fmt(e) for e in results)

        return {
            "status": "success",
            "city": city_name,
            "scope": scope,
            "forecast": results,
            "hourly_summary": hourly_summary,
            "message": (
                f"Forecast for {city_name} ({scope}), all times local:\n\n"
                f"{hourly_summary}\n\n"
                f"({len(results)} entries at 3-hour intervals — OpenWeather free tier's real resolution. "
                f"Present the full numbers above for each entry — temp, humidity, wind speed+direction, "
                f"precipitation — do not collapse this to a one-line summary. "
                f"For a specific time, call again with at_hour.)"
            ),
        }

    except Exception as e:
        logger.error(f"weather_forecast error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
