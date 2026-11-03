"""
skills/browser/skill.py — OADSON V3 Browser Skill.

Runs headless Chromium via Playwright directly on the Render dyno.

SETUP — add to render.yaml build command (runs as root during build):
  pip install -r requirements.txt && playwright install chromium

DO NOT use --with-deps — it tries apt-get at runtime which needs root.
The binaries are installed during the BUILD phase when Render runs as root.
Set env var: PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright

Actions:
  setup         — install chromium browser binaries (run once if browser fails)
  navigate      — go to a URL, return page title + text content
  read          — extract readable text from current or specified page
  click         — click an element by CSS selector or text
  type          — type text into an input field
  screenshot    — take a screenshot, save to sandbox, return path
  extract       — extract specific data (tables, links, headings, images)
  scroll        — scroll the page
  run_script    — run JavaScript and return result
  wait          — wait for element or milliseconds
  status        — check if browser session is active
  close         — close the browser session

Sessions are per-user, stored in memory, auto-closed after 10 minutes idle.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from core.oadson_home import get_user_home
from core.ssrf_guard import is_safe_url

logger = logging.getLogger("oadson.skill.browser")

# In-memory session store — {user_id: {browser, page, context, last_used}}
_sessions: dict = {}
SESSION_TIMEOUT = 600  # 10 minutes

MAX_TEXT_LENGTH = 30_000  # cap page text to avoid context explosion


def _session_key(user_id: str) -> str:
    return str(user_id)


def _browser_not_ready_error() -> dict:
    return {
        "status": "error",
        "reason": (
            "Chromium not installed. Fix: add this to your render.yaml build command:\n"
            "  pip install -r requirements.txt && playwright install chromium\n"
            "Set env var PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright\n"
            "Then redeploy. Or run action='setup' to attempt install now (needs build-time root)."
        ),
    }


async def _emit_browser_status(session_id: str, message: str) -> None:
    """Fire a live status update through the SSE event bus so the user sees progress."""
    try:
        from bootstrap import get as _get
        bus = _get("event_bus")
        await bus.publish(session_id, {"type": "thinking", "content": f"🌐 Browser: {message}"})
    except Exception:
        pass  # Never let status emission crash the skill


async def _keepalive_loop(session_key: str) -> None:
    """
    Ping the browser page every 20s to prevent Browserless idle timeout (default 30s).
    Automatically stops and closes the session after SESSION_TIMEOUT seconds of
    user inactivity — so abandoned sessions don't burn Browserless CPU hours.
    """
    import asyncio
    while True:
        await asyncio.sleep(20)
        if session_key not in _sessions:
            break
        session = _sessions[session_key]
        # Auto-close if user has been idle longer than SESSION_TIMEOUT
        if time.time() - session["last_used"] > SESSION_TIMEOUT:
            logger.info(f"Browser session {session_key} idle — auto-closing.")
            await _close_session_by_key(session_key)
            break
        try:
            page = session.get("page")
            if page:
                await page.evaluate("1+1")
        except Exception:
            # Session is dead — remove it so next call gets a fresh one
            # instead of reusing a dead session and crashing with
            # "Target page, context or browser has been closed"
            logger.info(f"Browser keepalive lost ({session_key}) — clearing stale session.")
            await _close_session_by_key(session_key)
            break


async def _close_session_by_key(key: str) -> None:
    """Close a session directly by its key (used by keepalive loop)."""
    if key not in _sessions:
        return
    session = _sessions.pop(key)
    if "keepalive" in session:
        session["keepalive"].cancel()
    try:
        await session["browser"].close()
    except Exception:
        pass
    try:
        await session["pw"].stop()
    except Exception:
        pass


async def _get_or_create_session(user_id: str) -> tuple:
    """
    Get existing Playwright session or create a new one.
    Returns (browser, context, page).
    Raises RuntimeError with a clear message if playwright or chromium not ready.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "playwright not installed. Add 'playwright>=1.40.0' to requirements.txt and redeploy."
        )

    key = _session_key(user_id)
    now = time.time()

    # Clean up expired sessions
    expired = [k for k, s in _sessions.items() if now - s["last_used"] > SESSION_TIMEOUT]
    for k in expired:
        try:
            await _sessions[k]["browser"].close()
        except Exception:
            pass
        del _sessions[k]

    if key in _sessions:
        _sessions[key]["last_used"] = now
        return (
            _sessions[key]["browser"],
            _sessions[key]["context"],
            _sessions[key]["page"],
        )

    # Launch new browser — either via Browserless (free external) or local Chromium (paid RAM)
    try:
        import os, asyncio
        browserless_url = os.getenv("BROWSERLESS_URL", "").strip()

        # Auto-construct full WSS URL if only the token was provided
        # (common mistake — env var set to token, not full URL)
        if browserless_url and not browserless_url.startswith(("wss://", "ws://")):
            browserless_url = f"wss://chrome.browserless.io?token={browserless_url}"

        # Extend idle timeout — Browserless free tier caps at 60000ms (60s)
        if browserless_url and "timeout=" not in browserless_url:
            sep = "&" if "?" in browserless_url else "?"
            browserless_url += f"{sep}timeout=60000"

        pw = await async_playwright().start()
        if browserless_url:
            browser = await pw.chromium.connect_over_cdp(browserless_url)
            # CDP connect reuses an EXISTING Chromium — don't create a new context,
            # use what's already there. Creating new_context() on a CDP session
            # causes "Target page, context or browser has been closed" on next call.
            existing_contexts = browser.contexts
            if existing_contexts:
                context = existing_contexts[0]
                existing_pages = context.pages
                page = existing_pages[0] if existing_pages else await context.new_page()
            else:
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                )
                page = await context.new_page()
        else:
            # Local Chromium — requires BROWSER_ENABLED=true + 1GB+ RAM plan
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                ],
            )
    except Exception as e:
        err = str(e)
        if "executable" in err.lower() or "not found" in err.lower() or "browsertype.launch" in err.lower():
            raise RuntimeError(
                "Chromium binary not found. "
                "Add to render.yaml build command: playwright install chromium\n"
                "Set PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright and redeploy."
            )
        raise RuntimeError(f"Failed to launch browser: {err}")

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    )
    page = await context.new_page()

    _sessions[key] = {
        "browser": browser,
        "context": context,
        "page": page,
        "pw": pw,
        "last_used": now,
    }

    # Start keepalive ping for Browserless sessions (prevents 30s idle timeout)
    if os.getenv("BROWSERLESS_URL", "").strip():
        import asyncio
        _sessions[key]["keepalive"] = asyncio.create_task(_keepalive_loop(key))

    return browser, context, page


async def _close_session(user_id: str) -> None:
    key = _session_key(user_id)
    if key in _sessions:
        session = _sessions.pop(key)
        # Cancel keepalive ping before closing
        if "keepalive" in session:
            session["keepalive"].cancel()
        try:
            await session["browser"].close()
            await session["pw"].stop()
        except Exception:
            pass


def _extract_text(html_or_text: str, max_len: int = MAX_TEXT_LENGTH) -> str:
    """Strip HTML tags and collapse whitespace for readable text extraction."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_or_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_len]


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id")
    if not user_id:
        return {"status": "error", "reason": "user_id required for browser skill"}

    # Memory guard — Chromium needs ~400MB. Render free tier is 512MB total.
    # Options to unlock:
    #   1. Set BROWSERLESS_URL=wss://chrome.browserless.io?token=YOUR_TOKEN (free tier available)
    #   2. Set BROWSER_ENABLED=true on a Render plan with 1GB+ RAM
    import os
    browser_enabled = os.getenv("BROWSER_ENABLED", "false").lower() == "true"
    browserless_url = os.getenv("BROWSERLESS_URL", "").strip()

    if not browser_enabled and not browserless_url:
        return {
            "status": "error",
            "reason": (
                "Browser skill needs one of:\n"
                "1. BROWSERLESS_URL=wss://chrome.browserless.io?token=YOUR_TOKEN "
                "(free at browserless.io — 6 CPU hours/month, no RAM cost on OADSON)\n"
                "2. BROWSER_ENABLED=true on a Render plan with 1GB+ RAM"
            ),
        }

    action = (args.get("action") or "navigate").strip().lower()

    # ── SETUP — install chromium binaries ─────────────────────────────────────
    if action == "setup":
        import subprocess
        import sys
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return {
                    "status": "success",
                    "message": "Chromium installed successfully. Browser skill is ready.",
                    "stdout": result.stdout[-500:] if result.stdout else "",
                }
            else:
                return {
                    "status": "error",
                    "reason": (
                        "playwright install chromium failed. "
                        "This usually works during Render BUILD (root access), not runtime. "
                        "Add to render.yaml build command: playwright install chromium\n"
                        f"stderr: {result.stderr[-300:]}"
                    ),
                }
        except Exception as e:
            return {"status": "error", "reason": f"setup failed: {e}"}

    # ── CLOSE ─────────────────────────────────────────────────────────────────
    if action == "close":
        await _close_session(user_id)
        return {"status": "success", "message": "Browser session closed."}

    # ── All other actions need a browser session ───────────────────────────────
    session_id = payload.get("session_id", "")
    try:
        await _emit_browser_status(session_id, "starting session...")
        browser, context, page = await _get_or_create_session(user_id)

        # Health check — verify the session is actually alive before using it
        # This auto-recovers if Browserless closed the session between calls
        try:
            await page.evaluate("1+1")
        except Exception:
            logger.info("Browser session dead on health check — reconnecting...")
            await _close_session_by_key(_session_key(user_id))
            browser, context, page = await _get_or_create_session(user_id)

    except RuntimeError as e:
        return {"status": "error", "reason": str(e)}

    try:
        # ── NAVIGATE ──────────────────────────────────────────────────────────
        if action == "navigate":
            url = (args.get("url") or "").strip()
            if not url:
                return {"status": "error", "reason": "navigate requires a 'url'"}
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            # SSRF GUARD ADDED 2026-08-14: a real headless browser (JS
            # execution, richer navigation) reaching an internal address
            # is worse than a plain GET would be — same shared guard,
            # checked before page.goto() rather than before an httpx call.
            safe, reason = await is_safe_url(url)
            if not safe:
                return {"status": "error", "reason": reason}

            await _emit_browser_status(session_id, f"navigating to {url}...")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            await _emit_browser_status(session_id, f"loaded — {title or url}")
            content = await page.content()
            text = _extract_text(content)
            return {
                "status": "success",
                "url": page.url,
                "title": title,
                "text": text,
                "text_length": len(text),
                "message": f"Navigated to {page.url} — '{title}'"
            }

        # ── READ ──────────────────────────────────────────────────────────────
        elif action == "read":
            url = (args.get("url") or "").strip()
            if url:
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                safe, reason = await is_safe_url(url)
                if not safe:
                    return {"status": "error", "reason": reason}
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            title = await page.title()
            content = await page.content()
            text = _extract_text(content)
            selector = (args.get("selector") or "").strip()
            if selector:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        el_text = await el.inner_text()
                        text = el_text[:MAX_TEXT_LENGTH]
                except Exception:
                    pass
            return {
                "status": "success",
                "url": page.url,
                "title": title,
                "text": text,
                "truncated": len(text) >= MAX_TEXT_LENGTH,
            }

        # ── CLICK ─────────────────────────────────────────────────────────────
        elif action == "click":
            selector = (args.get("selector") or "").strip()
            text_match = (args.get("text") or "").strip()
            if text_match:
                selector = f"text={text_match}"
            if not selector:
                return {"status": "error", "reason": "click requires 'selector' or 'text'"}
            await _emit_browser_status(session_id, f"clicking '{text_match or selector}'...")
            await page.click(selector, timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
            title = await page.title()
            await _emit_browser_status(session_id, f"clicked — now on {page.url}")
            return {"status": "success", "url": page.url, "title": title,
                    "message": f"Clicked '{selector}', now on: {page.url}"}

        # ── TYPE ──────────────────────────────────────────────────────────────
        elif action == "type":
            selector = (args.get("selector") or "").strip()
            text = (args.get("text") or "").strip()
            if not selector or not text:
                return {"status": "error", "reason": "type requires 'selector' and 'text'"}
            await _emit_browser_status(session_id, f"typing into '{selector}'...")
            clear_first = args.get("clear", True)
            if clear_first:
                await page.fill(selector, text, timeout=10000)
            else:
                await page.type(selector, text, timeout=10000)
            return {"status": "success", "message": f"Typed into '{selector}'"}

        # ── SCREENSHOT ────────────────────────────────────────────────────────
        elif action == "screenshot":
            await _emit_browser_status(session_id, "taking screenshot...")
            home = get_user_home(user_id)
            screenshots_dir = home / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            filename = f"screenshot_{int(time.time())}.jpg"
            path = screenshots_dir / filename
            await page.screenshot(
                path=str(path),
                full_page=args.get("full_page", False),
                type="jpeg",
                quality=75,
            )
            rel_path = str(path.relative_to(home))
            await _emit_browser_status(session_id, f"screenshot captured — analysing...")

            import base64 as _b64, os as _os
            with open(str(path), "rb") as f:
                img_bytes = f.read()
            img_b64 = _b64.b64encode(img_bytes).decode()

            # Gemini vision — let OADSON actually SEE the screenshot
            visual_description = ""
            gemini_key = _os.getenv("GEMINI_API_KEY", "")
            if gemini_key:
                try:
                    import httpx as _httpx
                    vision_url = (
                        f"https://generativelanguage.googleapis.com/v1beta/models/"
                        f"gemini-3.5-flash:generateContent"
                    )
                    vision_payload = {
                        "contents": [{
                            "role": "user",
                            "parts": [
                                {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                                {"text": "Describe what you see on this screenshot in detail. Include any visible text, UI elements, content, errors, or anything notable. Be specific and thorough."}
                            ]
                        }],
                        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.1},
                    }
                    # SECURITY FIX 2026-08-14: key moved from URL to
                    # x-goog-api-key header — was leaking into Render's
                    # httpx request logs.
                    async with _httpx.AsyncClient(timeout=20) as client:
                        v_res = await client.post(vision_url, json=vision_payload, headers={"x-goog-api-key": gemini_key})
                    if v_res.status_code == 200:
                        visual_description = v_res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                except Exception as ve:
                    logger.warning(f"Vision analysis failed (non-fatal): {ve}")

            await _emit_browser_status(session_id, "done ✓")
            return {
                "status": "success",
                "path": rel_path,
                "url": page.url,
                "filename": filename,
                "image_data": f"data:image/jpeg;base64,{img_b64}",
                "visual_description": visual_description,
                "message": (
                    f"Screenshot taken of {page.url}."
                    + (f"\n\nWhat I see:\n{visual_description}" if visual_description else "")
                ),
            }

        # ── EXTRACT ───────────────────────────────────────────────────────────
        elif action == "extract":
            what = (args.get("what") or args.get("query") or "links").strip().lower()

            if "link" in what:
                links = await page.eval_on_selector_all("a[href]", "els => els.map(e => ({text: e.innerText.trim(), href: e.href})).filter(l => l.href && l.href.startsWith('http'))")
                return {"status": "success", "links": links[:50], "count": len(links)}

            elif "table" in what:
                tables = await page.eval_on_selector_all("table", """
                    tables => tables.map(t => {
                        const rows = Array.from(t.querySelectorAll('tr'));
                        return rows.map(r => Array.from(r.querySelectorAll('th,td')).map(c => c.innerText.trim()));
                    })
                """)
                return {"status": "success", "tables": tables[:5], "count": len(tables)}

            elif "heading" in what or "header" in what:
                headings = await page.eval_on_selector_all("h1,h2,h3,h4", "els => els.map(e => ({level: e.tagName, text: e.innerText.trim()}))")
                return {"status": "success", "headings": headings}

            elif "image" in what:
                images = await page.eval_on_selector_all("img[src]", "els => els.map(e => ({src: e.src, alt: e.alt})).filter(i => i.src)")
                return {"status": "success", "images": images[:20]}

            else:
                # Generic: try as a CSS selector
                try:
                    elements = await page.eval_on_selector_all(what, "els => els.map(e => e.innerText.trim())")
                    return {"status": "success", "elements": elements[:20], "selector": what}
                except Exception as e:
                    return {"status": "error", "reason": f"Unknown extract target '{what}': {e}"}

        # ── SCROLL ────────────────────────────────────────────────────────────
        elif action == "scroll":
            direction = (args.get("direction") or "down").lower()
            amount = int(args.get("amount") or 500)
            if direction == "down":
                await page.evaluate(f"window.scrollBy(0, {amount})")
            elif direction == "up":
                await page.evaluate(f"window.scrollBy(0, -{amount})")
            elif direction == "bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction == "top":
                await page.evaluate("window.scrollTo(0, 0)")
            return {"status": "success", "message": f"Scrolled {direction}"}

        # ── RUN_SCRIPT ────────────────────────────────────────────────────────
        elif action == "run_script":
            script = (args.get("script") or "").strip()
            if not script:
                return {"status": "error", "reason": "run_script requires 'script'"}
            result = await page.evaluate(script)
            return {"status": "success", "result": result}

        # ── WAIT ──────────────────────────────────────────────────────────────
        elif action == "wait":
            selector = (args.get("selector") or "").strip()
            ms = int(args.get("ms") or 2000)
            if selector:
                await page.wait_for_selector(selector, timeout=15000)
                return {"status": "success", "message": f"Element '{selector}' appeared"}
            else:
                await asyncio.sleep(ms / 1000)
                return {"status": "success", "message": f"Waited {ms}ms"}

        # ── STATUS ────────────────────────────────────────────────────────────
        elif action == "status":
            key = _session_key(user_id)
            active = key in _sessions
            return {
                "status": "success",
                "browser_active": active,
                "current_url": page.url if active else None,
                "current_title": (await page.title()) if active else None,
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Valid: navigate, read, click, type, screenshot, extract, scroll, run_script, wait, status, close"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
