"""
Web Search Skill.

REWRITTEN — two separate bugs fixed:

1. WRONG API: this previously called https://api.duckduckgo.com/ directly
   (the "Instant Answer" API). That endpoint only returns structured
   instant-answer data (definitions, disambiguation pages, related
   topics) — it is NOT a general web search API and returns empty/no
   useful data for most ordinary queries, which is exactly why almost
   every search fell through to the "(fallback)" branch and returned a
   useless canned message instead of real results.

   Fixed to use the `duckduckgo-search` Python library (DDGS) instead —
   the same library already used elsewhere in this project for real web
   search — which actually returns ranked web results, not just instant
   answers.

2. IGNORED STRUCTURED ARGS: run() previously searched payload["message"]
   verbatim — the user's entire raw sentence, unparsed — which is why a
   request like "can you search for the best song on the web and then use
   spotify" produced a search literally for that whole sentence. Now it
   reads payload["args"]["query"], the clean, focused query Gemini builds
   per manifest.json's schema.
"""

import asyncio


async def run(payload):
    """Execute web search skill using structured args."""
    args = payload.get("args", {}) or {}
    query = (args.get("query") or "").strip()
    max_results = args.get("max_results") or 5

    if not query:
        return {"status": "error", "reason": "No search query provided"}

    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 5

    try:
        from ddgs import DDGS
    except ImportError:
        return {
            "status": "error",
            "reason": (
                "ddgs library not installed. Add 'ddgs' "
                "to requirements.txt — this skill cannot fall back to the "
                "api.duckduckgo.com Instant Answer endpoint, since that endpoint "
                "does not return real search results for most queries."
            )
        }

    try:
        # BUG FOUND 2026-08-21, confirmed in production logs: a single
        # web_search call fanned out across several backends internally
        # (ddgs's own multi-engine rotation — Wikipedia, Grokipedia,
        # Yahoo, Startpage all succeeded with 200s in the trace), but one
        # slow backend (Mojeek) timed out and another got rate-limited
        # (Brave, 429) — and ddgs raised on THAT, discarding every good
        # result that had already come back from the engines that
        # worked. The whole call then hit the blanket except below and
        # reported total failure to the user, even though real results
        # existed. Took 11 seconds end to end before finally giving up.
        #
        # Two independent fixes:
        # 1. asyncio.wait_for wraps the whole call with OADSON's own hard
        #    timeout, not dependent on whatever ddgs's internal per-
        #    backend timeout happens to be for the installed version —
        #    guarantees this skill can't hang the whole turn waiting on
        #    one slow engine.
        # 2. On timeout OR any ddgs-internal exception, retry once with
        #    backend='html' — ddgs's documented single-backend mode
        #    (skips the multi-engine rotation entirely, uses DuckDuckGo's
        #    own HTML endpoint only) — instead of giving up immediately.
        #    A single reliable backend beats a fan-out that's only as
        #    strong as its flakiest member.
        def _sync_search(backend=None):
            with DDGS() as ddgs:
                kwargs = {"max_results": max_results}
                if backend:
                    kwargs["backend"] = backend
                return list(ddgs.text(query, **kwargs))

        SEARCH_TIMEOUT_SECONDS = 12
        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(_sync_search), timeout=SEARCH_TIMEOUT_SECONDS
            )
        except Exception as first_err:
            # asyncio.TimeoutError IS Exception (an OSError subclass in
            # 3.11+) — no separate case needed, this catches both a hard
            # timeout and any ddgs-internal exception in one place.
            try:
                raw_results = await asyncio.wait_for(
                    asyncio.to_thread(_sync_search, "html"), timeout=SEARCH_TIMEOUT_SECONDS
                )
            except Exception:
                # Both attempts genuinely failed — re-raise the ORIGINAL
                # error so the message reflects what actually went wrong
                # first, not the fallback attempt's own failure.
                raise first_err

        if not raw_results:
            return {
                "status": "success",
                "query": query,
                "results": [],
                "count": 0,
                "message": f"No results found for '{query}'"
            }

        results = []
        for r in raw_results:
            results.append({
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
                "url": r.get("href", "")
            })

        return {
            "status": "success",
            "query": query,
            "results": results,
            "count": len(results),
            "message": f"Found {len(results)} result(s) for '{query}'"
        }

    except Exception as e:
        # Real failure (network error, rate limit, etc.) — surface it
        # honestly rather than returning a fake "success" with a canned
        # placeholder result, which is what the old fallback branch did.
        return {"status": "error", "reason": f"Search failed: {str(e)}"}
