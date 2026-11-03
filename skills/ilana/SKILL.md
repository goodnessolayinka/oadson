# ÌLÀNÀ — OADSON Skill

> Ìlànà (Yoruba): the disciplined way a thing is done.

Derived from SEN102/212 Software Engineering Process, Obafemi Awolowo University, a sample city.
Original repo: https://github.com/Ferousco-dev/ilana (MIT)

---

## What this skill does

Ìlànà is a software engineering process operating system. When it runs inside OADSON:

- It boots, interrogates, plans, gates, executes, measures, and records.
- It refuses to skip steps. It writes everything down.
- It works on every OADSON surface (web, terminal, WhatsApp, Telegram).
- In MEMO mode (no filesystem), it emits all artifacts as fenced code blocks.

---

## How the orchestrator uses it

`skill.py` returns a structured result dict containing:

- `system_context` — a compact operational briefing the model prepends before its answer.
- `boot` — the fork question + intake questions for cold starts.
- `resume_summary` — ledger recap for returning sessions.
- `checklist` — gate criteria at the current rigour level.

The orchestrator injects `system_context` into the Gemini call so the model speaks as the
correct Ìlànà agent ([analyst], [conductor], etc.) and holds the non-negotiables in working
memory throughout the turn.

---

## Modes

| Mode   | When                                      | Agents      |
|--------|-------------------------------------------|-------------|
| FLEET  | Build or rescue something end-to-end      | Up to 13    |
| TASK   | One artifact, one specialist              | 1           |
| AUDIT  | Score an existing codebase                | 4–6         |
| TUTOR  | Learn the process                         | conductor   |
| DOCTOR | Diagnose a broken process                 | 3           |
| DRILL  | Self-assessment quiz                      | 1           |

---

## Phases and gates

```
G0 INTAKE
  |
01 REQUIREMENTS --G1--> 02 ARCHITECTURE --G2--> 03 INTERFACE --G3--> 04 CONSTRUCTION
                                                                           |
                                                                          G4
                                                                           |
08 PROCESS ASSESSMENT <--G7-- 07 QUALITY ASSURANCE <--G6-- 06 SCM <--G5-- 05 VERIFICATION
         |
        G8 CLOSURE

09 PROCESS MODELING  ........... continuous
10 TOOLING           ........... continuous
11 ETHICS AND TEAMS  ........... continuous
```

---

## State persistence

Ìlànà state is stored in `db["ilana_state"]` keyed by `user_id + session_id`.

Fields: `mode`, `rigour`, `phase`, `agent`, `gate`, `open` (list of open IDs),
`overrides` (list of gate override records).

---

## Commands

- `boot` — cold start or resume (default)
- `status` — show current phase / gate / open items
- `gate G<n>` — run the checklist for gate G0–G8
- `override G<n> reason="..."` — force a gate open; recorded permanently
- `resume` — explicit resume from last state

---

## Non-negotiables (always active)

1. No requirement, no code.
2. No gate skip.
3. Ambiguity is a defect.
4. Prevention beats detection.
5. Measure or be silent.
6. Every change is a change request.
7. Public interest outranks the deadline.
8. Write it down or it did not happen.
9. Ask before assuming.

---

## MongoDB collection

`ilana_state` — one document per `(user_id, session_id)`.

Suggested index (add to `database.py → create_indexes()`):
```python
await db["ilana_state"].create_index(
    [("user_id", 1), ("session_id", 1)], unique=True
)
await db["ilana_state"].create_index([("user_id", 1)])
```
