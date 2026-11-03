"""
core/interactive_tool_schemas.py — Gemini function-declaration schema
for OADSON's own question-and-options capability.

ADDED 2026-09-09.

Deliberately a SEPARATE file/set from core/local_tool_schemas.py's
LOCAL_TOOL_NAMES, not merged into it, even though the pause/resume
MECHANISM this reuses (see core/tool_loop.py's INTERACTIVE_TOOL_NAMES
check) is architecturally the same shape as the local-tool pause. The
two are conceptually opposite despite the shared mechanism:

  - LOCAL_TOOL_NAMES = things ONLY the terminal client can execute,
    because they run commands/files on the user's own machine. Gated
    on local_execution=True, a flag conceptually (if not mechanically)
    specific to terminal/CLI surfaces — see api/chat.py's module
    docstring: "local_execution: true — terminal/CLI surfaces."

  - INTERACTIVE_TOOL_NAMES (this file) = the exact opposite surface.
    ask_user_tray only renders as a real tappable tray on web/app/pwa
    — see core/tool_loop.py's gate, keyed on `surface`, NOT on
    local_execution, and NOT reusing or checking that flag at all.
    Putting this tool in LOCAL_TOOL_NAMES would have been a real bug:
    either it'd accidentally require local_execution=True to fire on
    web (wrong — trays have nothing to do with local execution), or a
    terminal client sending local_execution=True would accidentally
    be able to trigger it (also wrong — terminal has its own separate
    plain-text A/B fallback, see below).

Offered to the model on EVERY surface, unconditionally — merged into
the base function_declarations in orchestrator.py's
_build_function_declarations() call site, not gated behind
local_execution the way LOCAL_TOOL_DECLARATIONS is. The model always
has exactly one tool to reach for when it wants to ask something
important; what actually happens when it's called differs by surface
(see tool_loop.py), not what's offered.

Non-tray surfaces (telegram, whatsapp, cli/terminal) still see this
declared and can still call it — the loop just executes it immediately
server-side into a real functionResponse, formatting the question and
lettered options as plain text ("A. ... / B. ... — reply with a letter
or tell me your answer") instead of pausing for a client-rendered tray.
Same tool, same schema, same reasoning model, different rendering —
deliberately NOT two different tools/prompts to keep the model's
behavior consistent across surfaces.
"""

INTERACTIVE_TOOL_DECLARATIONS = [
    {
        "name": "ask_user_tray",
        "description": (
            "Ask the user a clarifying or decision-important question with "
            "2-4 short, mutually exclusive options — use this before doing "
            "something significant or hard to undo (e.g. deleting something, "
            "picking between real alternatives, confirming an irreversible "
            "action) when you genuinely need their choice before proceeding. "
            "Do not use this for simple factual questions you can just ask "
            "in normal text, and do not use it if the answer is already "
            "clear from context. On surfaces that support it, this renders "
            "as tappable options; elsewhere it's presented as a short "
            "lettered list the user can reply to."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask, one sentence, no preamble.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2 to 4 short, mutually exclusive option labels.",
                    "minItems": 2,
                    "maxItems": 4,
                },
            },
            "required": ["question", "options"],
        },
    },
]

INTERACTIVE_TOOL_NAMES = frozenset(d["name"] for d in INTERACTIVE_TOOL_DECLARATIONS)
