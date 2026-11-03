# Usage Analytics

## What It Does
Reports the user's OADSON usage statistics: token consumption, model usage breakdown, most-triggered skills, session counts, and estimated API costs over time.

## When to Use It
- User says "how much have I used", "show my usage", "what's my token count"
- User wants to track costs across Gemini models
- User wants to see which skills they use most
- User optimizing their OADSON usage patterns

## When NOT to Use It
- User wants backend system health → use system_status
- User wants session message history → use session_log
- No usage data exists yet (new user)

## Calling Convention
This is an `action`-based skill:

| Action | What it does |
|---|---|
| `summary` | Overall totals |
| `skill_usage` | Per-skill breakdown |
| `daily` / `hourly` | Time-bucketed counts |
| `cost_breakdown` | Estimated cost per skill |
| `top_skills` | Most-used skills |
| `reset` | Clear stats |

## Output Format
```json
{
  "period": "June 2026",
  "total_sessions": 47,
  "total_messages": 312,
  "tokens": {
    "input": 184200,
    "output": 96500,
    "total": 280700
  },
  "estimated_cost_usd": 0.14,
  "model_breakdown": {
    "gemini-flash-lite": "68%",
    "gemini-flash": "32%"
  },
  "top_skills": ["web_search", "github", "bug_analyzer"],
  "most_active_day": "Tuesday"
}
```

## Constraints
- Token counts are estimates (Gemini API billing units, not exact bytes)
- Cost estimates based on published Gemini pricing at time of calculation
- Data tracked from account creation — no historical import
- Breakdown limited to last 30 days unless specified

## Examples
- "Am I burning tokens faster than expected?"
- "How much has OADSON cost me this month?"
- "What's my most used skill?"
