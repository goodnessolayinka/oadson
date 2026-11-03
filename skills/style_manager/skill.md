# style_manager — "memory_manager, but for how you think and text"

**Updated after your follow-up:** learning is now fully automatic, no
command needed, ever. Only `write` stays on-demand. Files: new
`core/style_profile.py`, new `skills/style_manager/` (both files),
and one small addition to the existing `core/orchestrator.py` (the
automatic trigger — everything else in that file is unchanged from
before this batch).

## The change

- **`learn` no longer needs to be asked for.** After every chat turn,
  the orchestrator fires a background check (fire-and-forget —
  `asyncio.create_task`, never awaited, never able to slow down or break
  the actual response) that counts how many new messages you've sent
  since the profile was last updated. Once that hits
  `AUTO_LEARN_MESSAGE_THRESHOLD` (15), it actually runs the learning
  pass. Below that, it's a cheap count-only Mongo query and nothing else
  happens.
- **Why batched instead of literally every message:** learning costs one
  real Gemini call. Firing that after every single turn would mean a
  constant extra API call on top of the decision call (and whatever
  thinking/planner calls you have enabled) — exactly the kind of silent
  quota cost you've pushed back on more than once in this project. This
  keeps it genuinely automatic (you never have to ask) while still only
  paying for it in batches of real new signal, not on every turn.
- **`learn` action still exists** in the skill, now reframed as a manual
  force-refresh — if you explicitly say "update my style profile now,"
  it can still do that immediately instead of waiting for the threshold.
- **`write`, `view`, `reset` are unchanged** — still on-demand only, per
  your original ask.

## Where the automatic trigger actually lives

`core/orchestrator.py`, right after `_persist_memory()` in `run()` — so
it fires after every real turn, using whatever messages just got saved
to `ai_sessions`. Fire-and-forget, same pattern as the job-queue kicks
from earlier — the chat response you get back is never delayed or
affected by this running in the background.

## What it does (unchanged from before)

- **`view`** — shows the current stored profile: tone summary, specific
  Pidgin/Yoruba phrases actually observed (with real examples, not
  invented ones), structural habits, recurring phrases.
- **`write`** — generates content in your voice for a specific request.
  Auto-bootstraps a profile on first use if none exists yet.
- **`reset`** — clears the profile entirely.

## Why it's a merge, not an overwrite, each time it learns

The existing profile gets fed back into the prompt alongside only the
NEW messages since last time, and Gemini is explicitly asked to keep
confirmed patterns, add newly noticed ones, adjust what's changed — not
start from scratch.

## Guardrail worth knowing about

The prompt explicitly instructs Gemini to only report Pidgin/Yoruba (or
any other pattern) if there's REAL evidence in the actual messages shown
— not to invent usage that isn't there. Worth spot-checking the first
`view` output against messages you actually remember sending.

## One honest limitation, not glossed over

This only ever sees what you've typed TO OADSON. It may not fully
capture how you'd write a formal assignment or text a close friend if
those registers genuinely differ from how you talk here.

## How to test

Since learning is now automatic, the fastest way to see it work: just
keep chatting normally. Once you've sent ~15 messages total (or since
the last auto-learn), the next turn should trigger a background pass —
check server logs for "Auto-learned style profile" to confirm it fired.
Then:

```json
{
  "message": "show me my style profile",
  "session_id": "2ce2e3f6-9816-4d55-be58-e37e7fb17dce",
  "surface": "web"
}
```

```json
{
  "message": "write a WhatsApp text in my style telling my friend I'll be 20 minutes late",
  "session_id": "2ce2e3f6-9816-4d55-be58-e37e7fb17dce",
  "surface": "web"
}
```
Judge for yourself whether it actually sounds like you.
