# Wayback Machine

## What It Does
Retrieves archived snapshots of web pages from the Internet Archive's Wayback Machine. Shows page history, available snapshots, and retrieves content from a specific archived date.

## When to Use It
- User says "archived version of", "what did this site look like in X year", "wayback"
- User wants to recover content from a deleted or changed page
- User researching how a website or company evolved
- User verifying what a page said at a specific date

## When NOT to Use It
- User wants current page content → use web_fetch
- Page has never been indexed by the Wayback Machine — return no results
- User wants real-time screenshots — not supported

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `check` | closest available snapshot. |
| `calendar` | list of all known snapshot timestamps. |
| `view_url` | generate a direct snapshot viewing URL, optionally for a specific date. |

## Output Format
```json
{
  "url": "https://example.edu",
  "snapshots_available": 847,
  "first_archived": "2001-03-14",
  "latest_archived": "2026-06-10",
  "requested_snapshot": {
    "date": "2020-01-15",
    "archive_url": "https://web.archive.org/web/20200115.../https://example.edu",
    "status": "200"
  }
}
```

## Constraints
- Not all pages are archived — coverage depends on crawl frequency
- JavaScript-heavy pages may render poorly in archive
- Content returned as static HTML — dynamic features won't work
- Private/password-protected pages not archived
- Some sites block archiving via robots.txt

## Examples
- "What did this company's pricing page say in 2023?"
- "Find an archive of this now-deleted blog post"
- "How has a university homepage's changed since 2015?"
