# Code Explainer

## What It Does
Breaks down code snippets, functions, patterns, and programming concepts into clear explanations. Supports all major languages. Adapts depth — from beginner-friendly to senior-level.

## When to Use It
- User says "explain", "what does this do", "how does X work"
- User pastes code and wants to understand it
- User is learning a new concept or pattern
- User wants to understand someone else's code

## When NOT to Use It
- User wants bugs found → use bug_analyzer
- User wants to *write* new code — just write it directly
- No code or concept is provided

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `explain` | plain-language walkthrough. |
| `complexity` | Big-O time/space analysis. |
| `patterns` | detect design patterns. |
| `refactor` | code quality/refactoring suggestions. |
| `security` | vulnerability review. |

## Output Format
```json
{
  "summary": "One-line purpose of the code",
  "breakdown": [
    {"line": "async def run(payload):", "explanation": "Async entry point..."}
  ],
  "concepts": ["async/await", "dependency injection"],
  "examples": ["..."],
  "level": "intermediate"
}
```

## Constraints
- Explanation quality depends on completeness of code provided
- Very large codebases: summarizes structure, won't line-by-line
- Cannot execute code to verify behavior

## Examples
- "What does this MongoDB Motor query actually do?"
- "Explain how slowapi rate limiting works"
- "Break down this React hook"
- "What's the difference between @app.get and @router.get in FastAPI?"
