# CVE Lookup

## What It Does
Searches the CVE (Common Vulnerabilities and Exposures) database for vulnerability details. Returns CVSS scores, affected software, severity ratings, patch status, and exploit references.

## When to Use It
- User mentions "CVE-XXXX-XXXXX" format
- User asks about vulnerabilities in specific software/versions
- User wants to know if a dependency is exploitable
- User doing security research or CTF

## When NOT to Use It
- User wants to scan their own system → use port_scanner
- User wants to check a file for malware → use virustotal
- User asking general security questions — answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `search` | keyword search. |
| `get` | full details for one CVE ID. |
| `by_product` | CVEs affecting a named product. |
| `by_severity` | filter by severity level. |

## Output Format
```json
{
  "cve_id": "CVE-2023-44487",
  "description": "HTTP/2 Rapid Reset Attack...",
  "severity": "High",
  "cvss_score": 7.5,
  "affected_software": ["Apache HTTP Server < 2.4.58"],
  "published": "2023-10-10",
  "patch_available": true,
  "references": ["https://nvd.nist.gov/vuln/detail/CVE-2023-44487"]
}
```

## Constraints
- Data sourced from NVD (National Vulnerability Database)
- May be delayed by NVD indexing (up to 48hrs for new CVEs)
- Exploit availability disclosed as-is — ethical use only
- CVSS v3.1 scoring used where available

## Examples
- "Is CVE-2021-44228 (Log4Shell) patched in Log4j 2.17?"
- "What critical CVEs dropped this week?"
- "What's the impact level of this CVE?"
