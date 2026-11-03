# News

## What It Does
Fetches and summarizes current news articles from multiple sources. Filters by topic, category, or country. Returns headlines, summaries, and source links.

## When to Use It
- User asks "what's happening", "latest news on X", "tech news today"
- User wants news filtered by topic (tech, cybersecurity, sports, Nigeria)
- User wants article summaries without opening links

## When NOT to Use It
- User wants deep research on a topic → use web_search
- User wants to read the full article → use web_fetch with the URL
- User asking about historical events — answer from knowledge

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `top_headlines` | top stories for a country. |
| `search` | keyword search. |
| `breaking` | current breaking news. |
| `by_category` | news for one category. |
| `sources` | list available news outlets. |

## Output Format
```json
{
  "articles": [
    {
      "title": "OpenAI launches new model",
      "source": "TechCrunch",
      "url": "https://techcrunch.com/...",
      "summary": "OpenAI released...",
      "published": "2026-06-14T12:00:00Z",
      "category": "technology"
    }
  ],
  "total": 10,
  "query": "tech news"
}
```

## Constraints
- Headlines may be up to 1 hour behind depending on source
- Paywalled articles return title and snippet only
- Nigerian local news may have limited English-language coverage
- Max 10 articles per query

## Examples
- "Any major cybersecurity incidents this week?"
- "What's happening in Nigerian tech?"
- "Summarize the top 5 headlines right now"
