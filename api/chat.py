"""
api/chat.py — OADSON V3 Chat Router.

Thin. Receives request. Hands to orchestrator. Returns response.

local_execution: true — terminal/CLI surfaces. As of 2026-08-23, these
use REAL native Gemini function-calling (see core/local_tool_schemas.py
and core/tool_loop.py's LOCAL_TOOL_NAMES pause/resume) rather than
prompt-parsed JSON-in-text — a decided local-tool call pauses the turn
and returns pending_tool_call + contents_snapshot for the client to
execute and resume with, instead of the model writing a JSON block into
its reply text for the client to regex out.
"""

import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from dependency import get_current_user, get_orchestrator, get_db

logger = logging.getLogger("oadson.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    surface: Optional[str] = "web"
    local_execution: Optional[bool] = False
    context: Optional[str] = None
    tool_results: Optional[list] = None
    # NATIVE FUNCTION-CALLING 2026-08-23: the client's echoed-back raw
    # Gemini contents snapshot (received as contents_snapshot in a prior
    # response's pending_tool_call, with the client's own functionResponse
    # appended after executing a local tool) — see core/tool_loop.py's
    # LOCAL_TOOL_NAMES pause/resume and core/local_tool_schemas.py's
    # module docstring for the full rationale. tool_results above is the
    # OLD prompt-parsed-JSON continuation shape, kept for compatibility;
    # this is what a native-function-calling resume actually sends now.
    resume_contents: Optional[list] = None
    # Real base64 image bytes only — never a URL, the model can't fetch
    # one and will hallucinate plausible-sounding content instead of
    # admitting it has nothing to look at.
    image_data: Optional[str] = None
    image_mime: Optional[str] = None
    # Added for parity with ChatStreamRequest (api/chat_stream.py) — this
    # endpoint's own docstring says "same request body", so a client
    # switching between /chat and /chat/stream shouldn't see 422s over
    # fields the other endpoint accepts.
    task_type: Optional[str] = None
    enable_reasoning: Optional[bool] = False
    file_data: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    cloudinary_url: Optional[str] = None
    file_text: Optional[str] = None
    attachments: Optional[list] = None
    # ADDED 2026-09-09 — user-selected model, from GET /models (see
    # api/models.py). ONLY honored for surface in ("web", "app", "pwa") —
    # enforced below, not here, so a client on any other surface sending
    # this by mistake gets silently ignored rather than 422ing or (worse)
    # accidentally reaching telegram/whatsapp/CLI's automatic routing.
    # Must exactly match an "id" from GET /models or it's treated as a
    # miss by core/provider_router.py's decide_preferred — which falls
    # through to normal automatic routing exactly as if this were never
    # sent, same fail-open pattern as every other fallback in this
    # codebase. Frontend wiring for the actual model-picker UI lives in
    # a separate piece of work — this field only carries the choice
    # through to the orchestrator.
    model: Optional[str] = None
    # ADDED 2026-11-02 — mobile call-mode. When true the orchestrator
    # forces the fastest configured model, skips the thinking/narration
    # step, instructs short plain sentences with no markdown, and caps
    # the tool loop to a single round. Default false = exactly today's
    # behaviour, so every existing client is unaffected.
    call_mode: Optional[bool] = False

    model_config = {
        "json_schema_extra": {
            "example": {
                "message": "What's on my calendar tomorrow?",
                "session_id": None,
                "surface": "web",
                "local_execution": False,
            }
        }
    }


class ChatResponse(BaseModel):
    response: str
    session_id: str
    model_used: str
    skill_triggered: Optional[str] = None
    steps: list = []
    # Was missing — orchestrator.run()'s exception handler always sets
    # this, but FastAPI's response_model silently drops any field not
    # declared here. That meant every crash showed only the generic
    # "Something went wrong. Try again." with zero diagnostic info ever
    # reaching the client, even though the real error was sitting in
    # Render's logs the whole time. Now it actually reaches you.
    error: Optional[str] = None
    # NATIVE FUNCTION-CALLING 2026-08-23 — same "silently dropped" risk
    # as the error field above, learned from the exact same lesson: a
    # turn paused on a local tool call (core/tool_loop.py's
    # LOCAL_TOOL_NAMES check) sets these on the result dict, but without
    # declaring them here FastAPI would have thrown them away and the
    # terminal client would see an empty response with no pending call
    # to act on at all — indistinguishable from a real, completed,
    # empty answer.
    pending_tool_call: Optional[dict] = None
    contents_snapshot: Optional[list] = None


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Send a message, get one JSON response",
    description=(
        "Runs the full orchestrator lifecycle (context, memory, skill "
        "calls, review, persistence) and returns one response after "
        "everything finishes. For live progress as skills execute, use "
        "POST /chat/stream instead — same request body, Server-Sent Events."
    ),
)
async def chat(
    body: ChatRequest,
    user: dict = Depends(get_current_user),
    orchestrator=Depends(get_orchestrator),
    db=Depends(get_db),
):
    # FIX 2026-08-24: was unconditional — rejected ANY empty message with
    # a 400, including a native-function-calling resume request, which
    # deliberately sends message="" since the actual turn content lives
    # in resume_contents instead (see core/orchestrator.py's contents
    # override right after the normal history-building block). Every
    # resume request hit this 400, every single time, deterministically
    # — not a network issue at all, despite genuinely LOOKING like one
    # from the client (the same request retried 2 more times per
    # agent.py's CONTINUATION_RETRIES, failing identically each time,
    # since retrying doesn't change a request the server was always
    # going to reject on shape alone).
    if not body.resume_contents and (not body.message or not body.message.strip()):
        raise HTTPException(400, "Message cannot be empty.")

    # Fire-and-forget — nudges the background job queue using this request's
    # traffic as the "the service happens to be awake" signal. See
    # core/job_queue.py's docstring for why this matters on Render's free
    # tier. Never awaited: must not add latency to this actual chat request.
    import asyncio
    from core.job_queue import kick_job_queue
    asyncio.create_task(kick_job_queue(db, orchestrator))

    session_id = body.session_id or str(uuid.uuid4())

    # Build objective — include context if CLI sent it
    #
    # SIZE CAP 2026-08-31: body.context was completely unbounded — a CLI
    # client sending a large or repeated shell/log dump as "context" on
    # every single message compounds real cost twice over: bigger tokens
    # per turn (every provider call pays for it), and bigger objects
    # held in memory server-side while building/transmitting the
    # request/response — a real contributor to a 512MB box running
    # closer to its floor than it should before any heavy operation
    # (like a repo clone) even starts. Capped generously — 30KB is far
    # more than a directory tree + git status + recent command output
    # legitimately needs — and truncated with a clear note rather than
    # silently dropped or hard-erroring, so the CLI side doesn't need a
    # matching change to keep working; it just stops being able to
    # flood a single turn.
    SHELL_CONTEXT_MAX_CHARS = 30_000
    objective = body.message.strip()
    if body.context:
        shell_context = body.context
        if len(shell_context) > SHELL_CONTEXT_MAX_CHARS:
            shell_context = (
                shell_context[:SHELL_CONTEXT_MAX_CHARS]
                + f"\n...[truncated — shell context was {len(body.context)} chars, "
                f"capped at {SHELL_CONTEXT_MAX_CHARS}]"
            )
        objective = f"{objective}\n\n[Shell Context]\n{shell_context}"

    # Real multi-file support via the shared processor — see
    # core/attachment_processor.py and api/chat_stream.py's identical
    # wiring for why. Falls back to the legacy single-file fields if the
    # client didn't send the new `attachments` array.
    from core.attachment_processor import process_attachments

    attachments_in = body.attachments
    if not attachments_in and (body.file_data or body.file_text or body.cloudinary_url):
        attachments_in = [{
            "file_name": body.file_name,
            "file_type": body.file_type,
            "file_data": body.file_data,
            "file_text": body.file_text,
            "cloudinary_url": body.cloudinary_url,
        }]

    att_result = await process_attachments(str(user["_id"]), objective, attachments_in)
    objective = att_result["objective"]

    image_data = body.image_data or att_result["image_data"]
    image_mime = body.image_mime or att_result["image_mime"]
    extra_images = att_result["extra_images"]

    # Durable attachment reference for persistence — see orchestrator.run()'s
    # attachment_url/type/filename docstring. image_data/extra_images above
    # are deliberately ephemeral (used only for this one Gemini call, never
    # saved), and nothing else was ever passed through as the durable
    # reference _persist_memory() actually writes to the message — so a
    # web-sent attachment had no way to survive being displayed again
    # after a reload. Only the first attachment with a real URL is used:
    # _persist_memory() (and the web client reading it back) only support
    # one attachment per message.
    attachment_url = None
    attachment_type = None
    attachment_filename = None
    for a in (attachments_in or []):
        url = a.get("cloudinary_url")
        if url:
            attachment_url = url
            attachment_type = a.get("file_type")
            attachment_filename = a.get("file_name")
            break

    result = await orchestrator.run(
        objective=objective,
        user=user,
        session_id=session_id,
        surface=body.surface or "web",
        local_execution=body.local_execution or False,
        image_data=image_data,
        image_mime=image_mime,
        extra_images=extra_images,
        enable_reasoning=body.enable_reasoning or False,
        attachment_url=attachment_url,
        attachment_type=attachment_type,
        attachment_filename=attachment_filename,
        tool_results=body.tool_results,
        resume_contents=body.resume_contents,
        # SURFACE GATE 2026-09-09 — model selection is a web/app/pwa-only
        # feature (see ChatRequest.model's field comment above). A
        # telegram/whatsapp/CLI/terminal request that somehow carries
        # this field still gets ignored here rather than trusted, so the
        # gate holds even if a future client bug sends it from the
        # wrong surface — this is the one real enforcement point, same
        # role core/provider_registry.py's local_qwen_cli comment
        # describes for ITS own surface gate.
        preferred_model=body.model if (body.surface or "web") in ("web", "app", "pwa") else None,
        call_mode=body.call_mode or False,
    )

    if "error" in result and not result.get("response"):
        raise HTTPException(500, result["error"])

    return ChatResponse(
        response=result["response"],
        session_id=session_id,
        model_used=result.get("model_used", "unknown"),
        skill_triggered=result.get("skill_triggered"),
        steps=result.get("steps", []),
        pending_tool_call=result.get("pending_tool_call"),
        contents_snapshot=result.get("contents_snapshot"),
        # Was silently dropped here even after ChatResponse gained this
        # field — orchestrator.run()'s exception handler always sets a
        # non-empty "response" alongside "error", so the guard above never
        # fires, and this constructor call is the only other place "error"
        # could reach the client. Now it actually does.
        error=result.get("error"),
    )
