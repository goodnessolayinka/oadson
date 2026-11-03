# Wikipedia

## What It Does
Searches and retrieves Wikipedia article summaries, full sections, and related topics. Returns structured content with source links.

## When to Use It
- User wants a factual overview of a concept, person, place, or event
- User says "what is", "who is", "explain X" for encyclopedic topics
- User wants a quick reference before diving deeper

## When NOT to Use It
- Topic is current news (last 48hrs) → use news or web_search
- User wants technical programming help — answer directly
- User wants highly specialized research — Wikipedia is a starting point only

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `search` | find matching articles. |
| `summary` | short extract for a known article title. |
| `full_article` | complete text. |
| `random` | a random article's summary. |

## Output Format
```json
{
  "title": "Obafemi Awolowo University",
  "url": "https://en.wikipedia.org/wiki/Obafemi_Awolowo_University",
  "summary": "a sample university is a federal university located in a sample city...",
  "sections": ["History", "Faculties", "Notable Alumni"],
  "categories": ["Universities in Nigeria", "Federal universities in Nigeria"],
  "last_updated": "2026-05-20"
}
```

## Constraints
- English Wikipedia by default — other languages on request
- Summary: first 2-3 paragraphs. Full article too large to return entirely
- Wikipedia may contain errors — verify critical facts with primary sources
- Stub articles return minimal content

## Examples
- "Give me a quick overview of public key cryptography"
- "Who founded a sample university and when?"
- "Explain OSINT in one paragraph"
