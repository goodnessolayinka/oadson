"""
skills/web_scraper/skill.py — OADSON V3 Web Scraper Skill.

ADDED 2026-08-31. Structured, static-HTML scraping via CSS selectors,
with an optional multi-page crawl (follows a 'next page' link, hard
page cap). Deliberately built as a peer of web_fetch, not a wrapper
around it — web_fetch returns plain readable text with no selector
support, which isn't enough for structured extraction (pull every
price, every listing title, every table row).

Deliberately NOT built on Playwright/browser: browser is a genuinely
heavy dependency (real Chromium process, real memory footprint the
moment it launches) that only makes sense once this server has more
RAM headroom to spare — see core/oadson_home.py's has_memory_headroom
and skills/github/skill.py's clone_to_workspace for the same "tight
512MB budget" reasoning already established elsewhere in this
codebase. This skill covers the large majority of real scraping
requests (static HTML — most listing pages, article pages, tables)
with none of that memory cost. If a page genuinely needs JavaScript to
render, browser is still the right tool, when/if it's configured.

Shares core.ssrf_guard with web_fetch (same protection, one
implementation) but keeps its own separate rate-limit bucket
("web_scraper", not "web_fetch") since a multi-page crawl is a
meaningfully heavier operation than a single fetch and shouldn't share
— or silently exhaust — web_fetch's budget.
"""

from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from core.ssrf_guard import is_safe_url

RATE_LIMIT_PER_MINUTE = 5  # lower than web_fetch's 10 — a crawl call can be up to 15 real requests, not 1
RATE_LIMIT_WINDOW_SECONDS = 60

MAX_PAGE_BYTES = 5 * 1024 * 1024  # matches web_fetch's own cap
FETCH_TIMEOUT_S = 15
MAX_REDIRECTS = 5
MAX_PAGES_HARD_CAP = 15  # manifest's max_pages ceiling — enforced here too, never trust args alone
DEFAULT_MAX_PAGES = 5

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


async def _fetch_html(url: str) -> tuple[Optional[BeautifulSoup], Optional[str]]:
    """
    Fetch one page and return (soup, None) on success or (None, error).
    Same streaming-with-abort discipline as web_fetch.fetch_url — the
    size check must happen DURING download, not after the whole body
    is already buffered, or the cap doesn't actually protect memory.
    """
    safe, reason = await is_safe_url(url)
    if not safe:
        return None, reason

    try:
        async with httpx.AsyncClient(max_redirects=MAX_REDIRECTS) as client:
            async with client.stream(
                "GET", url, headers=HEADERS, timeout=FETCH_TIMEOUT_S, follow_redirects=True,
            ) as response:
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type:
                    return None, f"Not an HTML page (content-type: {content_type or 'unknown'})"

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        return None, f"Page too large (>{MAX_PAGE_BYTES // (1024 * 1024)}MB)"

                if response.status_code >= 400:
                    return None, f"HTTP {response.status_code}"

            return BeautifulSoup(bytes(body), "html.parser"), None

    except httpx.TimeoutException:
        return None, f"Request timeout ({FETCH_TIMEOUT_S}s)"
    except Exception as e:
        return None, str(e)


def _extract(soup: BeautifulSoup, selectors: dict, repeat_selector: Optional[str]) -> dict:
    """
    Core extraction logic, shared by both scrape and crawl (once per
    page for crawl).

    Without repeat_selector: {field_name: [text, text, ...]} — every
    match for that field's selector on the whole page, since a field
    like "all article titles on this page" is naturally a flat list,
    not a single value; the caller decides whether they wanted one or
    many by how they wrote the selector (an #id selector naturally
    yields a 1-item list).

    With repeat_selector: {"rows": [{field: text, field: text}, ...]}
    — one dict per match of repeat_selector, with EACH field's
    selector looked up scoped to that one row's subtree instead of the
    whole page. This is what makes "one selector per product card,
    give me one record per card" actually work, instead of returning
    three flat lists the caller has to zip back together themselves
    and hope they lined up correctly.
    """
    if repeat_selector:
        rows = []
        for row_el in soup.select(repeat_selector):
            row = {}
            for field, sel in selectors.items():
                matches = row_el.select(sel)
                row[field] = matches[0].get_text(strip=True) if matches else None
            rows.append(row)
        return {"rows": rows, "row_count": len(rows)}

    result = {}
    for field, sel in selectors.items():
        matches = soup.select(sel)
        result[field] = [m.get_text(strip=True) for m in matches]
    return result


async def _check_and_record_rate_limit(db, user_id: Optional[str]) -> Optional[str]:
    """Same pattern as web_fetch's own rate limiter — separate bucket, see module docstring."""
    if db is None or not user_id:
        return None

    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    coll = db["skill_rate_limits"]

    doc = await coll.find_one({"user_id": user_id, "skill": "web_scraper"})
    recent_calls = [t for t in (doc.get("calls", []) if doc else []) if t > window_start]

    if len(recent_calls) >= RATE_LIMIT_PER_MINUTE:
        return f"Rate limit reached ({RATE_LIMIT_PER_MINUTE} scrape calls/minute). Try again in a moment."

    recent_calls.append(now)
    await coll.update_one(
        {"user_id": user_id, "skill": "web_scraper"},
        {"$set": {"calls": recent_calls}},
        upsert=True,
    )
    return None


async def _do_scrape(url: str, selectors: dict, repeat_selector: Optional[str]) -> dict:
    soup, err = await _fetch_html(url)
    if err:
        return {"status": "error", "reason": err}

    data = _extract(soup, selectors, repeat_selector)

    # Zero matches for EVERY selector is almost always a wrong selector,
    # not genuinely empty content — say so clearly instead of returning
    # a technically-successful result full of empty lists that looks
    # like real "the page has no data" evidence.
    all_empty = (
        (data.get("row_count") == 0)
        if repeat_selector
        else all(len(v) == 0 for v in data.values())
    )
    if all_empty:
        return {
            "status": "error",
            "reason": (
                "None of the given selectors matched anything on this page. "
                "This is almost always a wrong/outdated selector, not genuinely empty "
                "content — re-check the page's real HTML structure (fetch it with "
                "@web_fetch first to see the actual markup, or use "
                "@document_reader/read the raw source) before retrying with adjusted "
                "selectors, rather than assuming the page has no data."
            ),
            "url": url,
        }

    return {"status": "success", "url": url, "data": data}


async def _do_crawl(
    url: str, selectors: dict, repeat_selector: Optional[str],
    next_page_selector: str, max_pages: int,
) -> dict:
    from core.oadson_home import has_memory_headroom

    max_pages = min(max(1, max_pages), MAX_PAGES_HARD_CAP)

    # MEMORY GUARD 2026-08-31: same posture as clone_to_workspace's own
    # pre-flight check (skills/github/skill.py) — a multi-page crawl
    # holding several pages' worth of parsed BeautifulSoup trees plus
    # accumulated results in memory at once is exactly the kind of
    # "genuinely could use tens of MB" operation that check exists for.
    # Reused directly rather than re-implemented.
    ok, headroom_mb = has_memory_headroom(min_mb=100)
    if not ok:
        return {
            "status": "error",
            "reason": (
                f"Not enough free memory to safely crawl multiple pages right now "
                f"({headroom_mb}MB available, need 100MB). This server runs on a tight "
                "memory budget. Use action='scrape' on a single page instead, or try "
                "the crawl again in a moment."
            ),
        }

    pages_result = []
    current_url = url
    seen_urls = {url}  # cycle guard — a next-page link pointing back at an earlier page shouldn't loop forever

    for page_num in range(1, max_pages + 1):
        soup, err = await _fetch_html(current_url)
        if err:
            pages_result.append({"page": page_num, "url": current_url, "status": "error", "reason": err})
            break

        data = _extract(soup, selectors, repeat_selector)
        pages_result.append({"page": page_num, "url": current_url, "status": "success", "data": data})

        next_link = soup.select_one(next_page_selector)
        if not next_link or not next_link.get("href"):
            break  # no more pages — stop cleanly, not an error

        next_url = urljoin(current_url, next_link["href"])
        if next_url in seen_urls:
            break  # cycle detected — stop rather than loop
        seen_urls.add(next_url)
        current_url = next_url

    total_rows = sum(
        (p["data"].get("row_count", 0) if repeat_selector else sum(len(v) for v in p["data"].values()))
        for p in pages_result if p["status"] == "success"
    )

    return {
        "status": "success",
        "pages_fetched": len(pages_result),
        "max_pages": max_pages,
        "stopped_early": len(pages_result) < max_pages and pages_result[-1]["status"] == "success",
        "total_items_extracted": total_rows,
        "pages": pages_result,
    }


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    db = payload.get("db")
    user_id = payload.get("user_id")

    action = args.get("action", "scrape")
    url = (args.get("url") or "").strip()
    selectors = args.get("selectors") or {}
    repeat_selector = (args.get("repeat_selector") or "").strip() or None

    if not url:
        return {"status": "error", "reason": "'url' is required"}
    if not selectors or not isinstance(selectors, dict):
        return {"status": "error", "reason": "'selectors' is required — a map of field_name -> CSS selector"}

    rate_limit_error = await _check_and_record_rate_limit(db, user_id)
    if rate_limit_error:
        return {"status": "error", "reason": rate_limit_error}

    if action == "scrape":
        return await _do_scrape(url, selectors, repeat_selector)

    if action == "crawl":
        next_page_selector = (args.get("next_page_selector") or "").strip()
        if not next_page_selector:
            return {"status": "error", "reason": "'next_page_selector' is required for action='crawl'"}
        max_pages = int(args.get("max_pages") or DEFAULT_MAX_PAGES)
        return await _do_crawl(url, selectors, repeat_selector, next_page_selector, max_pages)

    return {"status": "error", "reason": f"Unknown action '{action}'. Use: scrape, crawl"}
