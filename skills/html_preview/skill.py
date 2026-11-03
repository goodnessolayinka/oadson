"""
skills/html_preview/skill.py

What this skill ACTUALLY is: an HTML analyser/reader.
It is NOT a visual renderer — it cannot render a webpage to screen.

Actions:
  fetch         — download HTML from a URL, return text content
  render_local  — read an HTML file from user's sandbox by path, return content
  validate      — check HTML for common structural issues
  extract_text  — strip tags, return readable text
  extract_links — pull all hrefs
  extract_meta  — pull title, description, keywords
"""

import re
import logging
from pathlib import Path

logger = logging.getLogger("oadson.skill.html_preview")

MAX_HTML_LEN = 8000


def _strip_tags(html: str, max_len: int = MAX_HTML_LEN) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_len]


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id", "")
    action = (args.get("action") or "").strip().lower()

    if not action:
        return {
            "status": "info",
            "message": (
                "html_preview actions: "
                "fetch (url → downloads HTML from a web URL), "
                "render_local (path → reads HTML file from your sandbox workspace), "
                "validate / extract_text / extract_links / extract_meta (html → analyse raw HTML string)."
            ),
        }

    # ── FETCH — download from a public URL ──
    if action == "fetch":
        import httpx
        from core.ssrf_guard import is_safe_url
        url = (args.get("url") or args.get("html_url") or "").strip()
        if not url:
            return {"status": "error", "reason": "fetch requires 'url' (a public HTTP/S URL)."}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        # SSRF GUARD ADDED 2026-08-14: this action fetches whatever URL
        # is passed, with no prior restriction — same shared guard
        # web_fetch uses.
        safe, reason = await is_safe_url(url)
        if not safe:
            return {"status": "error", "reason": reason}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                res = await client.get(url)
            if res.status_code != 200:
                return {"status": "error", "reason": f"HTTP {res.status_code} from {url}"}
            html = res.text
            text = _strip_tags(html)
            return {
                "status": "success",
                "url": url,
                "title": _extract_title(html),
                "text": text,
                "html_length": len(html),
                "truncated": len(html) > MAX_HTML_LEN,
                "message": f"Fetched {url} ({len(html)} chars). Note: this is text content only — not a visual render.",
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    # ── RENDER_LOCAL — read HTML file from user sandbox ──
    if action == "render_local":
        if not user_id:
            return {"status": "error", "reason": "user_id required for render_local."}

        rel_path = (args.get("path") or args.get("file") or "").strip()
        if not rel_path:
            return {"status": "error", "reason": "render_local requires 'path' (relative to your workspace, e.g. 'my-site/index.html')."}

        try:
            from core.oadson_home import get_user_home
            sandbox = get_user_home(user_id)
            file_path = (sandbox / rel_path).resolve()

            # Safety: must stay inside sandbox
            if not str(file_path).startswith(str(sandbox)):
                return {"status": "error", "reason": "Path must be inside your workspace."}

            if not file_path.exists():
                return {"status": "error", "reason": f"File not found: {rel_path}. Use @terminal to list files in your workspace."}

            html = file_path.read_text(encoding="utf-8", errors="replace")
            text = _strip_tags(html)
            validation = _validate(html)

            return {
                "status": "success",
                "file": rel_path,
                "title": _extract_title(html),
                "text_preview": text[:2000],
                "html_length": len(html),
                "validation": validation,
                "message": (
                    f"Read '{rel_path}' ({len(html)} chars). "
                    "To view it in a browser: push to GitHub via @github and deploy to Vercel/Netlify. "
                    "Or copy the file to your local machine."
                ),
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

    # ── Actions that take raw HTML ──
    html = (args.get("html") or "").strip()
    if not html and action in ("validate", "extract_text", "extract_links", "extract_meta"):
        return {"status": "error", "reason": f"action='{action}' requires 'html' (raw HTML string)."}

    if action == "validate":
        return {"status": "success", **_validate(html)}

    if action == "extract_text":
        text = _strip_tags(html)
        return {"status": "success", "text": text, "length": len(text)}

    if action == "extract_links":
        links = re.findall(r'href=["\']([^"\']+)["\']', html)
        return {"status": "success", "links": links[:100], "count": len(links)}

    if action in ("extract_meta", "extract_metadata"):
        return {"status": "success", **_extract_meta(html)}

    return {
        "status": "error",
        "reason": (
            f"Unknown action '{action}'. "
            "Valid: fetch, render_local, validate, extract_text, extract_links, extract_meta"
        ),
    }


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _validate(html: str) -> dict:
    errors = []
    if not re.search(r"<!doctype|<html", html[:200], re.IGNORECASE):
        errors.append("Missing DOCTYPE or <html> tag")
    if not re.search(r'charset', html[:1000], re.IGNORECASE):
        errors.append("No charset specified")
    if not re.search(r'<meta[^>]+viewport', html, re.IGNORECASE):
        errors.append("Missing viewport meta tag")
    if re.search(r'<img[^>]*>', html, re.IGNORECASE) and not re.search(r'<img[^>]+alt=', html, re.IGNORECASE):
        errors.append("img tags missing alt attributes")
    open_tags = len(re.findall(r"<[a-zA-Z][^/>]*>", html))
    close_tags = len(re.findall(r"</[a-zA-Z]+>", html))
    if open_tags > close_tags + 5:
        errors.append(f"Possibly unclosed tags ({open_tags} open, {close_tags} close)")
    return {
        "valid": len(errors) == 0,
        "issues": errors,
        "message": "No issues found." if not errors else f"{len(errors)} issue(s): {'; '.join(errors)}",
    }


def _extract_meta(html: str) -> dict:
    meta = {}
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        meta["title"] = m.group(1).strip()
    for name in ("description", "keywords", "author"):
        m = re.search(rf'<meta[^>]+name=["\']?{name}["\']?[^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if not m:
            m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']?{name}["\']?', html, re.IGNORECASE)
        if m:
            meta[name] = m.group(1).strip()
    return {"metadata": meta, "fields_found": list(meta.keys())}
