# Web Search

## What It Does
Searches the internet for current information, news, tutorials, and documentation. Returns ranked results with titles, URLs, and snippets from multiple sources.

## When to Use It
- User wants current information beyond training knowledge
- User says "search for", "look up", "find information about"
- User wants multiple sources on a topic
- No specific URL is known — user is exploring

## When NOT to Use It
- User provides a specific URL → use web_fetch
- Question has a clear known answer — answer directly without searching
- User wants official docs → use docs_fetcher for cleaner output

## Calling Convention
Single-purpose skill — no `action` parameter. Required: `query`.

## Output Format
```json
{
  "query": "FastAPI Railway deployment",
  "results": [
    {
      "title": "Deploy FastAPI to Railway in 5 Minutes",
      "url": "https://dev.to/...",
      "snippet": "Railway makes deploying Python backends...",
      "source": "dev.to",
      "published": "2026-03-12"
    }
  ],
  "total_results": 10,
  "search_time_ms": 340
}
```

## Constraints
- Returns top 10 results by relevance
- Results ranked by recency + quality signals
- Paywalled content returns title/snippet only
- Real-time results — may differ from model training knowledge
- Search index may lag by minutes on breaking news

## Examples
- "Latest changes to the Anthropic API"
- "How to fix Python datetime import error"
- "Search for a university cybersecurity department curriculum"
