"""
api/jobs.py — OADSON V3 Background Jobs API.

Lets a client fire off a chat-like request without holding the HTTP
connection open for however long the full agent loop takes, then poll
for the result. See core/job_queue.py's module docstring for the honest
limitation: this is NOT guaranteed 24/7 execution on Render's free tier —
jobs progress while the service is awake (your own traffic, or an
external keep-alive ping, is what keeps that true) and resume safely
whenever it wakes back up otherwise.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from dependency import get_current_user, get_db, get_orchestrator
from core.job_queue import enqueue_job, get_job, list_jobs, kick_job_queue

logger = logging.getLogger("oadson.api.jobs")

router = APIRouter()


class EnqueueJobRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    surface: Optional[str] = "background"

    model_config = {
        "json_schema_extra": {
            "example": {"message": "Summarize my last 20 emails and email me the summary"}
        }
    }


@router.post(
    "/jobs",
    summary="Enqueue a background chat task",
    description=(
        "Returns immediately with a job_id instead of waiting for the full "
        "agent loop to finish. Poll GET /jobs/{job_id} for status/result. "
        "Not guaranteed real-time on Render's free tier — see README "
        "'Background jobs' for why."
    ),
)
async def create_job(
    body: EnqueueJobRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    orchestrator=Depends(get_orchestrator),
):
    if not body.message or not body.message.strip():
        raise HTTPException(400, "Message cannot be empty.")

    import uuid
    session_id = body.session_id or str(uuid.uuid4())

    job_id = await enqueue_job(
        db, user_id=user["_id"], session_id=session_id, job_type="chat",
        payload={"message": body.message.strip(), "surface": body.surface or "background"},
    )

    # Nudge the queue immediately so a job doesn't just sit there until
    # the dedicated loop's next idle-interval tick or the next unrelated
    # request happens to hit the service.
    await kick_job_queue(db, orchestrator)

    return {"status": "queued", "job_id": job_id, "session_id": session_id}


@router.get("/jobs/{job_id}", summary="Check a background job's status/result")
async def get_job_status(
    job_id: str,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    job = await get_job(db, user_id=user["_id"], job_id=job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


@router.get("/jobs", summary="List recent background jobs")
async def list_recent_jobs(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
    limit: int = 20,
):
    jobs = await list_jobs(db, user_id=user["_id"], limit=limit)
    return {"jobs": jobs, "count": len(jobs)}
