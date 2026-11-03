"""
core/run_control.py — explicit Stop for a running chat turn.

Why this exists: the frontend's Stop only aborted the client's fetch. On
Render (behind a proxy, inside a WebView) the server often never sees the
disconnect, so the tool loop kept calling Claude until the answer finished.
Each step costs money, so Stop has to reach the server directly.

Flow: the stream registers its run task here. POST /chat/stop (owner only)
cancels the task and sets a flag. The tool loop checks the flag at the top of
every step, so no new model call starts. At most one call that was already in
flight can still finish and bill.
"""
import asyncio

_tasks: dict = {}          # session_id -> (owner_user_id, task)
_stop_requested: set = set()


def begin(session_id: str, task: asyncio.Task, owner: str) -> None:
    _stop_requested.discard(session_id)
    _tasks[session_id] = (owner, task)


def end(session_id: str, task: asyncio.Task) -> None:
    cur = _tasks.get(session_id)
    if cur and cur[1] is task:
        _tasks.pop(session_id, None)
        _stop_requested.discard(session_id)


def is_stopped(session_id: str) -> bool:
    return bool(session_id) and session_id in _stop_requested


def request_stop(session_id: str, owner: str) -> bool:
    """True if a run was in progress for this owner and has been stopped."""
    cur = _tasks.get(session_id)
    if not cur or cur[0] != owner:
        return False
    _stop_requested.add(session_id)
    task = cur[1]
    if not task.done():
        task.cancel()
    return True
