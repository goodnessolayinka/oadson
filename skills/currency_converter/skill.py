"""
currency_converter — closes a confirmed hallucination gap.

Evidence: the same "convert 100 USD to NGN" query got two different
invented numbers (158,000 then 158,500 NGN) seconds apart, in near-
identical repeated prompts — because no currency skill existed at all,
so the model was fabricating a plausible-looking exchange rate from
memory every time, with total confidence and no actual data behind it.

Uses open.er-api.com — free, no API key required, ~160 currencies
including NGN (verified NGN coverage before picking this over
frankfurter.app, whose ECB-based rates don't reliably include it).
Returns the real "last updated" timestamp from the API alongside the
result, so the answer's provenance is visible, not just a bare number.
"""

import logging
import httpx

logger = logging.getLogger("oadson.skill.currency_converter")


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    amount = args.get("amount")
    from_currency = str(args.get("from_currency", "")).strip().upper()
    to_currency = str(args.get("to_currency", "")).strip().upper()

    if amount is None:
        return {"status": "error", "reason": "amount is required"}
    if not from_currency or not to_currency:
        return {"status": "error", "reason": "from_currency and to_currency are required"}

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"status": "error", "reason": f"amount '{amount}' is not a valid number"}

    try:
        url = f"https://open.er-api.com/v6/latest/{from_currency}"
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url)

        if res.status_code != 200:
            return {"status": "error", "reason": f"Exchange rate API returned {res.status_code}"}

        data = res.json()
        if data.get("result") != "success":
            return {"status": "error", "reason": data.get("error-type", "Exchange rate API request failed")}

        rates = data.get("rates", {})
        if to_currency not in rates:
            return {"status": "error", "reason": f"'{to_currency}' is not a recognized currency code"}

        rate = rates[to_currency]
        converted = round(amount * rate, 2)
        last_updated = data.get("time_last_update_utc", "unknown")

        return {
            "status": "success",
            "amount": amount,
            "from_currency": from_currency,
            "to_currency": to_currency,
            "rate": rate,
            "converted": converted,
            "rates_last_updated": last_updated,
            "message": (
                f"{amount:,.2f} {from_currency} = {converted:,.2f} {to_currency} "
                f"(rate: 1 {from_currency} = {rate} {to_currency}, updated {last_updated})"
            ),
        }

    except Exception as e:
        logger.error(f"currency_converter error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
