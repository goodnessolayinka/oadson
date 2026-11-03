# Web Fetch

## What It Does
Fetches a publicly accessible URL and either (a) returns its readable
text, or (b) pulls structured data out of it via CSS selectors — from
one page or, via pagination, several.

MERGED 2026-09-03: the standalone `web_scraper` skill has been folded
in here as `mode='scrape'`'s multi-field form and a new `mode='crawl'`
— see "Modes" below. There is no separate scraping skill anymore;
everything static-HTML-related lives in `web_fetch`.

## When to Use It
- User provides a specific URL and wants its content → `mode='read'`
- User wants specific DATA out of a page — prices, listing titles,
  table rows, every link matching a pattern → `mode='scrape'`
- User wants that same data collected across several pages of results
  (pagination) → `mode='crawl'`

## When NOT to Use It
- User wants to search for pages (no URL given) → use web_search
- URL requires authentication/login to access
- User wants archived versions → use wayback
- The page genuinely needs JavaScript to render its content (an SPA,
  infinite-scroll-only content) → try `mode='scrape'` first; if
  selectors that clearly should match return nothing and the page
  looks right in a normal browser, that's the signal it's JS-rendered
  — use `@browser` instead (heavier, real Chromium process, may not be
  configured on this deployment)

## Modes

### `read` (default)
No extra params beyond `url`. Flattened visible text, first 3000 chars.

### `scrape` — one page, structured
Two forms:
- **Single field**: `selector` (+ optional `attribute`, `limit`) —
  original form, unchanged. Returns a flat list of matches.
- **Multiple fields**: `selectors` (a `{field_name: css_selector}`
  map), optionally with `repeat_selector` for one-record-per-item:
  ```
  web_fetch mode='scrape'
    url='https://example.com/products'
    repeat_selector='.product-card'
    selectors={"title": ".title", "price": ".price"}
  ```
  Returns `{"rows": [{"title": "...", "price": "..."}, ...], "row_count": N}`
  — each selector is looked up WITHIN each `.product-card` match, so
  fields from the same card are correctly paired. Without
  `repeat_selector`, returns `{field: [text, text, ...]}` — every match
  for that field's selector across the whole page.

Use `selector` OR `selectors`, not both.

### `crawl` — multiple pages via pagination
```
web_fetch mode='crawl'
  url='https://example.com/products?page=1'
  repeat_selector='.product-card'
  selectors={"title": ".title", "price": ".price"}
  next_page_selector='a.next-page'
  max_pages=5
```
Follows `next_page_selector`'s `href` from page to page, extracting the
same selectors on each, up to `max_pages` (default 5, hard ceiling 15).
Stops early and cleanly (not an error) once no next-page link is found,
or if it detects a cycle (a next-page link pointing back at an earlier
page).

## Picking selectors
If you're not certain of a page's real structure, fetch it with
`mode='read'` first to see the actual markup before guessing selectors.

## Zero matches is a real error, not empty data
For `selectors`-based scrape/crawl: if every given selector matches
nothing, this returns `status: 'error'`, not a technically-successful
result full of empty lists — almost always a wrong/outdated selector,
not genuinely empty content. (The original single-`selector` form
returns `status: 'success'` with `count: 0` instead, for backward
compatibility with existing callers.)

## Output Format (mode='read')
```json
{
  "status": "success",
  "url": "https://docs.render.com/deploy-fastapi",
  "title": "Deploy FastAPI on Render",
  "status_code": 200,
  "content": "To deploy a FastAPI application on Render...",
  "char_count": 2841,
  "word_count": 412,
  "links_found": 14,
  "fetched_at": "2026-06-15T12:00:00Z",
  "message": "Fetched 2841 chars from https://docs.render.com/deploy-fastapi"
}
```

## Constraints
- Public URLs only — no auth-gated content, and blocked entirely for
  URLs that resolve to a private, loopback, link-local, or otherwise
  internal address (IPv4 or IPv6)
- Static HTML only, no JavaScript execution — JS-rendered pages may
  return partial or no content
- HTML pages only — non-HTML responses (including PDFs) are rejected,
  not converted
- Max download size: 5MB per page (aborted mid-download if exceeded);
  `mode='read'` content is further capped to the first 100 lines / 3000
  characters
- Redirects followed up to 5 hops
- Rate limits: 10 `read`/`scrape` calls per minute; `crawl` has its own,
  lower budget (5/minute) since one crawl call can itself be up to 15
  real page fetches
- `crawl` checks free memory before starting and refuses cleanly if
  there isn't enough headroom (this server runs on a tight memory
  budget) — use `scrape` on a single page instead if this happens

## Examples
- "Read the Render deployment docs for me" → `mode='read'`
- "Get me every product's price on this page" → `mode='scrape'` with `selectors`
- "Collect every listing across all 5 pages of results" → `mode='crawl'`
