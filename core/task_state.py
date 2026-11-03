"""
core/task_state.py — OADSON Trustworthy Task State, piece 2 of 2.

Implements the EDD refined 2026-08-16 ("OADSON EDD — Persistent Task
State, Requirement Tracking & Closed-Loop Verification"), with four
amendments made during refinement before any code was written:

  1. (Section 5 gap) Requirement extraction is itself an unverified
     model call -- nothing in the original design checked IT. Fix:
     extracted requirements are surfaced back to the caller (and
     therefore to the user, or at minimum logged verbatim) BEFORE
     execution starts, via TaskState.requirements_preview(). A wrong
     extraction is now catchable before steps are spent building
     against the wrong target, instead of only being discoverable
     after the fact.

  2. (Section 13 gap) Added a named evidence type, "document_grounding",
     for exactly the failure class seen in real production use (see
     core/document_grounding.py's docstring for the full transcript).
     Not every requirement can be verified with a test run or an HTTP
     status -- "is this factual claim actually in the source document"
     needs its own evidence type, not a generic "the model re-read its
     own answer" pass.

  3. (Section 19/30 conflict) The EDD listed "repeated failure" as both
     a retry-stop condition and a model-escalation trigger without
     saying which wins. Resolved into one ladder, not two competing
     rules: failure 1-2 -> repair on the same tier; failure 3 ->
     escalate one tier up; failure 5 (absolute) -> stop and report
     honestly regardless of tier. See RETRY_LADDER below.

  4. (Cost-tier gap) Nothing in the original EDD said whether Task
     State bookkeeping (extraction, state updates, verification
     itself) runs on the same paid tier as execution. Under the V4
     Tiered Cognition plan, that would let verification overhead eat
     into a hard dollar cap even on tasks that would otherwise be
     free. BOOKKEEPING_TIER pins these operations to the free tier by
     default -- only the actual task EXECUTION should ever be routed
     to a paid model tier.
"""

import uuid
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger("oadson.task_state")


# ── Amendment 4: bookkeeping vs execution tier separation ──
# Requirement extraction, state updates, and verification bookkeeping
# are NOT reasoning-heavy tasks in the way a complex code-generation
# or multi-file build is -- they should run on the free tier by
# default under V4's routing, regardless of which tier the actual task
# EXECUTION escalates to. Only document_grounding-style checks that
# need real semantic judgment (rare -- most grounding checks in
# core/document_grounding.py are mechanical, not model calls at all)
# should ever route to a paid tier, and only as a last resort.
BOOKKEEPING_TIER = "free"


class RequirementStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REGRESSED = "regressed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RequirementSource(str, Enum):
    EXPLICIT_USER_REQUIREMENT = "explicit_user_requirement"
    INFERRED_REQUIREMENT = "inferred_requirement"
    PLANNER_REQUIREMENT = "planner_requirement"


class EvidenceType(str, Enum):
    """Amendment 2. Each type names what actually counts as proof for
    a requirement of that shape -- a model's own claim is never one of
    these types on its own."""
    TEST_RESULT = "test_result"                # pytest, unit tests
    HTTP_RESPONSE = "http_response"             # status code, response body
    FILE_EXISTENCE = "file_existence"           # artifact present on disk/storage
    FILE_CONTENT_MATCH = "file_content_match"   # artifact contains claimed content
    DOCUMENT_GROUNDING = "document_grounding"   # claim verified against source text
    # ^ see core/document_grounding.py -- this is the type that would
    # have caught the Module 3 fabrication.
    TOOL_OUTPUT = "tool_output"                 # raw skill_executor result
    UNKNOWN = "unknown"                         # no evidence available yet


class TaskStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


# ── Amendment 3: one retry ladder, not two competing rules ──
# failure_count -> action. Applied in order; the first matching
# threshold wins. This is intentionally a plain list, not a formula,
# so the exact behavior at each step is auditable at a glance rather
# than derived.
RETRY_LADDER = [
    # (failure_count_at_or_above, action)
    (5, "stop_and_report"),      # absolute ceiling -- never a silent 6th attempt
    (3, "escalate_tier"),        # 3rd failure -> try one tier up (e.g. Haiku -> Sonnet)
    (1, "repair_same_tier"),     # 1st-2nd failure -> retry with correction, same tier
]


def next_retry_action(failure_count: int) -> str:
    """Amendment 3 in code form. Given how many times a requirement's
    verification has failed, return the single next action to take."""
    for threshold, action in RETRY_LADDER:
        if failure_count >= threshold:
            return action
    return "repair_same_tier"


@dataclass
class Requirement:
    id: str
    description: str
    status: RequirementStatus = RequirementStatus.PENDING
    priority: str = "mandatory"  # "mandatory" | "derived" | "optional"
    source: RequirementSource = RequirementSource.EXPLICIT_USER_REQUIREMENT
    acceptance_criteria: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)  # [{"type": EvidenceType, "detail": "...", "passed": bool}]
    failure_count: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str | None = None

    def record_evidence(self, evidence_type: EvidenceType, detail: str, passed: bool) -> None:
        self.evidence.append({
            "type": evidence_type.value if isinstance(evidence_type, EvidenceType) else evidence_type,
            "detail": detail,
            "passed": passed,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_regressed(self, reason: str) -> None:
        """Section 14/15 of the EDD: a COMPLETED requirement whose
        verification later fails must reopen, never silently stay
        COMPLETED. This is the one state transition that must never be
        skipped by an optimistic status write."""
        if self.status == RequirementStatus.COMPLETED:
            self.status = RequirementStatus.REGRESSED
            self.record_evidence(EvidenceType.UNKNOWN, f"Regression: {reason}", passed=False)
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_verified(self, evidence_type: EvidenceType, detail: str) -> None:
        self.status = RequirementStatus.COMPLETED
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.record_evidence(evidence_type, detail, passed=True)

    def mark_failed(self, evidence_type: EvidenceType, detail: str) -> None:
        self.failure_count += 1
        self.status = RequirementStatus.FAILED
        self.record_evidence(evidence_type, detail, passed=False)

    def next_action(self) -> str:
        return next_retry_action(self.failure_count)

    # ── PERSISTENCE 2026-08-31 ──────────────────────────────────────
    # Added so TaskState can survive across turns in the same session
    # instead of being rebuilt from scratch on every single message
    # (see extract_task_state_for_turn's old behavior, and the
    # persist_task_state/load_task_state functions below for the full
    # story of why this was needed). Plain dict round-trip — status/
    # source/evidence-type enums serialize as their .value (already
    # plain strings via the str, Enum base) and reconstruct via the
    # enum constructor on load, which accepts its own .value.
    def to_dict(self) -> dict:
        return {
            "id": self.id, "description": self.description,
            "status": self.status.value if isinstance(self.status, RequirementStatus) else self.status,
            "priority": self.priority,
            "source": self.source.value if isinstance(self.source, RequirementSource) else self.source,
            "acceptance_criteria": self.acceptance_criteria,
            "dependencies": self.dependencies,
            "evidence": self.evidence,
            "failure_count": self.failure_count,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Requirement":
        return cls(
            id=d["id"], description=d["description"],
            status=RequirementStatus(d.get("status", "pending")),
            priority=d.get("priority", "mandatory"),
            source=RequirementSource(d.get("source", "explicit_user_requirement")),
            acceptance_criteria=d.get("acceptance_criteria", []),
            dependencies=d.get("dependencies", []),
            evidence=d.get("evidence", []),
            failure_count=d.get("failure_count", 0),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
            completed_at=d.get("completed_at"),
        )


@dataclass
class TaskState:
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    session_id: str = ""
    user_id: str = ""
    original_request: str = ""
    objective: str = ""
    requirements: list[Requirement] = field(default_factory=list)
    status: TaskStatus = TaskStatus.ACTIVE
    iteration: int = 0
    execution_history: list[dict] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)  # {path: {"produced_by_requirement": id, "content_hash": "..."}}
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ── Amendment 1: extraction must be checkable before execution ──
    def requirements_preview(self) -> str:
        """
        Human-readable summary of every extracted requirement, meant
        to be shown to the user (or at minimum logged verbatim) BEFORE
        any execution begins. This is the direct fix for the gap that
        nothing in the original EDD verified the EXTRACTION step
        itself -- a wrong read of "what did the user actually ask for"
        would otherwise propagate through the entire pipeline with
        full confidence and no checkpoint.
        """
        if not self.requirements:
            return f"Objective: {self.objective}\n(No requirements extracted yet.)"
        lines = [f"Objective: {self.objective}", "Requirements:"]
        for r in self.requirements:
            tag = {"mandatory": "[required]", "derived": "[implied]", "optional": "[optional]"}.get(r.priority, "")
            lines.append(f"  - {r.id} {tag} {r.description}")
        return "\n".join(lines)

    def add_requirement(self, description: str, priority: str = "mandatory",
                         source: RequirementSource = RequirementSource.EXPLICIT_USER_REQUIREMENT,
                         acceptance_criteria: list[str] | None = None,
                         dependencies: list[str] | None = None) -> Requirement:
        req_id = f"REQ-{len(self.requirements) + 1:03d}"
        req = Requirement(
            id=req_id, description=description, priority=priority, source=source,
            acceptance_criteria=acceptance_criteria or [],
            dependencies=dependencies or [],
        )
        self.requirements.append(req)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return req

    def get(self, req_id: str) -> Requirement | None:
        return next((r for r in self.requirements if r.id == req_id), None)

    def mandatory_requirements(self) -> list[Requirement]:
        return [r for r in self.requirements if r.priority == "mandatory"]

    # ── Section 20: the completion gate, as an enforceable boolean ──
    def is_complete(self) -> bool:
        mandatory = self.mandatory_requirements()
        if not mandatory:
            return False  # never "complete" with zero extracted requirements
        all_verified = all(r.status == RequirementStatus.COMPLETED for r in mandatory)
        no_regressions = not any(r.status == RequirementStatus.REGRESSED for r in self.requirements)
        no_blockers = not any(r.status == RequirementStatus.BLOCKED for r in mandatory)
        return all_verified and no_regressions and no_blockers

    def status_summary(self) -> str:
        """Compact state representation for injecting into a model
        call -- Section 23 of the EDD. Deliberately terse; the full
        TaskState stays available to the system, not the model, on
        every turn."""
        symbols = {
            RequirementStatus.COMPLETED: "\u2713",
            RequirementStatus.REGRESSED: "\u2717 REGRESSED",
            RequirementStatus.FAILED: "\u2717 FAILED",
            RequirementStatus.BLOCKED: "\u23f8 BLOCKED",
            RequirementStatus.IN_PROGRESS: "\u23f3",
            RequirementStatus.PENDING: "\u23f3 pending",
            RequirementStatus.SKIPPED: "skipped",
            RequirementStatus.CANCELLED: "cancelled",
        }
        lines = [f"OBJECTIVE: {self.objective}", "REQUIREMENTS:"]
        for r in self.requirements:
            lines.append(f"  {r.id} {symbols.get(r.status, r.status.value)} {r.description}")
        regressed = [r for r in self.requirements if r.status == RequirementStatus.REGRESSED]
        if regressed:
            lines.append("REGRESSIONS NEEDING REPAIR:")
            for r in regressed:
                last_evidence = r.evidence[-1]["detail"] if r.evidence else "no detail recorded"
                lines.append(f"  {r.id}: {last_evidence}")
        return "\n".join(lines)

    def record_execution(self, step_id: str, skill: str, success: bool,
                          requirements_affected: list[str], detail: str = "") -> None:
        self.execution_history.append({
            "step_id": step_id, "skill": skill, "success": success,
            "requirements_affected": requirements_affected, "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.iteration += 1
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def apply_user_correction(self, req_id: str, note: str) -> Requirement | None:
        """Section 22: 'You broke X' is a state update on the EXISTING
        task, never a fresh disconnected instruction. Caller resolves
        which requirement X refers to (often via document_grounding or
        simple text match against requirement descriptions) and calls
        this rather than starting a new TaskState."""
        req = self.get(req_id)
        if req:
            req.mark_regressed(f"User-reported: {note}")
        return req

    # ── PERSISTENCE 2026-08-31 ──────────────────────────────────────
    # See Requirement.to_dict's comment for the full reasoning. This is
    # the same round-trip one level up — the whole TaskState, including
    # every Requirement nested inside it.
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "session_id": self.session_id, "user_id": self.user_id,
            "original_request": self.original_request, "objective": self.objective,
            "requirements": [r.to_dict() for r in self.requirements],
            "status": self.status.value if isinstance(self.status, TaskStatus) else self.status,
            "iteration": self.iteration,
            "execution_history": self.execution_history,
            "artifacts": self.artifacts,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskState":
        return cls(
            task_id=d.get("task_id") or f"task_{uuid.uuid4().hex[:12]}",
            session_id=d.get("session_id", ""),
            user_id=d.get("user_id", ""),
            original_request=d.get("original_request", ""),
            objective=d.get("objective", ""),
            requirements=[Requirement.from_dict(r) for r in d.get("requirements", [])],
            status=TaskStatus(d.get("status", "active")),
            iteration=d.get("iteration", 0),
            execution_history=d.get("execution_history", []),
            artifacts=d.get("artifacts", {}),
            created_at=d.get("created_at", datetime.now(timezone.utc).isoformat()),
            updated_at=d.get("updated_at", datetime.now(timezone.utc).isoformat()),
        )


def extract_requirements_heuristic(user_request: str) -> list[dict]:
    """
    Deliberately simple, non-model requirement extraction for the
    common case: a request containing an explicit enumerated or
    conjunctive list ("X, Y, and Z" / "1. X 2. Y 3. Z").

    This exists as a fast, free-tier-safe (Amendment 4) first pass.
    It intentionally does NOT try to handle every possible phrasing --
    a genuinely ambiguous or implicit request should fall through to a
    model-assisted extraction (built by the caller, e.g. in
    orchestrator.py, using BOOKKEEPING_TIER), with THAT extraction then
    still subject to Amendment 1 (shown back before execution).
    Silently guessing structure for a request this function can't
    parse would reintroduce exactly the kind of unverified assumption
    this whole subsystem exists to remove.

    Returns a list of {"description": str, "priority": str} dicts,
    or an empty list if no clear enumerable structure was found (the
    caller should treat an empty list as "needs model-assisted
    extraction", not as "there were no requirements").
    """
    import re as _re

    text = user_request.strip()
    found = []

    # Numbered/lettered list: "1. X" / "1) X" / "a. X"
    #
    # TIGHTENED 2026-09-02 — was `\d+[\.\)]` with no upper bound and no
    # check on what follows, which matched a timestamp like "10.54 some
    # log text" or "19.46 WATCHDOG: ..." exactly as readily as a real
    # "1. Do this" list item — the digits-then-punctuation shape is
    # identical, `\d+` doesn't care that "10" or "19" isn't a realistic
    # list-item number. Now requires: the number is 1-2 digits (real
    # task lists essentially never enumerate past ~20 items; timestamps
    # commonly aren't in this narrower range specifically, but the
    # follow-up check below is what actually carries the fix) AND the
    # text immediately after the punctuation does NOT itself start with
    # more digits — "10.54 [INFO]..." fails this because "54" follows,
    # while "1. Build the backend" passes because "Build" does not look
    # like a continuation of the same number.
    numbered = _re.findall(
        r"(?:^|\n)\s*(?:\d{1,2}[\.\)]\s*(?!\d)|[a-z][\.\)]\s*(?!\d))(.+)",
        text, _re.MULTILINE,
    )
    if len(numbered) >= 2:
        found = [n.strip() for n in numbered if n.strip()]

    # Conjunctive list: "X, Y, and Z" / "X, Y and Z" -- only attempt
    # this if no numbered list was found, to avoid double-extracting.
    if not found:
        conj_match = _re.search(r"(.+?),\s*(?:and\s+)?(.+?)(?:\s+and\s+(.+))?$", text)
        if "," in text and (" and " in text.lower()):
            parts = _re.split(r",\s*(?:and\s+)?|\s+and\s+", text)
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2:
                found = parts

    return [{"description": f, "priority": "mandatory"} for f in found]


# ── MIGRATED 2026-08-21 from core/orchestrator.py ──
# Moved here (not just left inline in _plan_and_execute) as part of
# breaking that ~1,330-line function into smaller, independently
# readable/testable pieces — see this project's architecture notes on
# the ChatContext migration for the full rationale. This block was
# already fully self-contained (reads only objective/session_id, writes
# only a TaskState-or-None, no dependency on loop state or `self`), which
# is exactly what made it safe to extract mechanically in this pass
# without the higher risk a full tool-loop extraction would carry.
# Logic and comments below are moved verbatim, not rewritten, to avoid
# introducing any behavioral drift during the move.

def is_task_shaped(objective: str) -> bool:
    """
    ADDED 2026-08-16 — direct fix for a real production bug: the
    fallback "treat the whole objective as one requirement" was firing
    on EVERYTHING, including "I have stomach ache what should I do",
    "Use news skill instead", and even the user's own commentary like
    "Now you're saying the truth... Took you a 1172 insertion push" —
    none of which are tasks with a checkable "done" state at all. The
    completion gate then correctly reported these as "not satisfied"
    forever, because a symptom description or a piece of commentary has
    no verification criteria and can never be marked complete — and
    that caveat kept firing on every subsequent turn in the session.

    This is a deliberately conservative allowlist-style check, not a
    denylist — it's much safer to under-apply task tracking (falls back
    to normal conversation, no state tracking, exactly like before this
    subsystem existed) than to over-apply it and produce a confusing,
    never-resolving "not done" caveat on an ordinary question or remark.

    Task-shaped: an explicit request to build/fix/generate/extract/
    change something, OR a multi-part enumerated/conjunctive request.
    NOT task-shaped: questions, symptom/state descriptions, one-line
    corrections or clarifications, commentary/feedback, greetings.
    """
    text = objective.strip().lower()
    if not text:
        return False

    # Too short to be a real multi-step task in almost every case —
    # "use news skill instead", "why?", "what's not fully done?" are
    # all under this and are corrections/questions, not new tasks.
    if len(text.split()) <= 6:
        return False

    # Questions aren't tasks with a completion state — they're answered
    # or not, but "answering a question" isn't a requirement to verify
    # against an artifact the way "build X" is.
    question_starters = ("what", "why", "how", "when", "where", "who", "is ", "are ", "can ", "should ", "do ", "does ")
    if text.startswith(question_starters) and text.rstrip().endswith("?"):
        return False

    # Symptom/state descriptions ("I have X", "I feel Y") aren't tasks
    # to complete — they're context for a conversational response.
    if re.match(r"^i\s+(?:have|feel|am|got|had)\b", text):
        return False

    # ADDED 2026-09-02 — direct fix for a real production bug: pasted
    # server logs, terminal output, and other structural/mechanical
    # text were being treated as task-shaped requests. Two separate
    # ways this happened, both closed here:
    #   1. extract_requirements_heuristic's numbered-list regex matches
    #      ANY line starting with a digit + "." or ")" — timestamps
    #      (10.54), status codes, stack frame numbers, line numbers all
    #      match that shape without being an enumerated requirements
    #      list at all. A 20-line log paste could easily produce
    #      "15 requirements not yet verified" purely from its own
    #      incidental formatting, none of which the user ever asked to
    #      have "completed".
    #   2. Log/output text legitimately often CONTAINS one of the
    #      action_verbs below in its own content ("pip install",
    #      "config update", a stack trace mentioning "modify") even
    #      though the user's actual intent was "here's some output,
    #      look at this" — not "perform these actions".
    # Detected via a density check rather than a single keyword, same
    # "prefer under-applying task tracking" philosophy the rest of this
    # function already documents: a real multi-part task requestor
    # writes prose with occasional punctuation; a log/paste is
    # DOMINATED by line-start digits/timestamps/brackets. Requiring a
    # genuine majority of lines to look mechanical (not just one or two
    # incidental matches) avoids false-negatives on an actual numbered
    # task list that happens to mention a timestamp once.
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) >= 4:
        mechanical_line_re = re.compile(
            r"^\s*(?:"
            r"\d{1,4}[:.]\d{2}|"                          # timestamps: 10:54, 19.46
            r"\[[\d\w\-: ]+\]|"                            # [2026-09-02 03:17:26] style brackets
            r"(?:get|post|put|delete|patch)\s+/|"          # HTTP method + path (log lines)
            r"\d{3}\s|"                                    # leading HTTP status code
            r"(?:error|warning|info|debug|trace)[:\]]|"    # log level prefixes
            r"at\s+[\w.$]+\(|"                              # stack trace frames
            r"file \"|"                                     # python traceback lines
            r"traceback"
            r")",
            re.IGNORECASE,
        )
        mechanical_count = sum(1 for l in lines if mechanical_line_re.match(l))
        if mechanical_count / len(lines) >= 0.4:
            return False

    # Explicit action verbs at or near the start are the strongest
    # positive signal of a real task.
    action_verbs = (
        "build", "create", "generate", "make", "write", "fix", "extract",
        "add", "remove", "implement", "update", "change", "modify",
        "install", "deploy", "refactor", "design", "convert", "migrate",
        "understand", "read", "analyze", "review", "index", "scan",
    )
    first_words = text.split()[:4]
    if any(v in first_words for v in action_verbs):
        return True

    # Multi-part enumerated/conjunctive requests (Section 5's own
    # worked example — "Build X, extract Y, store Z, return W") are
    # task-shaped even without a leading verb, IF they also contain a
    # real action verb somewhere in the text (not just any comma list —
    # "I like apples, oranges, and pears" is not a task).
    has_list_structure = bool(re.search(r",\s*(?:and\s+)?\w+.*,|\band\s+then\b|;\s*\w", text))
    has_any_action_verb = any(v in text.split() for v in action_verbs)
    if has_list_structure and has_any_action_verb:
        return True

    return False


def extract_task_state_for_turn(
    objective: str, session_id: str, user_id: str = "", existing: "TaskState | None" = None,
) -> "TaskState | None":
    """
    MIGRATED 2026-08-21 from core/orchestrator.py's _plan_and_execute —
    was ~40 lines of inline logic there; moved here verbatim (same
    is_task_shaped gate, same heuristic extraction, same non-blocking
    try/except posture) as its own function so that function is shorter
    and this logic is independently readable/callable without needing
    to read the whole tool-calling loop around it.

    Per the Persistent Task State EDD, amendment 1: extraction is itself
    an unverified model-adjacent step, so it's logged BEFORE execution
    starts via TaskState.requirements_preview(), not trusted silently.

    is_task_shaped() gates this — task tracking only activates for
    messages that actually look like a build/fix/change request (see
    that function's own docstring for the real production bug this
    fixed: ordinary questions/commentary/symptom descriptions used to
    get wrapped as unresolvable "requirements" that never complete).

    Non-blocking by design, same posture as the grounding check and the
    hallucination reviewer elsewhere in the pipeline — a bug in
    extraction must never prevent the actual turn from running, hence
    returning None (not raising) on any internal failure.

    FIX 2026-08-22: `objective` as received here can be the terminal
    surface's shell-context-augmented string — api/chat.py appends
    "\n\n[Shell Context]\n<cwd, git status, directory tree, ...>" onto
    the raw user message before it ever reaches this function, since
    the MODEL genuinely needs that situational context. But this
    function has no business seeing it: when no enumerable list
    structure is found, the fallback below treats the ENTIRE objective
    as one requirement's description — meaning the shell context block
    was silently becoming part of a "you have an unmet requirement"
    caveat shown back to the user (e.g. a truncated, garbled fragment
    like "read the readme.md\n\n[Shell Context]\n=== SHELL CONTEXT ===
    cwd: /storage/emul..." instead of a clean, readable requirement).
    Stripped here, at the one place this function receives its input,
    rather than changing what api/chat.py builds for the model itself —
    the model still gets full shell awareness; only THIS extraction
    step, and anything shown back to the user from it, no longer does.

    PERSISTENCE 2026-08-31: previously this ALWAYS built a brand new
    TaskState from scratch, every single turn — meaning task state
    (what's been decided, what's built, what's broken) never actually
    survived past the one message that created it, despite
    status_summary()'s own docstring saying it's meant to stay
    available across the whole task. Confirmed as a real production
    problem: a ~2 hour session building an admin dashboard produced
    two contradictory UI states because nothing was carrying forward
    "we already decided X" from one turn to the next — the model had
    only raw conversation history to reconstruct that from, and drifted.

    `existing` is the TaskState already loaded for this session (via
    load_task_state, called by the orchestrator before this function),
    or None for a session's first task-shaped message. When present,
    newly-extracted requirements from THIS turn are ADDED to it rather
    than starting over — the objective/original_request fields update
    to reflect the latest ask (so status_summary() shows what's
    currently being worked on), but requirements, their statuses,
    evidence, and execution_history all carry forward untouched. This
    is deliberately additive-only here — marking something regressed
    happens via apply_user_correction() elsewhere, not by silently
    dropping it during extraction.
    """
    clean_objective = objective.split("\n\n[Shell Context]", 1)[0].strip()

    # ADDED 2026-09-02 — direct fix for a real production bug: the
    # system's OWN injected "[Attached file(s): ...]" note (added by
    # core/attachment_processor.py to tell the model exactly which
    # sandbox path to read and to explicitly NOT use fetch_upload) was
    # being fed straight into extract_requirements_heuristic below —
    # and that note's prose is genuinely full of commas/"and"s ("call
    # @document_reader with action=read_chunked (or read, if short) and
    # file_path=... Do NOT use fetch_upload... none exists, it was
    # saved directly, not fetched."), which the conjunctive-list
    # heuristic chopped into several nonsensical fake "requirements"
    # the user never asked for — confirmed in production: a session
    # with several pasted attachments accumulated FAKE REQ-009 through
    # REQ-023 entries purely from these notes' own internal punctuation,
    # none of which the user ever requested. Unlike [Shell Context]
    # (always the last thing appended, safe to keep only the text
    # before it), an attachment note can appear MULTIPLE times in one
    # message with real user text before, between, and after each one —
    # so this strips just the bracketed note blocks themselves via
    # regex, wherever they occur, keeping every genuine word around
    # them intact rather than truncating the message at the first one.
    clean_objective = re.sub(
        r"\[Attached file\(s\):.*?\]", "", clean_objective, flags=re.DOTALL,
    ).strip()
    # Collapse the double/triple blank lines left behind by removing a
    # note that used to sit between two real paragraphs of user text.
    clean_objective = re.sub(r"\n{3,}", "\n\n", clean_objective).strip()

    # ADDED 2026-09-05 — direct fix for a real production bug: a
    # TaskState with no completion mechanism other than is_complete()
    # (all mandatory requirements verified) had NO path to ever being
    # retired otherwise. A multi-day-old TaskState from an entirely
    # unrelated conversation (e.g. a backend-architecture discussion)
    # kept resurfacing its old, never-verified requirements on
    # completely unrelated later turns — confirmed directly in
    # production: a voice-note transcription request for a church
    # website's header text got shown "13 requirements not yet
    # verified" quoting an old, unrelated topic's requirements
    # verbatim, even though the voice note itself was NOT task-shaped
    # (it's narration/instruction-giving, correctly classified False
    # by is_task_shaped below) — which is exactly why this check MUST
    # run before, and independent of, the is_task_shaped gate. The bug
    # was never about misclassifying the new message; it's about an
    # old TaskState having no expiry at all once created.
    #
    # Conservative, narrow gate: only retires when BOTH (a) stale by
    # wall-clock time, AND (b) the new message shares no real word
    # overlap with the old objective — a genuine continuation of the
    # same task within the stale window still carries forward
    # untouched, same as before this fix.
    STALE_AFTER_HOURS = 2
    if existing is not None:
        try:
            last_touched = datetime.fromisoformat(existing.updated_at)
            if last_touched.tzinfo is None:
                last_touched = last_touched.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - last_touched).total_seconds() / 3600
        except (ValueError, TypeError):
            hours_since = 0  # can't parse timestamp — fail safe, don't retire on a data error

        if hours_since >= STALE_AFTER_HOURS:
            old_words = set(re.findall(r"[a-z0-9]+", existing.objective.lower()))
            new_words = set(re.findall(r"[a-z0-9]+", clean_objective.lower()))
            stopwords = {"the", "and", "for", "you", "your", "with", "this", "that", "should", "under", "then", "put"}
            old_words -= stopwords
            new_words -= stopwords
            overlap = old_words & new_words
            if not overlap or len(overlap) / max(len(new_words), 1) < 0.15:
                logger.info(
                    f"Task state for {session_id} retired as stale "
                    f"({hours_since:.1f}h since last touch, no topic overlap "
                    f"with new message) — starting fresh instead of carrying "
                    f"forward old unresolved requirements."
                )
                existing = None

    if not is_task_shaped(clean_objective):
        return existing  # not a new task-shaped ask — carry forward whatever's left after the staleness check above (possibly None now), don't wipe it further
    try:
        if existing is not None:
            task_state = existing
            task_state.objective = clean_objective
            task_state.updated_at = datetime.now(timezone.utc).isoformat()
        else:
            task_state = TaskState(session_id=session_id, user_id=user_id, original_request=clean_objective, objective=clean_objective)

        heuristic_reqs = extract_requirements_heuristic(clean_objective)
        # De-dupe against requirements already tracked for this session —
        # a near-identical description re-added every turn (e.g. the
        # model restating the same overall goal each message) would
        # otherwise accumulate duplicate REQ- entries forever instead of
        # recognizing it's the same requirement already in progress.
        existing_descriptions = {r.description.strip().lower() for r in task_state.requirements}
        if heuristic_reqs:
            for r in heuristic_reqs:
                if r["description"].strip().lower() not in existing_descriptions:
                    task_state.add_requirement(r["description"], priority=r["priority"])
        elif clean_objective.strip().lower() not in existing_descriptions:
            # Task-shaped but no enumerable list structure found — treat
            # the whole objective as one requirement rather than
            # guessing a breakdown.
            task_state.add_requirement(clean_objective, priority="mandatory")

        logger.info(f"Task state extracted for {session_id}:\n{task_state.requirements_preview()}")
        return task_state
    except Exception as e:
        logger.warning(f"Task state extraction failed (non-fatal): {e}")
        return existing


# ── PERSISTENCE 2026-08-31 ────────────────────────────────────────────
# Load/save for TaskState, mirroring memory/context_compressor.py's
# existing pattern exactly: same collection (ai_sessions), same
# session_id+user_id key, a sibling field ("task_state") next to
# context_compressor's own "context_summary" field on the same
# document — one write per turn covers both, no new collection, no new
# write path to keep in sync with the session's lifecycle (deletion,
# etc. — anything that already cleans up ai_sessions docs correctly
# cleans this up too, for free).
async def load_task_state(db, session_id: str, user_id: str) -> "TaskState | None":
    """
    Best-effort load — returns None on any failure (missing doc, bad
    stored shape, DB error) rather than raising, same non-blocking
    posture as extract_task_state_for_turn itself. A load failure
    should degrade to "start fresh this turn", never break the turn.
    """
    try:
        if db is None or not session_id:
            return None
        doc = await db["ai_sessions"].find_one(
            {"session_id": session_id, "user_id": user_id}, {"task_state": 1},
        )
        stored = (doc or {}).get("task_state")
        if not stored:
            return None
        return TaskState.from_dict(stored)
    except Exception as e:
        logger.warning(f"load_task_state failed for {session_id} (non-fatal, starting fresh): {e}")
        return None


async def persist_task_state(db, task_state: "TaskState | None") -> None:
    """
    Best-effort save — called once per turn after execution, same
    "never break the turn" posture as everything else in this file.
    No-ops cleanly if task_state is None (nothing task-shaped happened
    this turn and there was nothing already tracked for this session).
    """
    try:
        if db is None or task_state is None or not task_state.session_id:
            return
        await db["ai_sessions"].update_one(
            {"session_id": task_state.session_id, "user_id": task_state.user_id},
            {"$set": {"task_state": task_state.to_dict()}},
        )
    except Exception as e:
        logger.warning(f"persist_task_state failed for {task_state.session_id if task_state else '?'} (non-fatal): {e}")

