"""
core/chat_context.py — typed replacement for the loose `ctx: dict` that
core/orchestrator.py's whole pipeline threads through every stage.

WHY THIS EXISTS
================
Every stage of AIOrchestrator's pipeline (_build_context, _retrieve_memories,
_plan_and_execute, _review, _validate, _persist_memory, _finalize) took and
returned a plain `dict`, with ~40 keys written and read via bare string
literals (`ctx["skill_output"]`, `ctx.get("enable_reasoning")`, ...) scattered
across a ~3,000-line file. Nothing enforced a field's type, nothing caught a
typo'd key at write time, and nothing made a field's lifecycle (where it's
first set, where it's overwritten, where it's finally read) visible without
grepping the whole file.

That looseness is what let a real, shipped bug exist for a long time:
`skill_output` was reassigned unconditionally on EVERY skill call inside the
tool-calling loop (`skill_output = exec_result`), so by the time the turn
ended, ctx["skill_output"] just held whatever the LAST skill call happened to
be — not necessarily the one that generated a file. A safety net elsewhere
that specifically needed "the result that has a download_url" read the wrong
one and silently found nothing, even though a real download link existed
earlier in the same turn. A dataclass with an explicit
`download_result: dict | None` field, set only when a result actually HAS a
download_url (see `ChatContext.note_skill_result` below), makes that
distinction a first-class, self-documenting part of the type instead of a
bolt-on tracker variable a future edit could just as easily forget to update.

WHAT THIS IS NOT
================
This is not a full strict-schema rewrite of every one of the ~40 fields the
pipeline touches — attempting that in one pass, in a 3,000-line file, with no
existing test suite to catch a broken call site, would trade one kind of risk
(silent dict-key bugs) for another (a shipped attribute-typo that breaks
something untested). Instead: every field actually read/written via bare
`ctx[...]` / `ctx.get(...)` anywhere in orchestrator.py (audited via grep
against the current file) is declared here as a real, defaulted, documented
dataclass field. Anything genuinely long-tail or stage-local that doesn't
warrant a permanent field yet lives in `.extra: dict[str, Any]` — an
explicit, typed escape hatch, not a silent free-for-all.

DROP-IN COMPATIBILITY
======================
ChatContext implements __getitem__, __setitem__, __contains__, .get(), and
.keys() so every existing `ctx["x"]`, `ctx.get("x")`, `ctx["x"] = y` call site
across orchestrator.py keeps working completely unchanged — this is an
additive, non-breaking type, not a rewrite of every call site in one pass.
Known fields route through the real typed attribute; anything else falls
through to `.extra`. This lets the migration happen incrementally: swap the
`ctx = {...}` dict literal in `run()` for `ctx = ChatContext(...)`, verify
nothing broke, and only then start converting individual call sites to real
attribute access (`ctx.skill_output` instead of `ctx["skill_output"]`) where
it's actually worth the churn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatContext:
    # ── Set once, at the very start of run(), never reassigned ──
    objective: str
    user_id: str
    session_id: str
    user: dict
    surface: str = "web"
    local_execution: bool = False
    enable_reasoning: bool = False
    stream_tokens: bool = True

    # ── Ephemeral image input (never persisted — see run()'s own
    #    docstring on why image_data must be real bytes, not a URL) ──
    image_data: str | None = None
    image_mime: str | None = None
    extra_images: list[dict] = field(default_factory=list)

    # ── Durable attachment (persisted on the user's message — see
    #    run()'s docstring on attachment_url vs image_data) ──
    attachment_url: str | None = None
    attachment_type: str | None = None
    attachment_filename: str | None = None
    extra_attachment_urls: list = field(default_factory=list)

    # ── Built by _build_context / _retrieve_memories ──
    history: list = field(default_factory=list)
    context_summary: str = ""
    core_summary: str = ""
    core_memories: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    memory_context: str = ""
    session_doc: dict | None = None
    session_state: dict = field(default_factory=dict)
    intent: dict | None = None
    task_type: str | None = None
    plan: dict | None = None

    # ── Populated during _plan_and_execute's tool-calling loop ──
    steps: list = field(default_factory=list)  # execution trace visible to the frontend
    skill_name: str | None = None
    skill_output: dict | None = None
    # See this file's module docstring — the actual fix for the
    # download-link-lost bug. Set only by note_skill_result() below,
    # only when a result genuinely has a download_url, independent of
    # whatever skill_output/skill_name end up holding after later calls.
    download_result: dict | None = None
    # GENERIC SYSTEM_CONTEXT INJECTION 2026-08-26 — added for skills/ilana
    # but written generically, not ilana-specific: any skill can return a
    # "system_context" string in its result to have it actually treated
    # as a real system-level instruction for the rest of THIS turn's
    # Gemini calls (appended in orchestrator.py's _build_system_prompt),
    # not just inert JSON sitting in a tool-result blob the model may or
    # may not act on — which is all it was before this, since nothing
    # anywhere ever read a "system_context" key out of a skill result.
    # Sticky for the rest of the turn once set, same reasoning as
    # download_result above.
    skill_system_context: str | None = None
    evidence: Any = None
    step_budget: int | None = None
    step_exhaustion_detail: str | None = None
    task_state: dict | None = None
    task_state_incomplete: bool = False
    model_used: str | None = None
    raw_reply: str = ""
    persisted_at: Any = None

    # ── Populated during _review / _validate ──
    review_passed: bool | None = None
    review_issues: list = field(default_factory=list)
    hallucination_review: dict | None = None
    document_grounding_issues: list = field(default_factory=list)

    # ── Internal bookkeeping (loop-local, but a few call sites read it
    #    off ctx directly rather than a plain local — kept here so
    #    those still work unchanged) ──
    _executed_calls: set = field(default_factory=set)

    # ── Escape hatch for anything not listed above — see module
    #    docstring's "WHAT THIS IS NOT" section for why this exists
    #    instead of trying to enumerate every possible field. ──
    extra: dict[str, Any] = field(default_factory=dict)

    # ── Known-field lookup table for the dict-style accessors below.
    #    Kept as a set (not re-derived via dataclasses.fields() on every
    #    call) since __getitem__/__setitem__/__contains__ are on the hot
    #    path of every single pipeline stage. ──
    _KNOWN_FIELDS = frozenset({
        "objective", "user_id", "session_id", "user", "surface",
        "local_execution", "enable_reasoning", "stream_tokens",
        "image_data", "image_mime", "extra_images",
        "attachment_url", "attachment_type", "attachment_filename", "extra_attachment_urls",
        "history", "context_summary", "core_summary", "core_memories", "memories",
        "memory_context", "session_doc", "session_state", "intent", "task_type", "plan",
        "steps", "skill_name", "skill_output", "download_result", "evidence",
        "step_budget", "step_exhaustion_detail", "task_state", "task_state_incomplete",
        "model_used", "raw_reply", "persisted_at",
        "review_passed", "review_issues", "hallucination_review", "document_grounding_issues",
        "_executed_calls",
        # BUGFIX 2026-08-31: skill_system_context is a real declared field
        # (see its dataclass definition above) and note_skill_result()
        # genuinely sets self.skill_system_context correctly — but it was
        # missing from THIS set, which is what __getitem__/get/__setitem__
        # actually check to decide real-attribute vs extra-dict routing.
        # The result: ctx.get("skill_system_context") silently returned
        # None and ctx["skill_system_context"] raised KeyError, EVERY
        # time, everywhere in the codebase — including
        # _build_system_prompt's own read of exactly this key, meant to
        # inject a skill's (e.g. skills/ilana) system_context into the
        # model's prompt. Confirmed directly: a real note_skill_result()
        # call correctly set the attribute, but ctx.get() on the same
        # object returned None immediately after — the dict-compatibility
        # layer and the real dataclass field had silently diverged. This
        # is the actual root cause of Ìlànà's "system_context never
        # reaches the model" gap — not a missing write (there wasn't
        # one), but a missing entry in the read-routing table for a
        # write that was already happening correctly.
        "skill_system_context",
    })

    def note_skill_result(self, skill_name: str, result: dict) -> None:
        """
        Call this (instead of bare `self.skill_output = result` /
        `self.skill_name = skill_name`) every time a skill call completes
        inside the tool-calling loop. Updates skill_name/skill_output as
        before (so anything reading "the most recent skill result" keeps
        working exactly as it always did), AND — this is the actual fix —
        separately remembers this result in download_result specifically
        if it produced a file, so a LATER skill call in the same turn that
        doesn't produce a file can't silently overwrite the one that did.
        See this module's docstring for the full incident this prevents.
        """
        self.skill_name = skill_name
        self.skill_output = result
        if isinstance(result, dict) and result.get("download_url") and result.get("filename"):
            self.download_result = result
        if isinstance(result, dict) and result.get("system_context"):
            self.skill_system_context = result["system_context"]

    def best_download_result(self) -> dict | None:
        """
        What _finalize's "append the download link if the model forgot to
        mention it" safety net should read, instead of bare skill_output.
        Prefers skill_output itself if IT has a download_url (most
        recent call, still the right one — no correction needed), else
        falls back to whatever earlier call in the turn actually had one.

        Returns None when NEITHER has a real download_url — the method
        name promises "a result with a download in it", so returning an
        arbitrary non-download result here would be misleading to any
        caller that trusts the name at face value without re-checking
        .get("download_url") themselves (as _finalize happens to already
        do defensively, but that shouldn't be the only thing keeping
        this method honest). Caught by this project's real test suite —
        see tests/test_chat_context.py's DownloadResultTracking class —
        which is exactly the point of writing tests that assert on
        actual behavior instead of just checking the code runs.
        """
        if self.skill_output and self.skill_output.get("download_url"):
            return self.skill_output
        if self.download_result and self.download_result.get("download_url"):
            return self.download_result
        return None

    # ── dict-style compatibility layer — see module docstring's
    #    "DROP-IN COMPATIBILITY" section. Every method below exists
    #    solely so `ctx["x"]`, `ctx.get("x")`, `ctx["x"] = y`, `"x" in ctx`
    #    keep working exactly as they did when ctx was a plain dict,
    #    across every existing call site in orchestrator.py, with zero
    #    of those call sites needing to change for this migration to be
    #    safe to ship. ──

    def __getitem__(self, key: str) -> Any:
        if key in self._KNOWN_FIELDS:
            return getattr(self, key)
        return self.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in self._KNOWN_FIELDS:
            setattr(self, key, value)
        else:
            self.extra[key] = value

    def __contains__(self, key: str) -> bool:
        if key in self._KNOWN_FIELDS:
            return getattr(self, key) is not None
        return key in self.extra

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._KNOWN_FIELDS:
            val = getattr(self, key)
            return val if val is not None else default
        return self.extra.get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        """
        Same semantics as dict.setdefault — needed because orchestrator.py
        has at least one real call site (`ctx.setdefault("session_state",
        {})[...] = ...`) that relies on this. Caught by grepping the file
        for every dict-only method used on ctx before this migration
        shipped, rather than discovering it via a runtime AttributeError
        the first time a terminal skill call with new_cwd happened to run.
        """
        if key in self._KNOWN_FIELDS:
            current = getattr(self, key)
            if current is None:
                setattr(self, key, default)
                return default
            return current
        return self.extra.setdefault(key, default)

    def keys(self):
        present = [k for k in self._KNOWN_FIELDS if getattr(self, k) is not None]
        return present + list(self.extra.keys())
