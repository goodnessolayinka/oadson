# Bug Analyzer

## What It Does
Analyzes code or error messages to identify bugs, root causes, and fixes. Handles syntax errors, logic errors, runtime exceptions, and security issues. Works across Python, JavaScript, TypeScript, and more.

## When to Use It
- User pastes an error message or traceback
- User says "why is this broken", "debug this", "find the bug"
- User shares code that isn't behaving as expected
- User wants a code security review

## When NOT to Use It
- No code or error message is provided
- User wants general coding advice → use code_explainer
- User wants to test a live endpoint → use api_tester

## Calling Convention
Single-purpose skill — no `action` parameter. Required: `error_message`.

## Output Format
```json
{
  "bugs_found": [
    {
      "type": "NullPointerError",
      "line": 14,
      "severity": "high",
      "description": "user object can be None if login fails",
      "fix": "Add null check before accessing user.id"
    }
  ],
  "total_issues": 2,
  "security_flags": [],
  "recommendation": "..."
}
```

## Constraints
- Static analysis only — cannot run code
- May produce false positives on complex logic
- Best results with actual error messages + code together
- Context-dependent: partial code = lower confidence

## Examples
- "sessions.py try/except has wrong indentation — for loop never executes"
- "Why does my MongoDB query always return None?"
- "Find vulnerabilities in this auth handler"
