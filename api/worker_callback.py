"""
Where workers send results and plan progress. Mounted at the root of the owner
(not under /api/v3), so the path a worker signs is exactly the path the owner sees.

Both INTERNAL_SECRET and INTERNAL_SECRET_NEXT are accepted, so a rotation can
happen without a gap.
"""
import json
import os

from fastapi import APIRouter, HTTPException, Request

from core import remote_jobs as _rj
from core.worker_contracts.signing import SIG_HEADER, TS_HEADER, verify_any

router = APIRouter()


def _secrets() -> list:
    return [os.getenv("INTERNAL_SECRET", ""), os.getenv("INTERNAL_SECRET_NEXT", "")]


async def _checked(request: Request) -> bytes:
    body = await request.body()
    ok, reason = verify_any(_secrets(), "POST", request.url.path, body,
                            request.headers.get(TS_HEADER), request.headers.get(SIG_HEADER))
    if not ok:
        raise HTTPException(401, reason)
    return body


@router.post("/internal/v1/jobs/{job_id}/result")
async def worker_result(job_id: str, request: Request):
    body = await _checked(request)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(422, "body is not JSON")
    if not isinstance(payload, dict) or payload.get("job_id") != job_id:
        raise HTTPException(422, "job_id in the body does not match the path")
    try:
        await _rj.jobs.record_result(job_id, payload)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"status": "received"}


@router.post("/internal/v1/plans/{plan_id}/progress")
async def plan_progress(plan_id: str, request: Request):
    body = await _checked(request)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(422, "body is not JSON")
    if not isinstance(payload, dict) or payload.get("plan_id") != plan_id:
        raise HTTPException(422, "plan_id in the body does not match the path")
    await _rj.jobs.record_progress(plan_id, payload)
    return {"status": "received"}
