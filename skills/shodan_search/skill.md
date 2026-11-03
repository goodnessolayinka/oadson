# Shodan Search

## What It Does
Queries the Shodan database for internet-connected devices, exposed services, and network infrastructure. Returns IPs, ports, banners, OS info, and known vulnerabilities for discovered hosts.

## When to Use It
- User says "search Shodan for", "find exposed X on the internet"
- User doing security research on a specific service or technology
- User mapping infrastructure exposure for a target domain
- User CTF recon requiring passive OSINT

## When NOT to Use It
- User wants to actively scan ports → use port_scanner
- No Shodan API key is configured — return auth error
- Clearly malicious intent — ethical and authorized use only

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `search` | query Shodan for matching devices/services. |
| `host_info` | detailed info about one specific IP address. |

## Output Format
```json
{
  "query": "product:MongoDB country:NG",
  "total_results": 142,
  "results": [
    {
      "ip": "197.210.x.x",
      "port": 27017,
      "service": "MongoDB",
      "version": "4.4.6",
      "location": {"country": "Nigeria", "city": "Lagos"},
      "banner": "...",
      "vulnerabilities": ["CVE-2021-20330"]
    }
  ]
}
```

## Constraints
- Requires SHODAN_API_KEY in environment
- Free tier: limited to 1 result page (10 results)
- Paid tier: full result pagination
- Data is passive — no active scanning
- Ethical use only — research and authorized assessments

## Examples
- "How many Nginx servers are exposed in Nigeria?"
- "Find webcams with default credentials"
- "What infrastructure does this company have exposed?"
