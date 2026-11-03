"""
NASA Weather/Climate Skill — built from scratch.

The previous version of this file was a non-functional stub: it
referenced `Skill()` which was never defined anywhere in the file, so
this would have raised a NameError the moment it was actually invoked
(masked until now by the same blind try-every-method dispatch pattern
seen across the rest of this codebase, plus the fact that nothing
exercised it directly).

This uses NASA's POWER (Prediction Of Worldwide Energy Resources) API —
a real, public, no-special-authentication-required endpoint for
historical climate data (temperature, precipitation, solar radiation) by
latitude/longitude and date range. Note this is historical/climate data,
not a real-time weather forecast (see the separate "weather" skill for
that).
"""

import httpx


class NASAWeatherSkill:
    def __init__(self):
        self.base_url = "https://power.larc.nasa.gov/api/temporal/daily/point"

    async def get_climate_data(self, latitude: float, longitude: float, start_date: str, end_date: str) -> dict:
        """Fetch historical daily climate data for a location and date range."""
        try:
            params = {
                "parameters": "T2M,PRECTOTCORR,ALLSKY_SFC_SW_DWN",
                "community": "AG",
                "longitude": longitude,
                "latitude": latitude,
                "start": start_date,
                "end": end_date,
                "format": "JSON"
            }

            async with httpx.AsyncClient() as client:
                response = await client.get(self.base_url, params=params, timeout=20)

                if response.status_code != 200:
                    return {"status": "error", "reason": f"NASA POWER API returned {response.status_code}"}

                data = response.json()
                properties = data.get("properties", {}).get("parameter", {})

                temps = properties.get("T2M", {})
                precip = properties.get("PRECTOTCORR", {})
                solar = properties.get("ALLSKY_SFC_SW_DWN", {})

                if not temps:
                    return {"status": "error", "reason": "No data returned for this location/date range"}

                daily_data = []
                for date_key in sorted(temps.keys()):
                    daily_data.append({
                        "date": date_key,
                        "temp_celsius": temps.get(date_key),
                        "precipitation_mm": precip.get(date_key),
                        "solar_radiation": solar.get(date_key)
                    })

                avg_temp = sum(d["temp_celsius"] for d in daily_data if d["temp_celsius"] is not None) / len(daily_data)

                return {
                    "status": "success",
                    "latitude": latitude,
                    "longitude": longitude,
                    "start_date": start_date,
                    "end_date": end_date,
                    "daily_data": daily_data,
                    "average_temp_celsius": round(avg_temp, 1),
                    "message": f"Retrieved {len(daily_data)} day(s) of climate data, avg temp {round(avg_temp, 1)}°C"
                }

        except Exception as e:
            return {"status": "error", "reason": str(e)}


async def run(payload):
    """Execute nasa_weather skill with structured args."""
    args = payload.get("args", {}) or {}

    latitude = args.get("latitude")
    longitude = args.get("longitude")
    start_date = (args.get("start_date") or "").strip()
    end_date = (args.get("end_date") or "").strip()

    if latitude is None or longitude is None:
        return {"status": "error", "reason": "latitude and longitude are required"}
    if not start_date or not end_date:
        return {"status": "error", "reason": "start_date and end_date are required (format YYYYMMDD)"}

    try:
        skill = NASAWeatherSkill()
        return await skill.get_climate_data(float(latitude), float(longitude), start_date, end_date)
    except (TypeError, ValueError) as e:
        return {"status": "error", "reason": f"Invalid argument: {str(e)}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}
