"""
skills/ilana/skill.py — Ìlànà Software Engineering Process

Ìlànà (Yoruba): the disciplined way a thing is done.

Derived from SEN102/212 Software Engineering Process,
Obafemi Awolowo University, a sample city. MIT licence.

OADSON integration note:
  - All surfaces (web, terminal, WhatsApp, Telegram) call this via the
    orchestrator's skill loop. No surface-specific code lives here.
  - Filesystem writes go to the user's working directory via the
    run_bash / write_file tools already available in the orchestrator.
    In MEMO mode (no filesystem access), all artifacts are emitted as
    fenced code blocks with their intended path clearly labelled.
  - The orchestrator's Gemini function-calling loop handles the
    multi-turn interrogation; this skill returns structured data the
    model uses to drive the session forward.
"""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("oadson.skill.ilana")

# ── PHASE TABLE ──────────────────────────────────────────────────────────────

PHASES = {
    "01": {
        "name": "Requirements",
        "agent": "analyst",
        "artifact": "SRS + traceability matrix",
        "gate": "G1",
    },
    "02": {
        "name": "Architecture Design",
        "agent": "architect",
        "artifact": "Design description + ADRs",
        "gate": "G2",
    },
    "03": {
        "name": "Interface Design",
        "agent": "interaction-designer",
        "artifact": "Interface specification",
        "gate": "G3",
    },
    "04": {
        "name": "Construction",
        "agent": "constructor",
        "artifact": "Source + coding standard",
        "gate": "G4",
    },
    "05": {
        "name": "Verification",
        "agent": "verifier",
        "artifact": "Test plan + defect log",
        "gate": "G5",
    },
    "06": {
        "name": "Configuration Management",
        "agent": "configuration-engineer",
        "artifact": "SCM plan + release notes",
        "gate": "G6",
    },
    "07": {
        "name": "Quality Assurance",
        "agent": "quality-auditor",
        "artifact": "Quality management plan",
        "gate": "G7",
    },
    "08": {
        "name": "Process Assessment",
        "agent": "metrologist",
        "artifact": "Maturity assessment",
        "gate": "G8",
    },
    "09": {"name": "Process Modeling", "agent": "process-modeler", "artifact": "Process model", "gate": None},
    "10": {"name": "Tooling", "agent": "documentarian", "artifact": "Toolchain decision record", "gate": None},
    "11": {"name": "Ethics and Teams", "agent": "ethics-officer", "artifact": "Ethics and roles register", "gate": None},
}

AGENTS = [
    "conductor", "analyst", "architect", "interaction-designer",
    "constructor", "verifier", "configuration-engineer", "quality-auditor",
    "metrologist", "process-modeler", "documentarian", "ethics-officer",
    "release-manager",
]

MODES = ["FLEET", "TASK", "AUDIT", "TUTOR", "DOCTOR", "DRILL"]

# ── GATE CRITERIA (summary form for G0 and G1 — the most critical) ─────────

GATE_CRITERIA = {
    "G0": [
        {"id": 1, "criterion": "Problem stated in one sentence, in the user's words", "rigour": 1},
        {"id": 2, "criterion": "Primary users and stakeholders named", "rigour": 1},
        {"id": 3, "criterion": "Mode selected and recorded", "rigour": 1},
        {"id": 4, "criterion": "Rigour selected and justified", "rigour": 1},
        {"id": 5, "criterion": "Process style selected: agile / plan-driven / hybrid / devops", "rigour": 2},
        {"id": 6, "criterion": "Domain and regulatory exposure identified", "rigour": 3},
    ],
    "G1": [
        {"id": 1, "criterion": "Every requirement has a unique, stable ID", "rigour": 1},
        {"id": 2, "criterion": "All three types present (functional, non-functional, domain) or absence justified", "rigour": 1},
        {"id": 3, "criterion": "No requirement contains an unmeasurable adjective", "rigour": 1},
        {"id": 4, "criterion": "Each requirement is independently testable", "rigour": 1},
        {"id": 5, "criterion": "Scope boundary written: what the system will NOT do", "rigour": 2},
        {"id": 6, "criterion": "Every NFR carries a number and a unit", "rigour": 3},
        {"id": 7, "criterion": "Requirements validated with an actual stakeholder", "rigour": 3},
        {"id": 8, "criterion": "Conflicts identified and dispositioned", "rigour": 3},
    ],
    "G4": [
        {"id": 1, "criterion": "All code implements a named requirement (REQ/NFR/DOM)", "rigour": 1},
        {"id": 2, "criterion": "Coding standard exists and is cited in the commit", "rigour": 2},
        {"id": 3, "criterion": "Integration tests exist for every module boundary", "rigour": 3},
        {"id": 4, "criterion": "No TODO/FIXME without a DEF or CR tracking it", "rigour": 3},
        {"id": 5, "criterion": "Static analysis run; findings dispositioned", "rigour": 4},
    ],
    # ADDED 2026-08-31 — G2, G3, G5, G6, G7, G8 previously had ZERO
    # criteria defined here (only G0/G1/G4 existed), which meant calling
    # `gate G2` (or G3/G5/G6/G7/G8) always hit the "no criteria defined"
    # empty-list fallback in _build_gate_checklist — a project could pass
    # straight through Architecture, Interface, Verification, SCM, QA,
    # and Closure with nothing whatsoever to actually check against, in
    # a system whose entire premise is "no gate skip." Built from each
    # phase's own real definition in PHASES (name/artifact) and the
    # module boundaries the corresponding SEN102/212 lifecycle stage
    # actually covers — same depth and rigour-tiering style as the
    # existing G0/G1/G4 entries, not a placeholder.
    "G2": [
        {"id": 1, "criterion": "Every architectural component traces to at least one REQ/NFR/DOM", "rigour": 1},
        {"id": 2, "criterion": "Major design decisions recorded as ADRs (context, decision, consequences)", "rigour": 1},
        {"id": 3, "criterion": "Data model / storage design specified for every persisted entity", "rigour": 2},
        {"id": 4, "criterion": "External integrations and their failure modes identified", "rigour": 2},
        {"id": 5, "criterion": "NFRs (performance, security, scale) addressed by a specific design choice, not asserted", "rigour": 3},
        {"id": 6, "criterion": "At least one alternative architecture considered and rejected with a stated reason", "rigour": 3},
        {"id": 7, "criterion": "Architecture reviewed by someone other than its author", "rigour": 4},
    ],
    "G3": [
        {"id": 1, "criterion": "Every user-facing interaction traces to a REQ", "rigour": 1},
        {"id": 2, "criterion": "API/UI contract specified precisely enough to implement without guessing (request/response shapes, states, error cases)", "rigour": 1},
        {"id": 3, "criterion": "Every documented error/empty/loading state maps to how the interface actually shows it", "rigour": 2},
        {"id": 4, "criterion": "Accessibility or platform constraints stated, not left implicit", "rigour": 3},
        {"id": 5, "criterion": "Interface validated against the actual backend contract (endpoints/fields agree on both sides)", "rigour": 3},
    ],
    "G5": [
        {"id": 1, "criterion": "A test exists for every REQ/NFR/DOM marked as verified", "rigour": 1},
        {"id": 2, "criterion": "Test plan states pass/fail criteria before execution, not after", "rigour": 1},
        {"id": 3, "criterion": "Every found defect is logged with a DEF id and severity", "rigour": 2},
        {"id": 4, "criterion": "Every DEF is either fixed-and-reverified or explicitly deferred with a reason", "rigour": 2},
        {"id": 5, "criterion": "Regression suite re-run after every fix, not just the one failing case", "rigour": 3},
        {"id": 6, "criterion": "Coverage measured, not estimated", "rigour": 4},
    ],
    "G6": [
        {"id": 1, "criterion": "Every change to a controlled artifact has a CR id", "rigour": 1},
        {"id": 2, "criterion": "Branching/release strategy stated and actually followed (check real history, not intent)", "rigour": 1},
        {"id": 3, "criterion": "Release notes exist for every release and list what actually changed", "rigour": 2},
        {"id": 4, "criterion": "Rollback path defined before the release it applies to, not after an incident", "rigour": 3},
        {"id": 5, "criterion": "Configuration/secrets are not committed in plaintext anywhere in history", "rigour": 3},
    ],
    "G7": [
        {"id": 1, "criterion": "Quality management plan exists and names who owns quality decisions", "rigour": 1},
        {"id": 2, "criterion": "Defect density or an equivalent quality metric actually computed, not asserted", "rigour": 2},
        {"id": 3, "criterion": "Known-unresolved defects are explicitly risk-accepted by name, not silently shipped", "rigour": 2},
        {"id": 4, "criterion": "Process deviations from the stated plan are logged, not just the deviations someone noticed", "rigour": 3},
        {"id": 5, "criterion": "Independent quality review (not the constructor/verifier who did the work)", "rigour": 4},
    ],
    "G8": [
        {"id": 1, "criterion": "Every REQ/NFR/DOM is in a final state: MET, DEFERRED (with reason), or CUT (with reason) — none left ambiguous", "rigour": 1},
        {"id": 2, "criterion": "Maturity/process assessment actually written, not skipped as closeout busywork", "rigour": 1},
        {"id": 3, "criterion": "Lessons learned recorded — at least one concrete thing to do differently next time", "rigour": 2},
        {"id": 4, "criterion": "All open overrides from any gate reviewed and either closed out or explicitly carried forward with a reason", "rigour": 2},
        {"id": 5, "criterion": "Sign-off recorded from the person named in intake as having 'done' authority", "rigour": 3},
    ],
}

# ── INTAKE QUESTIONS ─────────────────────────────────────────────────────────

INTAKE_QUESTIONS = [
    "What problem are we solving? State it in one sentence in the user's words, not the solution.",
    "Who are the primary users and stakeholders? Name them specifically.",
    "Does this system touch money, health, safety, personal data, or a legal obligation? (Sets rigour floor.)",
    "What already exists? Code, docs, tickets — give me an inventory so we don't rebuild what works.",
    "What does 'done' mean, and who decides? Name the person with sign-off authority.",
    "What is the single risk that would most damage this project, and what would we see first if it materialised?",
    "Process style preference: agile, plan-driven, hybrid, or devops?",
]

# ── RIGOUR MATRIX ────────────────────────────────────────────────────────────

RIGOUR_MATRIX = {
    1: {"label": "advisory", "typical": "throwaway script, spike, prototype", "gates": "advisory, ledger still written"},
    2: {"label": "warn", "typical": "internal tool, portfolio site", "gates": "warn, one override allowed per gate"},
    3: {"label": "enforce", "typical": "production SaaS, startup product", "gates": "enforce, overrides logged"},
    4: {"label": "regulated", "typical": "finance, health records, government", "gates": "enforce, overrides need written justification"},
    5: {"label": "safety-critical", "typical": "medical device, avionics, traffic control", "gates": "no overrides, independent verification mandatory"},
}

# ── ADJECTIVE ANTI-PATTERN LIST (G1 criterion 3) ────────────────────────────

BANNED_ADJECTIVES = [
    "fast", "quick", "easy", "simple", "user-friendly", "user friendly",
    "intuitive", "secure", "scalable", "robust", "efficient", "reliable",
    "flexible", "modern", "seamless", "performant", "maintainable",
]

# ── HELPERS ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# BUG FOUND AND FIXED 2026-08-26 — "make ilana skill work". Confirmed via
# core/skill_executor.py: it only passes a skill's result through to the
# model when result["status"] is one of {"success","info","error",
# "timeout","blocked"} — anything else is treated as the SKILL BEING
# BUGGY, retried pointlessly (max_retries times, getting the identical
# status every time since this skill's logic is deterministic per call),
# then discarded entirely and replaced with a generic "Unexpected
# status" error. Every real status this skill used — "boot", "resumed",
# "ok", "override_recorded", "refused" — falls outside that allow-list,
# so the model has never once actually seen system_context, the boot
# block, a checklist, or a resume summary: the executor silently threw
# all of it away on every single call, every time, regardless of
# command. Wrapping every return point through this before it leaves
# run() — keeps the original, more specific status under "ilana_status"
# (still fully readable, still exactly what SKILL.md/INTEGRATION.md
# describe), while satisfying what the executor actually requires under
# "status" so the real payload finally reaches the model instead of
# being discarded.
_STATUS_MAP = {
    "boot": "success",
    "resumed": "success",
    "ok": "success",
    "override_recorded": "success",
    "refused": "blocked",
}


def _normalize_result(result: dict) -> dict:
    original = result.get("status")
    if original not in {"success", "info", "error", "timeout", "blocked"}:
        result = {**result, "ilana_status": original, "status": _STATUS_MAP.get(original, "success")}
    return result


def _detect_mode(message: str) -> str | None:
    """Cheaply map user intent to a mode before sending to the model."""
    msg = message.lower()
    if any(w in msg for w in ["fleet", "full lifecycle", "all agents", "full process", "end to end", "end-to-end"]):
        return "FLEET"
    if any(w in msg for w in ["audit this", "score this", "how bad is", "existing repo", "audit repo"]):
        return "AUDIT"
    if any(w in msg for w in ["teach me", "explain the process", "what is", "tutor", "learn"]):
        return "TUTOR"
    if any(w in msg for w in ["drill", "quiz", "assess me", "test me", "exam"]):
        return "DRILL"
    if any(w in msg for w in ["broken process", "missing deadlines", "doctor", "diagnose"]):
        return "DOCTOR"
    # Named single artifact → TASK
    artifacts = ["srs", "test plan", "design doc", "activity diagram", "defect report",
                 "branching strategy", "release notes", "scm plan", "traceability"]
    if any(a in msg for a in artifacts):
        return "TASK"
    return None


def _detect_rigour(answers: dict) -> int:
    """Estimate rigour from intake answers."""
    high_stakes = answers.get("high_stakes", "").lower()
    if any(w in high_stakes for w in ["medical", "health", "aviation", "traffic control", "critical"]):
        return 5
    if any(w in high_stakes for w in ["finance", "legal", "regulated", "government", "payment", "banking"]):
        return 4
    if any(w in high_stakes for w in ["personal data", "gdpr", "hipaa", "money", "security"]):
        return 3
    if answers.get("is_production", False):
        return 3
    return 2  # safe default for unknown projects


def _check_adjectives(text: str) -> list[str]:
    """Return banned adjectives found in text."""
    text_lower = text.lower()
    return [adj for adj in BANNED_ADJECTIVES if adj in text_lower]


def _build_gate_checklist(gate_id: str, rigour: int) -> list[dict]:
    """Return applicable criteria for this gate at this rigour."""
    criteria = GATE_CRITERIA.get(gate_id, [])
    return [c for c in criteria if c["rigour"] <= rigour]


def _build_system_prompt(mode: str, phase: str | None, agent: str | None, rigour: int) -> str:
    """
    Build the Ìlànà-flavoured system context injected into the Gemini call.
    This is not the full SKILL.md — it is a compact operational briefing
    sized for the current step.
    """
    lines = [
        "You are running inside OADSON's Ìlànà skill.",
        "Ìlànà is a software engineering process operating system.",
        "Ìlànà (Yoruba): the disciplined way a thing is done.",
        "",
        f"CURRENT MODE: {mode}",
        f"CURRENT RIGOUR: {rigour} ({RIGOUR_MATRIX.get(rigour, {}).get('label', '?')})",
    ]

    if phase:
        p = PHASES.get(phase, {})
        lines += [
            f"CURRENT PHASE: {phase} — {p.get('name', '?')}",
            f"PRIMARY AGENT: [{p.get('agent', '?')}]",
            f"PRIMARY ARTIFACT: {p.get('artifact', '?')}",
        ]

    if agent:
        lines += ["", f"YOU ARE SPEAKING AS: [{agent}]", "Prefix every message with your agent tag in brackets."]

    lines += [
        "",
        "NON-NEGOTIABLES (hold at all times):",
        "1. No requirement, no code. Every line traces to a REQ/NFR/DOM id.",
        "2. No gate skip. Gates are recorded with evidence.",
        "3. Ambiguity is a defect. Raise it, do not resolve it silently.",
        "4. Prevention beats detection. Reviews before tests.",
        "5. Measure or be silent. No 'faster' / 'better' without a metric.",
        "6. Every change is a change request. Scope creep is a process failure.",
        "7. Public interest outranks the deadline. ethics-officer may halt.",
        "8. Write it down or it did not happen. Ledger entry or it is not real.",
        "9. Ask before assuming.",
        "",
        "IDENTIFIER SCHEME (stable, never reused): REQ-###, NFR-###, DOM-###,",
        "DES-###, UI-###, TC-###, DEF-###, CR-###, RSK-###, DEC-###, GATE-##",
        "",
        "ARTIFACTS: every phase emits at least one file into the project repo.",
        "Emit artifacts as fenced code blocks with their intended path when",
        "filesystem access is unavailable (MEMO mode).",
    ]

    return "\n".join(lines)


# ── MAIN ENTRY POINT ─────────────────────────────────────────────────────────

async def run(payload: dict) -> dict:
    """
    Ìlànà OADSON skill entry point.

    payload keys (standard OADSON):
        message       — the user's current message
        user_id       — str
        session_id    — str
        surface       — 'web' | 'terminal' | 'whatsapp' | 'telegram' | 'app'
        args          — dict of parsed skill arguments (see manifest.json)
        db            — Motor database handle
        google_token  — live OAuth token (unused by this skill)
        github_token  — PAT (unused unless SCM phase)

    Returns a dict the orchestrator injects into its Gemini call as a
    structured skill result. The model then speaks as the appropriate agent.
    """
    message = payload.get("message", "").strip()
    args = payload.get("args", {})
    db = payload.get("db")
    user_id = payload.get("user_id", "")
    session_id = payload.get("session_id", "")
    surface = payload.get("surface", "web")

    command = args.get("command", "boot")  # boot | status | gate | override | resume
    gate_id = args.get("gate")             # G0-G8
    phase_id = args.get("phase")           # 01-11
    mode_override = args.get("mode")       # FLEET | TASK | AUDIT | ...
    rigour_override = args.get("rigour")   # 1-5
    agent_override = args.get("agent")     # named agent

    # ── Load existing Ìlànà state from DB ───────────────────────────────────
    state = None
    if db and user_id and session_id:
        try:
            doc = await db["ilana_state"].find_one(
                {"user_id": user_id, "session_id": session_id}
            )
            if doc:
                state = doc.get("state", {})
        except Exception as e:
            logger.warning(f"ilana: could not load state — {e}")

    is_cold_start = state is None
    state = state or {}

    # ── Resolve mode and rigour ──────────────────────────────────────────────
    mode = mode_override or state.get("mode") or _detect_mode(message) or "FLEET"
    rigour = int(rigour_override or state.get("rigour") or 2)
    phase = phase_id or state.get("phase")
    agent = agent_override or state.get("agent")
    current_gate = state.get("gate")
    open_items = state.get("open", [])
    overrides = state.get("overrides", [])

    # ── COMMAND ROUTING ──────────────────────────────────────────────────────

    if command == "status":
        return _normalize_result(_handle_status(state, mode, phase, agent, rigour, current_gate, open_items, overrides))

    if command == "gate":
        return _normalize_result(_handle_gate(gate_id, rigour, state, open_items))

    if command == "override":
        reason = args.get("reason", "user override — no reason given")
        return _normalize_result(_handle_override(gate_id, reason, state, overrides, rigour))

    # ── BOOT (default) ───────────────────────────────────────────────────────

    # Build the context block the orchestrator injects before the model call
    system_context = _build_system_prompt(mode, phase, agent, rigour)

    if is_cold_start:
        # Provide the fork question and intake questions as structured data
        # so the model can present them correctly on any surface.
        boot_block = {
            "announce": "Ìlànà online. Kernel loaded. Awaiting mode selection.",
            "ledger_found": False,
            "fork_question": {
                "prompt": "How should Ìlànà run this?",
                "options": [
                    {
                        "key": "1",
                        "label": "FLEET",
                        "description": "Spin up a bundle of specialist agents and run a full, gated lifecycle. Best for: a new feature, a new service, a rescue of something already broken, a full audit. Costs more. Produces a complete paper trail.",
                    },
                    {
                        "key": "2",
                        "label": "TASK",
                        "description": "One specialist, one focused operation, one artifact. Best for: 'write the test plan', 'review this diff', 'draw the activity diagram', 'compute defect density', 'check my Git branching'.",
                    },
                ],
                "other_modes": "Other modes: AUDIT (score an existing repo), TUTOR (learn the process), DOCTOR (diagnose the process itself), DRILL (self-assessment questions).",
            },
            "intake_questions": INTAKE_QUESTIONS,
            "note": "Ask intake questions in ONE batch — never one at a time.",
        }

        # If the user message already implies a mode, fast-path the fork
        detected = _detect_mode(message)
        if detected:
            boot_block["detected_mode"] = detected
            boot_block["detection_note"] = (
                f"Reading this as mode={detected}. "
                "Confirm or correct before proceeding."
            )

        result = {
            "status": "boot",
            "mode": mode,
            "rigour": rigour,
            "is_cold_start": True,
            "system_context": system_context,
            "boot": boot_block,
            "surface": surface,
        }

    else:
        # Resuming an existing Ìlànà session
        resume_summary = _build_resume_summary(state, mode, phase, agent, rigour, current_gate, open_items, overrides)

        result = {
            "status": "resumed",
            "mode": mode,
            "rigour": rigour,
            "phase": phase,
            "agent": agent,
            "gate": current_gate,
            "open_items": open_items,
            "overrides": overrides,
            "is_cold_start": False,
            "system_context": system_context,
            "resume_summary": resume_summary,
            "surface": surface,
        }

    # ── Persist updated state ────────────────────────────────────────────────
    updated_state = {
        "mode": mode,
        "rigour": rigour,
        "phase": phase,
        "agent": agent,
        "gate": current_gate,
        "open": open_items,
        "overrides": overrides,
        "updated_at": _now_iso(),
    }
    if db and user_id and session_id:
        try:
            await db["ilana_state"].update_one(
                {"user_id": user_id, "session_id": session_id},
                {"$set": {"state": updated_state, "user_id": user_id, "session_id": session_id}},
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"ilana: could not persist state — {e}")

    return _normalize_result(result)

def _handle_status(state, mode, phase, agent, rigour, current_gate, open_items, overrides) -> dict:
    phase_info = PHASES.get(phase, {}) if phase else {}
    rigour_info = RIGOUR_MATRIX.get(rigour, {})

    lines = [
        f"Ìlànà STATUS — {_now_iso()}",
        f"Mode:    {mode}",
        f"Phase:   {phase} — {phase_info.get('name', 'none')}",
        f"Agent:   [{agent}]" if agent else "Agent:   none",
        f"Gate:    {current_gate or 'none passed yet'}",
        f"Rigour:  {rigour} ({rigour_info.get('label', '?')})",
        f"Open:    {', '.join(open_items) if open_items else 'none'}",
    ]
    if overrides:
        lines.append(f"Overrides: {len(overrides)} recorded")

    return {
        "status": "ok",
        "summary": "\n".join(lines),
        "state": state,
        "open_items": open_items,
        "overrides": overrides,
    }


def _handle_gate(gate_id: str | None, rigour: int, state: dict, open_items: list) -> dict:
    if not gate_id:
        return {"status": "error", "reason": "gate_id required (G0-G8)"}

    criteria = _build_gate_checklist(gate_id, rigour)
    if not criteria:
        return {
            "status": "info",
            "gate": gate_id,
            "message": f"No criteria defined for {gate_id} at rigour {rigour}.",
        }

    checklist_text = [f"# {gate_id} GATE CHECKLIST (rigour {rigour})"]
    for c in criteria:
        checklist_text.append(f"- [ ] {c['id']}. {c['criterion']}")

    checklist_text += [
        "",
        "Fill in each item with: MET (evidence) | UNMET (reason) | N/A (rigour)",
        "Gate PASSES when all applicable criteria are MET.",
        "Gate FAILS: state which criteria are unmet and what evidence is needed.",
        "OVERRIDE: user may force open (rigour 1-4); records reason in ledger.",
        "TWO FAILURES at the same gate → escalation, not a third attempt.",
    ]

    return {
        "status": "ok",
        "gate": gate_id,
        "rigour": rigour,
        "checklist": "\n".join(checklist_text),
        "criteria": criteria,
        "open_items": open_items,
    }


def _handle_override(gate_id: str | None, reason: str, state: dict, overrides: list, rigour: int) -> dict:
    if not gate_id:
        return {"status": "error", "reason": "gate_id required"}

    if rigour >= 5:
        return {
            "status": "refused",
            "gate": gate_id,
            "message": (
                f"[conductor] OVERRIDE REFUSED. At RIGOUR 5 (safety-critical) "
                f"there are no overrides. {gate_id} must pass on its own merits "
                f"with independent verification. Ìlànà stops here."
            ),
        }

    override_record = {
        "gate": gate_id,
        "reason": reason,
        "at": _now_iso(),
        "rigour": rigour,
    }
    overrides.append(override_record)

    return {
        "status": "override_recorded",
        "gate": gate_id,
        "record": override_record,
        "message": (
            f"[conductor] OVERRIDE ACCEPTED for {gate_id}.\n"
            f"Reason recorded: \"{reason}\".\n"
            f"This override is permanent in the ledger. Advancing."
        ),
        "overrides": overrides,
    }


def _build_resume_summary(state, mode, phase, agent, rigour, current_gate, open_items, overrides) -> str:
    phase_info = PHASES.get(phase, {}) if phase else {}
    lines = [
        "[conductor] Resuming Ìlànà.",
        f"Phase: {phase} — {phase_info.get('name', 'none')}" if phase else "Phase: not started",
        f"Last gate passed: {current_gate}" if current_gate else "No gate passed yet.",
        f"Rigour: {rigour}",
    ]
    if open_items:
        lines.append(f"Open items: {', '.join(open_items)}")
    if overrides:
        lines.append(f"Overrides on record: {len(overrides)}")
    lines.append("What would you like to do next?")
    return "\n".join(lines)
