# Worker link (owner side)

The owner can run allowlisted skills on a Railway worker. This is off by default.

## Turn it on

On the owner (Render):

| Variable | Value |
|---|---|
| `WORKER_ENABLED` | `true` |
| `WORKER_URLS` | the worker's base URL, no path, e.g. `https://oadson-worker.up.railway.app`. Several URLs separated by commas |
| `INTERNAL_SECRET` | the same value as on the worker |

Turn it off by setting `WORKER_ENABLED` to `false`. Nothing else changes.

## What goes to the worker

Only skills on the worker's allowlist, currently 37 lookup, analysis, code, and scan skills. Everything else runs here, as before. Each user always reaches the same worker.

## What happens when

- Worker accepts: the owner waits for its signed callback, then returns the result to the tool loop as if the skill had run here.
- Worker unreachable: the skill runs here.
- Worker refuses (401, 409, 422): an error is returned that names the problem. For 401, check `INTERNAL_SECRET`.
- No callback in time: an error, "the worker did not send a result in time". Nothing is re-run.

## Endpoints

- `POST /internal/v1/jobs/{job_id}/result` (root path, signed). Workers call this when a job finishes.

## Not done yet

- Browser and GitHub are not on the worker yet. Their approval flow is not built.
- Stop cancels this user's unfinished remote jobs on the worker (tested).
- Terminal, browser, and GitHub are not on the worker yet.
- The owner runs one instance. Two instances would not share the waiting step.

## Heavy skills, approvals, and terminal

- **browser, github, terminal** never run on Render when the worker is down. The user is told to try again.
- **Approvals.** Pushes, file writes, and repo creation need the user's own message `approve <code>`. The code appears when the action is requested. It works only for the exact same arguments, once, and expires after 30 minutes. The model cannot approve on the user's behalf.
- **search_code** (github action) searches a repo already cloned into the workspace. It runs locally, with no GitHub call.
- **Terminal** runs on the worker only when both sides opt in: `WORKER_ROUTE_TERMINAL=true` on Render and `WORKER_ENABLE_TERMINAL=true` on the worker. The terminal passes its environment to commands, which is the accepted risk in the worker changelog.

## Plans, archives, and the rest (v2)

- **Plans.** The `plans` skill (`submit`, `status`) starts an approved list of steps on the worker. It runs overnight, checkpoints after every step, and reports progress and the final result back here. Plans that change something outside the sandbox need the user's `approve <code>`.
- **Uploads.** A zip uploaded in chat is indexed on the worker (`github` → `index_archive`). Render keeps only a pointer to the folder. Heavy turns ask the worker for relevant snippets.
- **Several owner instances.** Results are written to Mongo, so a waiting request sees a result that arrived on another instance. Approvals, the stop registry, and the notices for the model are still per process. Run one instance until those move to Mongo too.
- **Rotation.** Set `INTERNAL_SECRET_NEXT` on both sides, switch, then remove it. See PROTOCOL.md.
