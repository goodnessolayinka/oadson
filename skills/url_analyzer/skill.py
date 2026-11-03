"""
skills/url_analyzer/skill.py — OADSON V3 URL / Phishing Heuristic Analyzer.

Two jobs:
  1. Follow redirects so a shortened/obfuscated link (bit.ly, t.co, a
     tracking redirect, etc.) reveals where it ACTUALLY lands before
     anyone clicks it.
  2. Run cheap, fast heuristics on both the original and final URL —
     no ML, no external verdict needed for these, they're structural
     red flags that are true or false by construction.

If VIRUSTOTAL_API_KEY is set, also does a fast read-only reputation
check_url lookup (no fresh scan submitted — that's virustotal skill's
job if the user wants to wait for one).
"""

import os
import re
import httpx
import base64
from urllib.parse import urlparse

from core.ssrf_guard import is_safe_url

# A short list of common high-value phishing targets. Not exhaustive —
# this is a cheap "does the domain LOOK LIKE one of these but isn't"
# check, not a brand database.
_COMMON_BRANDS = [
    "paypal", "google", "microsoft", "apple", "amazon", "facebook",
    "instagram", "whatsapp", "netflix", "bank", "chase", "wellsfargo",
    "binance", "coinbase", "metamask", "outlook", "office365",
]

_SUSPICIOUS_TLDS = {
    "zip", "mov", "top", "xyz", "click", "gq", "tk", "ml", "cf", "ga",
    "work", "loan", "win", "review",
}

_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def _levenshtein_close(a: str, b: str, max_dist: int = 2) -> bool:
    """True if a and b are within max_dist edits — cheap lookalike check."""
    if abs(len(a) - len(b)) > max_dist:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1] <= max_dist


def _analyze_structure(url: str) -> list[str]:
    flags = []
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
    except Exception:
        return [f"Could not parse as a URL at all — treat as suspicious by default."]

    host = parsed.hostname or ""
    netloc = parsed.netloc or ""

    if "@" in netloc:
        flags.append(f"Contains '@' in the authority part ({netloc!r}) — everything before '@' is ignored by browsers; the real host is whatever comes after. Classic obfuscation trick.")

    if _IP_RE.match(host):
        flags.append(f"Host is a raw IP address ({host}) rather than a domain name — legitimate services rarely link this way.")

    if "xn--" in host:
        flags.append("Host contains punycode (xn--) — likely uses non-ASCII characters to visually impersonate a real domain (homoglyph attack). Treat with high suspicion.")

    labels = host.split(".")
    if len(labels) >= 5:
        flags.append(f"Unusually deep subdomain chain ({host}) — a common trick to bury the real domain (e.g. 'paypal.com.login-verify.xyz' — the real domain is the LAST two labels, not the first).")

    tld = labels[-1].lower() if labels else ""
    if tld in _SUSPICIOUS_TLDS:
        flags.append(f"TLD '.{tld}' is disproportionately used for spam/phishing campaigns (cheap/unregulated registration). Not proof of malice, but raises the bar for other evidence.")

    # Lookalike-brand check: does any label closely resemble a known
    # brand WITHOUT being that brand's real registered domain?
    registrable = ".".join(labels[-2:]) if len(labels) >= 2 else host
    for brand in _COMMON_BRANDS:
        if brand in registrable:
            continue  # actually contains it cleanly, not a lookalike concern here
        for label in labels:
            if _levenshtein_close(label, brand) and label != brand:
                flags.append(f"Domain label '{label}' closely resembles the brand '{brand}' (likely a lookalike/typosquat) without being it.")
                break

    if parsed.scheme and parsed.scheme not in ("http", "https"):
        flags.append(f"Unusual scheme '{parsed.scheme}' — not a standard web link.")

    return flags


async def _follow_redirects(url: str) -> dict:
    """
    SSRF GUARD ADDED 2026-08-14. This follows the ACTUAL redirect chain
    hop by hop — a malicious first URL can redirect to a completely
    different host on hop 2, 3, etc. Checking only the entry URL would
    miss e.g. https://evil.com -> 301 -> http://169.254.169.254/...
    So every hop is checked before it's fetched, not just the first one.
    This is exactly the kind of tool most likely to be fed an untrusted
    URL (its whole job is "is this link safe"), so it needed this most.
    """
    target = url if "://" in url else f"http://{url}"
    chain = [target]
    safe, reason = await is_safe_url(target)
    if not safe:
        return {"final_url": target, "status_code": None, "redirect_chain": chain, "error": f"Blocked: {reason}"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
            current = target
            for _ in range(10):
                resp = await client.get(current, headers={"User-Agent": "Mozilla/5.0 (OADSON-URL-Analyzer)"})
                if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                    nxt = resp.headers["location"]
                    if not nxt.startswith("http"):
                        # relative redirect — resolve against current
                        parsed = urlparse(current)
                        nxt = f"{parsed.scheme}://{parsed.netloc}{nxt}"
                    safe, reason = await is_safe_url(nxt)
                    if not safe:
                        chain.append(nxt)
                        return {"final_url": current, "status_code": resp.status_code, "redirect_chain": chain, "note": f"Redirect chain stopped early: {reason}"}
                    chain.append(nxt)
                    current = nxt
                    continue
                return {"final_url": current, "status_code": resp.status_code, "redirect_chain": chain}
        return {"final_url": chain[-1], "status_code": None, "redirect_chain": chain, "note": "Redirect limit hit (10) — chain may be incomplete."}
    except Exception as e:
        return {"final_url": chain[-1], "status_code": None, "redirect_chain": chain, "error": str(e)}


async def _virustotal_lookup(url: str) -> dict | None:
    api_key = os.getenv("VIRUSTOTAL_API_KEY")
    if not api_key:
        return None
    try:
        url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://www.virustotal.com/api/v3/urls/{url_id}",
                headers={"x-apikey": api_key},
            )
            if resp.status_code != 200:
                return {"checked": True, "found_in_database": False}
            stats = resp.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            return {"checked": True, "found_in_database": True, "stats": stats}
    except Exception as e:
        return {"checked": True, "error": str(e)}


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    url = (args.get("url") or "").strip()

    if not url:
        return {"status": "error", "reason": "No 'url' provided."}

    original_flags = _analyze_structure(url)
    redirect_info = await _follow_redirects(url)
    final_url = redirect_info.get("final_url", url)

    final_flags = original_flags if final_url == url else _analyze_structure(final_url)

    vt_result = await _virustotal_lookup(final_url)

    all_flags = list(dict.fromkeys(original_flags + final_flags))  # dedup, keep order

    if not all_flags and not (vt_result and vt_result.get("stats", {}).get("malicious")):
        verdict = "No structural red flags found. Not a guarantee of safety — this is heuristic, not a full reputation database."
    else:
        verdict = f"{len(all_flags)} structural red flag(s) found — review before trusting this link."

    return {
        "status": "success",
        "original_url": url,
        "final_url": final_url,
        "redirected": final_url != url,
        "redirect_chain": redirect_info.get("redirect_chain"),
        "structural_flags": all_flags,
        "virustotal": vt_result,
        "verdict": verdict,
    }
