# OADSON Worker — Design

Status: design only. Nothing here is built yet. Start from the build order at the bottom.

## Goal

Run OADSON's skills on a second server (Railway) so heavy work (browser, repo indexing, long terminal jobs) stops loading the chat server (Render, 512 MB). The worker is an **executor**: it runs one action exactly as asked and returns the result. It does not plan, and it does not decide what to do next. The owner (Render) keeps all reasoning, memory, and approvals.

## Step 0 — fix before anything else (security, on the owner today)

The terminal skill does not filter the environment its commands get. A command like `printenv` can read `ANTHROPIC_API_KEY`, `MONGODB_URI`, `GEMINI_API_KEY`, and every other server variable. This needs fixing now, not on the worker.

Fix:
- Pass an explicit, filtered environment to every subprocess the terminal starts.
- Allow only: `PATH`, `HOME` (set to the user's sandbox), `LANG`, `LC_*`, `TERM`, `TMPDIR`.
- Drop anything matching `*KEY*`, `*SECRET*`, `*TOKEN*`, `*PASSWORD*`, `*URI*`, `CRED_*`, `MONGO*`, `ANTHROPIC*`, `GEMINI*`.
- Redact known secret values from stdout and stderr before returning them: replace each value with `***`.
- Test: a terminal turn running `printenv` must show no secret values.

Verify the leak first on the laptop, then fix it.

## Roles

| Role | Host | Runs | Holds |
|---|---|---|---|
| Owner | Render | Chat, models, memory, approvals, OneSignal, Mongo | Mongo, model keys, messaging tokens, Google OAuth |
| Worker | Railway (one or more) | Executor for worker-eligible skills | Its own `CRED_*`, `GITHUB_TOKEN`, Railway volume |

Both run the same code from the same repo. `ROLE` decides what starts:
- `ROLE=owner`: the full app, as now.
- `ROLE=worker`: only the worker entrypoint and the skills it allows.

Why one repo: the skill code must match exactly on both sides. Two repos would drift. Scaling to more workers means more Railway services from the same image.

## Which skills can run on the worker

Classified from imports and keywords. This is a starting point. Confirm each one on the laptop before moving it.

**Tier A — stateless (worker-eligible now).** No database, memory, or owner account. Needs only its own API keys, if any.
- Lookups and analysis: `calculator`, `json_formatter`, `jwt_decoder`, `encoder_decoder`, `regex_builder`, `temp_converter`, `translator`, `humanizer`, `password_check`, `header_analyzer`, `ssl_checker`, `url_analyzer`, `ip_geolocation`, `whois`, `dns_lookup`, `wayback`, `wikipedia`, `stackoverflow`, `docs_fetcher`, `news`, `weather_forecast`, `nasa_weather`, `youtube`, `web_search`, `cve_lookup`, `virustotal`, `shodan_search`, `api_tester`
- Code tools: `bug_analyzer`, `code_debugger`, `code_explainer`, `html_preview`
- Scans (heavy, good for the worker): `port_scanner`, `subdomain_enum`, `dependency_audit`, `git_helper`, `docker_helper`

**Tier B — needs the worker's volume (move after the protocol works).** These touch files or browsers, so their state moves to the worker's disk.
- `terminal`, `workspace`, `backend_developer`, `project_creator`, `file_session`, `file_generation`, `archive_creator`, `document_reader`, `website_developer`, `code_index`
- `browser`, `web_scraper`, `web_fetch`
- `github` (clone and push, with the worker's `GITHUB_TOKEN`)

**Tier C — stays on the owner.** Needs owner accounts, owner data, or messaging.
- Google: `google_calendar`, `google_docs`, `google_drive`, `google_keep`, `google_tasks`, `gmail`, `ilana`, `spotify`
- Memory and chat: `memory_manager`, `chat_history_search`, `reminder`, `session_log`, `note_taker`, `project_memory`, `style_manager`, `proactive_config`, `voice_reply`, `sticker_reply`, `user_location`, `system_status`
- Messaging: `telegram`, `whatsapp_reader`
- Self-management: `skill_creator`, `skill_manager`
- Needs review: `threat_intel`, `recon_report`, `image_generation`, `usage_analytics`, `currency_converter`, `hash_identifier`, `github_daily_summary`

Note on the classification: a skill tagged "stateless" can still need an API key. That key must be set as a worker variable (`CRED_*`), not copied from the owner.

## Routing (owner side)

For each tool call the owner decides where it runs:

1. If the skill is Tier C → run on the owner, as now.
2. If `WORKER_URLS` is empty → run on the owner, as now.
3. Otherwise → send to a worker. Pick the worker by a stable hash of `user_id` over `WORKER_URLS`, so one user always reaches the same worker and their workspace stays in one place.
4. If the worker is unreachable:
   - Tier A: fall back to the owner. These skills are cheap.
   - Tier B: do not fall back. Return "worker offline, try again" to the chat. Running Tier B on the owner could load Chromium on the 512 MB box, or split the user's workspace across two servers.

Each remote job counts as one owner step. The worker runs one action per job and never starts another step by itself.

## Protocol

All calls are HTTPS between the two services. No database is shared.

**Owner → Worker**
- `POST /internal/v1/jobs`
  ```json
  {
    "job_id": "uuid",
    "idempotency_key": "uuid",
    "user_id": "string",
    "skill": "browser",
    "action": "screenshot",
    "args": {"url": "https://example.com"},
    "timeout_s": 120
  }
  ```
  Returns `202 {"job_id": "...", "state": "queued"}`. A repeated `idempotency_key` returns the original result, and does nothing a second time.
- `POST /internal/v1/jobs/{job_id}/cancel` → stops the job. The owner calls this when the user presses Stop.
- `GET /healthz` → `200` when the worker is ready. Used for UptimeRobot-style checks, not for job state.

**Worker → Owner** (callback)
- `POST {OWNER_URL}/internal/v1/jobs/{job_id}/result`
  ```json
  {
    "job_id": "uuid",
    "state": "done | failed | interrupted | cancelled",
    "result": {},
    "error": null,
    "explanation": "plain-language sentence",
    "artifacts": [{"kind": "image", "url": "https://res.cloudinary.com/..."}]
  }
  ```
  Retried with backoff until the owner returns `200`. The payload stays in the worker's outbox until then.

**Signing (both directions)**
- Header `X-Oadson-Timestamp`: Unix seconds.
- Header `X-Oadson-Signature`: `HMAC-SHA256(INTERNAL_SECRET, METHOD + "\n" + PATH + "\n" + TIMESTAMP + "\n" + sha256(BODY))`, hex.
- Reject if the timestamp is more than 300 seconds off, or if the signature doesn't match.
- Compare signatures with a constant-time function.

## Owner records (Mongo, owner only)

Collection `remote_jobs`:
`job_id`, `user_id`, `skill`, `action`, `worker`, `state`, `created_at`, `updated_at`, `result` (trimmed), `error`.

Owner rules:
- A job in `queued` or `running` for more than `timeout_s + 120` seconds with no callback becomes `lost`. The owner tells the user. It does not silently re-run anything.
- Re-running is allowed only for actions marked idempotent (reads, screenshots). Writes (push, send, delete) are never retried automatically.

## Worker layout

Railway volume mounted at `/data`:

```
/data/
  outbox/              callbacks waiting to be delivered
  jobs/                job state files (one per job_id)
  seen/                idempotency keys, kept 24 hours
  users/<user_id>/
    workspace/         the user's sandbox files (terminal, workspace, github clone)
    browser/           saved sessions, cookies encrypted with WORKER_STATE_KEY
  screens/             temporary screenshots, deleted after upload
```

Restart behavior:
- On boot, read `jobs/`. Any job still `running` becomes `interrupted`, and a callback says so.
- Deliver everything in `outbox/` first.
- The owner decides what to do with `interrupted`. The worker never resumes by itself.

Concurrency:
- One browser session per user.
- One terminal command per user at a time.
- A global cap `WORKER_CONCURRENCY`, set to fit the Railway plan's memory.
- Excess jobs wait in `jobs/` as `queued`.

## Credentials and secrets

- The owner refers to a credential by name, for example `use CRED_SITE_X`. It never sends the value.
- Skills read `CRED_*` on the worker only, by name.
- Results and logs are redacted: any `CRED_*` value and `INTERNAL_SECRET` are replaced with `***` before leaving the worker.
- Cookies and sessions on disk are encrypted with `WORKER_STATE_KEY`.
- The worker's `WORKER_ALLOWED_SKILLS` lists exactly what it may run. Anything else returns `blocked`.
- Owner's secrets never appear in a worker's environment.

## Approvals

Executor-only means the owner asks the user, not the worker.

- Read actions run automatically.
- Actions in `APPROVAL_REQUIRED` (for example `push`, `send`, `purchase`, `delete`) stop at the owner. The owner sends the user a push with what it's about to do, and runs it only after a "yes".

## Failure modes

| What happens | What the owner does |
|---|---|
| Worker down | Tier A: runs on owner. Tier B: "worker offline, try again" |
| Callback never arrives | After `timeout_s + 120` seconds: `lost`, user told |
| Signature mismatch | Rejected. Logged with timestamp and path, never the secret |
| Duplicate request | Returns the original result |
| Worker restarts mid-job | Callback with `interrupted`. Owner decides |
| Volume full | Worker refuses new jobs with `storage_full`. Owner tells the user |
| Stop pressed | Owner calls `cancel`. Worker stops the job and reports `cancelled` |

## Latency

- Remote calls add one round trip per job. Put Render and Railway in the same region.
- Chat replies that don't use the worker are unchanged.
- Long jobs send progress only when the owner asks for it. No polling loop runs in the background.

## Repo layout

Same repo, two entrypoints:
- `main.py` checks `ROLE`.
- `worker/` holds the worker entrypoint, the job runner, and the outbox.
- `contracts/` holds the signing and job schema, imported by both sides. One copy, so it can't drift.
- Tests for `contracts/` check the signature rules and the job schema.

If you still want a separate worker repo, generate it from this one at build time (copy `skills/` and `contracts/`). Don't copy by hand.

## Environment variables

Owner (Render), additions:

| Variable | Purpose |
|---|---|
| `WORKER_URLS` | Comma-separated Railway worker URLs. Empty means everything runs on the owner |
| `INTERNAL_SECRET` | Same value as the workers |
| `WORKER_ENABLED` | `true` to turn routing on. Off by default |
| `JOB_TIMEOUT_S` | Default per-job timeout, e.g. `300` |

Worker (Railway):

| Variable | Purpose |
|---|---|
| `ROLE` | `worker` |
| `INTERNAL_SECRET` | Same value as the owner |
| `OWNER_URL` | Where callbacks go |
| `WORKER_DATA_DIR` | `/data` |
| `WORKER_STATE_KEY` | Encrypts saved sessions |
| `WORKER_ALLOWED_SKILLS` | Comma-separated skill names |
| `WORKER_CONCURRENCY` | Global job cap |
| `BROWSER_ENABLED` | `true` |
| `PLAYWRIGHT_BROWSERS_PATH` | Chromium install location, set at build |
| `CLOUDINARY_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | Screenshot and file uploads |
| `GITHUB_TOKEN` | Only if the worker owns GitHub |
| `CRED_*` | One per credential the worker's skills need |
| Each skill's own API key | Only for the Tier A/B skills that use one |

The worker never gets `MONGODB_URI`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (unless a skill on the worker needs it), or the messaging tokens.

## Build order

0. Fix the terminal environment leak on the owner. Test with `printenv`.
1. `contracts/`: signing, job schema, and tests for both.
2. Worker entrypoint with `/healthz`, a signed `POST /internal/v1/jobs`, a single `echo` skill, the outbox, and the callback. Test end to end with a local owner stub.
3. Owner routing behind `WORKER_ENABLED`, Tier A only. Start with two skills, such as `calculator` and `whois`.
4. Terminal and workspace on the worker, using the volume. Test: a file written in one turn is read back in the next.
5. Browser on the worker. Screenshots go to Cloudinary, and the chat shows the link.
6. GitHub clone and push on the worker, with approvals for push.
7. Remaining Tier B skills.
8. Multiple workers: sticky routing by `user_id`.
9. Stop propagation: owner `cancel` reaches the worker.
10. Repo indexer as a job kind, once the rest is stable.

## Not doing

- Any thinking or planning on the worker. The owner decides every step.
- Shared database between owner and worker.
- Automatic retry of writes.
- Splitting Tier C skills, or Google accounts, onto the worker.
