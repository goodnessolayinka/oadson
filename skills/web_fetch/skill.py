"""
skills/web_fetch/skill.py — OADSON V3 Web Fetch (+ scrape/crawl).

MERGED 2026-09-03: web_scraper (formerly its own skill, added
2026-08-31) is folded in here as extra `mode` options rather than kept
as a second, easily-confused skill — web_fetch's own mode='scrape'
(added 2026-08-30) already covered single-selector extraction, so the
two skills overlapped exactly in the "get me some data from ONE page"
case and diverged only for multi-field/repeat-row extraction and
multi-page crawling. Folding web_scraper's `_extract`/`_do_crawl` logic
in here — byte-for-byte the same algorithm, just moved — means the
model picks between three modes on ONE skill instead of guessing which
of two skills to reach for. skills/web_scraper/ has been removed.

mode='read'   (default, unchanged): flattened visible text.
mode='scrape' (extended): single selector+attribute (original
              behavior, untouched for backward compatibility) OR a
              `selectors` dict for multi-field extraction, optionally
              scoped per-row via `repeat_selector` (web_scraper's
              richer extraction, now here).
mode='crawl'  (new to this skill, moved from web_scraper): same
              selectors/repeat_selector extraction, repeated across
              pages by following `next_page_selector`'s href, up to
              `max_pages`. Own rate-limit bucket and memory-headroom
              check, both moved verbatim from web_scraper — a crawl is
              a meaningfully heavier operation than a single fetch and
              still shouldn't share/exhaust mode='read'/'scrape's
              budget.
"""

from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from core.ssrf_guard import is_safe_url as _shared_is_safe_url

RATE_LIMIT_PER_MINUTE = 10
RATE_LIMIT_WINDOW_SECONDS = 60

# MOVED FROM web_scraper 2026-09-03 — crawl's own, lower budget. A
# crawl call can itself be up to MAX_PAGES_HARD_CAP real page fetches,
# so it must not share (and can't be allowed to silently exhaust)
# mode='read'/'scrape's RATE_LIMIT_PER_MINUTE above.
CRAWL_RATE_LIMIT_PER_MINUTE = 5
MAX_PAGES_HARD_CAP = 15
DEFAULT_MAX_PAGES = 5


class WebFetchSkill:
    def __init__(self):
        self.max_size = 5 * 1024 * 1024  # 5MB — see skill.md
        self.timeout = 15
        self.max_redirects = 5  # matches skill.md's documented "up to 5 hops"

    async def is_safe_url(self, url: str) -> tuple[bool, str]:
        """
        MOVED 2026-08-14 to core/ssrf_guard.py — this was the only real
        SSRF check in the codebase, and five other skills that fetch
        arbitrary URLs the same way (google_drive's read_url, api_tester,
        docs_fetcher, header_analyzer, html_preview) had none at all.
        Kept as a thin wrapper here so nothing else in this file needs
        to change; the real implementation now lives in one shared
        place instead of being copy-pasted into six.
        """
        return await _shared_is_safe_url(url)

    async def _fetch_soup(self, url: str) -> tuple[Optional[BeautifulSoup], Optional[int], Optional[str]]:
        """
        MOVED FROM web_scraper 2026-09-03 (was _fetch_html there) —
        shared streaming-download-with-abort logic, now used by
        mode='scrape' (multi-field path), mode='crawl', AND mode='read'/
        the original single-selector 'scrape' path below all go through
        this one function instead of two near-identical copies of the
        same download loop.
        Returns (soup, status_code, None) on success or (None, None, error).
        """
        safe, reason = await self.is_safe_url(url)
        if not safe:
            return None, None, reason

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            async with httpx.AsyncClient(max_redirects=self.max_redirects) as client:
                async with client.stream(
                    "GET", url, headers=headers, timeout=self.timeout, follow_redirects=True,
                ) as response:
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type:
                        return None, response.status_code, f"Not an HTML page (content-type: {content_type or 'unknown'})"

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_size:
                            return None, response.status_code, f"Page too large (>{self.max_size // (1024 * 1024)}MB)"

                    status_code = response.status_code
                    if status_code >= 400:
                        return None, status_code, f"HTTP {status_code}"

                return BeautifulSoup(bytes(body), "html.parser"), status_code, None
        except httpx.TimeoutException:
            return None, None, f"Request timeout ({self.timeout}s)"
        except Exception as e:
            return None, None, str(e)

    def _extract_multi(self, soup: BeautifulSoup, selectors: dict, repeat_selector: Optional[str]) -> dict:
        """
        MOVED FROM web_scraper 2026-09-03 (was _extract() there),
        unchanged. See that module's original docstring:

        Without repeat_selector: {field_name: [text, text, ...]} — every
        match for that field's selector on the whole page.

        With repeat_selector: {"rows": [{field: text, ...}, ...]} — one
        dict per match of repeat_selector, each field's selector looked
        up scoped to that row's subtree, so fields from the same
        card/row are correctly paired instead of separate flat lists
        the caller would have to zip back together themselves.
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

    async def fetch_url(self, url: str, mode: str = "read", selector: Optional[str] = None,
                         attribute: Optional[str] = None, limit: int = 50,
                         selectors: Optional[dict] = None, repeat_selector: Optional[str] = None,
                         next_page_selector: Optional[str] = None, max_pages: int = DEFAULT_MAX_PAGES) -> dict:
        """
        Fetch and parse a webpage.

        mode='read' (default, unchanged behavior): flattened visible text,
        first 3000 chars — good for "read this article".

        mode='scrape': structured extraction. Two ways to call it —
          - `selector` (+ optional `attribute`) — ORIGINAL single-selector
            form (added 2026-08-30), unchanged, still works exactly as
            before for existing callers.
          - `selectors` (dict of field_name -> CSS selector), optionally
            with `repeat_selector` for one-record-per-row extraction —
            MERGED FROM web_scraper 2026-09-03, for when a single
            selector/attribute pair isn't enough (multiple fields per
            page, or per repeated card/listing).
        Exactly one of `selector` or `selectors` should be given for
        mode='scrape'.

        mode='crawl' — MERGED FROM web_scraper 2026-09-03: same
        selectors/repeat_selector extraction as mode='scrape', repeated
        across pages by following `next_page_selector`'s href, up to
        `max_pages`.
        """
        if mode == "scrape" and not selector and not selectors:
            return {"status": "error", "reason": "mode='scrape' requires either 'selector' (single field) or 'selectors' (dict of field_name -> CSS selector, for multi-field/repeat_selector extraction)"}
        if mode == "crawl":
            if not selectors:
                return {"status": "error", "reason": "mode='crawl' requires 'selectors' — a map of field_name -> CSS selector"}
            if not next_page_selector:
                return {"status": "error", "reason": "mode='crawl' requires 'next_page_selector' — the CSS selector for the 'next page' link"}
            return await self._crawl(url, selectors, repeat_selector, next_page_selector, max_pages)

        soup, status_code, err = await self._fetch_soup(url)
        if err:
            return {"status": "error", "reason": err, **({"status_code": status_code} if status_code else {})}

        links_found = len(soup.find_all("a", href=True))

        if mode == "scrape":
            if selectors:
                return self._extract_structured_multi(soup, url, selectors, repeat_selector, status_code)
            return self._extract_structured(soup, url, selector, attribute, limit, status_code, links_found)

        # ── mode='read' — unchanged original behavior below ──

        # Remove script/style tags
        for tag in soup(["script", "style", "meta", "noscript"]):
            tag.decompose()

        # Extract text
        text = soup.get_text(separator="\n", strip=True)

        # Clean up whitespace
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        full_content = "\n".join(lines[:100])  # First 100 lines
        content = full_content[:3000]  # First 3000 chars — the actual returned content

        # Get title — .string returns None for titles containing
        # nested markup (e.g. <title>Foo <span>Bar</span></title>);
        # get_text() handles that correctly.
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else "No title"

        return {
            "status": "success",
            "url": url,
            "title": title_text,
            "status_code": status_code,
            "content": content,
            # Reflects the actual returned `content` length, not the
            # pre-3000-char-truncation string — previously char_count
            # could report a bigger number than what was truly returned.
            "char_count": len(content),
            "word_count": len(full_content.split()),
            "links_found": links_found,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "message": f"Fetched {len(content)} chars from {url}",
        }

    def _extract_structured(self, soup, url: str, selector: str, attribute: Optional[str],
                             limit: int, status_code: int, links_found: int) -> dict:
        """
        ADDED 2026-08-30 — the original single-selector scrape-mode
        extraction. Kept as its own method (not inlined into fetch_url)
        so it's independently testable and so fetch_url's control flow
        stays readable — one early return into here, rather than a long
        if/else straddling the whole function body.

        limit caps how many matched elements are returned — a broad
        selector like "div" on a real page can match thousands of nodes;
        without a cap this could return a response far larger than
        anything web_fetch's existing max_size guard was designed around
        (that cap limits the DOWNLOAD, not the size of what gets sent
        back to the model after parsing). Defaults to 50, which is
        already generous for "give me the list of X on this page" use
        cases and keeps the tool-result payload bounded regardless of
        selector breadth.
        """
        try:
            matches = soup.select(selector)
        except Exception as e:
            return {"status": "error", "reason": f"Invalid CSS selector '{selector}': {e}"}

        if not matches:
            return {
                "status": "success",
                "url": url,
                "status_code": status_code,
                "selector": selector,
                "results": [],
                "count": 0,
                "links_found": links_found,
                "fetched_at": datetime.utcnow().isoformat() + "Z",
                "message": f"Selector '{selector}' matched 0 elements on this page — check the selector, or the content may be JS-rendered (this tool only sees the initial HTML, not JS-executed DOM changes; use @browser for JS-rendered pages instead).",
            }

        truncated = len(matches) > limit
        results = []
        for el in matches[:limit]:
            if attribute:
                value = el.get(attribute)
                results.append(value if value is not None else None)
            else:
                results.append(el.get_text(strip=True))

        return {
            "status": "success",
            "url": url,
            "status_code": status_code,
            "selector": selector,
            "attribute": attribute,
            "results": results,
            "count": len(results),
            "total_matched": len(matches),
            "truncated": truncated,
            "links_found": links_found,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "message": (
                f"Selector '{selector}' matched {len(matches)} element(s)"
                + (f", returning first {limit}" if truncated else "")
                + f" — extracted {'attribute ' + repr(attribute) if attribute else 'text content'}."
            ),
        }

    def _extract_structured_multi(self, soup, url: str, selectors: dict,
                                   repeat_selector: Optional[str], status_code: int) -> dict:
        """
        MERGED FROM web_scraper 2026-09-03 (was _do_scrape's post-fetch
        half there). Multi-field/repeat_selector extraction via
        _extract_multi(), plus the same "zero matches is a real error,
        not empty data" check web_scraper had — a wrong/outdated
        selector is a far more common cause of an all-empty result than
        the page genuinely having none of the requested fields, so this
        says so plainly instead of returning a technically-successful
        response full of empty lists that looks like real evidence.
        """
        data = self._extract_multi(soup, selectors, repeat_selector)

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
                    "mode='read' first to see the actual markup) before retrying with "
                    "adjusted selectors, rather than assuming the page has no data."
                ),
                "url": url,
            }

        return {"status": "success", "url": url, "status_code": status_code, "data": data}

    async def _crawl(self, url: str, selectors: dict, repeat_selector: Optional[str],
                      next_page_selector: str, max_pages: int) -> dict:
        """
        MOVED FROM web_scraper 2026-09-03 (was _do_crawl there),
        unchanged apart from calling this class's _fetch_soup()/
        _extract_multi() instead of web_scraper's module-level
        functions of the same shape.
        """
        from core.oadson_home import has_memory_headroom

        max_pages = min(max(1, max_pages), MAX_PAGES_HARD_CAP)

        # MEMORY GUARD — same posture as clone_to_workspace's own
        # pre-flight check (skills/github/skill.py) — a multi-page crawl
        # holding several pages' worth of parsed BeautifulSoup trees plus
        # accumulated results in memory at once is exactly the kind of
        # "genuinely could use tens of MB" operation that check exists for.
        ok, headroom_mb = has_memory_headroom(min_mb=100)
        if not ok:
            return {
                "status": "error",
                "reason": (
                    f"Not enough free memory to safely crawl multiple pages right now "
                    f"({headroom_mb}MB available, need 100MB). This server runs on a tight "
                    "memory budget. Use mode='scrape' on a single page instead, or try "
                    "the crawl again in a moment."
                ),
            }

        pages_result = []
        current_url = url
        seen_urls = {url}  # cycle guard — a next-page link pointing back at an earlier page shouldn't loop forever

        for page_num in range(1, max_pages + 1):
            soup, _status, err = await self._fetch_soup(current_url)
            if err:
                pages_result.append({"page": page_num, "url": current_url, "status": "error", "reason": err})
                break

            data = self._extract_multi(soup, selectors, repeat_selector)
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


async def _check_and_record_rate_limit(db, user_id: Optional[str], skill_bucket: str, limit_per_minute: int) -> Optional[str]:
    """
    Returns an error message if the user has hit limit_per_minute calls
    in the last RATE_LIMIT_WINDOW_SECONDS for skill_bucket, else records
    this call and returns None.

    MERGED 2026-09-03: bucket name and per-minute limit are now
    parameters instead of hardcoded "web_fetch"/RATE_LIMIT_PER_MINUTE —
    mode='crawl' uses its own separate, lower bucket
    ("web_fetch_crawl", CRAWL_RATE_LIMIT_PER_MINUTE) moved from
    web_scraper's old "web_scraper" bucket, same reasoning as before:
    a crawl call can itself be up to MAX_PAGES_HARD_CAP real page
    fetches and shouldn't share (or silently exhaust) mode='read'/
    'scrape's budget.

    Fails OPEN (returns None, doesn't block the fetch) if db or user_id
    aren't available, since rate limiting is a courtesy protection, not
    the actual security boundary — the SSRF check is.
    """
    if db is None or not user_id:
        return None

    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    coll = db["skill_rate_limits"]

    doc = await coll.find_one({"user_id": user_id, "skill": skill_bucket})
    recent_calls = [t for t in (doc.get("calls", []) if doc else []) if t > window_start]

    if len(recent_calls) >= limit_per_minute:
        return (
            f"Rate limit reached ({limit_per_minute} calls/minute for {skill_bucket}). "
            "Try again in a moment."
        )

    recent_calls.append(now)
    await coll.update_one(
        {"user_id": user_id, "skill": skill_bucket},
        {"$set": {"calls": recent_calls}},
        upsert=True,
    )
    return None


# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute web_fetch skill with structured args.

    REWRITTEN: previously this regex-extracted a URL from the raw message
    (a relatively safe approach since it only ever matched real-looking
    URLs), but is now simplified to use a clean structured 'url' arg
    directly, removing the regex dependency and matching the rest of
    this codebase's structured-args pattern.
    """
    args = payload.get("args", {}) or {}
    url = (args.get("url") or "").strip()

    if not url:
        return {"status": "error", "reason": "A 'url' is required"}

    mode = (args.get("mode") or "read").strip().lower()
    if mode not in ("read", "scrape", "crawl"):
        return {"status": "error", "reason": "mode must be 'read', 'scrape', or 'crawl'"}
    selector = (args.get("selector") or "").strip() or None
    attribute = (args.get("attribute") or "").strip() or None
    limit = args.get("limit", 50)
    try:
        limit = max(1, min(int(limit), 200))  # hard ceiling regardless of what's requested
    except (TypeError, ValueError):
        limit = 50

    selectors = args.get("selectors") or None
    if selectors is not None and not isinstance(selectors, dict):
        return {"status": "error", "reason": "'selectors' must be a map of field_name -> CSS selector"}
    repeat_selector = (args.get("repeat_selector") or "").strip() or None
    next_page_selector = (args.get("next_page_selector") or "").strip() or None
    max_pages = args.get("max_pages", DEFAULT_MAX_PAGES)
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        max_pages = DEFAULT_MAX_PAGES

    db = payload.get("db")
    user_id = payload.get("user_id")

    if mode == "crawl":
        rate_limit_error = await _check_and_record_rate_limit(db, user_id, "web_fetch_crawl", CRAWL_RATE_LIMIT_PER_MINUTE)
    else:
        rate_limit_error = await _check_and_record_rate_limit(db, user_id, "web_fetch", RATE_LIMIT_PER_MINUTE)
    if rate_limit_error:
        return {"status": "error", "reason": rate_limit_error}

    try:
        skill = WebFetchSkill()
        return await skill.fetch_url(
            url, mode=mode, selector=selector, attribute=attribute, limit=limit,
            selectors=selectors, repeat_selector=repeat_selector,
            next_page_selector=next_page_selector, max_pages=max_pages,
        )
    except Exception as e:
        return {"status": "error", "reason": str(e)}
