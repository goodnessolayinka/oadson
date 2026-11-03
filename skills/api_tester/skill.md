# API Tester

## What It Does
Sends real HTTP requests to API endpoints and returns status codes, response bodies, headers, and timing. Supports GET, POST, PUT, PATCH, DELETE. Handles Bearer, Basic, and API-Key auth.

## When to Use It
- User says "test", "hit", "call", "ping", or "check" an endpoint
- User pastes a URL and expects a response
- User wants to verify auth, latency, or correctness of an API
- User is debugging why an endpoint is failing

## When NOT to Use It
- User wants documentation on how to *build* an API → use code_explainer
- User wants to scan for vulnerabilities → use port_scanner or header_analyzer
- No URL is provided

## Calling Convention
Single-purpose skill — no `action` parameter. Required: `url`.

## Output Format
```json
{
  "url": "https://api.example.com/users",
  "method": "GET",
  "http_status": 200,
  "response_time_ms": 143,
  "response_body": {...},
  "headers": {...},
  "auth_used": "Bearer"
}
```

## Constraints
- Timeout: 15 seconds per request
- Load test cap: 100 requests
- No persistent sessions between calls
- Cannot execute JavaScript or follow redirects beyond 3 hops

## Examples
- "Is my Render backend alive? GET https://oadson-backend.onrender.com/health"
- "Test POST /api/v2/auth/google with this token"
- "Why is my endpoint returning 422?"
