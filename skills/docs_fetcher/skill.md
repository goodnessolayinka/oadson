# Docs Fetcher

## What It Does
Fetches official documentation pages for libraries, frameworks, and APIs. Returns structured content — sections, code examples, and parameter references — from the source.

## When to Use It
- User says "get the docs for X", "fetch documentation", "look up the API reference"
- User wants official source material, not a summary from memory
- User checking a specific method signature or parameter

## When NOT to Use It
- User wants a general explanation — answer directly from knowledge
- URL is a blog post or tutorial, not official docs → use web_fetch
- User wants to search broadly → use web_search

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `summarize` | general overview. |
| `api_docs` | extract endpoints/auth/params. |
| `find_section` | locate one named section. |
| `compare` | diff two doc pages. |
| `examples` | extract code samples. |

## Output Format
```json
{
  "doc_title": "FastAPI - Dependencies",
  "source_url": "https://fastapi.tiangolo.com/tutorial/dependencies/",
  "last_updated": "2026-05-01",
  "sections": ["Intro", "First Steps", "Classes as Dependencies"],
  "content": "..."
}
```

## Constraints
- Fetches publicly accessible documentation only
- Private/paywalled docs return error
- Content truncated at 5000 characters — ask for specific section if needed
- Dynamic JS-rendered docs may return partial content

## Examples
- "Get the Anthropic API messages endpoint reference"
- "Fetch slowapi rate limiting docs for FastAPI"
- "What are the parameters for google.genai.Client()?"
