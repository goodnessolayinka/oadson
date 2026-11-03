# Header Analyzer

## What It Does
Fetches and analyzes HTTP response headers from a URL. Checks for security headers, server info leakage, caching policy, CORS configuration, and content security policy.

## When to Use It
- User says "analyze headers", "check security headers", "what headers does X return"
- User auditing a web server for security misconfigurations
- User troubleshooting CORS or caching issues
- User doing recon on a target server

## When NOT to Use It
- User wants SSL certificate details → use ssl_checker
- User wants full port scan → use port_scanner
- User wants DNS records → use dns_lookup

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `get_headers` | all raw response headers. |
| `security_audit` | check for missing security headers with a score. |
| `detect_server` | identify the web server/tech stack. |
| `explain_header` | explain what a specific header name+value means (no network request needed). |

## Output Format
```json
{
  "url": "https://example-app-backend.onrender.com",
  "headers": {
    "server": "uvicorn",
    "x-frame-options": "DENY",
    "content-security-policy": "...",
    "strict-transport-security": "max-age=31536000"
  },
  "security_rating": "B+",
  "missing_headers": ["X-Content-Type-Options", "Permissions-Policy"],
  "recommendations": [
    "Add X-Content-Type-Options: nosniff",
    "Consider hiding server version"
  ]
}
```

## Constraints
- HTTP GET request only — does not test POST behavior
- Redirects followed up to 3 hops
- Timeout: 10 seconds
- Cannot analyze headers requiring authentication
- Rating is heuristic — not a formal security audit

## Examples
- "Does my Render backend expose server version in headers?"
- "Is my API missing any critical security headers?"
- "Check if HSTS is enabled on my domain"
