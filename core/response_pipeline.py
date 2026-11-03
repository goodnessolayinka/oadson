"""
core/response_pipeline.py — the review/validate/persist/finalize stages,
extracted from core/orchestrator.py.

WHY THIS EXISTS
================
Same rationale as core/tool_loop.py's extraction (see that file's module
docstring for the full background on this project's structural-cleanup
pass) — _review, _validate, _persist_memory (plus its two private helpers
_trim_steps_for_persistence and _generate_session_title, and the three
MAX_* persistence-cap constants, which only ever existed for its sake),
_auto_extract_memories, and _finalize together made up ~475 lines living
directly on AIOrchestrator, the same large class the tool-calling loop
was extracted out of. These four stages are the back half of the
pipeline — everything that happens once a reply has been decided, before
it's handed back to the caller: sanity-check it, retry once if it's
empty/broken, persist it (trimmed, capped, titled), fire off async memory
extraction, and build the final response dict (including appending a
download link if the model forgot to mention one — see
core/chat_context.py's ChatContext.best_download_result for the fix that
feeds this).

Moved MECHANICALLY, not rewritten — every line of actual logic below is
byte-for-byte identical to what it was in orchestrator.py. The one
transformation applied: genuine AIOrchestrator dependencies (self.bus,
self.db, self.provider, self._add_step) became self.orchestrator.X, since
this class holds a reference to that instance rather than duplicating it.
Helpers that only ever existed for these four stages' own use
(_trim_steps_for_persistence, _generate_session_title,
_auto_extract_memories, and the MAX_* constants) moved WITH them into
this same class and kept their bare `self.X` calls unchanged, since
they're still calling a method on the same object, just a different
class than before.

orchestrator.py's run() now calls these via a single ResponsePipeline
instance instead of four separate self._review(ctx)/self._validate(ctx)/
self._persist_memory(ctx)/self._finalize(ctx) calls — see that method for
the (shorter, more explicit) calling side.

VERIFICATION NOTE — see core/tool_loop.py's own VERIFICATION NOTE for the
full caveat this project applies to every one of these extractions:
syntax/AST-verified, every self. reference audited and accounted for,
but NOT executed against a live Gemini/Mongo/skills stack from the
sandbox this was built in. The project's post-deploy verification
checklist covers what to confirm against real traffic before trusting
this as fully equivalent to the pre-extraction behavior.
"""

import os
import json
import asyncio
import logging
from datetime import datetime

import httpx

from core.event_bus import Events
from core.chat_context import ChatContext

logger = logging.getLogger("oadson.response_pipeline")


class ResponsePipeline:
    """
    Owns the review -> validate -> persist -> finalize tail of a turn.
    Constructed once per turn by AIOrchestrator.run(); each stage is
    called explicitly in sequence (same order run() always called them
    in), each still taking/returning the same ChatContext (or, for
    finalize, the same plain response dict) as before this extraction.
    """

    # Hard caps on what gets persisted — this is going into a database
    # that grows forever, not a one-off HTTP response. A single turn's
    # steps can contain full generated source code, entire file listings,
    # base64 previews, long API dumps — none of that needs to live in
    # ai_sessions forever just because it was useful to see once in the
    # response body. Keep enough to know WHAT happened, drop the payload.
    MAX_PERSISTED_STEPS = 30
    MAX_FIELD_CHARS = 300
    # Separate, more generous cap for args/result_data — still bounded
    # (this is what keeps persistence from growing unbounded, per the
    # comment above), just not the tight one-line-summary cap.
    MAX_RESULT_DATA_CHARS = 1500

    def __init__(self, orchestrator):
        # orchestrator: the AIOrchestrator instance this pipeline runs
        # for — self.orchestrator.bus / .db / .provider / ._add_step
        # below are all genuinely its methods/attributes.
        self.orchestrator = orchestrator

    async def _review(self, ctx: ChatContext) -> dict:
        """
        Fast review pass. Catches: empty response, off-topic, obvious failures.
        Not a full LLM review — that's expensive. Pattern check first.
        """
        reply = ctx.get("raw_reply", "")
        issues = []

        if not reply or not reply.strip():
            issues.append("Empty response.")

        if len(reply) < 5:
            issues.append("Response too short to be meaningful.")

        ctx["review_passed"] = len(issues) == 0
        ctx["review_issues"] = issues

        # ── Trustworthy Task State: surface completion-gate status ──────
        # ADDED 2026-08-16. Deliberately NOT added to `issues` above —
        # `issues` drives _validate's recovery-regeneration path, which
        # would throw away everything done this turn and regenerate from
        # the bare objective with no tool context. That's very likely to
        # make an incomplete task WORSE, not better. This instead makes
        # the incomplete/regressed state visible in the reply itself
        # (Section 20's actual requirement — "never declare complete
        # merely because the model says so") without discarding real
        # work. Appended here rather than at the raw_reply construction
        # site so it also covers replies where a caveat wasn't already
        # attached by the document-grounding check.
        incomplete_notes = ctx.get("task_state_incomplete")
        if incomplete_notes and "⚠️ I need to flag something" not in reply:
            gate_caveat = (
                "\n\n📋 Task state check: this doesn't look fully done yet — "
                + " ".join(incomplete_notes)
            )
            ctx["raw_reply"] = reply.rstrip() + gate_caveat
            self.orchestrator._add_step(ctx, "review", f"⚠ Completion gate not satisfied: {incomplete_notes}")

        if issues:
            await self.orchestrator.bus.emit(Events.REVIEW_FAILED, {"session_id": ctx["session_id"], "issues": issues})
            self.orchestrator._add_step(ctx, "review", f"⚠ Review issues: {issues}")
        else:
            await self.orchestrator.bus.emit(Events.REVIEW_PASSED, {"session_id": ctx["session_id"]})
            self.orchestrator._add_step(ctx, "review", "✓ Review passed.")

        return ctx

    # ── STEP 8: VALIDATE ──

    async def _validate(self, ctx: ChatContext) -> dict:
        """
        If review failed, attempt a recovery generation.
        One retry. If it fails again, return the original.
        """
        if ctx.get("review_passed", True):
            return ctx

        logger.warning("Review failed — attempting recovery generation.")
        recovery_messages = [
            {"role": "user", "content": ctx["objective"]},
        ]
        try:
            reply, model = await self.orchestrator.provider.generate(
                messages=recovery_messages,
                task_type="simple",
                system="Answer the user's question directly and clearly.",
            )
            ctx["raw_reply"] = reply
            ctx["model_used"] = model
            self.orchestrator._add_step(ctx, "validate", "Recovery generation used.")
        except Exception as e:
            logger.error(f"Recovery generation failed: {e}")
            self.orchestrator._add_step(ctx, "validate", f"Recovery failed: {e}")

        return ctx

    # ── STEP 9: PERSIST MEMORY ──

    # Hard caps on what gets persisted — this is going into a database
    # that grows forever, not a one-off HTTP response. A single turn's
    # steps can contain full generated source code, entire file listings,
    # base64 previews, long API dumps — none of that needs to live in
    # ai_sessions forever just because it was useful to see once in the
    # response body. Keep enough to know WHAT happened, drop the payload.
    MAX_PERSISTED_STEPS = 30
    MAX_FIELD_CHARS = 300
    # Separate, more generous cap for args/result_data — still bounded
    # (this is what keeps persistence from growing unbounded, per the
    # comment above), just not the tight one-line-summary cap.
    MAX_RESULT_DATA_CHARS = 1500

    def _trim_steps_for_persistence(self, steps: list) -> list:
        trimmed = []
        for s in steps[-self.MAX_PERSISTED_STEPS:]:
            # TRACE MODE 2026-08-20: "talk" steps carry the model's actual
            # interstitial prose as their message — not a status summary
            # like every other step type here. MAX_FIELD_CHARS (300) would
            # cut real sentences off mid-word. Give talk steps the same
            # generous cap args/result_data already get below, since this
            # is genuine content the user is meant to read back later, not
            # a truncatable log line.
            _msg_cap = self.MAX_RESULT_DATA_CHARS if s.get("type") == "talk" else self.MAX_FIELD_CHARS
            entry = {
                "type": s.get("type"),
                "message": (s.get("message") or "")[:_msg_cap],
                "time": s.get("time"),
            }
            if s.get("skill"):
                entry["skill"] = s["skill"]
                # Args as a short-ish string, not the raw object — a
                # skill call can carry a full file's content in its args
                # (project_creator, skill_creator), and letting that live
                # in session history forever unbounded is exactly what
                # this trimming exists to prevent. 300 chars was too
                # aggressive though — barely a few lines before cutting
                # off — so this uses the same more generous (but still
                # bounded) cap as result_data below instead of the tight
                # one-line-summary cap.
                args_str = str(s.get("args", {}))
                entry["args"] = args_str[:self.MAX_RESULT_DATA_CHARS] + ("..." if len(args_str) > self.MAX_RESULT_DATA_CHARS else "")
                # ADDED 2026-11-02 — the timeline bug. entry["args"] above
                # is a str() of the dict, so after a reload the app could
                # only ever render it as one opaque blob, never as
                # structured fields. Kept as-is (nothing that reads the
                # string breaks) and ALSO stored parsed, but only when the
                # original args were a dict/list that round-trips through
                # JSON cleanly — never for a value that was a string to
                # begin with, and never re-parsed from the truncated
                # string above (that would persist a broken half-object).
                _raw_args = s.get("args")
                if isinstance(_raw_args, (dict, list)):
                    try:
                        _args_obj = json.loads(json.dumps(_raw_args, default=str))
                        if len(json.dumps(_args_obj, default=str)) <= self.MAX_RESULT_DATA_CHARS:
                            entry["args_parsed"] = _args_obj
                    except Exception:
                        pass  # unparseable args — the string form above still stands

                result = s.get("result") if isinstance(s.get("result"), dict) else {}
                entry["result_status"] = result.get("status")
                result_msg = result.get("message") or result.get("reason") or ""
                entry["result_summary"] = result_msg[:self.MAX_FIELD_CHARS]

                # Was: only ever this one truncated message string got
                # kept, so "Result" on reload was indistinguishable from
                # the plain status message — a skill returning real data
                # (e.g. {"response": "..."} from dad_joke_fetcher) had
                # that data visible live but never actually persisted.
                # This is the same generous-but-bounded cap as args above
                # (not the 300-char one-liner cap) — large enough to
                # actually show real skill output on reload instead of
                # just its status.
                if result:
                    try:
                        result_json = json.dumps(result, default=str)
                    except Exception:
                        result_json = str(result)
                    entry["result_data"] = result_json[:self.MAX_RESULT_DATA_CHARS] + (
                        "..." if len(result_json) > self.MAX_RESULT_DATA_CHARS else ""
                    )

                # Small, bounded fields — previously not kept at all, so a
                # file a skill generated (file_generation, archive_creator)
                # had a working download link ONLY during the live session;
                # reloading history lost it entirely even though the file
                # itself was still sitting in file_sessions/Cloudinary.
                dl_url = result.get("download_url") or result.get("cloudinary_url") or result.get("serve_url")
                if dl_url:
                    entry["download_url"] = dl_url[:500]
                    entry["filename"] = (result.get("filename") or result.get("archive_name") or "download")[:200]

                if s.get("confidence"):
                    entry["confidence"] = s["confidence"].get("label")
            trimmed.append(entry)
        return trimmed

    async def _generate_session_title(self, objective: str, reply: str) -> str:
        """
        Short, descriptive session title from the first exchange —
        same idea as how Claude auto-titles a new conversation. Uses
        the existing lightweight 'simple' provider path (already used
        elsewhere for cheap, non-tool-calling generation, e.g. session
        reflection) rather than a full orchestrator run — this doesn't
        need tools, vision, or multi-step reasoning, just a short label.
        Never fatal — a failure here just leaves the session with its
        default placeholder name, doesn't affect the actual chat reply
        at all.
        """
        try:
            prompt = (
                "Generate a short title (3-6 words, no quotes, no punctuation at the end) "
                "for a chat that started like this:\n\n"
                f"User: {objective[:300]}\n"
                f"Assistant: {reply[:300]}\n\n"
                "Reply with ONLY the title, nothing else."
            )
            title, _ = await self.orchestrator.provider.generate(
                messages=[{"role": "user", "content": prompt}],
                task_type="simple",
            )
            title = title.strip().strip('"').strip("'").split("\n")[0][:60]
            return title if title else None
        except Exception as e:
            logger.warning(f"Session title generation failed (non-fatal): {e}")
            return None

    async def _generate_and_store_title(self, ctx: ChatContext) -> None:
        """
        Background companion to _persist_memory (see its BUGFIX 2026-09-19
        comment). Never raises — a failure just leaves the placeholder name.
        """
        try:
            title = await self._generate_session_title(ctx["objective"], ctx.get("raw_reply", ""))
            if not title:
                return
            await self.orchestrator.db["ai_sessions"].update_one(
                {"session_id": ctx["session_id"], "user_id": ctx["user_id"]},
                {"$set": {"name": title}},
            )
            logger.info(f"Session title set in background: {title!r} session={ctx['session_id']}")
        except Exception as e:
            logger.warning(f"Background title generation failed (non-fatal): {e}")

    async def _persist_memory(self, ctx: ChatContext) -> None:
        # Save message to session
        now = datetime.utcnow()
        # TIMESTAMP FIX 2026-08-20: stashed on ctx so run()'s final return
        # dict can hand this EXACT value back to the frontend. Previously
        # chat_stream.py's "done" event carried no timestamp at all, so
        # appendReplyWithMode() in chat.html fabricated one client-side via
        # new Date().toISOString() — the device clock at the moment the
        # reply finished RENDERING on screen, not when the backend actually
        # persisted the message. Those are two different clocks measuring
        # two different moments (client render time vs. server request
        # time), and the gap between them — skill execution time, network
        # latency, and possibly a genuine client device timezone/clock
        # offset — is what showed up as the reported ~1hr drift between
        # what the UI displayed and what MongoDB actually stored. Using
        # this same `now` for both closes the gap at the source instead of
        # trying to correct for it after the fact.
        ctx["persisted_at"] = now
        # BUGFIX 2026-09-09 — see core/orchestrator.py's turn_started_at
        # capture (right before CHAT_STARTED emits) for the full
        # rationale. Falls back to `now` only if turn_started_at is
        # somehow absent (e.g. a ctx built via some path that skips
        # orchestrator.run()'s normal top-of-function setup) — should
        # not happen in the normal turn flow, but a message with a
        # slightly-late user timestamp is a far smaller bug than a
        # crash, so this degrades rather than raises.
        user_message = {"role": "user", "content": ctx["objective"], "time": ctx.get("turn_started_at") or now}
        # Durable attachment reference (e.g. a WhatsApp image/voice note
        # saved via core.inline_storage) — distinct from image_data, which
        # is ephemeral bytes only used for this one Gemini call and never
        # saved. Only added when actually present, so old messages and
        # plain-text turns don't gain empty fields.
        if ctx.get("attachment_url"):
            user_message["attachment_url"] = ctx["attachment_url"]
            user_message["attachment_type"] = ctx.get("attachment_type")
            user_message["attachment_filename"] = ctx.get("attachment_filename")

        # Persist ALL attachments (primary + extras) so they survive reload.
        # Previously only the primary attachment_url was saved — extras were
        # passed to Gemini as inline base64 but never stored, so they
        # disappeared from chat history after the turn ended.
        all_attachments = []
        if ctx.get("attachment_url"):
            all_attachments.append({
                "url": ctx["attachment_url"],
                "file_type": ctx.get("attachment_type"),
                "filename": ctx.get("attachment_filename"),
            })
        for extra in (ctx.get("extra_attachment_urls") or []):
            if extra.get("url"):
                all_attachments.append(extra)
        if all_attachments:
            user_message["attachments"] = all_attachments

        message_pair = [
            user_message,
            {
                "role": "assistant",
                "content": ctx.get("raw_reply", ""),
                "time": now,
                # Trimmed on purpose — see _trim_steps_for_persistence.
                # Full args/results/code are in the HTTP response for
                # this one turn; only a lean summary is worth keeping
                # forever in the database.
                "steps": self._trim_steps_for_persistence(ctx.get("steps", [])),
                # ADDED 2026-08-15: separate from "content" (the clean,
                # short text shown to the user) and separate from "steps"
                # (used for the UI's execution timeline display only).
                # This is the ONLY field that exists purely to round-trip
                # back to the MODEL on a future turn — see the matching
                # read of this field in _build_context's history-building
                # loop. Only set when this turn actually ran out of step
                # budget; absent otherwise, so it doesn't bloat every
                # normal message.
                "step_exhaustion_detail": ctx.get("step_exhaustion_detail"),
                # Wasn't saved here before — model_used only ever lived in
                # the one-off HTTP response dict, so there was no persisted
                # history for any "which model answered this" analytics to
                # read. Messages saved before this fix simply won't have
                # this field, which is fine — analytics treats those as
                # "unknown" rather than guessing.
                "model_used": ctx.get("model_used", "unknown"),
                # Which display mode (chat.html's 💭 toggle) generated this
                # message. Previously the presence of a "thinking" step was
                # the only signal the frontend had for "reasoning was on,
                # default this timeline open on reload" — now that
                # "thinking" narration is gone entirely, this is the only
                # remaining way to restore that same expanded/collapsed
                # state after a reload instead of every message looking
                # identical regardless of which mode it was sent in.
                "reasoning_mode": bool(ctx.get("enable_reasoning")),
            },
        ]

        # Only generate a title once — on the first exchange, or if the
        # session still has no real name / just the generic placeholder
        # ("Chat Jul 20" from api/sessions.py's POST /sessions, or
        # nothing at all for sessions created implicitly through this
        # same flow, which was the common case and why nearly every
        # session just showed "New Chat" in the sidebar).
        existing_session = await self.orchestrator.db["ai_sessions"].find_one(
            {"session_id": ctx["session_id"], "user_id": ctx["user_id"]},
            {"name": 1},
        )
        existing_name = (existing_session or {}).get("name", "")
        needs_title = not existing_name or existing_name.startswith("Chat ")

        set_fields = {
            "updated_at": now,
            "surface_origin": ctx.get("surface", "web"),
        }
        # BUGFIX 2026-09-19 — this used to `await` title generation INLINE,
        # between the finished reply and the client's `done` event (and, for
        # WhatsApp, before send_whatsapp_reply ever ran). Title generation
        # goes through provider_manager.generate() — a SEPARATE model chain
        # from the router, starting at its own hardcoded gemini-3.5-flash-lite
        # — so on any turn that created a new session, a quota-hit there added
        # a whole extra fallback walk (each hop a full network round trip, up
        # to 90s) to the time the user waited for an answer that was already
        # finished. A title is cosmetic; it is now generated after the reply
        # is persisted, in the background, and written with a targeted
        # update_one. Failure leaves the default "Chat <date>" name in place.
        _title_needed = bool(needs_title and ctx.get("raw_reply"))

        # Persist session_state (e.g. terminal_cwd) so it survives to the
        # next message. Only write if session_state is non-empty — no point
        # touching the document for sessions that never used terminal.
        if ctx.get("session_state"):
            set_fields["state"] = ctx["session_state"]

        await self.orchestrator.db["ai_sessions"].update_one(
            {"session_id": ctx["session_id"], "user_id": ctx["user_id"]},
            {
                "$push": {"messages": {"$each": message_pair}},
                "$set": set_fields,
                # Same "Chat <date>" placeholder api/sessions.py and the
                # orchestrator's pause-path upsert already use, so a brand-new
                # session is never nameless while the background title (see
                # _generate_and_store_title) is still being generated.
                "$setOnInsert": {"created_at": now, "name": f"Chat {now.strftime('%b %d')}"},
            },
            upsert=True,
        )

        await self.orchestrator.bus.emit(Events.MEMORY_UPDATED, {"session_id": ctx["session_id"]})

        if _title_needed:
            asyncio.create_task(self._generate_and_store_title(ctx))

        # ── Autonomous Memory Extraction ──────────────────────────────────────
        # After every response, a lightweight Gemini call scans the turn for
        # new personal facts worth saving. The model decides:
        #   - What facts were learned
        #   - Whether they belong in core (always-in-context) or extended
        # This runs fire-and-forget — never blocks the response.
        asyncio.create_task(
            self._auto_extract_memories(ctx, ctx["objective"], ctx.get("raw_reply", ""))
        )

    async def _auto_extract_memories(self, ctx: ChatContext, user_message: str, assistant_reply: str) -> None:
        """
        Autonomous memory extraction — runs fire-and-forget after every response.

        A lightweight Gemini call scans the conversation turn and extracts:
        - Personal facts (name, school, location, schedule, goals, preferences)
        - Corrections the user made ("no, it's August not February")
        - Decisions the user stated ("I decided to...", "I'm going with...")

        Each extracted fact is classified as:
          CORE   — always-in-context (identity, schedule, goals, persistent preferences)
          EXTEND — extended memory (notes, decisions, one-off facts)
          SKIP   — not worth saving (small talk, questions, code output)

        Never raises. Never blocks the response. Deduplicates against existing
        core memory before saving to avoid polluting with repetitions.
        """
        try:
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                return

            # Skip extraction for purely technical turns (tool-heavy, no personal info)
            combined = (user_message + " " + assistant_reply).lower()
            personal_signals = [
                "my ", "i am", "i'm ", "i have", "i've", "i was", "i will",
                "my name", "i study", "i work", "my exam", "my course",
                "i like", "i prefer", "i live", "my school", "my university",
            ]
            if not any(s in combined for s in personal_signals):
                return  # Skip — no personal info in this turn

            # Get existing core memory keys to avoid duplicates
            from memory.vault import MemoryVault
            vault = MemoryVault(self.orchestrator.db, ctx["user_id"])
            existing_core = await vault.get_core_memories(limit=50)
            existing_keys = {m.get("key", "") for m in existing_core}

            extraction_prompt = f"""You are OADSON's memory extraction system. Analyse this conversation turn and extract personal facts worth remembering.

USER SAID: {user_message[:800]}

ASSISTANT REPLIED: {assistant_reply[:400]}

EXISTING CORE MEMORY KEYS (already saved — skip duplicates): {', '.join(list(existing_keys)[:20]) or 'none'}

Extract personal facts from the USER'S message only (not from the assistant's reply unless it's confirming something the user stated).

For each fact, output one line:
CORE|key_name|value  (for always-important facts: identity, schedule, goals, name, school, location, persistent preferences)
EXTEND|key_name|value  (for useful but non-critical facts: decisions, one-off notes)
SKIP  (if nothing worth saving)

Rules:
- key_name: lowercase_with_underscores, descriptive (e.g. oau_exam_timetable, preferred_language, university_level)
- value: concise but complete
- CORE only for things that should be in EVERY future conversation
- Do not save code, file contents, or technical outputs
- Do not duplicate existing keys
- If nothing personal was stated, output only: SKIP

Output only the extraction lines, nothing else."""

            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": extraction_prompt}]}],
                "generationConfig": {"maxOutputTokens": 300, "temperature": 0.1},
            }

            # SECURITY FIX 2026-08-14: key moved from URL to
            # x-goog-api-key header — was leaking into Render's httpx
            # request logs.
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
                if r.status_code != 200:
                    return

                raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

            saved = []
            core_facts = []
            for line in raw.splitlines():
                line = line.strip()
                if not line or line == "SKIP":
                    continue
                parts = line.split("|", 2)
                if len(parts) != 3:
                    continue
                tier, key, value = parts[0].strip(), parts[1].strip().lower().replace(" ", "_"), parts[2].strip()
                if not key or not value or key in existing_keys:
                    continue
                if tier == "CORE":
                    core_facts.append(f"{key}: {value}")
                    saved.append(f"core:{key}")
                elif tier == "EXTEND":
                    await vault.store(key, value, category="general")
                    saved.append(f"extend:{key}")

            # Extend summary with all new core facts in ONE Gemini call
            if core_facts:
                await vault.extend_core_summary(core_facts, api_key)

            if saved:
                logger.info(f"Auto-extracted memories for {ctx['user_id']}: {saved}")

        except Exception as e:
            logger.debug(f"Auto memory extraction failed (non-fatal): {e}")

    # ── STEP 10: FINALIZE ──

    async def _finalize(self, ctx: ChatContext) -> dict:
        skill_output = ctx.get("skill_output") or {}
        reply = ctx.get("raw_reply", "")

        # If file was generated — append download URL to response
        # Works on WhatsApp, Telegram, web, app — any surface
        if skill_output.get("download_url") and skill_output.get("filename"):
            url = skill_output["download_url"]
            fname = skill_output["filename"]
            surface = ctx.get("surface", "web")
            if surface == "terminal":
                # Terminal already has write_file tool call — no link needed
                pass
            elif url not in reply:
                reply = reply.rstrip()
                reply += f"\n\n📥 *{fname}*: {url}"

        response_dict = {
            "response": reply,
            "model_used": ctx.get("model_used", "unknown"),
            "skill_triggered": ctx.get("skill_name"),
            "skill_output": skill_output,
            "steps": ctx.get("steps", []),
            "session_id": ctx["session_id"],
            # TIMESTAMP FIX 2026-08-20 — see _persist_memory's comment on
            # ctx["persisted_at"] for the full rationale. Appends "Z":
            # datetime.utcnow() is naive UTC, and isoformat() on a naive
            # datetime omits any timezone marker at all — which JS's
            # `new Date(...)` then parses as the BROWSER'S local time,
            # not UTC. That silently shifted every timestamp by the
            # viewer's own UTC offset (confirmed: a WAT device reading a
            # genuinely-correct 09:13 UTC value as 09:13 WAT = 08:13 UTC
            # internally — a full hour off). The old version of this
            # comment deliberately left "Z" off "to match the frontend's
            # convention" — that convention (blindly assuming timeZone:
            # 'UTC' regardless of what the string said) is exactly what
            # was masking this bug; the frontend now parses whatever the
            # string actually says, so the string needs to actually say it.
            "persisted_at": ctx.get("persisted_at", datetime.utcnow()).isoformat() + "Z",
        }

        # ── PUSH NOTIFICATION 2026-08-25 — "notify when OADSON is done" ──
        # Fire-and-forget via asyncio.create_task, same pattern
        # orchestrator.py already uses for style-profile learning — never
        # awaited, never allowed to slow down or fail the actual response.
        # Deliberately scoped:
        #   - Only app/web/pwa: whatsapp/telegram already deliver their
        #     own native notifications through those platforms; sending a
        #     SECOND OneSignal push for the same reply there would be
        #     redundant noise, not the feature that was asked for.
        #   - Only when the turn actually invoked a real skill — see
        #     BUG FOUND AND FIXED note below on why this used to be a
        #     step-count check instead.
        surface_for_push = ctx.get("surface", "web")
        # BUG FOUND AND FIXED 2026-08-26 — "notifications for literally
        # every message, even a plain 'Hey'". The gate here used to be
        # `len(ctx.get("steps", [])) >= 2`, on the theory that a quick
        # one-shot reply wouldn't rack up steps. Checked orchestrator.py's
        # _add_step call sites: THREE unconditional bookkeeping steps
        # ("context" — history loaded, "memory" — retrieval, "intent" —
        # classification) get logged on every single turn, before any
        # skill/tool logic even runs. That means step_count was >=2 for
        # literally every message, with or without a skill, making this
        # gate a no-op since the feature shipped — it never once actually
        # filtered anything, which is exactly why a bare "Hey" triggered
        # a notification. Using ctx["skill_name"] instead — confirmed via
        # tool_loop.py that it's ONLY ever assigned inside actual
        # skill-execution code paths, never for a plain conversational
        # reply — so this now precisely matches "a skill actually ran",
        # not "the pipeline did its normal per-turn bookkeeping".
        _notifs_enabled = ((ctx.get("user") or {}).get("preferences") or {}).get("notifications_enabled", True)
        if surface_for_push in ("app", "web", "pwa") and ctx.get("skill_name") and _notifs_enabled:
            import asyncio as _asyncio
            from core.push_notifications import send_completion_notification
            # TARGETING: OneSignal external_id here must match whatever
            # median.onesignal.login(...) registers client-side — see
            # push_notifications.py's module docstring. Using email, not
            # the Mongo _id: the frontend doesn't currently have _id
            # available anywhere (checked — auth response includes it,
            # but nothing persists it to localStorage the way
            # user_email/user_name already are), and adding that plumbing
            # was broader scope than this feature. Email is already
            # stored client-side and stable, so it's the simpler correct
            # choice for both sides to agree on.
            _push_target = (ctx.get("user") or {}).get("email") or ctx["user_id"]
            _skill = ctx.get("skill_name")
            _title = f"OADSON finished — @{_skill}" if _skill else "OADSON finished"
            _preview = (reply or "").strip().replace("\n", " ")[:150]
            # BUGFIX 2026-10-08 — send_completion_notification has
            # accepted a url param since it was added (see its own
            # docstring: "optional deep link... tapping the notification
            # navigates the app there"), but this call site never
            # actually passed one — every completion notification opened
            # the app generically, never the specific conversation that
            # finished. CONFIRMED against the frontend's actual route
            # (BACKEND_PUSH_PATCH.md: "/chat/<id>?reply=1" on open,
            # "/chat/{session_id}" as the base — this f-string matches).
            #
            # UPDATED 2026-10-08 — also passes session_id (separate from
            # the URL string — the frontend's service worker reads
            # data.session_id directly, per its own spec) and
            # quick_reply_text so web_buttons gets populated. "Continue"
            # is the frontend dev's OWN example for a finished-task
            # notification (BACKEND_PUSH_PATCH.md: "a finished task ->
            # 'Continue'") — this is specifically the completion-
            # notification call site, so that example applies directly,
            # not a guess.
            _deep_link = f"/chat/{ctx['session_id']}"
            _asyncio.create_task(send_completion_notification(
                user_id=_push_target,
                title=_title,
                message=_preview or "Your response is ready.",
                url=_deep_link,
                session_id=ctx["session_id"],
                quick_reply_text="Continue",
            ))

        return response_dict


