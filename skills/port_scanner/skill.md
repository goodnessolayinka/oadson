# Port Scanner

## What It Does
Scans a host for open TCP ports and identifies running services with version detection. Returns open ports, service names, banners, and potential vulnerabilities associated with detected services.

## When to Use It
- User says "scan ports on", "what ports are open", "check services running on"
- User doing authorized security assessment of a target
- User verifying their own server exposes only intended ports
- User doing CTF recon

## When NOT to Use It
- Target is a host the user does not own or have permission to scan — ethical use only
- User wants subdomain enumeration → use subdomain_enum
- User wants full device intel → combine with shodan_search

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `scan_common` | top 1000 ports (fast). |
| `scan_all` | every port (slow). |
| `scan_specific` | only the ports listed. |
| `version_detection` | identify service versions on open ports. |

## Output Format
```json
{
  "host": "example-app-backend.onrender.com",
  "ip": "34.200.10.1",
  "scan_time_s": 4.2,
  "open_ports": [
    {"port": 443, "service": "https", "version": "nginx 1.18", "state": "open"},
    {"port": 80, "service": "http", "state": "open", "note": "Redirects to HTTPS"}
  ],
  "closed_ports": 998,
  "os_guess": "Linux 5.x"
}
```

## Constraints
- Authorized use only — scanning without permission is illegal
- Default: top 1000 ports. Full scan (65535) takes significantly longer
- Rate limited to prevent abuse
- Firewalled ports appear as "filtered", not "closed"
- No UDP scanning in current implementation

## Examples
- "Is my Render backend only exposing port 443?"
- "Scan my VPS for unexpected open ports"
- "CTF: what services are running on this machine?"
