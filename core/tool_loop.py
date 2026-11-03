"""
core/tool_loop.py — the tool-calling loop, extracted from
core/orchestrator.py's _plan_and_execute.

WHY THIS EXISTS
================
_plan_and_execute was ~1,330 lines: turn setup (task-state extraction,
system prompt build, planner call, fallback-state init) followed
immediately by an ~1,100-line `for step_num in range(max_steps):` loop
doing decide/execute/echo-back/stream/review, all as one function body
with 17+ closed-over local variables (contents, function_declarations,
active_model, active_protocol, openai_messages, skill_name,
skill_output, evidence, ...). That undifferentiated size and implicit
state is exactly the shape that let a real bug ship and stay hidden for
a long time: skill_output was reassigned unconditionally on every
iteration with nothing making its lifecycle visible without reading the
whole loop end to end (see core/chat_context.py's module docstring for
the full incident).

This file is the loop's actual logic — moved MECHANICALLY (not
rewritten) out of _plan_and_execute into ToolCallLoop, a class that owns
what used to be bare locals as real, inspectable instance attributes.
Every line of decision/execution/streaming/review logic below is
byte-for-byte the same as it was in orchestrator.py before this move —
this is a structural extraction, not a behavior change. The one
mechanical transformation applied throughout: every `self.X` reference
(self.bus, self.router, self.skills, self.executor, self.db,
self._add_step, self._build_system_prompt, self._strip_raw_tool_dump,
self._attempt_self_repair) became `self.orchestrator.X`, since those are
genuinely AIOrchestrator's methods/attributes and this class holds a
reference to that instance rather than duplicating them.

_plan_and_execute now does turn setup, builds a ToolCallLoop with that
setup's results, and returns `await loop.run()` — see that method in
orchestrator.py for the (much shorter) calling side.

VERIFICATION NOTE
==================
Verified via: syntax compile, AST parse, and a mechanical audit that
every `self.` reference inside the moved body was accounted for and
correctly redirected (see the extraction's own working notes — 29
occurrences across 9 distinct orchestrator attributes/methods, all
converted, confirmed via grep that no bare `self.` reference remained
unconverted afterward). This could NOT be executed against a live
Gemini/Mongo/skills stack from the sandbox this extraction was done in
— that is a real, open gap. See the project's post-deploy verification
checklist for exactly what to confirm against real traffic before
trusting this as fully equivalent to the pre-extraction behavior.
"""

import json
import os
import re
import time
import logging
from datetime import datetime

from core.event_bus import Events
from core.provider_router import QuotaExceeded, gemini_declarations_to_openai_tools
from core.confidence import score_skill_result
from core.failure_explainer import describe_call, explain_failure
from core.reflection import track_and_maybe_reflect, get_recent_reflections, update_reflection_resolution
from core.hallucination_firewall import review_answer, apply_rewrite_instruction
from core.document_grounding import check_module_claim
from core.task_state import EvidenceType
from core.local_tool_schemas import LOCAL_TOOL_NAMES
from core.interactive_tool_schemas import INTERACTIVE_TOOL_NAMES
from core.self_review import review_before_reply

logger = logging.getLogger("oadson.tool_loop")


def repeat_failure_state(prev_key, prev_streak, skill_name, call_args, failed):
    """
    Track the same call failing again and again. Returns (key, streak). A new
    or successful call resets the streak. Used to stop burning steps (and
    money) on a call that cannot succeed as written.
    """
    if not failed:
        return None, 0
    key = (skill_name, json.dumps(call_args, sort_keys=True, default=str))
    streak = prev_streak + 1 if key == prev_key else 1
    return key, streak


class ToolCallLoop:
    """
    Owns one turn's tool-calling loop. Constructed fresh per turn by
    _plan_and_execute once turn setup (system prompt, contents, function
    declarations, evidence store, etc.) is done; `await self.run()`
    executes the loop and returns the same ctx _plan_and_execute always
    returned — callers don't need to change anything about how they
    consume the result.
    """

    def __init__(
        self,
        orchestrator,
        ctx,
        system,
        contents,
        function_declarations,
        base_payload,
        evidence,
        max_steps,
        surface,
        local_execution,
        gemini_url_base,
        api_key,
        active_model,
        active_protocol,
        openai_messages,
        openai_call_counter,
        tool_call_count,
        skill_name,
        skill_output,
        previous_result,
    ):
        # orchestrator: the AIOrchestrator instance this loop runs for —
        # every self.orchestrator.X reference below is reaching back
        # into it for a method/attribute that genuinely lives there
        # (self.bus, self.router, self.skills, self.executor, self.db,
        # self._add_step, self._build_system_prompt,
        # self._strip_raw_tool_dump, self._attempt_self_repair).
        self.orchestrator = orchestrator
        self.ctx = ctx
        self.system = system
        self.contents = contents
        self.function_declarations = function_declarations
        self.base_payload = base_payload
        self.evidence = evidence
        self.max_steps = max_steps
        self.surface = surface
        self.local_execution = local_execution
        self.gemini_url_base = gemini_url_base
        self.api_key = api_key
        self.active_model = active_model
        self.active_protocol = active_protocol
        self.openai_messages = openai_messages
        self.openai_call_counter = openai_call_counter
        self.tool_call_count = tool_call_count
        self.skill_name = skill_name
        self.skill_output = skill_output
        self.previous_result = previous_result

    async def run(self):
        # Local aliases so the loop body below (moved verbatim from
        # _plan_and_execute) can keep referencing these as bare names —
        # e.g. `ctx`, `contents`, `active_model` — exactly as it always
        # did, rather than requiring every single reference throughout
        # 1,000+ lines to become self.ctx/self.contents/self.active_model.
        # This is deliberate: the point of this extraction is to give
        # the STATE an explicit home (these are real __init__ attributes
        # now, not implicit function-local closures), not to rewrite
        # 1,000+ lines of working logic into a different reference style
        # in the same pass — that would multiply the risk of this
        # already-large mechanical move for no behavioral benefit.
        ctx = self.ctx
        system = self.system
        contents = self.contents
        function_declarations = self.function_declarations
        base_payload = self.base_payload
        evidence = self.evidence
        max_steps = self.max_steps
        surface = self.surface
        local_execution = self.local_execution
        gemini_url_base = self.gemini_url_base
        api_key = self.api_key
        active_model = self.active_model
        active_protocol = self.active_protocol
        openai_messages = self.openai_messages
        openai_call_counter = self.openai_call_counter
        tool_call_count = self.tool_call_count
        skill_name = self.skill_name
        skill_output = self.skill_output
        previous_result = self.previous_result

        # Deferred protocol switch for a preferred openai-protocol model
        # picked at step_num==0 — see the USER-SELECTED MODEL block below
        # and its application right after `active_model = decision[...]`.
        # Always starts unset; only ever set within this one run() call.
        pending_protocol_switch = None
        pending_openai_messages = None

        # Bounds how much accumulated turn data (contents, echoed tool
        # results, evidence) this loop will carry before forcing an early,
        # honest wrap-up — independent of step COUNT. Added alongside
        # raising web/terminal/app's step budget to 40 (see
        # core/orchestrator.py's _step_limits comment): a higher step
        # ceiling alone raises exactly the risk that already caused two
        # real OOM crashes on this Render instance (Aug 21, both "Ran out
        # of memory (used over 512MB)") — more steps means more echoed
        # tool results held in `contents` for the rest of the turn, and
        # step COUNT was never actually the thing correlated with memory
        # use; total accumulated DATA is.
        #
        # RAISED 2026-10-01 — was 6MB, which turned out to cut off
        # genuinely healthy multi-step work: a real production turn with
        # 12 successful @terminal/@github steps (large file reads/writes,
        # the normal shape of real multi-file repo work) hit this guard
        # right as the model was mid-self-correction on step 13, ending
        # the turn with no connection to the step budget (40, nowhere
        # near exhausted) or to anything actually wrong — just this
        # number being sized far more conservatively than the real risk
        # required. 6MB left ~85x headroom below the 512MB ceiling that
        # caused the actual crashes; 20MB still leaves ~25x headroom
        # (this `contents` buffer is one of several things sharing that
        # 512MB alongside FastAPI/Mongo/etc, so a 25x margin is still a
        # large, genuinely conservative buffer, not a return to the
        # un-tested pre-Aug-21 behavior) while comfortably covering real
        # multi-file work instead of flagging it as dangerous.
        CONTENTS_SIZE_GUARD_BYTES = 20 * 1024 * 1024

        _repeat_key, _repeat_streak = None, 0
        for step_num in range(max_steps):
            # STOP — the user pressed Stop. Start no further model call.
            from core import run_control as _rc
            if _rc.is_stopped(ctx.get("session_id") or ""):
                import asyncio as _aio
                logger.info(f"TRACE stop requested — ending turn before step {step_num}")
                raise _aio.CancelledError()
            # Rebuild system prompt each iteration so the step budget counter
            # in efficiency_block reflects how many steps are actually left,
            # and evidence ledger reflects all tool receipts so far.
            ctx["evidence"] = evidence
            system = self.orchestrator._build_system_prompt(ctx)
            # CALL MODE 2026-11-02 — appended AFTER the base prompt is
            # built, so it reads as the final, most specific instruction
            # rather than something buried mid-prompt. Re-applied every
            # iteration like the prompt itself, so it can't be lost on a
            # later round. Also forces the cheap "simple" tier — call mode
            # is explicitly the fastest-model path (see api/voice.py's
            # module docstring and the spec it implements).
            if ctx.get("call_mode"):
                ctx["task_type"] = "simple"
                system = (system or "") + (
                    "\n\n[CALL MODE] This is a live voice call. Answer in short, "
                    "plain, spoken sentences — no markdown, no bullet points, no "
                    "headings, no code blocks, no emoji. Get straight to the "
                    "answer. If a tool is genuinely needed, call it once and then "
                    "answer; do not chain multiple rounds of tool calls."
                )

            # Cheap, once-per-iteration size check BEFORE spending an API
            # call on a payload that's already too large to keep growing
            # safely — see CONTENTS_SIZE_GUARD_BYTES's comment above.
            try:
                _contents_size = len(json.dumps(contents, default=str))
            except Exception:
                _contents_size = 0  # never let a measurement failure break the actual turn
            if _contents_size > CONTENTS_SIZE_GUARD_BYTES:
                logger.warning(
                    f"Tool loop wrapping up early — accumulated contents size "
                    f"{_contents_size} bytes exceeded {CONTENTS_SIZE_GUARD_BYTES} "
                    f"at step {step_num} for session={ctx['session_id']}"
                )
                # IMPROVED 2026-10-01 — was a terse one-line "Wrapping up
                # — ... (XMB). Say 'continue'" with no explanation of WHY
                # this matters or what actually happened so far. Confirmed
                # production reaction: this read as an arbitrary, opaque
                # cutoff ("weak-hearted" resilience) rather than a real,
                # justified safety measure — because it never said so.
                #
                # Also, checked honestly rather than assumed: this `break`
                # exits the `for...else` loop below WITHOUT running its
                # `else` clause (Python for-else semantics — else only
                # runs if the loop completes without break), which is
                # exactly where step_exhaustion_detail normally gets
                # built for the max_steps-exhausted case. Without
                # replicating that here, an earlier draft of this message
                # promised "pick up exactly where this left off" with
                # NOTHING actually backing that claim for THIS exit path —
                # _build_context's history-folding only looks for a field
                # this branch was never setting. Now builds the identical
                # per-step summary and sets step_exhaustion_detail the
                # same way, so the promise is real: the next turn
                # genuinely sees what was already attempted and won't
                # repeat it, not just a reassuring sentence.
                skill_attempts = [s for s in ctx["steps"] if s.get("type") == "skill" and s.get("skill")]
                lines = []
                for s in skill_attempts:
                    result = s.get("result") if isinstance(s.get("result"), dict) else {}
                    failed = bool(s.get("error")) or result.get("status") == "error"
                    if failed:
                        reason = s.get("error") or result.get("error") or result.get("reason") or "unknown error"
                        outcome = f"failed ({reason})"
                    else:
                        outcome = "succeeded"
                    result_summary = result.get("message") or result.get("status") or ""
                    detail = f": {result_summary}" if (result_summary and not failed) else ""
                    lines.append(f"- @{s['skill']}({s.get('args', {})}) — {outcome}{detail}")
                if lines:
                    ctx["step_exhaustion_detail"] = (
                        "[SYSTEM NOTE — steps already attempted before pausing for the data-size "
                        "safety limit last turn, do not repeat these:]\n" + "\n".join(lines)
                    )
                succeeded = sum(1 for s in skill_attempts if not (bool(s.get("error")) or (s.get("result") or {}).get("status") == "error"))
                # BUGFIX 2026-10-01 — _add_step only appends to ctx["steps"]
                # (the internal execution-timeline log the UI shows as
                # collapsed step cards), it does NOT set the actual
                # chat-visible reply. Checked _add_step's real
                # implementation directly rather than assume — confirmed
                # it has nothing to do with raw_reply at all. Without
                # setting raw_reply explicitly here too (same as the
                # max_steps-exhausted path below does), this whole pause
                # message would only ever show up buried in an execution-
                # timeline entry, never as something OADSON actually says
                # to the user.
                pause_message = (
                    f"Pausing here, not because of an error — {len(skill_attempts)} tool calls in this "
                    f"turn ({succeeded} succeeded) have accumulated {_contents_size // (1024*1024)}MB of "
                    f"combined results (file contents, command output), past the "
                    f"{CONTENTS_SIZE_GUARD_BYTES // (1024*1024)}MB safety limit for one turn on this server. "
                    f"Everything completed so far is saved — nothing is lost. Say \"continue\" and "
                    f"I'll pick up exactly where this left off without repeating what already worked."
                )
                self.orchestrator._add_step(ctx, "generate", pause_message)
                ctx["raw_reply"] = pause_message
                ctx["model_used"] = active_model
                ctx["skill_name"] = skill_name
                ctx["skill_output"] = ctx.best_download_result()
                break

            # ── DECIDE (function call or final answer) ──
            # NATIVE FUNCTION-CALLING 2026-08-23: was
            # `function_declarations if (function_declarations and not
            # local_execution) else None` — local_execution unconditionally
            # forced this to None, disabling native calling entirely for
            # terminal and relying on prompt-parsed JSON-in-text instead.
            # function_declarations now includes local-tool schemas too
            # when local_execution=True (see orchestrator.py's merge, right
            # after _build_function_declarations()), so there's no longer
            # any reason to withhold tools_for_call from this surface —
            # Gemini can now offer local tools as real, schema-validated
            # options exactly like server skills, same as web/app always
            # could.
            tools_for_call = function_declarations if function_declarations else None

            try:
                # TERMINAL-FIRST LOCAL MODEL 2026-09-07: on the terminal
                # surface, try the user's self-hosted local_qwen_cli model
                # BEFORE Gemini — it's free (own hardware, no quota to
                # protect) and was previously only reachable as a
                # Gemini-exhaustion fallback deep in decide_fallback,
                # meaning it was never actually used in practice since
                # Gemini's free tier rarely runs dry in a single session.
                #
                # UPDATED 2026-09-07: originally text-only. Confirmed
                # against dataset.jsonl (227 matching examples) that this
                # model WAS trained on tool-calling, in a JSON-in-text
                # format — see try_local_qwen_cli's docstring. It can now
                # produce a real tool_call, handled by the exact same
                # generic decision.get("tool_call") branch below that
                # already handles Gemini/OpenAI tool calls, no separate
                # execution path needed.
                #
                # SCOPE LIMIT, deliberate: only attempted on step_num==0
                # (the turn's FIRST decision). There is no local-model
                # continuation path — after this one call, whether it
                # answered directly or returned a tool_call that gets
                # executed, any FURTHER iteration of this loop (a
                # multi-step tool exchange) proceeds via the normal
                # active_protocol=="gemini" branch below, same as if
                # local_qwen_cli had never been tried. Building a real
                # multi-step local continuation would mean either a third
                # parallel message-format track alongside the existing
                # Gemini-native `contents` and OpenAI `openai_messages`
                # tracks, or lossy re-translation every iteration — the
                # exact anti-pattern decide_fallback's own docstring
                # warns against. One fast local turn (chat reply OR a
                # single tool call) is the real, tested scope; anything
                # needing a second round-trip in the same turn hands off
                # to Gemini exactly like the existing Gemini->OpenAI
                # quota fallback already does.
                #
                # ANY failure (tunnel down, timeout, bad response, no
                # tool_call and no usable text) falls through silently to
                # the existing Gemini path below — same fail-open pattern
                # as every other fallback here.
                decision = None
                decision_from_local = False
                if (
                    step_num == 0
                    and active_protocol == "gemini"
                    and surface == "terminal"
                ):
                    # DEFENSIVE 2026-09-07: this whole block gets its own
                    # try/except, separate from decide_gemini's below.
                    # Without this, ANY bug in the local-first path
                    # (however unrelated to actual provider exhaustion)
                    # was being caught by the outer except at the bottom
                    # of this try and misreported as "All providers
                    # exhausted or failed" — even though Gemini was never
                    # actually attempted. exc_info=True logs the real
                    # traceback so a genuine bug here is diagnosable
                    # instead of just resembling total failure.
                    try:
                        local_decision = await self.orchestrator.router.try_local_qwen_cli(system, contents)
                        if local_decision is not None:
                            logger.info("Provider: local_qwen_cli (terminal-first)")
                            decision = local_decision
                            decision_from_local = True
                    except Exception as local_e:
                        logger.error(f"local_qwen_cli (terminal-first) raised unexpectedly: {local_e}", exc_info=True)
                        decision = None
                        decision_from_local = False

                # TERMINAL-FIRST 2026-11-02 — terminal turns try GPT OSS 20B on
                # CoreWeave before Gemini. Gemini stays first on every other
                # surface. Any failure falls through to decide_gemini below,
                # exactly as before. Uses the same deferred switch to openai
                # format as the user-selected model block, so the next loop
                # step continues on this model with continue_openai.
                if decision is None and step_num == 0 and active_protocol == "gemini" and surface == "terminal":
                    try:
                        terminal_decision = await self.orchestrator.router.decide_terminal_first(
                            system, contents, tools_for_call, needs_tools=bool(tools_for_call),
                        )
                        logger.info(f"Provider: terminal-first {terminal_decision.get('model_used')}")
                        decision = terminal_decision
                        pending_protocol_switch = "openai"
                        pending_openai_messages = terminal_decision["openai_messages"]
                    except Exception as tf_e:
                        logger.warning(f"terminal-first GPT OSS unavailable, using Gemini: {tf_e}")
                        decision = None

                # USER-SELECTED MODEL 2026-09-09 — web/app/pwa only (see
                # api/chat.py's ChatRequest.model / api/chat_stream.py's
                # ChatStreamRequest.model docstrings for the surface
                # gate; ctx["preferred_model"] is simply absent/None for
                # any surface that doesn't offer selection, so this block
                # is a no-op there). Same scope limit as the
                # local_qwen_cli block above and the same reason: only
                # attempted at step_num==0, only while still on the
                # default "gemini" active_protocol (i.e. nothing has
                # already switched this turn for a real quota/failure
                # reason — a preferred_model pick doesn't get to override
                # an in-progress fallback). ANY failure here — model not
                # found, doesn't support tools, over quota, dead
                # endpoint — falls through silently to the existing
                # decide_gemini → decide_fallback chain below exactly as
                # if preferred_model had never been set. This is
                # deliberately an optimistic fast path in front of that
                # chain, never a replacement for it — reliability for a
                # turn with no preference set, or where the preference
                # fails, is completely unchanged.
                # Heavy turns (repo/codebase/debug/refactor) go to Claude first on
                # web/app/pwa/telegram/whatsapp. Explicit user picks still win.
                from core.model_policy import auto_model_for
                _effective_pref = ctx.get("preferred_model") or (
                    auto_model_for(ctx) if step_num == 0 else None
                )
                # REPO CONTEXT — if this session has an indexed codebase, Claude
                # gets the stable repo map in the (cached) system prompt and the
                # most relevant snippets in the latest user message. Gemini and
                # DeepSeek paths are untouched.
                _claude_system, _claude_contents = system, contents
                if _effective_pref and step_num == 0:
                    try:
                        import copy as _copy
                        from core import repo_index as _repo_index
                        # Stable system prompt (cacheable) + per-turn text in the
                        # latest user message (not cached). See orchestrator's
                        # PROMPT CACHE SPLIT for what goes where.
                        _claude_system = ctx.get("_stable_system") or system
                        _claude_contents = _copy.deepcopy(contents)
                        _extra = []
                        _vol = (ctx.get("_volatile_block") or "").strip()
                        if _vol:
                            _extra.append(_vol)
                        _idx = _repo_index.get_for_user(ctx.get("user_id") or "")
                        if _idx is None:
                            from core import workspace_pointer as _wp
                            from core import worker_client as _wc
                            _folder = await _wp.get_active(ctx.get("user_id") or "")
                            if _folder:
                                _remote = await _wc.try_remote("github", {
                                    "user_id": ctx.get("user_id") or "",
                                    "args": {"action": "search_code", "repo": _folder,
                                             "query": ctx.get("objective") or ""}}, wait_s=60)
                                if _remote and _remote.get("status") == "success" and _remote.get("snippets"):
                                    _extra.append("RELEVANT CODE FROM THE PROJECT (searched on the worker):\n"
                                                  + str(_remote["snippets"]))
                        if _idx is not None:
                            _claude_system = _claude_system + "\n\n" + _idx.repo_map()
                            _snips = _idx.snippets_for(ctx.get("objective") or "")
                            if _snips:
                                _extra.append("RELEVANT CODE FROM THE PROJECT (retrieved for this request):\n" + _snips)
                        if _extra:
                            for _turn in reversed(_claude_contents):
                                if _turn.get("role") == "user":
                                    _turn.setdefault("parts", []).append({"text": "\n\n".join(_extra)})
                                    break
                    except Exception as _re:
                        logger.warning(f"claude context build skipped: {_re}")
                if (
                    decision is None
                    and step_num == 0
                    and active_protocol == "gemini"
                    and _effective_pref
                ):
                    try:
                        preferred_decision = await self.orchestrator.router.decide_preferred(
                            _effective_pref, _claude_system, _claude_contents, tools_for_call,
                            needs_tools=bool(tools_for_call), surface=surface,
                        )
                        logger.info(f"Provider: user-selected {preferred_decision.get('model_used')}")
                        decision = preferred_decision
                        # DELIBERATE: active_protocol is NOT flipped here,
                        # even if preferred_decision["protocol"] ==
                        # "openai". The dispatch right below stays keyed
                        # on active_protocol alone (see its own BUGFIX
                        # 2026-09-07 comment) — flipping to "openai" here
                        # would route THIS SAME iteration into the `else:
                        # continue_openai(...)` branch, discarding
                        # decision and making a second, redundant call.
                        # Staying "gemini" for this one iteration lets the
                        # `if decision is None:` check below correctly
                        # skip re-fetching. pending_protocol_switch below
                        # carries the flip forward to the NEXT iteration
                        # only, exactly when continue_openai should start
                        # being used — same timing decide_fallback's own
                        # switch already relies on for its OWN next-loop
                        # continuation.
                        if preferred_decision.get("protocol") == "openai":
                            pending_protocol_switch = "openai"
                            pending_openai_messages = preferred_decision["openai_messages"]
                    except Exception as pref_e:
                        logger.warning(f"preferred_model '{ctx.get('preferred_model')}' unavailable, falling back to normal routing: {pref_e}")
                        decision = None

                # BUGFIX 2026-09-07: this dispatch MUST stay keyed on
                # active_protocol alone, exactly as it was before the
                # terminal-first local block above was added. It was
                # briefly written as `if decision is None and
                # active_protocol == "gemini":`, which seemed right
                # ("only fetch a decision if we don't have one yet") but
                # broke the if/else pairing below: when local_qwen_cli
                # succeeded, decision was NOT None, so this condition
                # went straight to the `else: continue_openai(active_model,
                # ...)` branch — but active_model at that point was still
                # whatever it was BEFORE this turn (stale, no "/"), since
                # decision["model_used"] doesn't get assigned to
                # active_model until after this whole if/else. Confirmed
                # in production: "not enough values to unpack" from
                # continue_openai's provider_name_model.split("/", 1)
                # on a bare stale model id. The actual fix: keep the
                # dispatch itself unconditional on active_protocol, and
                # only guard the decide_gemini CALL with `decision is
                # None` (a local decision means nothing further to fetch
                # here, but we must not fall into the else branch either).
                if active_protocol == "gemini":
                    if decision is None:
                        try:
                            decision = await self.orchestrator.router.decide_gemini(system, contents, tools_for_call)
                        except Exception as e:
                            # FIX 2026-08-08: used to split into two branches —
                            # QuotaExceeded tried decide_fallback, anything else
                            # gave up immediately with "AI request failed. Try
                            # again." — on the theory that a non-quota failure
                            # on the FIRST attempt meant nothing had really been
                            # tried yet, so fallback wasn't warranted.
                            #
                            # That reasoning doesn't hold now that
                            # decide_gemini() (provider_router.py) internally
                            # walks every Gemini model and only raises after
                            # ALL of them failed — by the time anything reaches
                            # here, real attempts already happened (quota
                            # exhaustion on the valid models, e.g.), and
                            # whatever exception comes out doesn't mean Groq/
                            # NVIDIA/Mistral/OpenRouter are equally broken. A
                            # single dead Gemini model ID was enough to skip
                            # trying 4 entire other providers and show a flat
                            # "AI request failed" even when a fallback would
                            # have produced a real answer — confirmed in
                            # production logs (weather + github requests both
                            # hit this exact path). Now every Gemini-side
                            # failure, whatever its type, gets one real shot at
                            # the fallback chain before giving up.
                            logger.warning(f"Gemini exhausted/failed — trying fallback providers: {e}")
                            # OADSON V3 Intelligent Response State EDD — signal
                            # WORKING_ON_IT before attempting the fallback chain,
                            # so the frontend stops attributing this delay to the
                            # user's own network. Best-effort: emit() already
                            # isolates handler failures (see EventBus.emit), so
                            # this can't break the actual fallback attempt below.
                            await self.orchestrator.bus.emit(Events.RESPONSE_STATUS_CHANGED, {
                                "session_id": ctx["session_id"],
                                "status": "working_on_it",
                                "reason": "provider_fallback",
                            })
                            try:
                                decision = await self.orchestrator.router.decide_fallback(
                                    system, contents, tools_for_call, needs_tools=bool(tools_for_call),
                                    surface=ctx["surface"],
                                )
                                active_protocol = "openai"
                                openai_messages = decision["openai_messages"]
                                logger.info(f"Fallback succeeded via {decision.get('model_used', 'unknown')}")
                                # Recovered — flip the frontend back to the normal
                                # THINKING label now that a working model answered.
                                await self.orchestrator.bus.emit(Events.RESPONSE_STATUS_CHANGED, {
                                    "session_id": ctx["session_id"],
                                    "status": "thinking",
                                    "reason": None,
                                })
                            except Exception as fallback_e:
                                logger.error(f"Gemini failed AND fallback chain exhausted: {e} / {fallback_e}")
                                ctx["raw_reply"] = "All AI providers are currently busy or exhausted — please try again in a moment."
                                ctx["model_used"] = active_model
                                ctx["skill_name"] = skill_name
                                ctx["skill_output"] = skill_output
                                return ctx
                else:
                    openai_tools = gemini_declarations_to_openai_tools(tools_for_call) if tools_for_call else None
                    try:
                        decision = await self.orchestrator.router.continue_openai(active_model, openai_messages, openai_tools)
                    except QuotaExceeded:
                        # This specific fallback model also ran dry mid-turn.
                        # Passing openai_messages here (not just contents) is
                        # what makes this an actual re-walk of the chain from
                        # the CURRENT conversation state — see decide_fallback's
                        # docstring for why the old contents-only call was
                        # silently discarding every tool call/response made
                        # since the first fallback switch.
                        await self.orchestrator.bus.emit(Events.RESPONSE_STATUS_CHANGED, {
                            "session_id": ctx["session_id"],
                            "status": "working_on_it",
                            "reason": "provider_fallback",
                        })
                        # Terminal: retry the terminal-first chain (GPT OSS, then
                        # DeepSeek, then Llama 3.1) from the current conversation
                        # before the general walk.
                        decision = None
                        if ctx["surface"] == "terminal":
                            try:
                                decision = await self.orchestrator.router.decide_terminal_first(
                                    system, contents, tools_for_call, needs_tools=bool(tools_for_call),
                                    openai_messages=openai_messages,
                                )
                            except Exception as tf_e:
                                logger.warning(f"terminal-first chain exhausted mid-turn: {tf_e}")
                                decision = None
                        if decision is None:
                            decision = await self.orchestrator.router.decide_fallback(
                                system, contents, tools_for_call, needs_tools=bool(tools_for_call),
                                openai_messages=openai_messages, surface=ctx["surface"],
                            )
                        openai_messages = decision["openai_messages"]
                        await self.orchestrator.bus.emit(Events.RESPONSE_STATUS_CHANGED, {
                            "session_id": ctx["session_id"],
                            "status": "thinking",
                            "reason": None,
                        })
            except Exception as e:
                # Reached only once the fallback chain itself
                # (decide_fallback) has genuinely been walked and every
                # candidate failed or was out of quota.
                #
                # BUGFIX 2026-09-02: `{e}` renders as an empty string for
                # any exception whose __str__ carries no message (a bare
                # StopIteration is the confirmed real-world case — see
                # continue_openai's fix above — but this could hit ANY
                # such exception, not just that one). Logging just the
                # type name as a fallback means this line is never
                # silently blank again, even for an exception class this
                # comment didn't anticipate.
                _err_detail = str(e) or f"{type(e).__name__} (no message)"
                logger.error(f"All providers exhausted or failed: {_err_detail}", exc_info=True)
                ctx["raw_reply"] = "All AI providers are currently busy or exhausted — please try again in a moment."
                ctx["model_used"] = "failed"
                ctx["skill_name"] = skill_name
                ctx["skill_output"] = skill_output
                return ctx

            active_model = decision["model_used"]

            # Apply a preferred-model protocol switch queued above (see
            # the USER-SELECTED MODEL block's pending_protocol_switch
            # comment) now that decision has been fully consumed for
            # THIS iteration — this is what makes the NEXT iteration
            # correctly call continue_openai instead of decide_gemini,
            # same effective timing as decide_fallback's own switch.
            if pending_protocol_switch:
                active_protocol = pending_protocol_switch
                openai_messages = pending_openai_messages
                pending_protocol_switch = None
                pending_openai_messages = None

            if not decision.get("tool_call"):
                # No tool call — model is done, this is the final reply.
                final_text = decision.get("text") or ""

                async def _emit_token(chunk: str) -> None:
                    await self.orchestrator.bus.emit(Events.TOKEN_STREAM, {
                        "session_id": ctx["session_id"],
                        "token": chunk,
                    })

                # OADSON V3 Intelligent Response State EDD §8 — the one
                # streaming->non-streaming fallback path provider_router.py
                # handles entirely internally (see stream_final_answer's
                # on_status param). Mirrors _emit_token's shape/pattern.
                async def _emit_status(status: str, reason: str) -> None:
                    await self.orchestrator.bus.emit(Events.RESPONSE_STATUS_CHANGED, {
                        "session_id": ctx["session_id"],
                        "status": status,
                        "reason": reason,
                    })

                # ADDED 2026-09-19 — see Events.STREAM_RESET and
                # provider_router.stream_final_answer's on_reset. Fires only
                # if a stream that already sent tokens failed part-way, so
                # the client can drop the partial text before the replacement
                # answer arrives (otherwise it renders the answer twice).
                async def _emit_reset(reason: str) -> None:
                    logger.warning(f"STREAM_RESET session={ctx['session_id']} reason={reason}")
                    await self.orchestrator.bus.emit(Events.STREAM_RESET, {
                        "session_id": ctx["session_id"],
                        "reason": reason,
                    })

                stream_tokens = ctx.get("stream_tokens", True)

                if active_protocol == "gemini" and stream_tokens and not decision_from_local:
                    # Lite mode: inject evidence ledger into system before streaming
                    # so the model only claims what it can prove from tool receipts.
                    evidence_system = system + evidence.tool_receipt_block()
                    try:
                        streamed = await self.orchestrator.router.stream_final_answer(
                            model_id=active_model,
                            system=evidence_system,
                            contents=contents,
                            on_token=_emit_token,
                            on_status=_emit_status,
                            on_reset=_emit_reset,
                        )
                        if streamed.strip():
                            final_text = streamed
                    except Exception as e:
                        logger.warning(f"stream_final_answer failed ({e}), using decide text")
                        if final_text:
                            await _emit_token(final_text)
                else:
                    # Turbo: evidence already in system prompt from decide call.
                    # Also the path local_qwen_cli answers take — its text is
                    # already complete from the one call in try_local_qwen_cli,
                    # nothing left to stream from a second provider call.
                    if final_text:
                        await _emit_token(final_text)

                # ── Hallucination Reviewer ────────────────────────────────
                # Lightweight Gemini call that checks every factual claim
                # against the evidence ledger. Non-blocking — any error
                # returns PASS so the original answer is never lost.
                # Skip for sticker/image-only messages — the reviewer
                # treating image descriptions as unverified factual claims
                # causes false positives on sticker batches.
                is_sticker_context = ctx.get("attachment_type") == "image/webp"
                if final_text.strip() and evidence.has_failures() and not is_sticker_context:
                    try:
                        api_key = os.getenv("GEMINI_API_KEY", "")
                        review = await review_answer(final_text, evidence, api_key)
                        final_text = apply_rewrite_instruction(final_text, review)
                        ctx["hallucination_review"] = review
                        if review.get("verdict") == "REWRITE":
                            logger.warning(
                                f"Hallucination reviewer flagged: "
                                f"{review.get('unsupported_claims')} "
                                f"(confidence {review.get('confidence')}%)"
                            )
                    except Exception as e:
                        logger.warning(f"Reviewer failed (non-fatal): {e}")

                # ── Document-Grounding Check (Trustworthy Task State) ─────
                # ADDED 2026-08-16, built and tested directly against a real
                # production transcript: a document_reader call SUCCEEDED
                # (real text extracted, no tool error), but the model's
                # final answer confidently mislabeled which module a real
                # section belonged to, then on correction FABRICATED a
                # second, entirely invented module title. Neither error was
                # ever caught by the reviewer above, because that reviewer
                # is gated on evidence.has_failures() — a TOOL failure flag.
                # This was never a tool failure. document_reader worked
                # perfectly; the model's INTERPRETATION of a successful
                # result was wrong. That is a structurally different
                # failure the existing firewall was never built to catch,
                # so it needs its own independent trigger, not a shared gate.
                #
                # Deliberately mechanical (regex-based structure extraction
                # + substring/fuzzy text matching), not another model call
                # judging the first model's claim — an LLM checking an LLM
                # reintroduces the same failure mode this exists to close.
                # See core/document_grounding.py's module docstring for the
                # full real transcript this was built and tested against.
                #
                # Non-blocking and additive by design: any error here falls
                # through silently and the original answer is unaffected,
                # same safety posture as the reviewer above. This does NOT
                # replace it — the two catch different failure classes and
                # both should run.
                try:
                    doc_reader_receipts = [
                        r for r in evidence.receipts
                        if r.get("tool") == "document_reader" and r.get("status") not in ("error", "timeout", "blocked")
                    ]
                    if doc_reader_receipts and final_text.strip():
                        # "Module N" / "Module N:" style claims in the final
                        # answer — the exact shape of claim that broke in
                        # the real transcript.
                        # BUG FOUND DURING TESTING 2026-08-16: the first
                        # version of this regex only matched the formal
                        # "Module 3: Title" / "Module 3 - Title" shape and
                        # completely missed natural prose — which is exactly
                        # how the real fabrication was phrased ("Module 3
                        # focuses on the Peoples of Northern Nigeria...").
                        # Verified against the actual transcript sentence
                        # before shipping: the original pattern extracted
                        # ZERO claims from it, which would have made this
                        # entire check silently inert on the real failure
                        # case it exists to catch. Fixed by matching "Module
                        # N" as its own anchor, then taking a wider capped
                        # window of the sentence/clause that follows —
                        # regardless of colon, dash, or ordinary prose
                        # connecting words — as the claimed-topic text to
                        # check, rather than requiring a specific
                        # punctuation shape immediately after the number.
                        module_claims = []
                        for m in re.finditer(r"[Mm]odule\s+(\d+)\b", final_text):
                            claimed_num = m.group(1)
                            # Take up to ~150 chars following the module
                            # number as the claimed-topic window — covers
                            # "Module 3: X", "Module 3 - X", and "Module 3
                            # focuses on X" alike.
                            window = final_text[m.end():m.end() + 150]
                            # Trim to the first sentence boundary so the
                            # window doesn't bleed into unrelated later text.
                            window = re.split(r"[.\n]", window, maxsplit=1)[0]
                            # Strip leading connector words/punctuation
                            # ("focuses on", ":", "-", "is titled", etc.) so
                            # what's left is the actual topic being claimed.
                            window = re.sub(
                                r"^\s*(?:[:\-]|focuses on|is titled|covers|is about|discusses)\s*",
                                "", window, flags=re.IGNORECASE
                            ).strip()
                            if window and len(window) >= 8:
                                module_claims.append((claimed_num, window))
                        if module_claims:
                            # Pull the source text this turn's document_reader
                            # call actually returned, to check claims against
                            # the REAL evidence for this turn, not a stale
                            # cached document from earlier in the session.
                            source_text = ""
                            for step in ctx.get("steps", []):
                                if step.get("skill") == "document_reader":
                                    result = step.get("result") or {}
                                    data = result.get("data") if isinstance(result, dict) else None
                                    source_text += (data or {}).get("content", "") if isinstance(data, dict) else ""
                            grounding_issues = []
                            grounding_passes = []
                            for claimed_num, claimed_title in module_claims[:5]:  # cap — avoid pathological long replies
                                gr = check_module_claim(claimed_num, claimed_title.strip(), source_text)
                                if gr.status == "not_grounded":
                                    grounding_issues.append(gr)
                                elif gr.status == "grounded":
                                    grounding_passes.append(gr)

                            # ── Trustworthy Task State: connect grounding to requirements ──
                            # ADDED 2026-08-16. This is the actual EVIDENCE-based
                            # verification Section 13/21 of the EDD describes —
                            # document_grounding IS the deterministic check for
                            # any requirement shaped like "correctly explain/
                            # summarize document content". Without this
                            # connection the grounding check and task_state
                            # were two parallel, disconnected systems: one
                            # could catch the fabrication, the other had no
                            # idea it happened. This does simple text-overlap
                            # matching between the requirement description and
                            # the claim (not a model call — see amendment 4 /
                            # BOOKKEEPING_TIER) since real requirement-to-step
                            # ID traceability (Section 11) isn't built yet.
                            if ctx.get("task_state"):
                                try:
                                    from core.task_state import EvidenceType as _EvidenceType
                                    for req in ctx["task_state"].requirements:
                                        req_desc_lower = req.description.lower()
                                        if "module" not in req_desc_lower and "explain" not in req_desc_lower and "teach" not in req_desc_lower:
                                            continue  # only touch requirements that plausibly relate to document content
                                        for gr in grounding_issues:
                                            req.mark_failed(_EvidenceType.DOCUMENT_GROUNDING, gr.reason)
                                        for gr in grounding_passes:
                                            if req.status.value != "completed":
                                                req.mark_verified(_EvidenceType.DOCUMENT_GROUNDING, gr.reason)
                                except Exception as e:
                                    logger.warning(f"Task state grounding link failed (non-fatal): {e}")

                            if grounding_issues:
                                ctx["document_grounding_issues"] = [
                                    {"claim": g.claim, "reason": g.reason} for g in grounding_issues
                                ]
                                logger.warning(
                                    f"Document grounding check FAILED for {len(grounding_issues)} "
                                    f"claim(s) — model asserted module content not found in the "
                                    f"actual source text: {[g.claim for g in grounding_issues]}"
                                )
                                # Prepend an honest, visible caveat rather than
                                # silently rewriting the model's words (which
                                # risks producing a second, different wrong
                                # answer with no source of truth to rewrite
                                # from — exactly what happened on the second
                                # fabrication attempt in the real transcript).
                                caveat = (
                                    "\n\n⚠️ I need to flag something: at least one claim above about "
                                    "which module contains what didn't check out against the document "
                                    "text I actually read. Before trusting this, ask me to show you the "
                                    "document's real structure directly."
                                )
                                final_text = final_text.rstrip() + caveat
                except Exception as e:
                    logger.warning(f"Document grounding check failed (non-fatal): {e}")

                # ── Self-Review: check claims about generated files ───────
                # ADDED 2026-08-16. Real production failure, twice on the
                # same request: "No timer. No next button. No submit
                # button." — none of this was true; nothing had actually
                # changed in the file. This gives OADSON the same reflex
                # asked of it here in this chat: read the artifact back and
                # check the specific, checkable claim against it before the
                # claim goes out, rather than trusting the model's own
                # description of what it just did.
                #
                # Scoped to the skills that actually write a sandbox file
                # this turn (terminal writes, file_generation, website_
                # developer) — reads the artifact straight from the
                # sandbox, not from the skill's own self-reported summary.
                try:
                    file_writing_receipts = [
                        r for r in evidence.receipts
                        if r.get("tool") in ("terminal", "file_generation", "website_developer")
                        and r.get("status") not in ("error", "timeout", "blocked")
                    ]
                    if file_writing_receipts and final_text.strip():
                        from core.oadson_home import get_user_home
                        home_dir = get_user_home(ctx["user_id"])
                        # Find any sandbox-relative filename mentioned in
                        # this turn's steps (skills report the path they
                        # wrote in their result dict under varying keys —
                        # check the common ones rather than assuming one).
                        candidate_paths = set()
                        for step in ctx.get("steps", []):
                            result = step.get("result") or {}
                            data = result.get("data") if isinstance(result, dict) else result
                            if isinstance(data, dict):
                                for key in ("file_path", "filename", "path"):
                                    val = data.get(key)
                                    if isinstance(val, str) and val:
                                        candidate_paths.add(val)
                        self_review_issues = []
                        issue_paths = {}  # claim.claim -> rel_path, for repair targeting
                        for rel_path in candidate_paths:
                            results = review_before_reply(rel_path, home_dir, final_text)
                            for r in results:
                                if r.status == "contradicted":
                                    self_review_issues.append(r)
                                    issue_paths[r.claim] = rel_path

                        if self_review_issues:
                            logger.warning(
                                f"Self-review FAILED for {len(self_review_issues)} claim(s) about "
                                f"generated file — reply claimed a change that isn't actually in the "
                                f"file: {[r.claim for r in self_review_issues]}"
                            )

                            # ── Attempt actual repair, not just a caveat ──
                            # ADDED 2026-08-16. Designed together with the
                            # architect against 3 named blockers (see
                            # _attempt_self_repair's docstring for the full
                            # resolution of each). Only the FIRST flagged
                            # claim is sent to repair — architect's explicit
                            # choice to keep this cheap and scoped rather
                            # than attempting to fix everything at once.
                            first_issue = self_review_issues[0]
                            # Which skill produced the specific file this
                            # claim is about — falls back to the first
                            # file-writing skill this turn if that mapping
                            # isn't cleanly recoverable (multiple skills
                            # could have run). Simpler and more honest than
                            # a fragile string-match against serialized
                            # result dicts, which was the first draft of
                            # this and not something I'd trust blind.
                            repair_skill = file_writing_receipts[0]["tool"]
                            repair_outcome = await self.orchestrator._attempt_self_repair(
                                ctx=ctx, evidence=evidence, base_payload=base_payload,
                                skill_name=repair_skill, rel_path=issue_paths[first_issue.claim],
                                home_dir=home_dir, failed_claim=first_issue, original_reply=final_text,
                            )

                            if repair_outcome["repaired"]:
                                # Real fix landed and re-verified — the
                                # ORIGINAL claim is now actually true, so no
                                # caveat is needed at all. Quietly note it
                                # happened for the audit trail (steps),
                                # never announce a repair the user didn't
                                # ask to see the mechanics of.
                                self.orchestrator._add_step(
                                    ctx, "repair",
                                    f"\u2713 Self-repair succeeded after {repair_outcome['attempts']} "
                                    f"attempt(s): {first_issue.claim}"
                                )
                                if repair_outcome.get("new_regressions"):
                                    # Repair succeeded on the original claim
                                    # but incidentally revealed something
                                    # else contradicted — disclose that
                                    # honestly even though the main issue
                                    # is fixed (architect's explicit choice:
                                    # never hide a new regression found
                                    # along the way).
                                    other = "; ".join(r.claim for r in repair_outcome["new_regressions"])
                                    final_text = final_text.rstrip() + (
                                        f"\n\n⚠️ Fixed that, but while checking I also noticed this may "
                                        f"not be right: {other}. Want me to look at that too?"
                                    )
                            else:
                                # Repair genuinely failed after the attempt
                                # cap — honest disclosure of BOTH the
                                # original issue and anything new surfaced,
                                # per the architect's explicit choice.
                                unresolved = "; ".join(r.claim for r in repair_outcome["unresolved_claims"])
                                new_ones = repair_outcome.get("new_regressions") or []
                                caveat = (
                                    f"\n\n⚠️ I claimed \"{unresolved}\" above, but that still isn't true "
                                    f"after {repair_outcome['attempts']} attempt(s) to fix it — checking "
                                    f"the actual file confirms it. I don't want to keep claiming this is "
                                    f"done when it isn't."
                                )
                                if new_ones:
                                    caveat += (
                                        " Also, while trying to fix it, I may have introduced a separate "
                                        f"problem: {'; '.join(r.claim for r in new_ones)}."
                                    )
                                final_text = final_text.rstrip() + caveat
                except Exception as e:
                    logger.warning(f"Self-review check failed (non-fatal): {e}")

                if not final_text.strip():
                    # BUG FIXED 2026-08-21 — confirmed real report: the
                    # model can genuinely finish its tool-calling loop
                    # with real, successful skill results already sitting
                    # in ctx["steps"] (a search that found the right
                    # entries, a delete that ran) but produce ZERO text
                    # for its actual final answer. That used to become
                    # this same canned "I worked through the task but
                    # generated an empty response. Try rephrasing." line
                    # every single time — completely discarding whatever
                    # the tools actually did, forcing the user to ask
                    # again from scratch with no idea what (if anything)
                    # happened. Build a real summary from the tool
                    # results that already exist instead of throwing them
                    # away — the work happened, the user should see it.
                    skill_steps = [s for s in ctx.get("steps", []) if s.get("type") == "skill"]
                    if skill_steps:
                        lines = []
                        for s in skill_steps:
                            ok = s.get("result_status") != "error"
                            mark = "✅" if ok else "❌"
                            summary = s.get("result_summary") or s.get("message") or "done"
                            lines.append(f"{mark} @{s.get('skill','?')}: {summary}")
                        final_text = (
                            "I ran the following but didn't write a proper summary — "
                            "here's exactly what happened:\n\n" + "\n".join(lines)
                        )
                    else:
                        # FIX 2026-08-24: this was the remaining gap in the
                        # Aug 21 fix above — that one only covers "tools ran,
                        # but produced no final text." When skill_steps is
                        # ALSO empty (the model got stuck and never called
                        # anything this turn), it fell through to this exact
                        # same flat "try rephrasing" line with zero
                        # diagnostic value, repeatedly, on every retry —
                        # confirmed directly from a real report: the same
                        # ambiguous, pronoun-heavy instruction ("add it to
                        # X, apart from Y, using that same Z") produced this
                        # placeholder three separate times across ~10
                        # minutes and multiple model fallbacks (3.6-flash,
                        # 3.5-flash, 3.5-flash-lite all hit the same wall),
                        # meaning it's genuinely the instruction's ambiguity
                        # causing the model to stall, not a per-model quirk.
                        # Naming that plainly is more actionable than a
                        # generic "try rephrasing" that doesn't tell the
                        # user WHAT to rephrase or why nothing happened.
                        final_text = (
                            "I couldn't turn that into a concrete action — I didn't call any "
                            "tools or produce an answer this turn. This usually means the "
                            "instruction was ambiguous to act on directly (e.g. relying on "
                            "\"it\"/\"that\" from earlier context, or describing what NOT to do "
                            "rather than exactly what to do). Try stating it as a direct, "
                            "self-contained instruction — the specific file(s), the exact change, "
                            "in one sentence — and I'll act on it."
                        )
                final_text = self.orchestrator._strip_raw_tool_dump(final_text)
                if surface in ("whatsapp", "telegram", "terminal"):
                    # ADDED 2026-09-08 — presentation_guard's prompt
                    # instruction not to use LaTeX on these surfaces was
                    # confirmed live to still leak through on WhatsApp.
                    # Same "prompt alone isn't 100% reliable" conclusion
                    # _strip_raw_tool_dump already reached for the JSON-
                    # dump problem, applied here as a second safety net.
                    final_text = self.orchestrator._strip_latex_for_nonrendering_surface(final_text)

                # ── Trustworthy Task State: completion gate ──────────────
                # ADDED 2026-08-16, per EDD Section 20/32. "OADSON must
                # NEVER declare a complex task complete merely because the
                # final model response says it is complete."
                #
                # HONEST SCOPE NOTE: a full implementation would re-enter
                # the planning loop here to actually repair an incomplete/
                # regressed requirement before returning anything to the
                # user (Section 18's self-repair loop). That's a materially
                # bigger structural change — making this return point
                # recursive risks interacting badly with the existing
                # step-budget logic and retry paths in ways that need
                # their own dedicated testing pass, not a same-night bolt-on
                # to a working pipeline. Tonight's implementation does the
                # safe, still-honest half of Section 20: it NEVER lets an
                # incomplete/regressed task state be silently reported as
                # fully successful — if the gate isn't satisfied, the user
                # is told exactly that, plainly, rather than the reply
                # claiming unqualified success. Wiring the actual repair
                # loop is the next pass, not this one.
                if ctx.get("task_state") and ctx["task_state"].requirements:
                    try:
                        ts = ctx["task_state"]
                        if not ts.is_complete():
                            unmet = [
                                r for r in ts.mandatory_requirements()
                                if r.status.value not in ("completed", "skipped", "cancelled")
                            ]
                            regressed = [r for r in ts.requirements if r.status.value == "regressed"]
                            if unmet or regressed:
                                gate_note_parts = []
                                if regressed:
                                    gate_note_parts.append(
                                        f"{len(regressed)} thing(s) that were working may have regressed: "
                                        + "; ".join(r.description[:80] for r in regressed)
                                    )
                                if unmet:
                                    gate_note_parts.append(
                                        f"{len(unmet)} requirement(s) not yet verified: "
                                        + "; ".join(r.description[:80] for r in unmet[:3])
                                    )
                                logger.warning(
                                    f"Completion gate NOT satisfied for {ctx['session_id']}: "
                                    f"{'; '.join(gate_note_parts)}"
                                )
                                ctx["task_state_incomplete"] = gate_note_parts
                    except Exception as e:
                        logger.warning(f"Completion gate check failed (non-fatal): {e}")

                # TRACE MODE 2026-08-20 — REVERTED. This used to flatten
                # every "talk" step from the whole turn into one joined
                # block and glue it onto the front of final_text. That
                # looked fine as a one-off but broke the actual point of
                # Trace Mode on reload: live streaming interleaves each
                # talk fragment right next to the skill call it's talking
                # about (talk -> skill -> talk -> skill -> ...); flattening
                # them all into one paragraph ahead of the real answer
                # collapsed that into "one big monologue, then the
                # timeline, then the answer" — structurally unable to
                # look interleaved no matter what the frontend does with
                # it, since the ordering information was already discarded
                # by the time it got to `content`.
                #
                # ctx["steps"] already has talk/skill/result in true
                # chronological order (see _add_step, called in call
                # order throughout _plan_and_execute) — that IS the
                # interleaved record, with no reconstruction needed. So:
                # leave final_text as just the real final answer, and let
                # chat.html's history renderer walk steps in order to
                # rebuild the same interleaved view live streaming shows,
                # instead of pre-flattening here and losing that order.
                # See chat.html's buildExecTimelineHtml / appendMsg for
                # the matching frontend change.

                ctx["raw_reply"] = final_text
                ctx["model_used"] = active_model
                ctx["skill_name"] = skill_name
                # MIGRATION 2026-08-21 — see ChatContext.best_download_result's
                # docstring. This turn's ctx.download_result was already
                # kept correct throughout the loop by note_skill_result();
                # this just makes ctx.skill_output reflect the same
                # "prefer a download-bearing result" choice at the exit
                # point _finalize's safety net actually reads.
                ctx["skill_output"] = ctx.best_download_result()
                self.orchestrator._add_step(ctx, "generate", f"Response via {active_model} ({tool_call_count} tool calls)")
                return ctx

            # Model wants to call a skill — decoded uniformly regardless
            # of which provider answered.
            skill_name = decision["tool_call"]["name"]
            call_args = decision["tool_call"]["args"]

            # ── TRACE MODE 2026-08-20 ────────────────────────────────────────
            # Emit any interstitial text the model produced ALONGSIDE this
            # tool call (captured now by provider_router.py's _call_gemini
            # fix — previously always None). This is the model's own real
            # words, generated by the same decide call and same system
            # prompt as everything else this turn — not a separate
            # narration template. Deliberately placed BEFORE the duplicate-
            # call guard below: even on a duplicate call we still want to
            # emit any genuine talk that came with it (the model may be
            # explaining a retry), but we emit it once per decide() call,
            # not once per skill execution, so it can't double-fire from
            # a retry loop the way a skill-execution-scoped emission could.
            #
            # Surface-gated to web/app only — terminal/whatsapp/telegram
            # get zero behavior change, this whole block is a no-op for
            # them (decision.get("text") is simply never read on those
            # surfaces). Also gated on ctx.get("enable_reasoning") — Trace
            # mode is what "reasoning on" now means for web/app (see
            # chat.html's default-on change), so a user who explicitly
            # turned reasoning off doesn't get interstitial talk either,
            # matching the same on/off semantics the toggle already had.
            interstitial_text = (decision.get("text") or "").strip()
            # DEBUG LOGGING 2026-08-20 — added because prod logs before/
            # after this feature look identical: there was no line
            # anywhere that confirms whether the model even produced
            # interstitial text, or which gate condition blocked it if
            # so. Logs unconditionally (not just on the happy path) so a
            # "nothing happened" report is actually diagnosable from
            # Render logs alone — look for "TRACE" lines around the
            # skill call in question.
            logger.info(
                f"TRACE decide() for skill={skill_name} session={ctx['session_id']}: "
                f"text_len={len(interstitial_text)} surface={surface!r} "
                f"enable_reasoning={ctx.get('enable_reasoning')!r} "
                f"raw_decision_keys={list(decision.keys())} "
                # Flags whether the TEMP TEST OVERRIDE (mandatory narration,
                # see _build_system_prompt) was live for this call — so a
                # log read later can't mistake a test-mode result for
                # normal-mode behavior, or vice versa. Remove this field
                # alongside the override block once testing is done.
                f"temp_override_active={surface in ('web','app') and bool(ctx.get('enable_reasoning'))}"
            )
            if interstitial_text and surface in ("web", "app") and ctx.get("enable_reasoning"):
                logger.info(f"TRACE emitting talk ({len(interstitial_text)} chars) for session={ctx['session_id']}")
                self.orchestrator._add_step(ctx, "talk", interstitial_text)
                await self.orchestrator.bus.emit(Events.TRACE_TALK, {
                    "session_id": ctx["session_id"],
                    "text": interstitial_text,
                })
            elif interstitial_text:
                logger.info(
                    f"TRACE text present but gated OUT — surface={surface!r} "
                    f"enable_reasoning={ctx.get('enable_reasoning')!r} session={ctx['session_id']}"
                )

            # ── Duplicate call guard ──────────────────────────────────────────
            # Gemini sometimes re-issues the exact same function call on the
            # next loop iteration, causing skills to run twice (double clone,
            # double push, etc.). Detect and skip exact duplicates within the
            # same turn. Use a fingerprint of skill_name + sorted args JSON.
            #
            # BUGFIX 2026-08-31: call_id must be generated BEFORE this guard,
            # not after it (as it previously was, further down at what's now
            # this comment's original line). The duplicate/hard-stop branches
            # below both reference call_id for the OpenAI-protocol tool
            # response — with it assigned only after a possible `continue`
            # here, a duplicate hit would reuse whatever call_id was last set
            # by the PREVIOUS loop iteration's real call (Python doesn't
            # scope a for-loop body's local variables per iteration), sending
            # a tool response with the wrong tool_call_id attached. That's
            # exactly the kind of id/count mismatch that caused the "not the
            # same number of function calls and responses" class of bug
            # documented elsewhere in this file — generating it once, up
            # front, for every call (real, duplicate, or hard-stopped alike)
            # closes that gap instead of only fixing it for the real-call path.
            call_id = f"call_{openai_call_counter + 1}"
            openai_call_counter += 1

            _call_fp = f"{skill_name}:{json.dumps(call_args, sort_keys=True)}"
            if "_executed_calls" not in ctx:  # ctx is a dict — use 'in', not hasattr
                ctx["_executed_calls"] = set()
            if _call_fp in ctx["_executed_calls"]:
                logger.warning(f"Duplicate skill call detected and skipped: {_call_fp[:80]}")
                # Tell Gemini the call was already done so it moves on
                _dup_msg = f"Already executed {skill_name} with these exact args this turn — result unchanged."
                if active_protocol == "gemini":
                    # FIX 2026-08-28: functionResponse parts must be sent
                    # with role "user" (confirmed against every official
                    # Gemini generateContent example — see get-started,
                    # migrate-to-interactions, and the Gemini 3 guide,
                    # which use 'user' or the newer 'tool' alias; the raw
                    # v1beta REST contents.role field only accepts
                    # user/model, so 'user' is the correct wire value
                    # here). This was "role": "model" — putting TWO
                    # consecutive "model" turns back to back (the echoed
                    # functionCall turn, then this) breaks the strict
                    # user/model alternation Gemini's request validator
                    # checks, which is almost certainly the real cause of
                    # the "Function call is missing a thought_signature"
                    # 400s seen in production (the validator hits the
                    # broken structure and reports the symptom it checks
                    # first, not the actual role bug). See the matching
                    # fixes at lines ~927 (functionResponse for blocked/
                    # not-found skill) and the real skill-result append
                    # near the bottom of the loop.
                    #
                    # BUGFIX 2026-09-02: this branch (and the hard-stop
                    # guard right below it) took `continue` WITHOUT ever
                    # appending the model's own turn for THIS call first —
                    # meaning the functionCall that triggered the
                    # duplicate/blocked path, thoughtSignature and all,
                    # was silently dropped from `contents` entirely, not
                    # just replaced with a stripped-down version (that
                    # was already fixed elsewhere — this is a different,
                    # deeper miss: no model turn at all for this call).
                    # Confirmed as the real cause of a production 400
                    # ("Function call is missing a thought_signature")
                    # that hit github several calls AFTER a run of
                    # terminal blocks — once one call in the sequence has
                    # no matching model-turn echo, every later request
                    # sends a conversation history with a missing
                    # thoughtSignature reference, and EVERY model
                    # (confirmed: 3 different ones in the same log)
                    # rejects it identically, since the malformed
                    # `contents` is the actual defect, not anything
                    # model-specific. Same echo_parts pattern as the real
                    # execution path below — echo the model's actual turn
                    # first, exactly as received, before the synthetic
                    # functionResponse.
                    _echo_parts = decision.get("raw_parts") or (
                        [decision["raw_part"]] if decision.get("raw_part") else None
                    ) or [{"functionCall": {"name": skill_name, "args": call_args}}]
                    contents.append({"role": "model", "parts": _echo_parts})
                    contents.append({"role": "user", "parts": [{"functionResponse": {"name": skill_name, "response": {"status": "duplicate_skipped", "message": _dup_msg}}}]})
                else:
                    openai_messages.append({
                        "role": "assistant", "content": decision.get("text") or None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                         "function": {"name": skill_name, "arguments": json.dumps(call_args)}}],
                    })
                    openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"status": "duplicate_skipped", "message": _dup_msg})})
                continue

            # ── Same-turn hard-stop guard (pre-execution) ───────────────────
            # HARDENING 2026-08-31: companion to the failure-streak tracking
            # further below (after this skill actually runs) — this half
            # stops it from running AGAIN once the streak already crossed
            # the ceiling on a PRIOR call this turn. Mirrors the duplicate
            # guard immediately above: same fingerprint-and-skip shape, same
            # continue-without-executing behavior, just keyed on skill_name
            # + a failure streak instead of exact-args equality — this is
            # the actual enforcement; the __instruction__ text added later in
            # this loop is the advisory half but a small/fast model can
            # ignore plain text, it can't ignore the call simply not running.
            SAME_TURN_FAILURE_CEILING = 2
            failure_streaks = ctx.setdefault("_same_turn_failure_streak", {})
            if failure_streaks.get(skill_name, 0) > SAME_TURN_FAILURE_CEILING:
                logger.warning(f"Hard-stopping repeated failing skill '{skill_name}' — {failure_streaks[skill_name]} consecutive failures this turn already.")
                _stop_msg = (
                    f"@{skill_name} is blocked for the rest of this turn — it has failed "
                    f"{failure_streaks[skill_name]} times in a row already with different attempts. "
                    "Stop trying variations of this call. Tell the user what failed and either ask "
                    "how to proceed or use a genuinely different tool for the underlying goal."
                )
                if active_protocol == "gemini":
                    # BUGFIX 2026-09-02 — same missing-echo bug as the
                    # duplicate-call guard above, same fix: append the
                    # model's real turn (with its thoughtSignature) before
                    # the synthetic "blocked" functionResponse, instead of
                    # dropping this call's model-turn from history
                    # entirely. This specific guard is the one directly
                    # implicated in the production log this was traced
                    # from — a 3rd terminal call hit this exact branch
                    # (SAME_TURN_FAILURE_CEILING=2, so the 3rd consecutive
                    # failure trips it), its thoughtSignature was silently
                    # dropped, and a later github call in the same turn
                    # then failed on every model tried with "missing a
                    # thought_signature" — the conversation history was
                    # already malformed several calls earlier.
                    _echo_parts = decision.get("raw_parts") or (
                        [decision["raw_part"]] if decision.get("raw_part") else None
                    ) or [{"functionCall": {"name": skill_name, "args": call_args}}]
                    contents.append({"role": "model", "parts": _echo_parts})
                    contents.append({"role": "user", "parts": [{"functionResponse": {"name": skill_name, "response": {"status": "blocked", "message": _stop_msg}}}]})
                else:
                    openai_messages.append({
                        "role": "assistant", "content": decision.get("text") or None,
                        "tool_calls": [{"id": call_id, "type": "function",
                                         "function": {"name": skill_name, "arguments": json.dumps(call_args)}}],
                    })
                    openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"status": "blocked", "message": _stop_msg})})
                continue

            ctx["_executed_calls"].add(_call_fp)
            # Echo model turn back into whichever conversation
            # representation is currently active (Gemini's contents, or
            # the OpenAI-shape messages list if we've fallen back).
            # call_id/openai_call_counter already assigned above, before
            # the duplicate/hard-stop guards — see that comment for why.
            if active_protocol == "gemini":
                # Must echo Gemini's ORIGINAL functionCall part back
                # verbatim, not a reconstructed {name, args}-only version.
                # Gemini 3-series models attach a "thoughtSignature" to
                # functionCall parts to preserve reasoning continuity
                # across a multi-step tool sequence, and REJECT the next
                # request with a 400 ("Function call is missing a
                # thought_signature...") if that field isn't echoed back
                # exactly as received. core.provider_router.py's
                # decide_gemini() already captures the full original part
                # as raw_part — this was silently discarding it and
                # rebuilding a stripped-down version instead, which is
                # exactly the bug in the logs (400 on the second tool call
                # in a sequence, right after the first one succeeded).
                #
                # WORDING CONTINUITY 2026-08-20 — this used to echo ONLY
                # raw_part (the functionCall alone), so any text Gemini
                # wrote alongside that same functionCall (the Trace Mode
                # talk fragment) never made it into `contents` — the
                # model's own conversation history. It could say "I'm
                # going to search Spotify..." live, then have zero memory
                # of having said that by the time it wrote the real final
                # answer a few seconds later, producing two genuinely
                # disconnected pieces of text instead of one continuous
                # train of thought. raw_parts (see _call_gemini) is
                # Gemini's FULL original parts array for that response —
                # echoing all of it back (not just the functionCall part)
                # means the model's next decision sees exactly what it
                # said, in its own words, the same way a human re-reading
                # their own sent message would. Falls back to raw_part
                # alone (old behavior) if raw_parts isn't present for any
                # reason, so this can't regress the thought_signature fix
                # above if something upstream ever stops sending it.
                echo_parts = decision.get("raw_parts") or (
                    [decision["raw_part"]] if decision.get("raw_part") else None
                ) or [{"functionCall": {"name": skill_name, "args": call_args}}]
                contents.append({"role": "model", "parts": echo_parts})
            else:
                # WORDING CONTINUITY 2026-08-20 — same fix as the gemini
                # branch above, mirrored for the OpenAI-shape fallback
                # conversation history: "content": None discarded any
                # text the fallback model wrote alongside its tool_call
                # (now captured — see continue_openai/decide_fallback's
                # "text" field), so the model had no memory of its own
                # talk sentence here either once a fallback provider took
                # over mid-turn.
                openai_messages.append({
                    "role": "assistant", "content": decision.get("text") or None,
                    "tool_calls": [{"id": call_id, "type": "function",
                                     "function": {"name": skill_name, "arguments": json.dumps(call_args)}}],
                })

            # NATIVE FUNCTION-CALLING 2026-08-23: a decided call to a LOCAL
            # tool (read_file, run_bash, etc. — see
            # core/local_tool_schemas.py) can't be executed here — only the
            # terminal client, running on the user's own machine, can run
            # these. Pause the loop and hand the pending call back to the
            # client instead of falling through to the "skill not found"
            # check below (which would incorrectly tell Gemini the tool
            # doesn't exist at all — it exists, just not on this server).
            # contents already includes the model's turn (echo_parts, just
            # appended above, verbatim including any thoughtSignature) —
            # that's the exact snapshot the client needs to carry forward
            # and send back once it's executed the tool locally and has a
            # real result to report. See core/response_pipeline.py for how
            # ctx["pending_tool_call"]/ctx["contents_snapshot"] short-
            # circuits normal persistence and gets surfaced in the API
            # response instead.
            if local_execution and skill_name in LOCAL_TOOL_NAMES:
                self.orchestrator._add_step(
                    ctx, "skill", f"@{skill_name} → client", skill=skill_name, args=call_args
                )
                ctx["pending_tool_call"] = {"name": skill_name, "args": call_args}
                ctx["contents_snapshot"] = contents
                return ctx

            # ADDED 2026-09-09 — ask_user_tray (see
            # core/interactive_tool_schemas.py's module docstring for why
            # this is a SEPARATE gate/set from LOCAL_TOOL_NAMES right
            # above, despite the shared pause-and-return-ctx shape).
            #
            # Deliberately its OWN if-block, not folded into the
            # LOCAL_TOOL_NAMES check above — different gate variable
            # (surface, not local_execution), different set
            # (INTERACTIVE_TOOL_NAMES, not LOCAL_TOOL_NAMES), and only
            # ONE of the two branches below actually pauses the loop the
            # way the local-tool block always does.
            #
            # web/app/pwa: genuinely pause, same short-circuit shape as
            # the local-tool block above — ctx["pending_tool_call"] +
            # ctx["contents_snapshot"], picked up by the SAME existing
            # orchestrator.py short-circuit (that check is already
            # generic on ctx.get("pending_tool_call"), not specific to
            # local tools — confirmed by reading it before relying on
            # this). The frontend distinguishes "terminal wants to run a
            # command" from "OADSON wants to ask a question" purely by
            # pending_tool_call["name"] — no new API field needed for
            # that distinction.
            #
            # Every other surface (telegram, whatsapp, cli/terminal):
            # there's no tray to render, so this does NOT pause — it
            # executes immediately, right here, formatting question +
            # lettered options as plain text and feeding it back as a
            # real functionResponse/tool result, exactly like any other
            # successful skill call. The loop then continues completely
            # normally toward a real final answer that INCLUDES this
            # question, which is what actually gets sent to the user —
            # same "A. .../B. ... — reply with a letter or tell me your
            # answer" text arriving as an ordinary message, not a new
            # message type these surfaces would need to learn.
            if skill_name in INTERACTIVE_TOOL_NAMES:
                if surface in ("web", "app", "pwa"):
                    # DEBUG LOGGING 2026-09-10 — added after a production
                    # session showed "TRACE emitting talk" (the interstitial
                    # narration above) followed by total silence for 43
                    # seconds, then the frontend 404ing on that session
                    # entirely. Every OTHER branch in this file logs
                    # something on the way through (see the duplicate-call
                    # guard, the hard-stop guard, the local-tool pause right
                    # above this block) — this one didn't, so there was no
                    # way to tell from Render logs alone whether this block
                    # ever actually ran, hung, or was reached at all. Logs
                    # unconditionally, mirroring the local-tool pause's own
                    # _add_step call immediately above.
                    logger.info(f"TRACE ask_user_tray pausing for tray — session={ctx['session_id']} question={call_args.get('question', '')[:80]!r} options={call_args.get('options')}")
                    self.orchestrator._add_step(
                        ctx, "skill", f"@{skill_name} → client", skill=skill_name, args=call_args
                    )
                    ctx["pending_tool_call"] = {"name": skill_name, "args": call_args}
                    ctx["contents_snapshot"] = contents
                    logger.info(f"TRACE ask_user_tray returning paused ctx — session={ctx['session_id']} pending_tool_call_set={bool(ctx.get('pending_tool_call'))}")
                    return ctx

                question = call_args.get("question", "")
                options = call_args.get("options", []) or []
                lettered = "\n".join(
                    f"{chr(65 + i)}. {opt}" for i, opt in enumerate(options)
                )
                plain_text_result = (
                    f"{question}\n\n{lettered}\n\n"
                    "Reply with a letter, or just tell me your answer."
                )
                self.orchestrator._add_step(
                    ctx, "skill", f"@{skill_name}", skill=skill_name, args=call_args,
                    result={"status": "success", "text": plain_text_result},
                )
                if active_protocol == "gemini":
                    contents.append({
                        "role": "user",
                        "parts": [{"functionResponse": {"name": skill_name, "response": {"text": plain_text_result}}}]
                    })
                else:
                    openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"text": plain_text_result})})
                continue

            # Block terminal skill on terminal surface — CLI handles locally
            if skill_name == "terminal" and surface == "terminal":
                block_msg = ("Terminal skill disabled on terminal surface. "
                             "Emit tool_call blocks for local execution.")
                if active_protocol == "gemini":
                    # FIX 2026-08-28: see role-fix comment above (duplicate
                    # call guard) — functionResponse must be role "user".
                    contents.append({
                        "role": "user",
                        "parts": [{"functionResponse": {"name": skill_name, "response": {"error": block_msg}}}]
                    })
                else:
                    openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": block_msg})})
                self.orchestrator._add_step(ctx, "skill", f"@{skill_name} blocked", skill=skill_name, error=block_msg)
                continue

            # Validate skill exists
            if not self.orchestrator.skills.has(skill_name):
                error_msg = f"Skill '{skill_name}' not found. Available: {', '.join(self.orchestrator.skills.names())}"
                if active_protocol == "gemini":
                    # FIX 2026-08-28: see role-fix comment above (duplicate
                    # call guard) — functionResponse must be role "user".
                    contents.append({
                        "role": "user",
                        "parts": [{"functionResponse": {"name": skill_name, "response": {"error": error_msg}}}]
                    })
                else:
                    openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": error_msg})})
                self.orchestrator._add_step(ctx, "skill", f"@{skill_name} not found", skill=skill_name, error=error_msg)
                continue

            # Was missing skill=/args= here entirely, and nothing ever
            # attached the actual result afterward — this step sat
            # forever as just {"type":"skill","message":"@X executing..."},
            # which is exactly why the Execution Timeline showed
            # "@unknown / Message: Completed" for every successful skill
            # call. Now captures the step so its real outcome can be
            # filled in below once exec_result is known.
            self.orchestrator._add_step(ctx, "skill", f"@{skill_name} executing...", skill=skill_name, args=call_args)
            step_ref = ctx["steps"][-1]
            await self.orchestrator.bus.emit(Events.TASK_STARTED, {
                "session_id": ctx["session_id"],
                "skill": skill_name,
                "args": call_args,
            })

            step_payload = {**base_payload, "args": call_args}
            if previous_result is not None:
                step_payload["previous_result"] = previous_result

            # Added 2026-08-09 for skill_reliability.py: no timing data
            # existed ANYWHERE in the execution pipeline before this —
            # the reliability report's "average latency" field literally
            # couldn't be computed without it. Two lines, same existing
            # event/collection, no new infra.
            _exec_start = time.monotonic()
            try:
                exec_result = await self.orchestrator.executor.execute(skill_name, step_payload)
            except Exception as e:
                # The executor itself crashed (not a normal skill-returned
                # error dict) — e.g. a bad payload shape blowing up inside
                # a skill before it could return anything, or a bug in
                # skill_executor.py's own dispatch logic. Without this,
                # step_ref above (args already set) never gets its
                # message/result filled in and the exception propagates
                # straight out of _plan_and_execute, leaving the frontend
                # with a step stuck showing args and no result forever.
                logger.error(f"@{skill_name} executor raised: {e}", exc_info=True)
                exec_result = {
                    "status": "error",
                    "data": None,
                    "error": f"Skill crashed: {e}",
                    "attempts": 1,
                }
            skill_output = exec_result
            previous_result = exec_result
            # See ChatContext.note_skill_result's docstring — this keeps
            # ctx.skill_output/ctx.download_result correct for whatever
            # reads them later, independent of loop order. It ALSO
            # already handles skill_system_context (see its own body) —
            # confirmed by reading it directly rather than assumed: a
            # skill returning "system_context" in its result is already
            # correctly picked up here and reaches _build_system_prompt's
            # "Skill-provided system context" block. That mechanism was
            # NOT missing — checked before building anything on top of it.
            ctx.note_skill_result(skill_name, exec_result)
            _exec_duration_ms = round((time.monotonic() - _exec_start) * 1000)

            # ── Ilana <-> TaskState cross-awareness ──────────────────────────
            # ADDED 2026-08-31: previously these were two entirely separate
            # persistence systems running blind to each other in the same
            # session — Ilana tracked PROCESS (phase/gate/mode) with zero
            # visibility into what TaskState already knew about actual
            # requirements/execution history, and vice versa. When Ilana is
            # the active skill this turn, fold a compact TaskState summary
            # into its system_context (already correctly wired via
            # note_skill_result above — this appends to what's already
            # there, not a second write path) so the acting agent (e.g.
            # [constructor], [verifier]) sees real requirement/execution
            # state instead of operating purely on its own gate ledger —
            # the two are meant to reinforce each other (Ilana enforces
            # PROCESS discipline; TaskState tracks what's actually been
            # BUILT), not duplicate or ignore one another.
            if skill_name == "ilana" and ctx.get("task_state") and ctx["task_state"].requirements:
                _ts_summary = ctx["task_state"].status_summary()
                ctx["skill_system_context"] = (
                    f"{ctx.get('skill_system_context', '')}\n\n"
                    f"TRACKED TASK STATE (from this session's TaskState, not Ìlànà's own "
                    f"ledger — real requirement/execution history already recorded outside "
                    f"the gate process):\n{_ts_summary}\n"
                    "Cross-check this against Ìlànà's own gate criteria rather than treating "
                    "them as unrelated — a requirement shown COMPLETED here still needs to "
                    "satisfy the current gate's actual criteria before the gate itself passes."
                )

            # Record in evidence store for hallucination review
            evidence.record_tool(skill_name, call_args, exec_result)

            # ── Trustworthy Task State: record execution ────────────────
            # ADDED 2026-08-16. Section 12/13 of the EDD: every skill
            # execution should produce structured evidence the task state
            # can verify against, not just a "done" claim. This does NOT
            # try to map a skill call to a SPECIFIC requirement ID (that
            # would need real requirement-to-step traceability per
            # Section 11, which needs the Planner itself to tag steps
            # with requirement IDs — a larger change than tonight's pass).
            # What it does today: records the raw execution outcome
            # against task_state so a later verification pass (the
            # document-grounding check below, or a future evidence-based
            # check) has real execution history to reason from, and so a
            # skill FAILURE is visible in task_state's execution_history
            # even before per-requirement mapping exists.
            if ctx.get("task_state"):
                try:
                    ctx["task_state"].record_execution(
                        step_id=f"step_{len(ctx['task_state'].execution_history) + 1}",
                        skill=skill_name,
                        success=exec_result.get("status") not in ("error", "timeout", "blocked"),
                        requirements_affected=[],  # see docstring above — not yet mapped
                        detail=(exec_result.get("reason") or exec_result.get("error") or "")[:200],
                    )

                    # BUG FIXED 2026-08-16 (real user report): mark_verified
                    # was ONLY ever called from inside the document-grounding
                    # block below — meaning a requirement could ONLY ever be
                    # marked complete if it happened to be a document-content
                    # claim that passed grounding. Every other task shape
                    # (image generation, file builds, anything that doesn't
                    # trigger the module-claim regex) created a requirement
                    # that could NEVER be marked complete, because nothing
                    # else in the pipeline called mark_verified on it — so
                    # the completion gate correctly, mechanically reported
                    # "not verified" on every successful image generation,
                    # every successful build, forever. This is a general
                    # fallback: on a successful skill execution, if the task
                    # has exactly one mandatory requirement still pending and
                    # no more specific check already owns this skill, mark
                    # it verified from the raw tool-success evidence.
                    #
                    # DELIBERATELY EXCLUDES document_reader and any
                    # file-writing skill (terminal, file_generation,
                    # website_developer) — those are exactly the skills the
                    # grounding check and self-review check are designed to
                    # verify AFTER the model's full answer is generated, not
                    # at the moment the raw tool call succeeds. This runs
                    # INSIDE the per-step loop (once per tool call), while
                    # grounding/self-review only run once, after the loop
                    # ends, against the model's FINAL answer. If this
                    # fallback marked document_reader complete the instant
                    # the read succeeded, it would mark the requirement
                    # COMPLETED before the model even generated a claim to
                    # check — completely defeating the Module-3 fabrication
                    # catch this same session was built to fix. Only skills
                    # with no more specific downstream check get the
                    # fallback; skills grounding/self-review already own are
                    # left for those checks to verify or fail explicitly.
                    _SKILLS_WITH_DEDICATED_VERIFICATION = {
                        "document_reader", "terminal", "file_generation", "website_developer",
                    }
                    if (
                        exec_result.get("status") not in ("error", "timeout", "blocked")
                        and skill_name not in _SKILLS_WITH_DEDICATED_VERIFICATION
                    ):
                        from core.task_state import EvidenceType as _EvidenceType, RequirementStatus as _ReqStatus
                        pending = [
                            r for r in ctx["task_state"].requirements
                            if r.status in (_ReqStatus.PENDING, _ReqStatus.IN_PROGRESS)
                        ]
                        if len(pending) == 1:
                            pending[0].mark_verified(
                                _EvidenceType.TOOL_OUTPUT,
                                f"{skill_name} completed successfully (status={exec_result.get('status')})",
                            )
                except Exception as e:
                    logger.warning(f"Task state execution recording failed (non-fatal): {e}")


            # Persist terminal working directory — if a terminal cd command
            # changed cwd, the skill returns new_cwd. Store it in ctx so
            # the next terminal call in this loop gets the updated path,
            # and _persist_memory writes it to the DB so it survives to the
            # next message in this session.
            if skill_name == "terminal" and exec_result.get("new_cwd"):
                ctx.setdefault("session_state", {})["terminal_cwd"] = exec_result["new_cwd"]
                # Also update base_payload so subsequent skill calls this
                # turn also see the new cwd.
                base_payload["session_state"] = ctx["session_state"]

            # Fill in the real outcome on the SAME step recorded above —
            # this is the part that never happened at all before.
            step_ref["message"] = f"@{skill_name}"
            step_ref["result"] = exec_result

            retry_count = max(0, exec_result.get("attempts", 1) - 1)
            confidence = score_skill_result(skill_name, call_args, exec_result, retry_count)
            await track_and_maybe_reflect(ctx, self.orchestrator.db, skill_name, call_args, exec_result, confidence)

            # ── Same-turn consecutive-failure hard stop ─────────────────────
            # HARDENING 2026-08-31: reflection.py already injects "prior
            # failure patterns, try a different approach" as advisory text
            # into the tool result — but that's a suggestion a model can
            # still ignore. Confirmed in production: a terminal sequence
            # hit reflection's own count>=2 threshold after the 2nd failed
            # command (git init -> author identity unknown), got the
            # injected warning, and the model made a 3rd bad terminal call
            # anyway (git push -> remote already exists) before finally
            # switching to @github. Advisory text alone wasn't enough to
            # stop a fast/small model from trying the same broken tool a
            # third time in the same turn.
            #
            # This tracks CONSECUTIVE failures of the same skill within
            # THIS turn only (ctx-scoped, resets every new turn — separate
            # from reflection.py's cross-session Mongo counter, which is
            # about long-term "this skill has been flaky" memory, not
            # same-turn loop-breaking). A success resets the streak
            # immediately. Once the streak crosses the hard ceiling, the
            # next failure's __instruction__ becomes a real, unambiguous
            # stop instead of "try something different" — telling the
            # model exactly which skill is now off-limits for the rest of
            # this turn, and naming the specific alternative when one is
            # known (github's push_workspace is the only case common
            # enough in production to name directly here; anything else
            # just gets told to stop and ask the user).
            SAME_TURN_FAILURE_CEILING = 2
            failure_streaks = ctx.setdefault("_same_turn_failure_streak", {})
            skill_failed_this_call = exec_result.get("status") in ("error", "timeout", "blocked")
            # REPEAT GUARD — the same call failing again and again burns steps and money.
            _repeat_key, _repeat_streak = repeat_failure_state(
                _repeat_key, _repeat_streak, skill_name, call_args, skill_failed_this_call)
            if _repeat_streak >= 2:
                exec_result["reason"] = (str(exec_result.get("reason") or "") + f" NOTE: this exact call has now failed {_repeat_streak} times in a row. Do not retry it unchanged; use a different approach or tell the user what is blocking it.")
            if _repeat_streak >= 3:
                logger.warning(f"REPEAT GUARD: {skill_name} failed {_repeat_streak}x with the same args — stopping the loop")
                break
            # PLAIN LANGUAGE — every result says what the call was and, if it failed, what to do.
            exec_result.setdefault("call_summary", describe_call(skill_name, call_args))
            if skill_failed_this_call:
                exec_result["explanation"] = explain_failure(exec_result)
            if skill_failed_this_call:
                failure_streaks[skill_name] = failure_streaks.get(skill_name, 0) + 1
            else:
                failure_streaks[skill_name] = 0
            hard_stop_triggered = (
                skill_failed_this_call
                and failure_streaks[skill_name] > SAME_TURN_FAILURE_CEILING
            )

            await self.orchestrator.bus.emit(Events.TOOL_EXECUTED, {
                "skill": skill_name,
                "args": call_args,
                "status": exec_result.get("status"),
                "duration_ms": _exec_duration_ms,
                "output": exec_result.get("data") or exec_result.get("stdout"),
                "error": (
                    exec_result.get("error")
                    or exec_result.get("reason")
                    or exec_result.get("stderr")
                    or None
                ),
                "confidence": confidence,
                "user_id": ctx["user_id"],
                "session_id": ctx["session_id"],
            })

            # Previously this stripped "message" out entirely — any skill
            # that returned a genuinely useful message ("Found 3 repos
            # matching X", "Saved to workspace folder 'x'") had it
            # silently discarded before reaching the frontend, leaving
            # only the generic "@skill → status" line with no real
            # content behind it. Now: keep the skill's own message if it
            # gave one, and synthesize a sensible default if it didn't —
            # every single skill call gets a real message the UI can show,
            # not just the ones that happened to include one.
            visible_result = dict(exec_result)
            if not visible_result.get("message"):
                if exec_result.get("status") == "success":
                    visible_result["message"] = f"{skill_name} completed successfully."
                else:
                    visible_result["message"] = (
                        exec_result.get("error")
                        or exec_result.get("reason")
                        or exec_result.get("stderr")
                        or f"{skill_name} did not return a message."
                    )
            tool_call_count += 1
            # UPDATE the "executing" step in place instead of appending a
            # new one — two _add_step calls per skill was the duplicate bug.
            step_ref.update({
                "message": f"@{skill_name} → {exec_result.get('status', '?')} (confidence: {confidence['label']})",
                "result": visible_result,
                "confidence": confidence,
            })

            # Feed REAL result back to the model for its next decision.
            # json round-trip with default=str: skills like memory_manager
            # return raw datetime objects (from Mongo docs) inside their
            # result dict. Neither httpx's json= param (Gemini path) nor
            # a plain json.dumps (OpenAI-protocol path) know how to
            # serialize a datetime — that's the exact "Object of type
            # datetime is not JSON serializable" crash seen in production.
            # FastAPI's own response serialization handles it fine (which
            # is why the HTTP response back to the client looked OK), but
            # these internal provider calls don't get that same
            # protection. This round-trip forces every value through
            # str() if it isn't natively JSON-safe.
            safe_result = json.loads(json.dumps(exec_result, default=str))

            # ADDED 2026-09-04: pulled out here, BEFORE the size-truncation
            # check right below — a skill (currently only document_reader's
            # scanned-PDF fallback, see skills/document_reader/skill.py's
            # _rasterize_pdf_for_vision) can return base64 page images under
            # this key for the model to read visually. Those images are
            # exactly the kind of "this skill returned something huge" case
            # MAX_RESULT_CHARS below guards against, but truncating a base64
            # image string corrupts it rather than just shortening readable
            # text — so this is pulled out of safe_result entirely before
            # that check ever sees it, sent to the model as real inline_data
            # image parts alongside the functionResponse instead (Gemini's
            # contents API allows both in the same turn), and never touches
            # the JSON blob that gets size-checked, logged, or persisted.
            vision_images = safe_result.pop("__vision_images__", None)

            # General backstop — not specific to whatsapp_reader (which
            # was the actual cause the one time this was traced: its
            # "chats" action had zero limit and could return every
            # contact on the account). ANY skill returning something
            # this large would do the same damage, and nothing anywhere
            # was catching it before it got fed back into the
            # conversation for the next turn. Confirmed in production
            # logs: a single turn's context hit 293K-377K tokens this
            # way, which broke every fallback provider in the chain
            # (Groq/NVIDIA/Mistral all cap between 8K-262K) on top of
            # exhausting Gemini itself. This doesn't fix a misbehaving
            # skill's own output shape — it just stops one skill's bloat
            # from cascading into a total outage for the rest of the turn.
            result_str = json.dumps(safe_result)
            MAX_RESULT_CHARS = 40000  # ~10K tokens, generous for a single skill result
            if len(result_str) > MAX_RESULT_CHARS:
                logger.warning(f"@{skill_name} returned an oversized result ({len(result_str)} chars) — truncating before it re-enters the conversation.")
                safe_result = {
                    "status": safe_result.get("status", "success"),
                    "truncated": True,
                    "original_size_chars": len(result_str),
                    "preview": result_str[:MAX_RESULT_CHARS],
                    "note": f"@{skill_name}'s result was too large ({len(result_str)} chars) to include in "
                            "full — showing a truncated preview. Ask for a narrower request (fewer items, "
                            "a smaller count/limit) if you need more of this.",
                }

            if active_protocol == "gemini":
                # When a skill errors, augment the response with an explicit
                # failure marker so the model sees it clearly in tool history.
                # Without this, the model sees an empty/error result and
                # silently fills the answer from context — hallucination.
                if safe_result.get("status") in ("error", "timeout", "blocked"):
                    failure_reason = (
                        safe_result.get("reason")
                        or safe_result.get("error")
                        or safe_result.get("stderr")
                        or "unknown error"
                    )
                    if hard_stop_triggered:
                        # Real enforced stop, not advisory text — see the
                        # same-turn failure streak comment above for why
                        # this exists as a separate, stronger tier above
                        # the reflection injection below.
                        _alt = (
                            "Use @github action='push_workspace' instead — it needs no git "
                            "identity, no remote setup, no auth handshake through terminal at all."
                            if skill_name == "terminal"
                            else f"Stop calling @{skill_name} this turn. Tell the user what failed and ask how to proceed — do not guess another variation."
                        )
                        instruction_text = (
                            f"@{skill_name} has now failed {failure_streaks[skill_name]} times in a row "
                            f"THIS TURN with different attempts that all failed the same way. "
                            f"STOP calling @{skill_name} for the rest of this turn — repeating it with "
                            f"yet another variation is not going to work. {_alt}"
                        )
                    else:
                        instruction_text = (
                            "This skill call FAILED. You do NOT have the answer. "
                            "Do NOT infer, guess, or recall from context. "
                            "Tell the user the skill failed and offer to retry."
                        )
                    safe_result = {
                        **safe_result,
                        "__skill_failed__": True,
                        "__failure_reason__": failure_reason,
                        "__instruction__": instruction_text,
                    }
                    # Inject recent reflections so the model knows what
                    # went wrong before and does NOT repeat the same call.
                    try:
                        recent = await get_recent_reflections(
                            self.orchestrator.db, ctx["user_id"], skill_name, limit=2
                        )
                        if recent:
                            refl_lines = ["PRIOR FAILURE PATTERNS FOR THIS SKILL — DO NOT REPEAT THESE:"]
                            for r in recent:
                                refl_lines.append(
                                    f"- Failed command: {r.get('command','?')} | "
                                    f"Error: {r.get('reason','?')} | "
                                    f"Error type: {r.get('error_type','unknown')} | "
                                    f"Try a completely different approach."
                                )
                            safe_result["__reflections__"] = " ".join(refl_lines)
                    except Exception:
                        pass
                else:
                    # Skill succeeded — resolve prior reflections
                    try:
                        await update_reflection_resolution(
                            self.orchestrator.db, ctx["user_id"], skill_name, "Succeeded"
                        )
                    except Exception:
                        pass
                # FIX 2026-08-28: see role-fix comment above (duplicate
                # call guard) — functionResponse must be role "user", not
                # "model". This is the main one — the real skill-result
                # append that fires on every single tool call.
                _parts = [{"functionResponse": {"name": skill_name, "response": safe_result}}]
                # ADDED 2026-09-04 — see vision_images pop-out comment above.
                # Appended in the SAME turn as the functionResponse (not a
                # separate turn) so the model can read these page images in
                # its very next reply, no extra round trip needed.
                if vision_images:
                    for img in vision_images:
                        _parts.append({"inline_data": {"mime_type": img["mime"], "data": img["data"]}})
                contents.append({"role": "user", "parts": _parts})
            else:
                openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(safe_result)})

        # max_steps exhausted — build a REAL summary of what was actually
        # attempted, not a vague placeholder.
        #
        # BUG FIXED 2026-08-15: this used to dump the full raw args dict
        # for every skill call straight into ctx["raw_reply"] — which is
        # BOTH the text shown to the user in chat AND the text saved as
        # this turn's persisted content. That meant every step-exhaustion
        # turn showed the user a wall of "@skill({...full args dict...})"
        # lines in the chat UI, which is genuinely bad UX (flagged
        # directly by a user).
        #
        # The detail itself was never the problem — the model DOES need
        # to remember exactly what it already tried so a "continue"
        # doesn't repeat the same calls. But there was already a
        # separate, purpose-built channel for exactly this
        # (ctx["steps"] -> _trim_steps_for_persistence -> saved on the
        # message's "steps" field) that just wasn't being read back into
        # the model's own history — _build_context's history loop (see
        # the `for m in raw_history` block) only ever reads m["content"],
        # never m["steps"]. So raw_reply was overloaded into being the
        # ONLY channel that actually round-trips to the model, which is
        # what forced all that detail into user-visible text in the
        # first place.
        #
        # Real fix: raw_reply now stays short and human-readable (what
        # the user sees). The full per-step detail still gets computed
        # here and stashed on ctx["step_exhaustion_detail"] — a NEW
        # field, separate from raw_reply/content, that _persist_memory
        # saves onto the message and that _build_context's history loop
        # now folds back into what the model sees on the NEXT turn (as
        # a distinct block, not visible chat text). See both of those
        # call sites for the other half of this fix.
        skill_attempts = [s for s in ctx["steps"] if s.get("type") == "skill" and s.get("skill")]
        if skill_attempts:
            lines = []
            for s in skill_attempts:
                result = s.get("result") if isinstance(s.get("result"), dict) else {}
                failed = bool(s.get("error")) or result.get("status") == "error"
                if failed:
                    reason = s.get("error") or result.get("error") or result.get("reason") or "unknown error"
                    outcome = f"failed ({reason})"
                else:
                    outcome = "succeeded"
                result_summary = result.get("message") or result.get("status") or ""
                detail = f": {result_summary}" if (result_summary and not failed) else ""
                lines.append(f"- @{s['skill']}({s.get('args', {})}) — {outcome}{detail}")

            succeeded = sum(1 for s in skill_attempts if not (bool(s.get("error")) or (s.get("result") or {}).get("status") == "error"))
            skill_names = ", ".join(dict.fromkeys(s["skill"] for s in skill_attempts))  # dedupe, keep order
            ctx["raw_reply"] = (
                f"I ran out of steps this turn ({len(skill_attempts)} tool calls used: {skill_names}, "
                f"{succeeded} succeeded). Say \"continue\" and I'll pick up exactly where I left off "
                f"without repeating what already worked."
            )
            ctx["step_exhaustion_detail"] = (
                "[SYSTEM NOTE — steps already attempted before running out of budget last turn, "
                "do not repeat these:]\n" + "\n".join(lines)
            )
        else:
            ctx["raw_reply"] = "I worked through several steps but didn't reach a final answer. Here's what I found so far."

        ctx["model_used"] = active_model
        ctx["skill_name"] = skill_name
        # Same fallback as the normal-completion exit above — a file
        # could genuinely have been generated earlier in the turn even
        # if the turn then ran out of step budget before wrapping up.
        ctx["skill_output"] = ctx.best_download_result()
        return ctx
