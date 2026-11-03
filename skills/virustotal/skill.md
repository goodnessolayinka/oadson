# VirusTotal

## What It Does
Scans files, URLs, domains, and IPs against 70+ antivirus engines and threat intelligence databases via the VirusTotal API. Returns detection rates, threat categories, and reputation scores.

## When to Use It
- User says "scan this URL", "is this file malicious", "check this IP on VirusTotal"
- User received a suspicious link or file
- User doing malware analysis or threat research
- User verifying a domain's reputation before integration

## When NOT to Use It
- No VIRUSTOTAL_API_KEY configured — return auth error
- User wants to *create* malware — refused categorically
- URL is clearly benign (google.com, github.com) — answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `check_url` | look up an existing report (no new scan). |
| `scan_url` | submit a URL for a new scan (use this if check_url comes back not-found). |
| `check_hash` | look up a file hash's reputation. |

## Output Format
```json
{
  "target": "http://suspicious-link.com",
  "target_type": "url",
  "scan_date": "2026-06-15T10:00:00Z",
  "detection_ratio": "3/72",
  "positives": 3,
  "total_engines": 72,
  "threat_category": "phishing",
  "permalink": "https://www.virustotal.com/...",
  "flagged_by": ["Kaspersky", "Bitdefender", "ESET"],
  "verdict": "suspicious"
}
```

## Constraints
- Requires VIRUSTOTAL_API_KEY
- Free tier: 4 requests/minute, 500/day
- File upload: max 32MB via hash lookup (no direct file upload)
- Results reflect last scan — request a new scan if data is stale
- False positives possible — use alongside other tools

## Examples
- "Is this download link safe?"
- "Scan the domain my client wants me to integrate with"
- "This file hash appeared in a threat report — check it"
