# WHOIS

## What It Does
Queries WHOIS records for domain registration data. Returns registrar, registration date, expiry date, nameservers, and registrant details (where not redacted by privacy protection).

## When to Use It
- User says "whois", "who owns this domain", "when does this domain expire"
- User checking if a domain is available or recently registered
- User doing OSINT recon on a target domain
- User verifying their own domain registration details

## When NOT to Use It
- User wants DNS records → use dns_lookup
- User wants IP ownership info → use ip_geolocation
- User wants site content → use web_fetch

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `lookup` | fetch WHOIS data for a domain. |
| `parse` | extract structured fields (registrar, dates, nameservers) from raw WHOIS text you already have. |

## Output Format
```json
{
  "domain": "example-app.com",
  "registrar": "Namecheap Inc.",
  "registration_date": "2026-01-15",
  "expiry_date": "2027-01-15",
  "last_updated": "2026-01-15",
  "nameservers": ["ns1.namecheap.com", "ns2.namecheap.com"],
  "registrant": {
    "name": "REDACTED (privacy protection)",
    "country": "NG"
  },
  "status": "clientTransferProhibited"
}
```

## Constraints
- GDPR/WHOIS privacy protection hides registrant data for most .com/.org domains
- .ng domains may have less privacy redaction
- Data reflects WHOIS at query time — propagation delays apply
- Expired domains may show previous owner data briefly

## Examples
- "Does this suspicious domain have a very recent registration date?"
- "When do I need to renew my domain?"
- "Who registered this phishing-looking domain?"
