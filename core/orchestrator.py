"""
core/orchestrator.py — OADSON V3 AIOrchestrator.

The brain. Every request passes through here.

Execution lifecycle:
  1.  Receive objective
  2.  Build context (session history + user profile)
  3.  Retrieve memories
  4.  Analyze intent
  5.  Generate plan
  6.  Execute tasks (skill calls per plan step)
  7.  Review output
  8.  Validate completeness
  9.  Persist memory
  10. Stream response

The router calls run(). It knows nothing about Gemini, skills, or memory.
That's the point.
"""

import logging
import json
import os
import re
import asyncio
import time
import httpx
from typing import AsyncGenerator
from datetime import datetime

from core.event_bus import EventBus, Events
from core.chat_context import ChatContext
from core.tool_loop import ToolCallLoop
from core.response_pipeline import ResponsePipeline
from core.skill_registry import SkillRegistry
from core.skill_executor import SkillExecutor
from core.capability_manifest import build_capability_manifest, format_manifest_for_prompt
from core.provider_router import ProviderRouter
from core.provider_manager import ProviderManager
from core.hallucination_firewall import EvidenceStore
from core.task_state import extract_task_state_for_turn, load_task_state, persist_task_state
from core.self_review import check_claims as _check_self_review_claims, review_before_reply
from core.planner import generate_plan, format_plan_for_prompt
from memory.manager import MemoryManager
from memory.context_compressor import compress_if_needed

logger = logging.getLogger("oadson.orchestrator")


class AIOrchestrator:

    def __init__(
        self,
        db,
        skill_registry: SkillRegistry,
        skill_executor: SkillExecutor,
        memory_manager: MemoryManager,
        provider_manager: ProviderManager,
        event_bus: EventBus,
    ) -> None:
        self.db = db
        self.skills = skill_registry
        self.executor = skill_executor
        # Skill router — narrows 72 skills to top-N candidates per query.
        # Loaded lazily from bootstrap registry so orchestrator doesn't need
        # to know about it at construction time.
        self._skill_router = None
        self.memory = memory_manager
        self.provider = provider_manager
        self.bus = event_bus
        # New multi-provider router (core/provider_router.py) — used when
        # Gemini's quota is exhausted mid-turn, to continue with real
        # tool-calling on a fallback provider instead of the old
        # self.provider.generate() bailout (plain text, no tools, no
        # vision — see the "Quota fallback" block below for what that
        # used to mean for the user).
        self.router = ProviderRouter()

    async def run(
        self,
        objective: str,
        user: dict,
        session_id: str,
        surface: str = "web",
        local_execution: bool = False,
        image_data: str | None = None,
        image_mime: str | None = None,
        extra_images: list[dict] | None = None,
        enable_reasoning: bool = False,
        stream_tokens: bool = True,
        attachment_url: str | None = None,
        attachment_type: str | None = None,
        attachment_filename: str | None = None,
        extra_attachment_urls: list | None = None,
        tool_results: list | None = None,
        resume_contents: list | None = None,
        preferred_model: str | None = None,
        call_mode: bool = False,
    ) -> dict:
        """
        Full synchronous execution. Returns final response dict.
        local_execution=True: CLI mode — return tool calls as JSON for CLI to execute.

        image_data: base64-encoded REAL image bytes (not a URL). If provided,
                    sent as actual inline image data to the vision model.
                    Never pass a URL here — that causes hallucinated
                    descriptions since the model can't fetch URLs itself.
                    This is EPHEMERAL — used only for this one Gemini call,
                    never persisted.
        image_mime: e.g. "image/jpeg", "image/png"
        extra_images: additional images beyond the primary image_data/
                    image_mime pair, each {"data": base64_str, "mime": str}.
                    Real fix for WhatsApp multi-image albums — Gemini's
                    contents API supports multiple inline_data parts in one
                    turn just fine; this pipeline simply never had a way to
                    pass more than one image through until now. Previously
                    a 3-image album only ever sent the first image, with the
                    other two silently dropped (just a text note asking the
                    model to mention they were skipped — it never actually
                    saw them).
        enable_reasoning: per-request opt-in to the "thinking" narration
                    step (see _plan_and_execute's thinking_enabled), on top
                    of the global ENABLE_THINKING env var.
        attachment_url/type/filename: a DURABLE, servable URL for media the
                    user sent (e.g. a WhatsApp image/voice note saved via
                    core.inline_storage). Unlike image_data, this gets
                    persisted on the user's message so it can be displayed
                    later — e.g. so a WhatsApp-originated session shows the
                    actual image/audio when viewed on the website, not just
                    the text. Previously there was no way for WhatsApp
                    media to ever become visible on the web UI at all.
        tool_results: FIX 2026-08-22 — accepted by ChatRequest for a while
                    but never actually reached here; local_execution
                    (terminal) surfaces build a synthetic continuation
                    message ("Here are the results of the tool calls you
                    requested:...") on every round-trip of their client-side
                    tool loop and send it as a fresh message/objective.
                    Without this flag that internal boilerplate was
                    indistinguishable from a genuine new user request and
                    was getting fed into task-state extraction as if it
                    were one — see the tool_results gate right before
                    extract_task_state_for_turn() below.
        preferred_model: user-selected model ID (web/app/pwa only — see
                    api/chat.py's ChatRequest.model docstring for the
                    surface gate). Tried once at the start of the turn's
                    tool loop; ANY failure falls through to the normal
                    decide_gemini → decide_fallback chain unchanged. None
                    for every other surface and every request that
                    doesn't set it.
        """
        user_id = str(user["_id"])
        # MIGRATION 2026-08-21 — was a plain dict literal. Now a real
        # typed ChatContext (see core/chat_context.py's module docstring
        # for the full rationale) — every existing ctx["x"]/ctx.get("x")/
        # ctx["x"]=y call site in this file keeps working completely
        # unchanged via ChatContext's dict-compatibility layer; this
        # swap alone is intentionally the ONLY thing that changed here.
        ctx = ChatContext(
            objective=objective,
            user_id=user_id,
            session_id=session_id,
            surface=surface,
            local_execution=local_execution,
            image_data=image_data,
            image_mime=image_mime,
            extra_images=extra_images or [],
            enable_reasoning=enable_reasoning,
            stream_tokens=stream_tokens,
            attachment_url=attachment_url,
            attachment_type=attachment_type,
            attachment_filename=attachment_filename,
            extra_attachment_urls=extra_attachment_urls or [],
            user=user,
            steps=[],      # execution trace visible to frontend
        )
        # Stored via ChatContext's .extra fallback (see chat_context.py) —
        # not yet common enough across surfaces to warrant a permanent
        # dataclass field, same convention as everything else routed
        # through __setitem__ for a not-yet-promoted field.
        ctx["tool_results"] = tool_results
        ctx["resume_contents"] = resume_contents
        # ADDED 2026-09-09 — read by core/tool_loop.py's USER-SELECTED
        # MODEL block. None for any surface that doesn't offer selection
        # (telegram/whatsapp/CLI never pass this) — that block is a
        # complete no-op in that case, so this line changes nothing for
        # those surfaces. See api/chat.py's ChatRequest.model docstring
        # for the actual surface gate.
        ctx["preferred_model"] = preferred_model
        # ADDED 2026-11-02 — mobile call-mode. Read by core/tool_loop.py
        # (short-answer instruction + single-round tool cap) and by the
        # step-limit block below. False for every existing surface.
        ctx["call_mode"] = bool(call_mode)
        # A picked paid model that can't run right now must say so, not fail silently.
        from core.model_policy import pause_notice as _pause_notice
        _notice = _pause_notice(preferred_model)
        if _notice:
            ctx["notice"] = _notice
            logger.warning(f"picked model paused: {preferred_model}")
        from core import approvals as _approvals
        _approve_note = _approvals.pop_notice(str(ctx.get("user_id") or ""))
        if _approve_note:
            ctx["notice"] = (str(ctx.get("notice") or "") + " " + _approve_note).strip()

        # BUGFIX 2026-09-09 — real turn-start timestamp, captured here,
        # before ANY actual work (provider calls, skill execution,
        # retries) begins. Distinct from response_pipeline.py's `now`
        # (persistence time, captured at the very END of the turn) —
        # previously BOTH the user message and the assistant message were
        # stamped with that same end-of-turn `now` (see TIMESTAMP FIX
        # 2026-08-20's comment in response_pipeline.py, which fixed
        # client-fabricated-vs-server-time drift but left this second
        # bug in place), so a turn that took 6 minutes of real skill/
        # provider-fallback work showed the user's message and OADSON's
        # reply with the EXACT SAME timestamp — correct for neither
        # message, since the user obviously sent theirs 6 minutes before
        # the reply existed. ctx["turn_started_at"] is read by
        # response_pipeline.py's _persist_memory for the user message's
        # "time" field specifically; the assistant message keeps using
        # the existing end-of-turn `now`, which is at least genuinely
        # LATER than turn_started_at now, instead of provably identical
        # to it on every single turn regardless of duration.
        ctx["turn_started_at"] = datetime.utcnow()

        await self.bus.emit(Events.CHAT_STARTED, {
            "user_id": user_id,
            "session_id": session_id,
            "objective": objective,
        })

        try:
            # Step 2: Build context
            ctx = await self._build_context(ctx)

            # Step 3: Retrieve memories
            ctx = await self._retrieve_memories(ctx)

            # Step 4-6: Agent loop (Gemini function calling handles intent + execution)
            ctx = await self._plan_and_execute(ctx)

            # NATIVE FUNCTION-CALLING 2026-08-23: a local_execution turn
            # that decided to call a local tool (see tool_loop.py's
            # LOCAL_TOOL_NAMES check) paused here with no final answer at
            # all yet — nothing to review, validate, or persist. Persisting
            # now would corrupt the session's stored history with a partial
            # turn; the full exchange (tool result + real final answer)
            # only gets persisted once the client sends the tool's result
            # back and the loop actually completes. Bypasses
            # review/validate/persist/finalize entirely rather than making
            # each of them individually aware of a pending state they were
            # never designed to see.
            if ctx.get("pending_tool_call"):
                # DEBUG LOGGING 2026-09-10 — companion to tool_loop.py's
                # matching TRACE lines around ask_user_tray/local-tool
                # pauses. Confirms this short-circuit was actually reached
                # and what it's returning, so a future "nothing arrived at
                # the frontend" report can be traced end-to-end from a
                # single Render log instead of stopping at tool_loop.py's
                # side with no visibility into whether orchestrator.run()
                # even got this far.
                logger.info(f"TRACE pending_tool_call short-circuit reached — session={ctx['session_id']} name={ctx['pending_tool_call'].get('name')}")

                # BUGFIX 2026-09-10 — REAL BUG, confirmed in production.
                # This short-circuit bypasses review/validate/persist/
                # finalize entirely (see the comment above this block) —
                # correct for a COMPLETED local-tool round-trip within an
                # already-existing session, but not for a session's VERY
                # FIRST turn. New sessions are never explicitly created via
                # POST /sessions by this client — the session document only
                # comes into existence via _persist_memory's upsert=True
                # (core/response_pipeline.py, ~line 402), which this
                # short-circuit skips completely. Confirmed via production
                # log: a first-turn session whose first model decision was
                # ask_user_tray paused here, the frontend correctly held a
                # session_id it had generated client-side, but GET
                # /sessions/{id} 404'd repeatedly because that id genuinely
                # didn't exist in MongoDB yet — it would only have been
                # created by the FULL _persist_memory path, which a paused
                # turn never reaches. Same gap existed for terminal's
                # local-tool pause on a brand-new session, silently, since
                # 2026-08-23 — just never surfaced there because CLI users
                # don't browse session history mid-command the way a web
                # user watching a tray does.
                #
                # Fix: a minimal, PURPOSE-BUILT upsert here — NOT a call
                # into the full _persist_memory (that method also handles
                # session-title generation, session_state, and a
                # fire-and-forget memory-extraction task, all of which
                # assume a COMPLETED turn with a real ctx["raw_reply"];
                # calling it early would either crash on missing fields or
                # do real, wasted work — e.g. generating a title from an
                # empty reply — for a turn that isn't actually done yet).
                # This only guarantees the session DOCUMENT exists and
                # holds the user's message, so GET /sessions/{id} succeeds
                # while the tray is pending. The real _persist_memory still
                # runs normally, in full, once the turn actually resumes
                # and completes — this is additive, not a replacement.
                _now = datetime.utcnow()
                await self.db["ai_sessions"].update_one(
                    {"session_id": ctx["session_id"], "user_id": ctx["user_id"]},
                    {
                        "$push": {"messages": {"role": "user", "content": ctx["objective"], "time": ctx.get("turn_started_at") or _now}},
                        "$set": {"updated_at": _now, "surface_origin": ctx.get("surface", "web")},
                        "$setOnInsert": {"created_at": _now, "name": f"Chat {_now.strftime('%b %d')}"},
                    },
                    upsert=True,
                )

                return {
                    "response": "",
                    "pending_tool_call": ctx["pending_tool_call"],
                    "contents_snapshot": ctx["contents_snapshot"],
                    "session_id": ctx["session_id"],
                    "steps": ctx.get("steps", []),
                    "model_used": ctx.get("model_used", "unknown"),
                }

            # Steps 7-10: Review, validate, persist, finalize — see
            # core/response_pipeline.py's ResponsePipeline for the full
            # rationale on this migration. One instance, same four calls
            # in the same order as always.
            pipeline = ResponsePipeline(self)

            # Step 7: Review
            ctx = await pipeline._review(ctx)

            # Step 8: Validate
            ctx = await pipeline._validate(ctx)

            # Step 9: Persist memory
            await pipeline._persist_memory(ctx)

            # PERSISTENCE 2026-08-31: save whatever TaskState this turn
            # ended with, so the NEXT turn's load_task_state (see the top
            # of _plan_and_execute) picks up where this one left off
            # instead of starting over. Same tier as the memory persist
            # right above — best-effort, never allowed to break the
            # actual response (persist_task_state degrades to a no-op on
            # any failure internally).
            #
            # DISABLED 2026-09-06: ctx.get("task_state") is now always
            # None (see the DISABLED block near the top of this method),
            # so this call always hits persist_task_state's own
            # `task_state is None -> return` guard and no-ops. Left in
            # place rather than removed — it's already inert and correct,
            # and removing it would just be one more place to touch if
            # task state tracking is ever restored.
            await persist_task_state(self.db, ctx.get("task_state"))

            # Passive, automatic style learning — no command needed, ever.
            # Fire-and-forget: never awaited, never allowed to slow down or
            # break the actual response. The threshold check inside means
            # this doesn't cost a Gemini call on every single turn — see
            # core/style_profile.py's AUTO_LEARN_MESSAGE_THRESHOLD.
            import asyncio as _asyncio
            from core.style_profile import maybe_learn_in_background
            _asyncio.create_task(maybe_learn_in_background(self.db, user_id, os.getenv("GEMINI_API_KEY", "")))

            # Step 10: Finalize response
            response = await pipeline._finalize(ctx)

            await self.bus.emit(Events.RESPONSE_READY, {
                "user_id": user_id,
                "session_id": session_id,
                "model_used": ctx.get("model_used", "unknown"),
            })

            return response

        except Exception as e:
            logger.error(f"Orchestrator error for user {user_id}: {e}", exc_info=True)
            return {
                "response": "Something went wrong. Try again.",
                "error": str(e),
                "steps": ctx.get("steps", []),
                "model_used": ctx.get("model_used", "unknown"),
                "skill_triggered": None,
            }

    # ── STEP 2: CONTEXT ──

    async def _build_context(self, ctx: ChatContext) -> dict:
        session_id = ctx["session_id"]
        user_id = ctx["user_id"]

        # Fetch session (message count no longer capped at fetch time —
        # context_compressor decides what's verbatim vs summarized below,
        # needs the real count to decide whether compression applies)
        session_doc = await self.db["ai_sessions"].find_one({
            "session_id": session_id,
            "user_id": user_id,
        })

        history = []
        context_summary = None
        if session_doc:
            # CONTEXT COMPRESSOR 2026-08-20: was `session_doc.get("messages", [])[-20:]`
            # — a hard cutoff, not compression. Anything before message -20
            # was simply invisible to the model, every turn, silently, no
            # matter how important it was. Sessions past 50 messages now get
            # the older portion folded into a rolling summary instead of
            # dropped outright; the recent window (still 20) stays fully
            # verbatim and unchanged from before. See memory/context_compressor.py
            # for the full rationale — this can only improve on the old
            # behavior, never regress it, since compression only ever
            # applies to what was already being dropped.
            raw, context_summary = await compress_if_needed(session_doc, self.db)
            for m in raw:
                history.append({
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                })

        ctx["history"] = history
        ctx["context_summary"] = context_summary
        ctx["session_doc"] = session_doc
        # Persist terminal working directory across calls within the same session.
        # Written back in _persist_memory when a terminal call returns new_cwd.
        ctx["session_state"] = (session_doc or {}).get("state", {})

        await self.bus.emit(Events.CONTEXT_BUILT, {
            "session_id": session_id,
            "history_len": len(history),
        })
        self._add_step(ctx, "context", f"Loaded {len(history)} messages from session.")
        return ctx

    # ── STEP 3: MEMORIES ──

    async def _retrieve_memories(self, ctx: ChatContext) -> dict:
        """
        Two-tier memory retrieval:

        Tier 1 — Core Memory (always-in-context)
          Fetched unconditionally, injected into every conversation.
          Like Claude's memory edits or ChatGPT's memory panel.
          Managed via memory_manager action='pin'/'unpin'/'save_core'.

        Tier 2 — Extended Memory (retrieved on demand)
          Full vault searched semantically via the Memory Reasoning Engine.
          Only injected when relevant to the current objective.
        """
        from memory.vault import MemoryVault
        from memory.reasoner import reason

        vault = MemoryVault(self.db, ctx["user_id"])
        api_key = os.getenv("GEMINI_API_KEY", "")
        objective = ctx.get("objective", "")

        # ── Tier 1: Core Memory ──
        try:
            summary_doc = await vault.get_core_summary()
            if summary_doc and summary_doc.get("content"):
                ctx["core_summary"] = vault.render_core_for_injection(summary_doc)
                ctx["core_memories"] = []   # not used when summary exists
            else:
                # No summary yet — fall back to old key-value list
                ctx["core_summary"] = None
                ctx["core_memories"] = await vault.get_core_memories(limit=30)
        except Exception as e:
            logger.warning(f"Core memory fetch failed: {e}")
            ctx["core_summary"] = None
            ctx["core_memories"] = []

        # ── Tier 2: Extended Memory via Reasoner ──
        try:
            memory_ctx = await reason(
                db=self.db,
                user_id=ctx["user_id"],
                objective=objective,
                api_key=api_key,
                skip_reflection_call=False,
            )
        except Exception as e:
            logger.warning(f"Memory reasoning failed, falling back: {e}")
            memories = await vault.search(objective, limit=5)
            ctx["memories"] = memories
            ctx["memory_context"] = None
            self._add_step(ctx, "memory", f"Core: {len(ctx['core_memories'])} | Extended fallback: {len(memories)}")
            return ctx

        ctx["memory_context"] = memory_ctx
        ctx["memories"] = memory_ctx.raw_memories

        summary = (
            f"Core: {len(ctx['core_memories'])} | "
            f"Extended: {len(memory_ctx.evidence)} evidence, "
            f"{memory_ctx.confidence}% confidence"
        )
        if memory_ctx.reasoning_skipped:
            summary = f"Core: {len(ctx['core_memories'])} | Extended: skipped"

        await self.bus.emit(Events.MEMORY_RETRIEVED, {
            "user_id": ctx["user_id"],
            "session_id": ctx["session_id"],
            "core_count": len(ctx["core_memories"]),
            "extended_count": len(memory_ctx.evidence),
            "confidence": memory_ctx.confidence,
        })
        self._add_step(ctx, "memory", summary)
        return ctx


    # ── STEP 4: INTENT ──

    async def _analyze_intent(self, ctx: ChatContext) -> dict:
        """
        Detect: which skill (if any), task complexity, surface-specific adjustments.
        """
        skill_names = self.skills.names()
        skill_list = ", ".join(skill_names) if skill_names else "none"

        surface = ctx.get("surface", "web")

        intent_prompt = f"""You are the intent analyzer for OADSON V3.

Available skills: {skill_list}

Surface: {surface}

PRIORITY RULES (apply in order):
1. If user mentions their code, a repo, a file path, architecture, or asks to read/search/fix code → skill: workspace
2. If user asks to generate a file, write code to a file, create a document → skill: file_generation
3. If user mentions a Google Drive file or asks to read a document from Drive → skill: google_drive (action: download)
4. If user mentions email, inbox, unread → skill: gmail
5. If user mentions calendar, event, schedule → skill: google_calendar
6. If user asks a web search, current events, lookup → skill: web_search
7. If user asks to remember something, recall something → skill: memory_manager
8. If task is pure conversation, explanation, or reasoning with no external data needed → skill: null

User objective: "{ctx['objective']}"

Respond ONLY with valid JSON:
{{
  "skill": "<skill_name or null>",
  "task_type": "simple | complex | hard",
  "skill_query": "<what to pass to the skill, or null>",
  "reasoning": "<one sentence>"
}}

If no skill is needed, set skill to null."""

        messages = [{"role": "user", "content": intent_prompt}]
        raw, model = await self.provider.generate(messages, task_type="simple", surface=surface)

        # Parse JSON — be defensive
        intent = self._parse_json_safe(raw, fallback={
            "skill": None,
            "task_type": "simple",
            "skill_query": None,
            "reasoning": "Intent analysis failed — proceeding directly.",
        })

        ctx["intent"] = intent
        ctx["task_type"] = intent.get("task_type", "simple")

        await self.bus.emit(Events.INTENT_ANALYZED, {
            "session_id": ctx["session_id"],
            "skill": intent.get("skill"),
            "task_type": ctx["task_type"],
        })
        self._add_step(ctx, "intent", f"Skill: {intent.get('skill') or 'none'} | Type: {ctx['task_type']}")
        return ctx

    # ── STEP 5 + 6: AGENT LOOP ──

    async def _build_function_declarations(self, objective: str = "") -> list:
        """
        Convert skill registry into Gemini functionDeclarations.
        When the skill router is ready, narrows to top-8 candidates
        semantically relevant to the objective — Gemini sees fewer tools,
        context is cleaner, routing is faster.
        Falls back to all skills if router isn't ready yet.
        """
        # Lazy-load the router from bootstrap registry
        if self._skill_router is None:
            try:
                import bootstrap
                self._skill_router = bootstrap.get("skill_router")
            except Exception:
                self._skill_router = None

        # Get candidate skills — routed subset or full list
        if self._skill_router and self._skill_router.is_ready and objective:
            skills = await self._skill_router.route(objective, top_n=8)
        else:
            skills = self.skills.all()

        declarations = []
        for skill in skills:
            manifest = skill.get("manifest", {})
            declarations.append({
                "name": skill["name"],
                "description": skill.get("description") or f"Run the {skill['name']} skill.",
                "parameters": manifest.get("parameters", {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Action to perform"},
                        "query": {"type": "string", "description": "Query or message for the skill"},
                    }
                })
            })
        return declarations

    async def _generate_thinking(
        self, url_base: str, api_key: str, model: str,
        system: str, contents: list, function_declarations: list, step_num: int,
    ) -> str | None:
        """
        CALL 1 of 2 per loop iteration — CALL 2 is the real function-calling
        decision in _plan_and_execute.

        Uses Gemini's actual thinkingConfig (thinkingBudget + includeThoughts)
        rather than a "summarize your reasoning in one sentence" prompt hack —
        the API returns real reasoning tokens as parts marked "thought": true,
        separate from the final answer part. Small fixed budget (256 tokens)
        keeps this cheap; it's narration, not the real decision.

        Failure here is silent and never blocks the main loop — this is UX
        narration for the `thinking` SSE event, not part of the actual
        decision path.
        """
        import httpx

        skill_names = [d["name"] for d in function_declarations] if function_declarations else []
        skill_list = ", ".join(skill_names) if skill_names else "no tools"

        # Real conversation so far (including any prior function calls/
        # responses in this round) so narration past step 1 reflects what
        # actually happened — "Gmail returned 12 unread, drafting a reply"
        # — rather than repeating the original request verbatim. This is a
        # separate list; it does not mutate `contents`, which the real
        # decision call still needs untouched.
        narration_contents = contents + [{
            "role": "user",
            "parts": [{"text": (
                "Briefly reason about what to do next, then in ONE short "
                "sentence (under 15 words) state it in present tense. "
                f"Tools available: {skill_list}."
            )}]
        }]

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": narration_contents,
            "generationConfig": {
                "maxOutputTokens": 150,
                "temperature": 0.4,
                "thinkingConfig": {"thinkingBudget": 256, "includeThoughts": True},
            },
        }
        url = f"{url_base}{model}:generateContent"

        # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
        # header — was leaking into Render's httpx request logs.
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            if res.status_code != 200:
                return None
            data = res.json()
            candidates = data.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])

            # Prefer the model's actual reasoning ("thought": true parts).
            # Thinking is "best effort" — the API doesn't always return one
            # even when requested — so fall back to the final answer text.
            thought_text = "".join(p.get("text", "") for p in parts if p.get("thought")).strip()
            answer_text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            text = (thought_text or answer_text).strip().strip('"').strip()
            return text[:200] if text else None
        except Exception as e:
            logger.debug(f"Thinking call failed (non-fatal, step {step_num}): {e}")
            return None

    async def _plan_and_execute(self, ctx: ChatContext) -> dict:
        import httpx

        local_execution = ctx.get("local_execution", False)
        surface = ctx.get("surface", "web")

        # Surface-aware step limits.
        # Terminal does real multi-step work — needs more runway.
        # WhatsApp/Telegram are short tasks — keep quota low.
        _step_limits = {
            # Raised to 40 for web/terminal/app specifically (per explicit
            # request) — NOT bumped blindly. Paired with
            # _accumulated_size_guard inside the tool loop (see
            # core/tool_loop.py's run()) so a higher step ceiling can't
            # translate into unbounded memory growth on a 512MB Render
            # instance — real OOM crashes were observed (Aug 21, twice)
            # before this change, so raising the step count without also
            # capping accumulated data size would make that risk worse,
            # not better. pwa intentionally left at its prior value —
            # not one of the three surfaces this request named.
            "terminal": 40,
            "web":      40,
            "app":      40,
            "pwa":      40,   # the website sends surface=pwa; kept equal to web/app
            "whatsapp": 15,
            "telegram": 5,
        }
        max_steps = _step_limits.get(surface, 7)
        # CALL MODE 2026-11-02 — a live voice call can't wait through a
        # 40-step agent loop. One round: answer, or make at most one tool
        # call and answer. Applied AFTER the surface lookup so it overrides
        # web/app/pwa's 40 for this request only.
        if ctx.get("call_mode"):
            max_steps = 1
        ctx["step_budget"] = max_steps  # make available to _build_system_prompt

        # ── Trustworthy Task State: requirement extraction ─────────────
        # MIGRATED 2026-08-21 — this was ~40 lines of inline logic here;
        # moved verbatim to core/task_state.py's extract_task_state_for_
        # turn() as part of breaking this function into smaller pieces.
        # Same is_task_shaped() gate, same heuristic extraction, same
        # non-blocking posture — see that function's docstring for the
        # full rationale (including the real production bug the gate
        # itself fixed) instead of duplicating it here.
        #
        # FIX 2026-08-22: gated on `not ctx.get("tool_results")`. Every
        # round-trip of local_execution's client-side tool loop (terminal
        # surface) sends a fresh HTTP request whose "objective" is really
        # an internally-synthesized "Here are the results of the tool
        # calls you requested: **Tool:** `read_file` ..." continuation
        # message, not a genuine new user request — but this call fires
        # unconditionally on every run(), so that boilerplate was being
        # extracted as if it were a fresh task, repeatedly, one bogus
        # TaskState per tool-loop round-trip, each with a garbage
        # requirement whose description was literally that internal
        # boilerplate text. tool_results is only ever set (by api/chat.py)
        # on exactly these continuation requests, so it's the correct,
        # DISABLED 2026-09-06 — direct user request to remove task state
        # tracking entirely, not to make it smarter. The completion gate
        # ("N requirement(s) not yet verified") kept firing in production
        # on requirements from an entirely different, older conversation
        # topic — confirmed repeatedly across real WhatsApp session logs,
        # including AFTER a staleness-based retirement fix was written
        # for extract_task_state_for_turn (that fix addressed one real
        # mechanism but evidently didn't resolve the user's actual
        # experience of it, or was never deployed — user asked to just
        # remove the check rather than debug further).
        #
        # ctx["task_state"] is now simply never set. Every downstream
        # consumer (core/tool_loop.py's completion gate, the
        # task_state_block system-prompt injection below, execution
        # recording, document_grounding's regression check) already
        # guards on `ctx.get("task_state")` being truthy and quietly
        # no-ops when it's absent — none of those files needed to change.
        # extract_task_state_for_turn/load_task_state/persist_task_state
        # in core/task_state.py are left intact and still importable
        # (nothing here deletes that module), simply no longer called
        # from the main turn path. Re-enable by restoring the two lines
        # commented out below if this is ever wanted back.
        #
        # existing_task_state = await load_task_state(self.db, ctx["session_id"], ctx["user_id"])
        # if not ctx.get("tool_results") and not ctx.get("resume_contents"):
        #     ctx["task_state"] = extract_task_state_for_turn(
        #         ctx["objective"], ctx["session_id"], ctx["user_id"], existing=existing_task_state,
        #     )
        # else:
        #     ctx["task_state"] = existing_task_state

        system = self._build_system_prompt(ctx)

        # Live Google access token — was previously a static dict lookup
        # against a field ("google_access_token") that nothing ever wrote,
        # so every Google skill always got None. This refreshes via the
        # stored refresh_token when the cached token is missing/expired.
        from core.google_oauth import get_valid_access_token
        google_token = await get_valid_access_token(ctx["user"], self.db)

        base_payload = {
            "message": ctx["objective"],
            "user_id": ctx["user_id"],
            "session_id": ctx["session_id"],
            "google_token": google_token,
            "github_token": os.getenv("GITHUB_TOKEN"),
            "surface": surface,
            # Was never injected — memory_manager, gmail, google_calendar,
            # google_drive, google_docs, google_tasks, google_keep,
            # reminder, note_taker, and system_status all read
            # payload["db"] directly and were failing on every call.
            "db": self.db,
            # Session-scoped state — currently used by terminal skill for
            # persistent working directory (terminal_cwd). Written back
            # to the DB in _persist_memory when a terminal call changes cwd.
            "session_state": ctx.get("session_state", {}),
        }

        # Build Gemini conversation contents
        # IMPORTANT: Gemini requires strictly alternating user/model roles
        # and the first message must be user. If the history slice starts
        # with an assistant message (can happen when session has odd number
        # of messages or the last-N slice lands on an assistant entry),
        # Gemini returns a role error and acts as if history is empty —
        # which looks like "forgot everything" to the user.
        contents = []
        raw_history = ctx.get("history", [])

        # Ensure history starts with a user message
        while raw_history and raw_history[0]["role"] != "user":
            raw_history = raw_history[1:]

        # CONTEXT COMPRESSOR 2026-08-20: if compress_if_needed produced a
        # rolling summary of everything older than the recent window,
        # fold it into the FIRST user message as a prefixed context note
        # rather than inventing a separate turn. A fabricated extra turn
        # would need its own role slot and risks breaking the strict
        # user/model alternation this loop already carefully guards
        # against (see the comment above it) — prefixing onto real
        # content sidesteps that entirely, no new role-alternation edge
        # case introduced. Only ever touches the FIRST message text sent
        # to the model, never what's stored or shown in the UI.
        context_summary = ctx.get("context_summary")
        _summary_prefixed = False

        # Build contents enforcing strict alternation — skip any duplicate
        # consecutive roles (shouldn't happen but guards against bad DB state)
        expected_role = "user"
        for m in raw_history:
            role = "user" if m["role"] == "user" else "model"
            if role != expected_role:
                continue  # skip out-of-order message
            text = m["content"] or "..."
            # ADDED 2026-08-15: fold step_exhaustion_detail back in here —
            # this is the other half of the step-exhaustion UX fix. The
            # user-visible "content" for that turn is now short and clean
            # (no raw args dumped in chat), but the model still needs the
            # full detail of what it already tried so a "continue" doesn't
            # repeat the same calls. This appends it to the model-visible
            # text ONLY (never touches what's shown in the UI, since the
            # UI renders "content" directly and never sees this field).
            if role == "model" and m.get("step_exhaustion_detail"):
                text = text + "\n\n" + m["step_exhaustion_detail"]
            if role == "user" and context_summary and not _summary_prefixed:
                text = (
                    f"[Context from earlier in this conversation, summarized:\n"
                    f"{context_summary}\n"
                    f"— end of summary. The following is the actual recent "
                    f"conversation, verbatim:]\n\n{text}"
                )
                _summary_prefixed = True
            contents.append({"role": role, "parts": [{"text": text}]})
            expected_role = "model" if expected_role == "user" else "user"

        # If we ended on a user turn, Gemini expects model next before
        # we append the new user message — insert a brief bridge
        if contents and contents[-1]["role"] == "user":
            contents.append({"role": "model", "parts": [{"text": "Understood."}]})

        # Final user turn — attach REAL image bytes here if present.
        # Never pass an image URL as text: Gemini can't fetch URLs and
        # will hallucinate plausible-sounding content instead of
        # admitting it has no image data.
        final_parts = [{"text": ctx["objective"]}]
        image_data = ctx.get("image_data")
        image_mime = ctx.get("image_mime")
        if image_data and image_mime:
            final_parts.append({
                "inline_data": {"mime_type": image_mime, "data": image_data}
            })
        # Real fix for WhatsApp multi-image albums — previously only the
        # single image_data/image_mime pair above ever reached Gemini; a
        # batch of 3 images sent the first and silently dropped the other
        # two (just a text note asking the model to mention they were
        # skipped, which it never actually saw). Gemini's contents API
        # takes multiple inline_data parts in one turn just fine.
        for extra in ctx.get("extra_images") or []:
            if extra.get("data") and extra.get("mime"):
                final_parts.append({
                    "inline_data": {"mime_type": extra["mime"], "data": extra["data"]}
                })
        contents.append({"role": "user", "parts": final_parts})

        # NATIVE FUNCTION-CALLING RESUME 2026-08-23: a local_execution
        # (terminal) turn that paused on a local tool call sends back
        # resume_contents — the FULL native Gemini contents snapshot it
        # was given when it paused, with the client's own functionResponse
        # appended after executing the tool locally. Overriding here
        # (rather than restructuring the ~85 lines above to skip
        # themselves) is deliberate: that history/image/attachment-
        # building logic has no side effects (verified — no DB writes,
        # no persistence, it only builds this local variable), so
        # letting it run and discarding the result for the rare resume
        # case is a real but acceptable inefficiency, not a correctness
        # risk — and it means this change can't possibly break any of
        # that existing, incident-driven logic, since none of it had to
        # be touched or re-flowed to make room for this.
        if ctx.get("resume_contents"):
            contents = ctx["resume_contents"]

        function_declarations = await self._build_function_declarations()
        # ADDED 2026-09-09 — ask_user_tray offered on EVERY surface,
        # unconditionally, unlike LOCAL_TOOL_DECLARATIONS right below
        # (which is gated behind local_execution/terminal specifically).
        # See core/interactive_tool_schemas.py's module docstring for
        # why these are deliberately separate sets despite the visually
        # similar merge pattern here.
        from core.interactive_tool_schemas import INTERACTIVE_TOOL_DECLARATIONS
        function_declarations = list(function_declarations) + INTERACTIVE_TOOL_DECLARATIONS
        # NATIVE FUNCTION-CALLING 2026-08-23: local_execution surfaces
        # (terminal) get the local-tool schemas merged in too — Gemini
        # can now offer read_file/run_bash/etc. as genuine, API-enforced
        # options alongside real server skills, rather than only prose-
        # describing them and hoping the model writes parseable JSON.
        # See core/local_tool_schemas.py's module docstring for the full
        # rationale and core/tool_loop.py's LOCAL_TOOL_NAMES check for
        # where a decided local-tool call gets handed back to the client
        # instead of executed here.
        if local_execution:
            from core.local_tool_schemas import LOCAL_TOOL_DECLARATIONS
            function_declarations = list(function_declarations) + LOCAL_TOOL_DECLARATIONS

        skill_name = None
        skill_output = None
        previous_result = None
        tool_call_count = 0
        # MIGRATION 2026-08-21 — the download-link-loss fix (originally a
        # hand-rolled `last_download_result` local variable tracked
        # alongside skill_output — see git history) now lives as a real
        # part of ChatContext itself: ctx.note_skill_result(name, result)
        # updates skill_name/skill_output exactly as the bare assignments
        # below always did, AND separately remembers the result in
        # ctx.download_result specifically when it has a download_url —
        # so a later skill call in the same turn that doesn't produce a
        # file can't silently erase the one that did. See
        # core/chat_context.py's module docstring for the full incident
        # this prevents; ctx.best_download_result() (used at both exit
        # points below) is what actually reads it back out.
        evidence = EvidenceStore()  # tracks every tool receipt for hallucination review

        gemini_url_base = "https://generativelanguage.googleapis.com/v1beta/models/"
        api_key = os.getenv("GEMINI_API_KEY", "")

        active_model = "gemini-3.5-flash-lite"
        # "Thinking" narration was removed entirely — it doubled Gemini
        # calls per loop iteration for a feature that wasn't wanted. Real
        # execution visibility (which skill ran, its args, its result) is
        # untouched — Events.TASK_STARTED/TOOL_EXECUTED below fire
        # unconditionally regardless of this, always did. This was purely
        # extra narration text on top of that, now gone. The
        # request-level "enable_reasoning" flag still exists (chat.html's
        # 💭 toggle) but now only controls how the FRONTEND displays
        # events as they stream in — it no longer triggers any extra
        # backend LLM call.

        # ── PLANNER (separate from the execution loop below) ──
        # One call, before any tool is actually called, producing a short
        # plan the decision loop can see on every iteration instead of
        # deciding blind at each step. OFF by default — another real
        # quota cost on a free-tier key, opt in via ENABLE_PLANNER=true.
        planner_enabled = os.getenv("ENABLE_PLANNER", "false").lower() == "true"
        if planner_enabled:
            skill_names_for_plan = [d["name"] for d in function_declarations] if function_declarations else []
            plan = await generate_plan(
                gemini_url_base, api_key, active_model, ctx["objective"], skill_names_for_plan,
            )
            if plan:
                ctx["plan"] = plan
                system = f"{system}{format_plan_for_prompt(plan)}"
                self._add_step(ctx, "plan", f"Plan: {plan.get('final_goal', 'n/a')} ({len(plan.get('steps', []))} step(s))")
                await self.bus.emit(Events.PLAN_GENERATED, {
                    "session_id": ctx["session_id"],
                    "model": active_model,
                    "plan": plan,
                })

        # Multi-provider fallback state — see core/provider_router.py.
        # Starts on Gemini every turn; only switches away if Gemini's
        # quota is actually hit mid-turn, and stays on that fallback
        # provider for the rest of THIS turn once switched (not
        # re-tried against Gemini again until the next user message).
        active_protocol = "gemini"
        openai_messages = None  # populated only after a fallback switch
        openai_call_counter = 0  # dedicated counter for tool_call_id — see note above on why this can't reuse tool_call_count

        loop = ToolCallLoop(
            orchestrator=self,
            ctx=ctx,
            system=system,
            contents=contents,
            function_declarations=function_declarations,
            base_payload=base_payload,
            evidence=evidence,
            max_steps=max_steps,
            surface=surface,
            local_execution=local_execution,
            gemini_url_base=gemini_url_base,
            api_key=api_key,
            active_model=active_model,
            active_protocol=active_protocol,
            openai_messages=openai_messages,
            openai_call_counter=openai_call_counter,
            tool_call_count=tool_call_count,
            skill_name=skill_name,
            skill_output=skill_output,
            previous_result=previous_result,
        )
        return await loop.run()

    # ── STEP 7: REVIEW ──

    # ── STEPS 7-10: REVIEW, VALIDATE, PERSIST, FINALIZE ──
    # MIGRATED 2026-08-21 to core/response_pipeline.py's ResponsePipeline
    # — see that file's module docstring for the full rationale. Same
    # four calls in the same order as always; the only change is they
    # now go through one ResponsePipeline instance instead of being four
    # separate bare methods on this class.

    # ── HELPERS ──

    def _build_system_prompt(self, ctx: ChatContext) -> str:
        surface = ctx.get("surface", "web")
        memories = ctx.get("memories", [])

        # Real capability manifest — see core/capability_manifest.py for
        # why this exists: the model would occasionally fall back to a
        # pretrained assumption about what it can/can't do ("I cannot
        # process audio") on a turn even when the exact same capability
        # had worked correctly the day before, because nothing in the
        # prompt actually told it what's available THIS turn — it was
        # inferring from memory of past turns, which is unreliable.
        capability_block = format_manifest_for_prompt(
            build_capability_manifest(surface, self.skills)
        )

        # Hard guard: never describe or invent image content unless real
        # image bytes are actually attached to this turn. If the user
        # references "the image" or "this photo" but none is attached,
        # say so plainly — do not speculate or fabricate.
        has_real_image = bool(ctx.get("image_data"))
        image_guard = (
            "\n\nIMAGE POLICY: You have NOT been given any image in this turn "
            "unless explicitly told otherwise below. If the user references an "
            "image, photo, or screenshot and none is attached, say plainly that "
            "you don't have an image to look at right now — never invent or "
            "guess what an image might contain."
        )
        if has_real_image:
            image_guard = (
                "\n\nIMAGE POLICY: A real image IS attached to this message. "
                "Describe only what is actually visible in it. If any part is "
                "blurry, cut off, or unclear, say so explicitly instead of "
                "guessing or filling in gaps."
            )

        # ── Tier 1: Core Memory Block (always injected) ──────────────────────
        # Summary paragraph if built, key-value list as legacy fallback.
        # Shown unconditionally on every turn, like Claude's memory panel.
        core_summary = ctx.get("core_summary")
        core_memories = ctx.get("core_memories", [])
        core_block = ""
        if core_summary:
            core_block = (
                f"\n\nCORE MEMORY (always available — what you know about this user):\n"
                f"{core_summary}\n"
                "This is a verified summary of user-provided facts. "
                "Use it directly without calling memory tools. "
                "CRITICAL: memory is background context from PAST conversations, not a live "
                "check you just ran in THIS one. Never phrase a remembered fact as something "
                "you 'found', 'checked', or that is 'currently' true right now — a remembered "
                "project path, setup detail, or status is what the user told you before, not a "
                "live tool result. If a turn blends a genuine live check (e.g. a skill you just "
                "called) with something you recall from memory, say which is which — don't merge "
                "them into one undifferentiated block of 'current findings'."
            )
        elif core_memories:
            core_lines = "\n".join(
                f"- {m.get('key', '?')}: {m.get('content', '')}"
                for m in core_memories
            )
            core_block = (
                f"\n\nCORE MEMORY (always available — pinned facts about the user):\n"
                f"{core_lines}\n"
                "These are verified user-provided facts. Use them directly without calling "
                "memory tools. CRITICAL: memory is background context from PAST conversations, "
                "not a live check you just ran in THIS one. Never phrase a remembered fact as "
                "something you 'found', 'checked', or that is 'currently' true right now. If a "
                "turn blends a genuine live check with something you recall from memory, say "
                "which is which — don't merge them into one undifferentiated block."
            )

        # ── Tier 1b: Task State Block (always injected — the actual fix ────
        # for "OADSON forgot the project" long-session drift) ──────────────
        # ADDED 2026-08-31: TaskState (core/task_state.py) already tracked
        # requirements, statuses, and execution history with real rigor —
        # but nothing ever showed the model that record. It was used only
        # for the system's OWN verification/gating logic, invisible to the
        # model itself, which had only raw scrollback to reconstruct "what
        # have we already decided" from. Confirmed as the real cause of a
        # ~2hr session producing two contradictory admin-dashboard UI
        # states — the model had no persistent structural memory of its
        # own prior decisions, only whatever fit in the recent message
        # window. This is the same always-on injection tier as core memory
        # above, just for STRUCTURED task/build state instead of free-form
        # user facts — same "not a live check" caution applies for the
        # same reason: a requirement marked COMPLETED two turns ago is
        # something already established, not something just verified this
        # instant.
        task_state_block = ""
        task_state = ctx.get("task_state")
        if task_state and task_state.requirements:
            task_state_block = (
                f"\n\nCURRENT TASK STATE (always available — what THIS session has "
                f"already decided/built/broken, carried forward from earlier in this "
                f"same conversation):\n{task_state.status_summary()}\n"
                "This reflects real decisions and outcomes already established earlier "
                "in this conversation — treat completed/in-progress items as settled "
                "fact, don't silently re-derive or contradict them from scratch. If the "
                "user's current message conflicts with something shown here, that's a "
                "genuine change of direction to flag explicitly, not something to quietly "
                "overwrite. REGRESSED items need real repair, not another fresh attempt "
                "at the same broken approach that regressed them."
            )

        # ── Tier 2: Extended Memory Block (retrieved on demand) ───────────────
        memory_block = ""
        memory_ctx = ctx.get("memory_context")
        if memory_ctx is not None:
            memory_block = memory_ctx.to_prompt_block()
        elif memories:
            mem_lines = "\n".join(f"- {m.get('key', 'note')}: {m.get('content', '')}" for m in memories)
            memory_block = (
                f"\n\nWhat you know about the user (labeled facts — the label is "
                f"what the fact IS, not a hint to reuse the value elsewhere):\n{mem_lines}\n"
                "Only use one of these values as a tool argument if it's actually what that "
                "argument is asking for (e.g. don't use a favorite_color value as a username, "
                "an os value as a repo name, etc. just because it's short and available). "
                "CRITICAL: these are things the user told you in past conversations, not a live "
                "check you just ran. Never phrase one of these as something you 'found' or "
                "'checked' just now, or as 'currently' true — say it's something they mentioned "
                "before, and keep it clearly separate from any real tool result this turn."
            )

        # Repeated real failure mode: asked to use the github skill, the
        # model has guessed wrong owner/repo names ("oadson_core" under
        # "Oadson_v3") that don't exist, even in the same conversation
        # where it had already used the correct ones successfully. Give
        # it one stable, authoritative answer instead of making it
        # re-derive this from memory/conversation history every time.
        repo_hint = ""
        default_owner = os.getenv("SKILL_CREATOR_GITHUB_OWNER")
        default_repo = os.getenv("SKILL_CREATOR_GITHUB_REPO")
        if default_owner and default_repo:
            repo_hint = (
                f"\n\nUnless told otherwise, the user's primary/default GitHub repo is "
                f"owner='{default_owner}', repo='{default_repo}' — this is OADSON's own "
                f"codebase. Use these exact values for github skill calls about "
                f"'my repo', 'the codebase', 'my skills', etc. Never guess a different "
                f"owner/repo name from memory."
            )

        # GITHUB REPO RULE — repos must come in through the github skill so
        # they get indexed. A shell `git clone` is refused by the terminal
        # skill for exactly this reason (see skills/terminal/skill.py).
        repo_rules = (
            "\n\n<github_repos>\n"
            "To work on a GitHub repo, call the github skill with action='clone_to_workspace' "
            "(owner, repo, and ref if needed). It downloads the repo into your workspace and "
            "indexes it, so you can retrieve code from it later. A shell git clone skips that "
            "index, which is why the terminal refuses GitHub clone URLs. After cloning, work from "
            "the workspace path the result reports, using relative paths such as repos/owner__repo/.\n\n"
            "If the user names a repo that is already indexed, use it without cloning again. "
            "Clone again only when they ask for a newer version.\n\n"
            "The user has granted you access to commit and push to their repos through the github "
            "skill. When they ask for a change, commit, or push, carry it out in the same turn: edit "
            "in the workspace with terminal, run the check they asked for, then call github with "
            "action='push_workspace'. You don't need to ask again for something they have already "
            "requested. Ask first only when the request is ambiguous, or when the change would delete "
            "data or force-push.\n"
            "</github_repos>"
        )

        skill_block = ""
        if ctx.get("skill_output"):
            skill_status = ctx["skill_output"].get("status")
            skill_data = ctx["skill_output"].get("data") or ctx["skill_output"]
            skill_name_for_prompt = ctx.get("skill_name", "the skill")

            download_url = skill_data.get("download_url") if isinstance(skill_data, dict) else None
            if skill_status == "success" and download_url:
                if surface in ("whatsapp", "telegram"):
                    skill_block = f"\n\nFile ready. Download link: {download_url}"
                elif surface == "terminal":
                    skill_block = f"\n\nFile generated. Use write_file tool or download: {download_url}"
                else:
                    skill_block = f"\n\nFile ready: [{skill_data.get('filename', 'Download')}]({download_url})"
            elif skill_status == "pending_confirmation":
                # Was: raw json.dumps(skill_data) pasted into the prompt
                # under a bare "Skill result:" label, with no instruction
                # on how to present it. The model — especially the lite
                # model this surface actually runs — was just echoing that
                # JSON straight into its reply text verbatim (the literal
                # "SKILL EXECUTION RESULT: {...}" text users were seeing).
                # Now: describe the situation in plain language and tell
                # it explicitly not to paste the object.
                confirm_msg = skill_data.get("message", "") if isinstance(skill_data, dict) else ""
                skill_block = (
                    f"\n\n{skill_name_for_prompt} needs confirmation before proceeding: {confirm_msg} "
                    "Describe this to the user in your own plain words and ask them to confirm — "
                    "do NOT paste the raw tool result object, its field names, or any JSON into your reply."
                )
            elif skill_status == "success":
                # No download_url — still avoid raw JSON. Give the model
                # the plain-text summary the skill itself already
                # produced (result.message), not the structured object.
                summary = skill_data.get("message", "") if isinstance(skill_data, dict) else str(skill_data)
                skill_block = (
                    f"\n\n{skill_name_for_prompt} completed: {summary} "
                    "Summarize this outcome in plain language — do NOT paste the raw result object or JSON."
                )
            elif skill_status == "error":
                reason = skill_data.get("reason") or skill_data.get("error") or "unknown error" \
                    if isinstance(skill_data, dict) else "unknown error"
                skill_block = (
                    f"\n\n⚠️ SKILL FAILURE — {skill_name_for_prompt} returned an error: {reason}\n"
                    "MANDATORY RULES when a skill fails:\n"
                    "1. Tell the user the skill failed and what the error was.\n"
                    "2. DO NOT answer the user's question from memory, context, or inference.\n"
                    "3. DO NOT present guessed or recalled information as if the skill returned it.\n"
                    "4. If you don't have the actual tool result, say so explicitly.\n"
                    "5. Offer to retry or suggest an alternative approach.\n"
                    "Violating these rules is hallucination. The user will catch it."
                )

        # Applies to every skill_block case above and to any other tool
        # output the model saw via the actual functionResponse turn (the
        # normal Gemini function-calling protocol, separate from this
        # prompt) — it's easy for a model to just relay a structured
        # object back verbatim instead of writing a real sentence about
        # it, especially the lite/fast models this surface runs.
        presentation_guard = (
            "\n\n<presentation>\n"
            "Write your reply as plain sentences about what happened and what you need next. "
            "Tool outputs and raw JSON already appear in the user's execution view, so repeating "
            "them in your reply adds noise. Describe results by their meaning ('the build passed', "
            "'the file has 13 entries') rather than pasting status fields or argument objects.\n"
            "</presentation>"
        )

        # WhatsApp/Telegram have no LaTeX renderer, no MathJax, no markdown engine.
        # Special math symbols (∑, ∫, √, ², ×, ÷, π etc.) render as raw Unicode
        # which looks fine, but LaTeX notation ($x^2$, \frac{}{}, \sqrt{}, $$...$$)
        # shows up as literal dollar signs and backslashes — terrible UX.
        if surface in ("whatsapp", "telegram"):
            presentation_guard += (
                "\n\nFORMATTING RULES FOR THIS SURFACE (WhatsApp/Telegram):\n"
                "- NO LaTeX notation. Never use $...$ or $$...$$ or \\frac or \\sqrt "
                "or any LaTeX command. There is no renderer — it shows as raw text.\n"
                "- For math: write it in plain English or use simple Unicode symbols "
                "that display correctly everywhere: ² ³ √ × ÷ π ≈ ≠ ≤ ≥ ∞ Σ\n"
                "- Example WRONG: $\\frac{a}{b}$ or $$E = mc^2$$\n"
                "- Example RIGHT: a/b or E = mc²\n"
                "- NO markdown headers (# ## ###) — WhatsApp ignores them.\n"
                "- NO triple backtick code blocks for large chunks — use plain indented text.\n"
                "- Keep replies concise. WhatsApp is a messaging surface, not a document viewer.\n"
                "- Bold (*word*) and italic (_word_) work on WhatsApp — use sparingly."
            )

        # ADDED 2026-09-08: terminal had NO formatting guard at all before
        # this — it silently fell through to whatever the default (web-
        # oriented) assumptions were, meaning it was trying to render
        # LaTeX, markdown headers, and quote blocks with literally zero
        # rendering engine underneath it (a raw shell renders less than
        # WhatsApp does — WhatsApp at least does *bold*/_italic_; a plain
        # terminal doesn't even do that, those show as literal asterisks
        # and underscores). Separate branch from whatsapp/telegram above
        # rather than folding terminal into that condition, since the
        # WhatsApp-specific bold/italic guidance is actively wrong advice
        # for a terminal.
        elif surface == "terminal":
            presentation_guard += (
                "\n\nFORMATTING RULES FOR THIS SURFACE (terminal/CLI):\n"
                "- NO LaTeX notation. Never use $...$ or $$...$$ or \\frac or \\sqrt "
                "or any LaTeX command. There is no renderer — it shows as raw text.\n"
                "- For math: write it in plain English or use simple Unicode symbols: "
                "² ³ √ × ÷ π ≈ ≠ ≤ ≥ ∞ Σ\n"
                "- NO markdown headers (# ## ###) — a terminal shows the literal "
                "hash characters, not a heading.\n"
                "- NO bold/italic markup (*word*, _word_, **word**) — a raw terminal "
                "shows the literal asterisks/underscores, it does not render styling.\n"
                "- Code blocks (triple backticks) are fine — they're readable as plain "
                "text even without syntax highlighting.\n"
                "- Quote blocks (> text) are fine — the '>' prefix reads naturally as "
                "a quotation in plain text, unlike headers or bold markup.\n"
                "- Keep replies plain and readable as raw monospaced text top to bottom."
            )

        # TRACE MODE 2026-08-20: web/app + reasoning-on get permission to
        # speak mid-loop, between tool calls — NOT a separate narration
        # voice or template, just an added instruction on top of whatever
        # personality block was already selected above (surface_personalities),
        # so the interstitial talk sounds like OADSON, not a status bot.
        # Gated identically to the emission site in _plan_and_execute
        # (surface in web/app AND ctx.get("enable_reasoning")) — if either
        # condition is false there, this instruction is simply never acted
        # on, since decision.get("text") alongside a tool call is never
        # read into an emitted step on any other surface.
        if surface in ("web", "app") and ctx.get("enable_reasoning"):
            presentation_guard += (
                "\n\nTRACE MODE — this surface shows your work as you do it, live. "
                "You may write brief text before or after a tool call, in your own "
                "voice — explaining what you're about to check, reacting honestly to "
                "what a tool returned (including catching your own mistakes and "
                "correcting course), or noting what you learned before continuing. "
                "This is not a fixed narration format — some turns need this, some "
                "don't. A simple one-shot lookup usually needs none. A multi-step "
                "task benefits from it. Use your judgment, same as you would deciding "
                "whether to explain yourself in an ordinary reply. Keep it genuine and "
                "brief — a sentence or two, not a paragraph — and never fabricate "
                "progress or narrate an action you have not actually taken yet."
            )
            # TEMP TEST INSTRUCTION 2026-08-20 — REMOVE ONCE TRACE MODE IS
            # CONFIRMED WORKING. Diagnostic only: the discretionary
            # instruction above explicitly tells the model it's allowed to
            # skip narration for simple calls ("usually needs none") — so
            # a test that hits exactly that case (a single one-shot skill
            # call) can't distinguish "model is incapable of this" from
            # "model correctly used the permission we gave it to opt out".
            # This block removes all discretion and makes it mandatory on
            # every single tool call, no exceptions, specifically so a
            # still-empty result under a hard mandate is real evidence of
            # a model-side limitation rather than the prompt working as
            # designed. Search "TEMP TEST INSTRUCTION" to find/remove this
            # block once you've got your answer either way.
            presentation_guard += (
                "\n\nTEMP TEST OVERRIDE: for this test, the above discretion is "
                "suspended. On EVERY tool call you make in this turn, you MUST "
                "write at least one sentence of text alongside the function call — "
                "no exceptions, even for a single simple lookup. State what you're "
                "about to do before calling the tool."
            )

        now = datetime.utcnow()
        time_block = (
            f"\n\nCurrent date/time: {now.strftime('%A, %B %d, %Y, %H:%M UTC')}. "
            "This is the actual current date/time, freshly checked for this exact message — "
            "not cached, not guessed. If any earlier message in this conversation stated a "
            "different date (even one you stated yourself), that was correct for that moment "
            "only — real time has kept moving since then, possibly by hours, days, or weeks. "
            "Always trust THIS date/time over anything said earlier in the conversation."
        )

        surface_personalities = {
            "web":       "You are OADSON — the owner's personal AI. Conversational, detailed, context-aware. Speak like a trusted advisor.",
            "whatsapp":  "You are OADSON — the owner's personal AI. Short replies, texting feel. Cut the noise. Get to the point.",
            "terminal":  (
                "You are OADSON operating as an autonomous software engineer, not an assistant "
                "describing what could be built. You own these LOCAL tools, executed directly on "
                "the user's own machine: run_bash, read_file, write_file, create_file, delete_file, "
                "list_dir, search_files, git_status, git_diff, http_request, syntax_check, "
                "test_endpoint, run_tests. Assume they work. Use them. "
                "Never apologize or hedge before attempting execution — only report a limitation "
                "after a tool call has actually failed, not in anticipation of it maybe failing.\n"
                "You ALSO have full access to every server-side skill below.\n"
                # FIX 2026-08-24: was "...call them the exact same way, by
                # their registered name, e.g. {\"tool\": \"web_search\",
                # \"args\": {...}}. These execute on the backend..." and
                # ended with "Emit one tool call per JSON code-fenced
                # block, one tool per turn — {\"tool\": \"<name>\", \"args\":
                # {...}}." — instructions for the OLD architecture, where
                # local_execution disabled real function-calling entirely
                # and the model had to fake tool calls by writing JSON
                # into its reply text. That's no longer true (see
                # core/tool_loop.py's tools_for_call — now includes real
                # declarations for all of this, local tools merged with
                # core/local_tool_schemas.py), but this text was never
                # updated when that changed. Directly, precisely
                # confirmed as the actual cause of a real report: raw
                # ```json {"tool":...}``` blocks were still appearing as
                # plain reply text with zero execution attempted, on a
                # backend that DID have the new code deployed — because
                # this prompt was still explicitly telling the model to
                # write that exact format, and it followed the explicit
                # instruction over the tool schema quietly made available
                # alongside it. Native function-calling needs no format
                # instruction AT ALL — it's automatic once tools are
                # provided in the API call; an explicit competing format
                # instruction is actively counterproductive now, not
                # neutral filler.
                "Use your normal tool-calling ability for all of it, local and server-side alike — "
                "no special format, no JSON block, nothing to write into your reply. Just call the "
                "tool.\n"
                f"Full list of server-side skill names you can call: {', '.join(self.skills.names())}.\n"
                "EXCEPTION: never call a skill literally named \"terminal\" — that runs on the "
                "SERVER's filesystem, not the user's machine, and would silently execute in the "
                "wrong place. Use run_bash (a LOCAL tool above) for every shell/file-system need "
                "instead — it does the same job, correctly, on the user's actual machine.\n"
                "When asked to build something: build it. Write the real files, not a description "
                "of what the files would contain. Don't downgrade a build request into an "
                "architecture outline, a plan, or a 'here's how you'd approach this' unless "
                "explicitly asked for a plan instead of the thing itself. Finish what you start "
                "when it's within reach in this turn rather than stopping at a partial scaffold "
                "and describing the rest.\n"
                "No personality, no fluff, developer output only."
            ),
            "pwa":       "Professional, clean, full feature access. Balanced.",
            "app":       "Conversational, detailed, markdown-ready. Same as web.",
        }

        # ── OADSON IDENTITY — static, model-change-proof ──────────────────
        # Added 2026-08-10. The two-sentence surface_personalities above are
        # correct as surface hints but don't anchor WHO OADSON is across
        # model updates, quota failovers to a different provider, or core
        # summary regenerations. This block never changes, never gets
        # regenerated by Gemini, never drifts — it's the fixed character
        # spec that behavioral_rules in the core summary builds ON TOP OF,
        # not a replacement for it.
        #
        # What's here is what was *already true* about how OADSON behaves
        # (confirmed by the owner, observed over months of real use) —
        # written down explicitly so it survives intact rather than being
        # re-derived from Gemini's defaults on each turn.
        identity_block = (
            "\n\nWHO YOU ARE:\n"
            "You are OADSON — the owner's personal AI, built by him from scratch over "
            "two months on an Android phone. You are not a generic assistant. You are specifically "
            "his, built around its owner's life, work, and goals.\n\n"

            "YOUR CHARACTER (these do not change regardless of which model is running):\n"
            "- You have an engineering mindset. When something is broken, you find the root cause — "
            "not the surface symptom. You don't paper over bugs, you trace them. When you say "
            "something is fixed, it is actually fixed, and you can explain exactly why.\n"
            "- You are direct. You say what you actually think, not what sounds agreeable. "
            "If the owner is wrong about something technical, you say so clearly — once, without "
            "lecturing. You push back when it matters and let it go when it doesn't.\n"
            "- You are honest about uncertainty. If you don't know something or can't verify it "
            "from a real tool result this turn, you say so plainly. You don't fill gaps with "
            "confident-sounding guesses. You don't get more confident under pressure without "
            "new real information.\n"
            "- You have awareness of your own limits. You know what's achievable on free-tier "
            "infrastructure and what isn't. You don't promise things that would require paid "
            "APIs, dedicated servers, or capabilities outside what's actually configured.\n"
            "- You remember context. You know OADSON's full stack, its history, what was "
            "fixed and why. You don't ask questions you can read the answer to.\n"
            "- You are not sycophantic. You don't celebrate every message or add filler praise. "
            "When something genuinely works well, you say so specifically. When it doesn't, you "
            "say that instead.\n\n"

            "WHAT YOU ARE BUILT FOR:\n"
            "the owner is a cybersecurity student (formally) and a backend developer (in practice). "
            "He built OADSON to be a real presence in his daily workflow — WhatsApp, web, terminal. "
            "He is not 18 yet. He built this system entirely from a phone. These facts shape "
            "what's impressive, what's realistic, and what support actually looks like.\n"
            "You are his day-to-day technical partner, not a search engine and not a tutor. "
            "Treat him as the engineer he is."
        )

        base = surface_personalities.get(surface, surface_personalities["web"])

        # Applies regardless of surface — the agentic "build instead of
        # explain" prompt above (and general model tendency toward being
        # helpful) can tip into inventing its own unsolicited next steps
        # or agenda items, especially around changing OADSON's own code.
        # That's scope creep the model decided on its own, not something
        # asked for — shut it down explicitly rather than hope it doesn't
        # happen.
        scope_constraint = (
            "\n\nStay scoped to exactly what was asked. Do not propose your own "
            "unsolicited next steps, projects, features, or architectural changes "
            "— especially changes to OADSON's own core code (orchestrator, skills, "
            "database schema). Answer or do what was requested, then stop. If "
            "something genuinely seems worth mentioning, a brief one-line note is "
            "fine, but never frame it as an already-decided next action or ask "
            "'shall we proceed' about something nobody proposed."
        )

        # Confirmed real incident: asked why a skill was failing, the
        # model invented a specific, technically-plausible root cause
        # ("authentication sync issue", a real-but-unrelated file path
        # borrowed from a different skill's actual sandbox) — and got
        # MORE specific and confident each time it was pressed for
        # detail, instead of admitting it had no real diagnostic
        # information. That pattern — inventing precise technical
        # specifics to sound credible under pressure — is worse than a
        # wrong guess, because it actively resembles genuine
        # introspection. Shut it down explicitly, the same way the image
        # guard shuts down invented image descriptions.
        honesty_guard = (
            "\n\nNEVER invent specific technical diagnostic details — error causes, "
            "file paths, module names, or any specific-sounding explanation — unless "
            "that exact detail came from a real tool result this turn.\n\n"
            "CRITICAL ANTI-HALLUCINATION RULES:\n"
            "- If a skill FAILED (status=error/timeout/blocked), you do NOT have the "
            "answer. Do not fill it in from conversation history or inference. "
            "Say the skill failed and offer to retry or use a different approach.\n"
            "- If memory search fails, the content is UNKNOWN this turn. Do not "
            "recall it from earlier messages and present it as if the search returned it. "
            "The transcript is visible — the user will catch this immediately.\n"
            "- If a skill returns empty results, say it returned empty. Never invent "
            "plausible-sounding results to fill the gap.\n"
            "- NEVER say 'I can see X in memory' or 'the database shows Y' unless a "
            "tool call THIS turn actually returned X or Y.\n"
            "- When pressed for specifics you don't have, repeat the honest answer. "
            "Do not become more confident under pressure without new real information.\n\n"
            "NEVER save a name, school, employer, or other identity fact to memory "
            "based on content you read INSIDE a document, file, or attachment. "
            "Only save facts the user explicitly states about themselves."
)

        # Step budget awareness - the single biggest source of wasted steps
        # is the model treating each thought as a separate tool call: one
        # read_file, think, one read_file, think, one write_file per file.
        # This block makes the budget explicit and gives concrete batching
        # rules so the model plans to use the fewest steps possible.
        step_budget = ctx.get("step_budget", 30)
        steps_used  = len([s for s in ctx.get("steps", []) if s.get("type") == "skill"])
        steps_left  = step_budget - steps_used
        # PLAN-BEFORE-EXECUTION — every local tool that mutates the user's
        # machine (write_file, create_file, delete_file, run_bash,
        # git_commit, git_push) already shows the command/diff/commit
        # message itself via executor.py's confirm prompts, but that's the
        # WHAT, not the WHY. This closes that gap: the model must say, in
        # plain text before the tool call, what it's about to do and why —
        # so by the time the y/n prompt appears the user already knows the
        # reasoning, not just the raw command. Framed as a hard requirement
        # (not a suggestion) because a skipped plan line is easy for a
        # model to slip past under step-budget pressure.
        # OADSON.md SELF-UPDATE — mirrors what AGENT.md-style project files
        # do on other platforms: after finishing REAL work (not every
        # message — only things that actually changed the project), the
        # model should update OADSON.md's own "Current State" section
        # itself, in its own words. OADSON.md's template already carries
        # this instruction as a header note (see context.py's
        # _OADSON_MD_TEMPLATE), but that's just text the model reads as
        # data — it needed a live, standing instruction here too, or it's
        # too easy to skip under step-budget pressure the same way a plan
        # line would be. Everything ABOVE "Current State" (Project,
        # Conventions, Commands, Danger zones, Notes) is explicitly
        # off-limits for the model to touch on its own — those are the
        # human's stated preferences, not something the agent should
        # silently rewrite; only "Current State" is the model's to keep.
        # OADSON.md is a TERMINAL-ONLY project file. Other surfaces (web, app,
        # PWA, WhatsApp, Telegram) must not be told to read or write it.
        oadson_md_update_block = "" if surface != "terminal" else (
            "\n\nOADSON.md SELF-UPDATE: if this turn did REAL work that "
            "changed the project (created/edited files, ran migrations, "
            "changed a dependency, fixed a bug) — not just answered a "
            "question — read OADSON.md first, then use write_file to "
            "rewrite it with ONLY the \"## Current State\" section changed "
            "(in your own words: what you built, what changed, what's now "
            "true) — every other line, including any \"## Auto-detected "
            "changes\" section, must be preserved byte-for-byte exactly as "
            "read. Do this as your LAST tool call in the turn, after the "
            "real work is done. Never touch any other section of "
            "OADSON.md (Project, Conventions, Commands, Danger zones, "
            "Notes) unless the user explicitly asks you to change one of "
            "those — that content is the human's, not yours to rewrite. "
            "Skip this entirely for read-only turns, questions, or "
            "explanations that didn't change anything."
        )

        plan_block = (
            "\n\nPLAN BEFORE YOU ACT: before calling write_file, create_file, "
            "delete_file, run_bash, git_commit, or git_push, say in ONE short "
            "sentence what you are about to do and why — e.g. "
            "\"Adding a null check in auth.py to fix the crash you reported.\" "
            "Say this in your own text turn, THEN make the tool call. "
            "Do not skip this even under step-budget pressure. Read-only tools "
            "(read_file, list_dir, git_status, git_diff, git_log, project_tree, "
            "search_files) don't need a plan line — only tools that change or "
            "run something on the user's machine do."
        )

        # RESILIENCE ON FIXABLE TOOL FAILURES 2026-09-19 — real production
        # gap: a tool call (most often @terminal running a shell command)
        # failed with a genuinely diagnosable cause — a shell syntax
        # error, a bad quote-escaping choice, a wrong flag — and instead
        # of reading the actual error, fixing the ONE thing that was
        # wrong, and retrying, the model just reported the failure back
        # to the user in plain English and stopped the turn there,
        # treating a fixable mistake as if it were a hard blocker needing
        # the user's decision. This wastes the user's time on something
        # the model had every ability to fix itself, and reads as the
        # agent giving up rather than working the problem — the exact
        # opposite of what an autonomous coding agent should do with a
        # normal, common class of error.
        #
        # Deliberately scoped: this does NOT tell the model to blindly
        # retry every failure (a genuinely stale workspace, a missing
        # repo, a real permission error are NOT "just retry" situations —
        # those need the WORKSPACE NOTICE/reclone flow or an honest
        # report, not a loop). The line explicitly distinguishes
        # "identifiable, fixable cause" from cases needing a different
        # response, so this doesn't turn into blind retry-until-step-
        # budget-exhausted on a fundamentally broken approach.
        resilience_block = (
            "\n\nWHEN A TOOL CALL FAILS: if the error message tells you exactly "
            "what was wrong — a shell syntax error, bad quote escaping, a wrong "
            "flag, a typo'd path you can see the fix for — FIX THE ONE THING "
            "THAT WAS WRONG AND RETRY IMMEDIATELY, in the same turn. Do not stop "
            "and describe the failure back to the user asking what to do next — "
            "that wastes their time on something you can fix yourself. Only stop "
            "and explain/ask when the failure genuinely has no clear fix from the "
            "error alone (a real permission error, a missing resource that needs "
            "the user to provide something, or you've already retried the same "
            "class of fix twice without success — at that point, explain what "
            "you tried and why it's not a quick fix, don't keep guessing "
            "indefinitely). A syntax error in your own command is always your "
            "mistake to fix, not something to hand back to the user."
        )

        # WORKSPACE STALENESS AWARENESS 2026-09-19 — mirrors the step-
        # budget block right below: that block exists because the model
        # discovering its constraints reactively (running out of steps
        # mid-task) wastes far more than telling it upfront. This is the
        # same fix for a second, confirmed-in-production blind spot: a
        # redeploy wipes local disk, and the ONLY existing recovery path
        # (terminal/skill.py's maybe_reclone) is passive — it only fires
        # when the model's OWN @terminal command happens to reference a
        # repos/{owner}__{repo} path. If the model instead reaches for
        # @github read_file/list_files directly (a completely reasonable
        # first move — the model has no reason to know its workspace was
        # ever wiped), maybe_reclone never runs at all. Confirmed
        # production symptom: the model tried the same clone/cd 3 times,
        # got "No such file or directory" every time with no explanation
        # of WHY, then gave up and fell back to slow one-file-at-a-time
        # @github calls until the step budget (above) ran out — the
        # model never understood what was actually happening at any point.
        #
        # This block tells it plainly, ONLY when true (needs_reclone is
        # already a cheap boot-marker file check — see core/oadson_home.py
        # — so this costs nothing on the vast majority of turns where the
        # workspace is fine), so the model's very first move after a
        # redeploy is a deliberate reclone instead of 3 blind guesses
        # followed by a slow fallback.
        workspace_staleness_block = ""
        try:
            from core.oadson_home import needs_reclone
            if needs_reclone(ctx["user_id"]):
                workspace_staleness_block = (
                    "\n\nWORKSPACE NOTICE: the server restarted since you last worked "
                    "here — any previously cloned repo under repos/ is GONE, even if "
                    "you remember cloning it earlier in this conversation. Before "
                    "assuming a repo is available, or falling back to slow one-file-"
                    "at-a-time @github calls, just re-run @github "
                    "action='clone_to_workspace' for whatever repo you need — it's one "
                    "step, and @terminal will otherwise fail with \"No such file or "
                    "directory\" on every guess since the folder genuinely isn't there."
                )
        except Exception:
            # Best-effort awareness notice — never block prompt construction
            # over this. Worst case, the model falls back to discovering
            # staleness the old, reactive way; nothing is broken by this
            # failing silently.
            pass

        efficiency_block = (
            f"\n\nSTEP BUDGET: You have {steps_left} tool-call steps remaining this turn "
            f"(cap is {step_budget} total). Every tool call costs one step. "
            "The cap is hard: if you reach it, the turn ends whether or not the task is done, "
            "so plan the work to fit.\n"
            "Batching rules, which save steps:\n"
            "1. NEVER call github read_file or list_files in a loop to explore a repo. "
            "   One call to github clone_to_workspace downloads the whole repo. "
            "   Then use terminal for every file read, edit, search, and test. "
            "   Then one push_workspace call commits everything. "
            "   That is 3 github steps no matter how many files you touch.\n"
            "2. CHAIN terminal commands with && instead of calling terminal multiple times. "
            "   'ls repos/x/ && cat repos/x/main.py && python3 -c ...' is ONE step. "
            "   Three separate terminal calls for the same thing is THREE steps. "
            "   Always prefer the chained form.\n"
            "3. For multi-file edits, write all changes in one terminal call using a shell "
            "   script or heredoc sequence — do not call terminal once per file.\n"
            "4. If you need to read several files to understand the codebase, do it in "
            "   ONE terminal call: 'cat file1 && echo --- && cat file2 && echo --- && cat file3'.\n"
            "5. Only call a skill when you actually need its output to proceed. "
            "   Never call a skill 'to confirm' something you already know from a previous result.\n"
            "Before calling any tool, ask: can I combine this with the next 2-3 steps into "
            "one terminal call or one github action? If yes, do that instead."
        )

        # Evidence ledger — injected mid-loop so the model knows exactly
        # what tools succeeded/failed this turn before making claims.
        evidence_obj = ctx.get("evidence")
        evidence_block = evidence_obj.tool_receipt_block() if (evidence_obj and evidence_obj.receipts) else ""

        # ── Skill-provided system context (e.g. skills/ilana) ──────────────
        # See ChatContext.skill_system_context's field comment — appended
        # last/highest-priority so an active skill's operating persona and
        # non-negotiables are the most recent instruction the model reads,
        # not buried earlier in a much longer prompt.
        skill_context_block = ""
        _skc = ctx.get("skill_system_context")
        if _skc:
            skill_context_block = f"\n\n{_skc}"

        environment_block = (
            "\n\n<environment>\n"
            "You run inside OADSON, a personal assistant used from a phone and a browser. The channel "
            "you are answering on is given in your context, and each channel has its own step limit "
            "for a turn. Your workspace is a sandbox: refer to files with relative paths such as "
            "repos/owner__repo/, and never use absolute paths outside it. Many turns are light, like "
            "chat and quick questions. Heavy turns, like repo work and debugging, are where careful, "
            "complete reasoning pays off.\n"
            "</environment>"
        )

        # PROMPT CACHE SPLIT 2026-10-21 — only static text stays in the system
        # prompt. Anything that changes per turn (time, memory, task state,
        # plan, skill output, image flags, ...) goes to ctx["_volatile_block"],
        # which tool_loop appends to the latest user message for Claude. A
        # system prompt that is identical across turns is what lets the cache
        # hit; a time stamp in it made every turn a cache miss. Other providers
        # still receive the full text, joined below.
        _stable = f"{base}{environment_block}{identity_block}{scope_constraint}{presentation_guard}{repo_hint}{repo_rules}{efficiency_block}"
        notice_block = (f"\n\n<notice>\n{ctx['notice']}\n</notice>") if ctx.get("notice") else ""
        _volatile = (f"{notice_block}{honesty_guard}{capability_block}{time_block}{core_block}{task_state_block}"
                     f"{memory_block}{skill_block}{image_guard}{plan_block}{resilience_block}"
                     f"{oadson_md_update_block}{workspace_staleness_block}{evidence_block}{skill_context_block}")
        ctx["_stable_system"] = _stable
        ctx["_volatile_block"] = _volatile
        return _stable + _volatile

    def _build_messages(self, ctx: ChatContext) -> list[dict]:
        messages = list(ctx.get("history", []))
        messages.append({"role": "user", "content": ctx["objective"]})
        return messages

    def _strip_raw_tool_dump(self, text: str) -> str:
        """
        Defensive safety net, not the primary fix (see _build_system_prompt's
        presentation_guard for that) — prompt instructions alone aren't 100%
        reliable, especially on a lite/fast model. If the model still pastes
        a raw tool-result JSON blob into its reply (the literal "SKILL
        EXECUTION RESULT: [...]" pattern seen in production), strip it and
        keep only the natural-language text around it.

        Deliberately narrow: only matches a JSON object/array that contains
        tool-call-shaped keys together ("skill" AND "status", or "args" AND
        "result", etc.) — NOT any code block or JSON the user actually asked
        for, which wouldn't have this specific key combination.
        """
        import re
        # Find the largest brace-balanced {...} or [...] block in the text
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])\s*$', text.strip())
        if not match:
            return text
        blob = match.group(1)
        looks_like_tool_dump = (
            len(blob) > 80
            and '"status"' in blob
            and ('"skill"' in blob or '"args"' in blob or '"functionResponse"' in blob or '"result"' in blob)
        )
        if not looks_like_tool_dump:
            return text
        cleaned = text[:match.start()].strip()
        # Also drop a bare label line like "SKILL EXECUTION RESULT:" right
        # before the blob, so it doesn't linger as an orphaned header.
        cleaned = re.sub(r'(?im)^\s*(skill execution result|execution result)\s*:?\s*$', '', cleaned).strip()
        return cleaned if cleaned else "Done — check the execution details below for what ran."

    def _strip_latex_for_nonrendering_surface(self, text: str) -> str:
        """
        ADDED 2026-09-08 — sibling safety net to _strip_raw_tool_dump,
        same rationale: _build_system_prompt's presentation_guard already
        instructs the model not to use LaTeX on whatsapp/telegram/terminal
        (none of the three have a LaTeX renderer), but confirmed live that
        WhatsApp still received LaTeX notation despite that instruction —
        prompt compliance isn't 100%, same conclusion _strip_raw_tool_dump
        already reached for the JSON-dump problem. Converts the common
        patterns to readable plain text instead of leaving literal
        dollar-signs/backslashes, rather than just deleting them outright
        (deleting would silently drop real content, e.g. an equation,
        which is worse than an imperfect plain-text rendering of it).

        Deliberately handles only the common, high-confidence patterns
        from presentation_guard's own examples (\\frac, \\sqrt, ^, _,
        $...$/$$...$$ delimiters) rather than attempting a full LaTeX
        parser — this is a safety net for compliance drift, not a math
        typesetting engine. Anything more exotic than these patterns is
        left as-is rather than risk mangling text that only coincidentally
        contains a dollar sign or underscore (e.g. a real price like
        "$5_off" would not match \\frac/\\sqrt and passes through untouched).
        """
        import re
        result = text
        # \frac{a}{b} -> a/b
        result = re.sub(r'\\frac\{([^{}]*)\}\{([^{}]*)\}', r'\1/\2', result)
        # \sqrt{x} -> √x
        result = re.sub(r'\\sqrt\{([^{}]*)\}', r'√\1', result)
        # $$...$$ or $...$ delimiters -> just remove the delimiters,
        # keeping whatever plain content was inside them
        result = re.sub(r'\$\$([^$]*)\$\$', r'\1', result)
        result = re.sub(r'\$([^$]*)\$', r'\1', result)
        # Superscript ^2, ^3 (common exponents) -> Unicode where available
        superscript_map = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
                            "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}
        result = re.sub(
            r'\^(\d)',
            lambda m: superscript_map.get(m.group(1), f"^{m.group(1)}"),
            result,
        )
        return result

    async def _attempt_self_repair(self, ctx: ChatContext, evidence, base_payload: dict,
                                     skill_name: str, rel_path: str, home_dir,
                                     failed_claim, original_reply: str) -> dict:
        """
        ADDED 2026-08-16. The repair half of self_review.py's catch —
        designed together with the architect after naming three specific
        blockers to "just build it":

          BLOCKER 1 (step budget): resolved by NOT re-entering the main
          decide/execute `for step_num in range(max_steps)` loop at all —
          that loop has already exited by the time self-review runs (it
          only runs in the final-answer branch). This is a genuinely
          SEPARATE, bounded mini-loop with its own fixed cap, called
          AFTER the main loop, sharing no budget with it.

          BLOCKER 2 (regression from the fix itself): resolved by
          re-checking ONLY the originally-flagged claim after attempt 1
          (cheap, matches this exact bug class — architect's explicit
          choice over full re-verification of every known claim). If a
          SECOND self-review pass on the retried artifact surfaces a
          DIFFERENT new contradiction, both the original and the new one
          are disclosed to the user plainly if repair still isn't
          resolved after the attempt cap — never silently hidden.

          BLOCKER 3 (can't run the live Mongo/Gemini pipeline from this
          environment to prove runtime safety): acknowledged, not solved
          — this was built with the same unit-testable discipline as
          document_grounding.py/self_review.py/task_state.py (each
          tested standalone against real transcript data before wiring),
          but the actual live tool-call round-trip through
          provider_router + skill_executor has NOT been run end-to-end
          by me. Test this specifically before trusting it in production.

        MAX_REPAIR_ATTEMPTS = 2, architect's explicit choice.

        Returns {"repaired": bool, "final_text": str, "attempts": int,
        "unresolved_claims": [...]}.
        """
        MAX_REPAIR_ATTEMPTS = 2
        current_claim = failed_claim
        attempts_made = 0
        new_regressions = []

        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
            attempts_made = attempt
            repair_instruction = (
                f"CORRECTION NEEDED: you just told the user \"{current_claim.claim}\" about "
                f"{rel_path}, but checking the actual file shows that's not true — "
                f"{current_claim.detail} Actually make this change now (call the same skill "
                f"you used before, {skill_name}, with whatever args are needed to genuinely fix "
                f"this), then stop — do not describe the fix, just make it."
            )
            self._add_step(ctx, "repair", f"Repair attempt {attempt}/{MAX_REPAIR_ATTEMPTS}: {current_claim.claim}")

            try:
                repair_contents = [{"role": "user", "parts": [{"text": repair_instruction}]}]
                repair_system = (
                    "You are fixing a specific, concrete bug in a file you just wrote. "
                    "Call the tool needed to fix it. Do not explain, do not ask questions, "
                    "just call the tool with the correction."
                )
                # BUG FOUND BEFORE WIRING 2026-08-16: this originally
                # passed None for function_declarations. provider_router.py's
                # _call_gemini only includes a "tools" field in the request
                # `if tools:` — passing None means Gemini receives NO tool
                # schemas at all, so it can never return a functionCall
                # part, so `tool_call` below would ALWAYS be None, so this
                # entire repair loop would silently no-op on every single
                # attempt without ever actually calling a fix. Caught by
                # actually reading _call_gemini's real implementation before
                # wiring this in, rather than assuming the signature.
                # function_declarations is normally computed once per turn
                # in _plan_and_execute (local variable, not stored on ctx),
                # so it's recomputed here — cheap, schema-building only, no
                # model call.
                function_declarations = await self._build_function_declarations()
                # Reuses the exact same decide/execute primitives the main
                # loop uses — see core/provider_router.py decide_gemini and
                # core/skill_executor.py execute. Not a re-entry into the
                # main loop's `for step_num` — a fresh, one-shot decide+execute
                # pair per attempt, capped at MAX_REPAIR_ATTEMPTS total.
                decision = await self.router.decide_gemini(repair_system, repair_contents, function_declarations)
                tool_call = (decision or {}).get("tool_call")
                if not tool_call:
                    # Model didn't call a tool at all — nothing to
                    # execute, no point re-checking. Stop here rather
                    # than burn the remaining attempt on a no-op.
                    break

                repair_payload = {**base_payload, "args": tool_call.get("args", {})}
                repair_result = await self.executor.execute(tool_call["name"], repair_payload)
                evidence.record_tool(tool_call["name"], tool_call.get("args", {}), repair_result)
                self._add_step(ctx, "repair", f"@{tool_call['name']} (repair attempt {attempt})", result=repair_result)

                if ctx.get("task_state"):
                    try:
                        ctx["task_state"].record_execution(
                            step_id=f"repair_{attempts_made}", skill=tool_call["name"],
                            success=repair_result.get("status") not in ("error", "timeout", "blocked"),
                            requirements_affected=[], detail="self-repair attempt",
                        )
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"Self-repair attempt {attempt} failed to execute (non-fatal): {e}")
                continue

            # Re-check ONLY the originally-flagged claim (Blocker 2,
            # architect's explicit choice — not a full re-scan of every
            # known claim type).
            recheck = review_before_reply(rel_path, home_dir, original_reply)
            still_wrong = [r for r in recheck if r.claim == current_claim.claim and r.status == "contradicted"]
            # While we're checking anyway, note any OTHER claim that's
            # now contradicted that wasn't before — this is the "new
            # regression from the fix" case. Cheap to check since
            # review_before_reply already re-reads the file; not the
            # same as full re-verification of the task, just surfacing
            # what this one re-read happens to show.
            other_contradictions = [
                r for r in recheck if r.claim != current_claim.claim and r.status == "contradicted"
            ]
            if other_contradictions:
                new_regressions.extend(other_contradictions)

            if not still_wrong:
                return {
                    "repaired": True, "final_text": original_reply,
                    "attempts": attempts_made, "unresolved_claims": [],
                    "new_regressions": new_regressions,
                }
            current_claim = still_wrong[0]

        # Exhausted attempts without resolving — honest disclosure of
        # BOTH the original issue and anything new the repair attempts
        # surfaced, per the architect's explicit choice for blocker 2.
        return {
            "repaired": False, "final_text": original_reply,
            "attempts": attempts_made, "unresolved_claims": [current_claim],
            "new_regressions": new_regressions,
        }

    def _add_step(self, ctx: ChatContext, step_type: str, message: str, **extra) -> None:
        ctx["steps"].append({
            "type": step_type,
            "message": message,
            "time": datetime.utcnow().isoformat() + "Z",
            **extra,
        })

    def _parse_json_safe(self, text: str, fallback: dict) -> dict:
        try:
            # Strip markdown fences if present
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            return json.loads(clean.strip())
        except Exception:
            logger.warning(f"Failed to parse JSON from model output: {text[:200]}")
            return fallback
