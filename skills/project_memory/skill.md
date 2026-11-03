# project_memory

## What It Does
Long-term memory scoped to a specific PROJECT — architecture decisions,
conventions, known gotchas ("don't do X, it broke before"). Distinct
from `memory_manager`, which stores facts about the USER, not a project.

## When to Use It
- User wants a technical decision or convention remembered for a
  specific project going forward
- Before making a change to a project, to check for known gotchas or
  past decisions that should inform the approach
- User asks "what did we decide about X" for a specific project

## When NOT to Use It
- The fact is about the user themselves (preferences, personal
  context) → `memory_manager`, not this
- A temporary note for just this session → `note_taker`

## Calling Convention
This is an `action`-based skill:

| Action | What it does | Key params |
|---|---|---|
| `save` | Store a new fact for a project | `key` (short id, e.g. `'auth_flow'`, `'db_choice'`), `value` (the fact), `project` (defaults to `'default'`) |
| `recall` | Retrieve one fact by its exact key | `key`, `project` |
| `list` | List all facts saved for a project | `project` |
| `delete` | Remove one fact | `key`, `project` |

## Constraints
- `project` defaults to `'default'` if omitted — for anyone juggling
  more than one active project, always pass it explicitly, or facts
  from different projects will collide in the same bucket.
