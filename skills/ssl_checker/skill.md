# SSL Checker

## What It Does
Checks SSL/TLS certificate details for any domain. Returns issuer, expiry date, validity status, protocol versions, cipher suites, and security rating.

## When to Use It
- User says "check SSL for", "is the certificate valid", "when does the cert expire"
- User verifying their deployed site has a valid cert
- User troubleshooting HTTPS errors
- User auditing TLS configuration for security

## When NOT to Use It
- User wants full HTTP header analysis → use header_analyzer
- User wants DNS records → use dns_lookup
- User asking general HTTPS concepts — answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `full_check` | complete certificate details. |
| `expiry_check` | quick expiry status only (fastest). |
| `chain` | certificate chain info. |
| `protocol_support` | which TLS versions the host supports. |

## Output Format
```json
{
  "domain": "example-app-backend.onrender.com",
  "valid": true,
  "issuer": "Let's Encrypt Authority X3",
  "issued_to": "*.onrender.com",
  "valid_from": "2026-03-01",
  "valid_until": "2026-09-01",
  "days_remaining": 79,
  "tls_version": "TLS 1.3",
  "cipher": "TLS_AES_256_GCM_SHA384",
  "security_rating": "A",
  "warnings": []
}
```

## Constraints
- HTTPS domains only (port 443 by default)
- Custom ports: specify explicitly (e.g., domain:8443)
- Certificate chain validation included
- Cannot retrieve private key information (by design)
- Self-signed certs flagged as warning, not error

## Examples
- "Does my Render backend have a valid SSL cert?"
- "Is my cert about to expire?"
- "Is this site using TLS 1.3 or an older version?"
