"""
skills/recon_report/skill.py — OADSON V3 Recon Report Aggregator.

Same pattern as threat_intel.py: calls straight into the SkillRegistry
(`from bootstrap import get`) to chain several existing security skills
for one target and return a single synthesized report, instead of the
model doing 5-6 separate tool calls across a conversation and the user
having to mentally merge them.

Scope, deliberately: DNS, WHOIS/host-info, TLS, passive subdomain
enumeration, HTTP security headers, and threat_intel (which itself
already wraps Shodan/VirusTotal/CVE). All passive/read-only.
port_scanner is NEVER called here — that skill has its own explicit
confirm=true gate before it will actively scan a host, and a "give me
a report" request isn't informed consent for that. If an active scan
is actually wanted, it's a separate, deliberate call.

Every sub-skill call is independently best-effort: if one isn't
registered, or its API key isn't configured, that section is marked
unavailable rather than failing the whole report.
"""

import re

_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


async def _try(registry, skill_name: str, args: dict) -> dict:
    if not registry.has(skill_name):
        return {"status": "unavailable", "reason": f"{skill_name} skill not registered."}
    try:
        return await registry.execute(skill_name, {"args": args})
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    target = (args.get("target") or "").strip()
    depth = args.get("depth") or "standard"

    if not target:
        return {"status": "error", "reason": "No 'target' provided."}
    if depth not in ("quick", "standard", "deep"):
        depth = "standard"

    try:
        from bootstrap import get
        registry = get("skill_registry")
    except Exception as e:
        return {"status": "error", "reason": f"Registry unavailable: {e}"}

    is_ip = bool(_IP_RE.match(target))
    sections = {}
    findings = []

    # ── DNS / host identity ──
    if is_ip:
        sections["reverse_dns"] = await _try(registry, "dns_lookup", {"record_type": "reverse", "ip_address": target})
    else:
        sections["dns_a"] = await _try(registry, "dns_lookup", {"record_type": "A", "hostname": target})
        sections["dns_mx"] = await _try(registry, "dns_lookup", {"record_type": "MX", "hostname": target})
        sections["whois"] = await _try(registry, "whois", {"action": "lookup", "domain": target})

    # ── TLS ──
    ssl_host = target
    sections["ssl"] = await _try(registry, "ssl_checker", {"action": "expiry_check", "hostname": ssl_host})
    ssl_data = sections["ssl"].get("data") if isinstance(sections["ssl"], dict) else None
    if isinstance(ssl_data, dict) and ssl_data.get("days_until_expiry") is not None:
        days = ssl_data["days_until_expiry"]
        if days < 14:
            findings.append(f"TLS certificate expires in {days} day(s) — renew soon.")

    # ── HTTP security headers ──
    http_target = target if "://" in target else f"https://{target}"
    sections["headers"] = await _try(registry, "header_analyzer", {"action": "security_audit", "url": http_target})
    header_data = sections["headers"].get("data") if isinstance(sections["headers"], dict) else None
    if isinstance(header_data, dict) and header_data.get("score") is not None:
        findings.append(f"HTTP security header score: {header_data['score']}.")

    # ── Passive subdomain enumeration (domains only) ──
    if not is_ip and depth in ("standard", "deep"):
        enum_action = "all" if depth == "deep" else "crt_sh"
        sections["subdomains"] = await _try(
            registry, "subdomain_enum",
            {"action": enum_action, "domain": target, **({"wordlist_size": "small"} if depth == "deep" else {})}
        )
        sub_data = sections["subdomains"].get("data") if isinstance(sections["subdomains"], dict) else None
        if isinstance(sub_data, dict):
            count = sub_data.get("total_found") or len(sub_data.get("subdomains", []) or [])
            if count:
                findings.append(f"{count} subdomain(s) found via passive enumeration.")

    # ── threat_intel (Shodan + VirusTotal + CVE, already an aggregator itself) ──
    sections["threat_intel"] = await _try(registry, "threat_intel", {"target": target})
    ti_data = sections["threat_intel"] if isinstance(sections["threat_intel"], dict) else {}
    if ti_data.get("verdict"):
        findings.append(f"Threat intel: {ti_data['verdict']}")

    verdict = "; ".join(findings) if findings else "Passive recon complete — nothing flagged, but this is not an exhaustive audit."

    return {
        "status": "success",
        "target": target,
        "is_ip": is_ip,
        "depth": depth,
        "sections": sections,
        "findings": findings,
        "verdict": verdict,
        "note": "Passive recon only — no active port scan was run. Ask separately (with explicit confirmation) if an active scan is actually wanted.",
    }
