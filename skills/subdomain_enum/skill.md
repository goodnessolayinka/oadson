# Subdomain Enumerator

## What It Does
Discovers subdomains of a target domain using passive DNS enumeration, certificate transparency logs, and wordlist-based brute forcing. Maps out the attack surface of a domain.

## When to Use It
- User says "enumerate subdomains of", "find subdomains for", "map this domain"
- User doing authorized security recon on a target
- User checking their own domain's exposure
- User CTF reconnaissance phase

## When NOT to Use It
- Target is a domain the user doesn't own or have permission to test — ethical use only
- User wants DNS records for a known subdomain → use dns_lookup
- User wants full port scan of discovered hosts → follow up with port_scanner

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `crt_sh` | Certificate Transparency logs (fast, passive). |
| `dns_brute` | brute-force common subdomain names via DNS. |
| `search_engines` | scrape search results. |
| `all` | run crt_sh and dns_brute together and combine results (most thorough, slowest). |

## Output Format
```json
{
  "domain": "example.com",
  "discovery_methods": ["certificate_transparency", "passive_dns", "wordlist"],
  "subdomains": [
    {"subdomain": "api.example.com", "ip": "1.2.3.4", "status": "active"},
    {"subdomain": "staging.example.com", "ip": "1.2.3.5", "status": "active"},
    {"subdomain": "old.example.com", "ip": null, "status": "inactive"}
  ],
  "total_found": 12,
  "scan_time_s": 8.3
}
```

## Constraints
- Passive methods only by default (no active brute force unless specified)
- Active wordlist brute force significantly slower (minutes, not seconds)
- Wildcards in DNS (*. records) may inflate results
- Ethical and authorized use only
- Results depend on public certificate logs and passive DNS coverage

## Examples
- "What subdomains is the company exposing?"
- "Find staging or dev environments on this domain"
- "Map all subdomains of my own site"
