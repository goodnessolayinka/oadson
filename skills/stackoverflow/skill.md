# Stack Overflow

## What It Does
Searches Stack Overflow for programming questions and returns top answers with vote counts, accepted answers, and code snippets. Filters by language tag and relevance.

## When to Use It
- User says "search Stack Overflow for", "find an answer to", "how do I X in Python"
- User encountering a specific error and wants community answers
- User wants battle-tested solutions from real developers

## When NOT to Use It
- Question is simple enough to answer directly — do it
- User wants official library docs → use docs_fetcher
- User wants general web search → use web_search

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `search` | title keyword search. |
| `get_question` | a specific question + its answers by ID. |
| `by_tag` | questions filtered by tag. |
| `top_answerers` | reputation leaders for a tag. |

## Output Format
```json
{
  "query": "FastAPI 422 Unprocessable Entity",
  "results": [
    {
      "question": "FastAPI returns 422 Unprocessable Entity for valid request",
      "url": "https://stackoverflow.com/questions/...",
      "votes": 234,
      "answers": 8,
      "accepted_answer": {
        "votes": 189,
        "code_snippet": "from pydantic import BaseModel\n...",
        "explanation": "The issue is your Pydantic model..."
      },
      "tags": ["python", "fastapi", "pydantic"],
      "created": "2023-04-12"
    }
  ]
}
```

## Constraints
- Returns top 5 results by relevance and vote score
- Code snippets truncated at 500 characters — follow URL for full answer
- Outdated answers may exist — check dates
- Stack Overflow API rate limits apply

## Examples
- "Why does my MongoDB Motor query always return None?"
- "How to properly handle CORS in FastAPI?"
- "Stack Overflow: hmac.new() missing digestmod argument"
