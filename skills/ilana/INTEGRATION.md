# Ìlànà — OADSON Integration Guide

Two things to do after dropping the `ilana/` folder into `skills/`.

---

## 1. Add the MongoDB index

In `Oadson_V3/database.py`, inside `create_indexes()`, add:

```python
# ÌLÀNÀ process state
await db["ilana_state"].create_index(
    [("user_id", 1), ("session_id", 1)], unique=True
)
await db["ilana_state"].create_index([("user_id", 1)])
```

---

## 2. Add the `/ilana` REPL command (oadson_pkg)

In `oadson_pkg/oadson/cli.py`, add this to the REPL command table:

```python
"/ilana": "Boot Ìlànà — the software engineering process",
```

And in the command handler block (`_handle_command` or the `if cmd.startswith("/")`
section), add:

```python
elif cmd == "/ilana" or cmd.startswith("/ilana "):
    # Pass any text after /ilana as the initial message
    suffix = cmd[7:].strip()
    initial = suffix or "boot ilana"
    reply = run(initial, session_id, stream=False)
    console.print(reply)
    return
```

---

## 3. Trigger phrases (already in manifest.json description)

The orchestrator's skill router will load Ìlànà automatically on:
- "run the process", "ship this properly", "gate this", "audit this repo"
- "ilana", "/ilana"
- Any mention of requirements, SRS, test plan, CMMI, ISO/IEC 12207,
  coding standards, defect tracking, SCM, quality assurance, ethics

---

## 4. Surface behaviour

| Surface   | Behavior                                                              |
|-----------|-----------------------------------------------------------------------|
| Web/App   | Full markdown output; artifacts shown as fenced code blocks           |
| Terminal  | Rich-formatted output; `/ilana` REPL command                          |
| WhatsApp  | MEMO mode auto-detected (no filesystem); artifacts as fenced blocks   |
| Telegram  | Same as WhatsApp                                                      |

---

## 5. MEMO mode (WhatsApp / Telegram)

When `surface` is `whatsapp` or `telegram`, the skill signals MEMO mode.
The model emits artifacts as:

```
📄 .ilana/ledger.md
\`\`\`markdown
## 2026-08-26 20:41 UTC | G0 | conductor | INTAKE COMPLETE
...
\`\`\`
```

The user can copy and paste these into their repo manually, or run
`oadson "write this to .ilana/ledger.md"` from the terminal.

---

## File layout after installation

```
Oadson_V3/
  skills/
    ilana/
      skill.py       ← main entry point
      manifest.json  ← Gemini functionDeclaration schema
      SKILL.md       ← operator guide
      INTEGRATION.md ← this file
```
