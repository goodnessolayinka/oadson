"""
skills/threat_intel/skill.py — OADSON V3 Threat Intel Aggregator.

Calls into the SkillRegistry directly (same pattern as
skills/skill_manager/skill.py: `from bootstrap import get`) to chain
shodan_search -> virustotal -> cve_lookup for one target, instead of
making the model do three separate tool calls and merge the results
itself. Each sub-call is independently best-effort: if one skill's API
key isn't configured, that section is marked unavailable rather than
failing the whole aggregation.
"""

import re

_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    target = (args.get("target") or "").strip()

    if not target:
        return {"status": "error", "reason": "No 'target' provided."}

    try:
        from bootstrap import get
        registry = get("skill_registry")
    except Exception as e:
        return {"status": "error", "reason": f"Registry unavailable: {e}"}

    is_ip = bool(_IP_RE.match(target))
    sections = {}
    products_found = set()

    # ── Shodan ──
    if registry.has("shodan_search"):
        if is_ip:
            shodan_result = await registry.execute("shodan_search", {"args": {"action": "host_info", "ip_address": target}})
        else:
            shodan_result = await registry.execute("shodan_search", {"args": {"action": "search", "query": f"hostname:{target}", "limit": 5}})
        sections["shodan"] = shodan_result

        # Pull product/service names out of whatever shape shodan_search
        # returned, best-effort — used to feed cve_lookup below.
        data = shodan_result.get("data") if isinstance(shodan_result, dict) else None
        if isinstance(data, dict):
            for key in ("matches", "data", "results"):
                items = data.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("product"):
                            products_found.add(item["product"])
    else:
        sections["shodan"] = {"status": "unavailable", "reason": "shodan_search skill not registered."}

    # ── VirusTotal ──
    if registry.has("virustotal"):
        vt_url = target if "://" in target else f"http://{target}"
        vt_result = await registry.execute("virustotal", {"args": {"action": "check_url", "url": vt_url}})
        sections["virustotal"] = vt_result
    else:
        sections["virustotal"] = {"status": "unavailable", "reason": "virustotal skill not registered."}

    # ── CVE lookup, based on products Shodan actually found on this host ──
    cve_sections = {}
    if registry.has("cve_lookup") and products_found:
        for product in list(products_found)[:3]:  # cap to avoid a long fan-out
            cve_result = await registry.execute("cve_lookup", {"args": {"action": "by_product", "product": product, "limit": 5}})
            cve_sections[product] = cve_result
        sections["cve_by_product"] = cve_sections
    elif not registry.has("cve_lookup"):
        sections["cve_by_product"] = {"status": "unavailable", "reason": "cve_lookup skill not registered."}
    else:
        sections["cve_by_product"] = {"status": "info", "message": "No specific product/service names surfaced by Shodan to check against CVE database."}

    # ── Synthesized verdict ──
    risk_notes = []
    vt = sections.get("virustotal", {})
    vt_data = vt.get("data") if isinstance(vt, dict) else None
    if isinstance(vt_data, dict):
        report = vt_data.get("report", {})
        if report.get("malicious", 0) > 0:
            risk_notes.append(f"VirusTotal: {report['malicious']} engines flag this as malicious.")

    if products_found:
        risk_notes.append(f"Shodan found exposed service(s): {', '.join(sorted(products_found))}.")

    for product, res in cve_sections.items():
        if isinstance(res, dict):
            data = res.get("data")
            count = None
            if isinstance(data, dict):
                count = data.get("total_results") or (len(data.get("results", [])) if isinstance(data.get("results"), list) else None)
            if count:
                risk_notes.append(f"{count} known CVE(s) associated with '{product}' — verify the running version against the fix version before assuming exposure.")

    verdict = "; ".join(risk_notes) if risk_notes else "No red flags surfaced across Shodan/VirusTotal/CVE for this target — not a guarantee of safety, just nothing flagged by these sources."

    return {
        "status": "success",
        "target": target,
        "is_ip": is_ip,
        "sections": sections,
        "verdict": verdict,
    }
