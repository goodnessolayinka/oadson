# Memory Manager

## What It Does
Manages OADSON's persistent memory vault for the user — key facts,
preferences, and context that survive across sessions. Two tiers:
**extended** (retrieved only when relevant to what's being discussed) and
**core** (always injected into every conversation, for the handful of
facts that matter on every single turn).

## Calling Convention — this is an `action`-based skill
Every call needs `action` set to one of the values below — there is no
free-text `message` parameter. Map what the user said to the right
action instead of describing it in prose.

## Action Reference
| Action | Use for | Key params |
|---|---|---|
| `save` | Store a new fact (extended tier) | `key` (short id, e.g. `github_username`), `value`, `category` (optional) |
| `recall` | Retrieve one memory by its EXACT key | `key` — if you're not certain of the exact key, use `search` instead |
| `search` | Find memories matching a term, across key AND content | `query`, `limit` (default 10) |
| `list` | List all memories, optionally by category | `category` (optional) |
| `delete` | Remove ONE memory by its exact, already-known key | `key` |
| `delete_matching` | Remove EVERY memory matching a topic, in one call | `query` (describes the topic, e.g. `'sleep tracking'`) |
| `pin` | Promote an existing extended memory to core (always-in-context) | `key` |
| `unpin` | Demote a core memory back to extended (retrieved only when relevant) | `key` |
| `save_core` | Store a new fact directly as core (skip the extended tier) | `key`, `value` |
| `list_core` | Show everything currently in core memory | none |
| `update_core` | Add a fact into the core-memory summary, as a written sentence | `fact` (a complete, descriptive sentence) |
| `show_core` | Display the current core-memory summary as the user would see it | none |

## Two important call-count rules

**Forgetting a whole topic is ONE call, not a search-then-loop.** When
the user asks to forget/clear/remove everything about a topic ("forget my
sleep tracking", "clear everything about the GST project"), call
`delete_matching` with `query` describing the topic — it finds and
removes every matching memory itself in one call. Do not `search` then
`delete` each result in a loop: that's slower (a separate round-trip per
delete) and can leave stragglers if the search missed one. Reserve plain
`delete` for when the user names one exact, already-known key.

**Prefer `search` over `recall` unless you're certain of the exact
key.** `recall` requires an exact match; a slightly-off guess just
returns nothing. `search` matches against both key and content.

## Core vs Extended — which one to use
- Something the user will want remembered but doesn't need surfaced on
  every unrelated turn → `save` (extended). This is the default for most
  facts.
- Something that should shape EVERY response regardless of topic (a
  standing instruction, a defining fact about who they are or what
  they're always working on) and the user explicitly asked for it to be
  always-on → `save_core` or `pin` an existing extended memory.
- Use `update_core` specifically when the user asks to "add this to core
  memory" / "always remember this" as a described fact rather than a
  discrete key/value pair.

## Constraints
- Memory is scoped per user — private, never shared across users.
- Max 500 memory entries per user.
- Entries are text-based (no binary/file storage — that's `file_session`
  or `document_reader`'s sandbox, not this).
- Memory does NOT auto-inject into every conversation (except the core
  tier) — extended memories must be actively retrieved via `recall`/`search`.
- Conflicting memories: last-written wins unless explicitly merged.

## Examples
- "Remember my GitHub username is goodness-olamiposi" → `save`,
  `key='github_username'`, `value='goodness-olamiposi'`
- "What stack did I say I was using for example-app?" → `search`,
  `query='example-app stack'`
- "Forget everything about my old project idea" → `delete_matching`,
  `query='old project idea'`
- "Always remember I prefer concise answers" → `update_core`,
  `fact='Prefers concise answers.'`
