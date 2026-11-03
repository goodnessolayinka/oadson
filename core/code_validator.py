"""
core/code_validator.py — real code validation, not just ast.parse.

Three tiers, cheapest first:
  1. Syntax check (ast.parse) — free, instant, Python-only.
  2. Execution check (actually run it in a subprocess) — catches real
     logic/runtime errors ast.parse can't see (NameError, wrong imports,
     a function that raises immediately, etc.) — this is the "test
     endpoints locally" behavior, generalized: run the script, or boot
     the FastAPI app and hit real endpoints with real HTTP requests.
  3. Iterative repair — when either check finds problems, feed the
     actual error back to Gemini for a fix, re-check, repeat up to
     max_iterations times.

HONEST SECURITY NOTE, not buried: tier 2 executes model-generated code
as a real subprocess ON THE SAME RENDER WEB SERVICE that's serving your
actual production traffic. This is not a sandbox — it's a subprocess
with a hard timeout and forced kill, which bounds *how long* something
can misbehave, not *what* it can do while running. A generated script
could still consume real CPU/memory for up to the timeout window, open
network connections, or attempt filesystem writes (which land on
Render's ephemeral disk, at least, so nothing persists badly). This is
reasonable for a single-user personal system validating its own
generated code — it would NOT be reasonable to expose this to arbitrary
untrusted input from other people. Keep it that way: this only ever
runs on code OADSON itself generated or that you explicitly asked it to
test, never on arbitrary user-submitted code from an open surface.

Quota-conscious by design, same principle as thinking/planner: syntax
and execution checks cost nothing extra (no Gemini call). Only the
REPAIR step costs a Gemini call, and only fires when something's
actually broken — a clean first generation never pays for repair.
"""

import ast
import asyncio
import json
import logging
import os
import socket
import tempfile
import shutil
from pathlib import Path

import httpx

logger = logging.getLogger("oadson.code_validator")

EXEC_TIMEOUT_SECONDS = 8
BOOT_TIMEOUT_SECONDS = 6
MAX_OUTPUT_CHARS = 2000


def check_python_syntax(files: list) -> list:
    """Returns [{"path":, "error":}] for every .py file that fails ast.parse()."""
    errors = []
    for f in files:
        path = f.get("path", "")
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(f.get("content", ""), filename=path)
        except SyntaxError as e:
            errors.append({"path": path, "error": f"Line {e.lineno}: {e.msg}"})
    return errors


async def run_script(content: str, timeout: int = EXEC_TIMEOUT_SECONDS) -> dict:
    """
    Actually runs one Python file in a subprocess (not imported in-process
    — a crash or hang here must never take down the main OADSON server).
    Hard timeout, forced kill, always cleans up.
    """
    tmpdir = tempfile.mkdtemp(prefix="oadson_run_")
    script_path = os.path.join(tmpdir, "script.py")
    try:
        with open(script_path, "w") as f:
            f.write(content)

        proc = await asyncio.create_subprocess_exec(
            "python3", script_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=tmpdir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout, stderr = b"", b"Execution exceeded timeout - killed."
            timed_out = True

        return {
            "ran": True,
            "timed_out": timed_out,
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace")[:MAX_OUTPUT_CHARS],
            "stderr": stderr.decode(errors="replace")[:MAX_OUTPUT_CHARS],
            "success": (not timed_out) and proc.returncode == 0,
        }
    except Exception as e:
        return {"ran": False, "error": str(e), "success": False}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_fastapi_endpoints(
    files: list, entrypoint: str, endpoints: list,
    boot_timeout: int = BOOT_TIMEOUT_SECONDS,
) -> dict:
    """
    Writes the project to a temp directory, boots it with uvicorn as a
    subprocess on a free local port, hits each endpoint with a real GET
    request, then kills the subprocess. This is the "test_endpoint"
    behavior generalized to server-side generated projects (the CLI's
    local `test_endpoint` tool does the equivalent thing on your own
    machine when surface="terminal" — this is the same idea, but for
    code OADSON just generated server-side, where there's no CLI
    attached to run it locally).

    entrypoint: module:app style, e.g. "main:app" — must match how the
    generated project's ASGI app is actually exposed.
    """
    tmpdir = tempfile.mkdtemp(prefix="oadson_test_")
    proc = None
    try:
        for f in files:
            path = os.path.join(tmpdir, f.get("path", "").lstrip("/"))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(f.get("content", ""))

        port = _free_port()
        proc = await asyncio.create_subprocess_exec(
            "uvicorn", entrypoint, "--host", "127.0.0.1", "--port", str(port),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=tmpdir,
        )

        # Poll for boot instead of a fixed sleep — faster when it boots
        # quickly, still bounded by boot_timeout when it doesn't.
        base_url = f"http://127.0.0.1:{port}"
        booted = False
        async with httpx.AsyncClient(timeout=1) as client:
            for _ in range(boot_timeout * 2):
                if proc.returncode is not None:
                    break  # crashed before boot finished
                try:
                    await client.get(base_url)
                    booted = True
                    break
                except Exception:
                    await asyncio.sleep(0.5)

        if not booted:
            stdout, stderr = b"", b""
            if proc.returncode is not None:
                stdout, stderr = await proc.communicate()
            return {
                "booted": False,
                "error": "App did not boot within timeout — likely an import error or startup exception.",
                "stderr": stderr.decode(errors="replace")[:MAX_OUTPUT_CHARS] if stderr else None,
                "success": False,
            }

        results = []
        async with httpx.AsyncClient(timeout=5) as client:
            for ep in endpoints:
                try:
                    res = await client.get(f"{base_url}{ep}")
                    results.append({
                        "endpoint": ep, "status_code": res.status_code,
                        "ok": res.status_code < 500,
                        "body_preview": res.text[:500],
                    })
                except Exception as e:
                    results.append({"endpoint": ep, "ok": False, "error": str(e)})

        all_ok = all(r.get("ok") for r in results) if results else True
        return {"booted": True, "endpoints": results, "success": all_ok}

    except Exception as e:
        return {"booted": False, "error": str(e), "success": False}
    finally:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _request_fix(api_key: str, model: str, files: list, problems: list) -> list:
    """One Gemini call: given the broken files + the actual errors, get back fixed content."""
    broken_paths = {p["path"] for p in problems if "path" in p}
    broken_files = [f for f in files if f["path"] in broken_paths] or files

    problems_text = "\n".join(
        f"- {p.get('path', 'unknown')}: {p.get('error', p.get('stderr', 'unknown error'))}"
        for p in problems
    )
    files_text = "\n\n".join(
        f"--- {f['path']} ---\n{f['content']}" for f in broken_files
    )

    prompt = f"""These files have real errors. Fix them.

ERRORS FOUND:
{problems_text}

FILES:
{files_text}

Output ONLY valid JSON (no markdown fences, no commentary):
{{"files": [{{"path": "same path", "content": "full corrected file content"}}]}}

Only include files you actually changed. Keep everything else about each file the same except what's needed to fix the reported error."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 4000, "temperature": 0.1},
    }

    # SECURITY FIX 2026-08-14: key moved from URL to x-goog-api-key
    # header — was leaking into Render's httpx request logs.
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
        if res.status_code != 200:
            logger.warning(f"Repair call failed: {res.status_code}")
            return []
        data = res.json()
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        fixed = json.loads(text.strip())
        return fixed.get("files", [])
    except Exception as e:
        logger.warning(f"Repair call failed (non-fatal): {e}")
        return []


async def iterative_repair(
    files: list, api_key: str, model: str = "gemini-3.1-flash-lite",
    max_iterations: int = 2, run_entrypoint: str = None,
) -> dict:
    """
    The actual self-healing loop: syntax check -> (optional) execution
    check -> if broken, ask Gemini to fix just the broken files -> repeat.
    Stops early the moment everything's clean. Returns the final file
    list plus a full trace of what was found and fixed at each round.
    """
    current_files = [dict(f) for f in files]
    trace = []

    for iteration in range(max_iterations + 1):  # +1: iteration 0 is the initial check
        problems = check_python_syntax(current_files)

        exec_result = None
        if not problems and run_entrypoint:
            main_file = next((f for f in current_files if f["path"] == run_entrypoint), None)
            if main_file:
                exec_result = await run_script(main_file["content"])
                if not exec_result.get("success"):
                    problems.append({
                        "path": run_entrypoint,
                        "error": exec_result.get("stderr") or exec_result.get("error") or "execution failed",
                    })

        trace.append({
            "iteration": iteration,
            "syntax_errors": check_python_syntax(current_files),
            "execution_result": exec_result,
            "clean": len(problems) == 0,
        })

        if not problems:
            return {"files": current_files, "iterations": iteration, "clean": True, "trace": trace}

        if iteration >= max_iterations:
            return {"files": current_files, "iterations": iteration, "clean": False, "remaining_errors": problems, "trace": trace}

        fixed = await _request_fix(api_key, model, current_files, problems)
        if not fixed:
            # Repair call itself failed — stop here rather than loop forever on nothing.
            return {"files": current_files, "iterations": iteration, "clean": False, "remaining_errors": problems, "trace": trace}

        fixed_by_path = {f["path"]: f["content"] for f in fixed if "path" in f}
        for f in current_files:
            if f["path"] in fixed_by_path:
                f["content"] = fixed_by_path[f["path"]]

    return {"files": current_files, "iterations": max_iterations, "clean": False, "trace": trace}
