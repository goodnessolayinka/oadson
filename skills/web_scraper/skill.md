# Web Scraper

## What It Does
Pulls structured data out of web pages via CSS selectors — prices,
listing titles, table rows, article bodies, repeated card/item
elements — from one page (`scrape`) or across several pages by
following a "next page" link (`crawl`). Static HTML only, via a direct
HTTP fetch — no JavaScript rendering.

## web_scraper vs web_fetch vs browser — pick the right one
- **`web_fetch`** — one page, plain readable text, no selectors, no
  structure. Use for "read me this article" / "what does this page say".
- **`web_scraper`** (this skill) — one or more pages, structured
  extraction by CSS selector. Use for "get me every product's price",
  "pull the table rows", "collect every article title across these 3
  pages of results".
- **`browser`** — only when the page genuinely needs JavaScript to
  render its content (an SPA, infinite-scroll-only content, something
  that requires clicking/logging in first). Heavier — real Chromium
  process — and may not be configured on this deployment at all. Try
  `web_scraper` first; only reach for `browser` if the data you need
  simply isn't present in the raw HTML this skill fetches (check by
  scraping first — if your selectors return nothing and the page looks
  right in a browser, that's the actual signal it's JS-rendered).

## Calling Convention

### scrape — one page
```
web_scraper action='scrape'
  url='https://example.com/products'
  selectors={"title": "h1", "price": ".price"}
```
Returns `{field: [text, text, ...]}` — every match for each selector on
the page, as a flat list. A selector matching one unique element (an
`#id` or a heading) naturally comes back as a 1-item list.

**For a repeated structure (a list of cards/items/rows), use
`repeat_selector` to get one record per item instead of separate flat
lists you'd have to zip back together yourself:**
```
web_scraper action='scrape'
  url='https://example.com/products'
  repeat_selector='.product-card'
  selectors={"title": ".title", "price": ".price"}
```
Returns `{"rows": [{"title": "...", "price": "..."}, ...], "row_count": N}`
— each selector is looked up WITHIN each `.product-card` match, so
titles and prices from the same card are correctly paired, not just two
separate lists you'd have to hope line up in the same order.

### crawl — multiple pages via pagination
```
web_scraper action='crawl'
  url='https://example.com/products?page=1'
  repeat_selector='.product-card'
  selectors={"title": ".title", "price": ".price"}
  next_page_selector='a.next-page'
  max_pages=5
```
Follows `next_page_selector`'s `href` from page to page, extracting the
same selectors on each, up to `max_pages` (default 5, hard ceiling 15 —
this server runs on a tight memory budget; even a request for more than
15 is silently capped there, not honored). Stops early and cleanly
(not an error) once no next-page link is found, or if it detects the
"next page" link would revisit a page already seen (a cycle — the crawl
stops there rather than looping).

## Picking selectors
If you're not certain of a page's real structure, fetch it with
`@web_fetch` first (or `read` its raw source) to see the actual HTML
before guessing selectors — a wrong selector fails clearly (see below),
but getting it right the first time saves a round trip.

## Zero matches is a real error, not empty data
If every given selector matches nothing on the page, this returns
`status: 'error'`, not a technically-successful result full of empty
lists. Zero matches is almost always a wrong or outdated selector —
treat it as "re-check the selector", not "this page has no data".

## Constraints
- Static HTML only — content that only appears after JavaScript runs
  won't be there. If selectors consistently return nothing on a page
  that clearly has the data when viewed normally, that's the signal to
  try `browser` instead (if available) rather than retrying more
  selector variations.
- 5MB per-page size cap, 15-second timeout per page, up to 5 redirect
  hops — same limits as `web_fetch`.
- `crawl` checks free memory before starting and refuses cleanly if
  there isn't enough headroom, rather than risking a crash partway
  through a multi-page fetch — use `scrape` on a single page instead if
  this happens.
- Rate limited separately from `web_fetch` (5 scrape/crawl calls per
  minute) — a `crawl` call can itself be up to 15 real page fetches, so
  it has its own, lower budget rather than sharing `web_fetch`'s.
- Same SSRF protection as every other URL-fetching skill in this
  codebase — private/internal addresses are blocked before any request
  is made.
