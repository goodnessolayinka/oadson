# proactive_config

## What It Does
Configures how OADSON's proactive/autonomous WhatsApp messages (morning
messages, check-ins) sound — tone, formality, language/accent. Only
affects messages OADSON sends on its own initiative, not replies within
an active conversation.

## When to Use It
- User asks to change how proactive messages sound: "talk to me more
  casually in the morning", "use pidgin for check-ins", "be more formal"
- User wants to see the current voice setting
- User wants to undo a customization and go back to default

## Calling Convention
This is an `action`-based skill:

| Action | What it does | Key params |
|---|---|---|
| `set_voice_style` | Update how OADSON sounds in proactive messages | `voice_style` (required — plain-English description, e.g. `'Casual Nigerian friend, can use pidgin'`, `'Formal and concise, like a professional assistant'`) |
| `view` | Show the current voice setting | none |
| `reset` | Restore the default (warm butler-English) | none |

## Constraints
- `voice_style` is a free-text description, not an enum — write it the
  way the user described it, don't try to map it to a fixed category.
- Only affects OADSON-initiated proactive messages, not normal chat replies.
