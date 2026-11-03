# DNS Lookup

## What It Does
Queries DNS records for any domain. Supports A, AAAA, MX, CNAME, TXT, NS, SOA record types. Returns resolved IPs, TTL, and authoritative nameservers.

## When to Use It
- User says "resolve", "DNS", "what IP is", "check records for"
- User troubleshooting email delivery (MX records)
- User verifying domain configuration
- User doing recon on a domain

## When NOT to Use It
- User wants full WHOIS/ownership info → use whois
- User wants subdomains → use subdomain_enum
- Question is about general networking concepts — answer directly

## Calling Convention
Single-purpose skill — no `action` parameter. Required: `record_type`.

## Output Format
```json
{
  "query": "google.com",
  "record_type": "A",
  "answers": ["142.250.185.14"],
  "ttl": 300,
  "authoritative_ns": "ns1.google.com"
}
```

## Constraints
- Supports: A, AAAA, MX, CNAME, TXT, NS, SOA
- TTL shows cache duration in seconds
- Propagation delays affect accuracy for recently changed records
- Rate limits may apply on high-volume queries

## Examples
- "What IP does oadson-backend.onrender.com resolve to?"
- "Show all DNS records for my domain"
- "Is the MX record pointing to the right mail server?"
