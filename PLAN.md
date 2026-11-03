# OADSON V3 — Plan

Read this first when you sit down at a laptop. It lists what's done, what's next, and the decisions that need you.

## Status

Working in production (from logs):
- Chat on web, app, PWA, WhatsApp, Telegram
- Claude Haiku 5.5 on heavy turns, with prompt caching (cache reads confirmed in logs)
- Gemini on everyday turns, with fallback across models
- Repo clone and index through the github skill, and zip upload through chat
- Spend caps and the spend ledger in Mongo
- Stop button reaches the server (`POST /api/v3/chat/stop`), tested locally, not yet on Render
- Path hints, repeat-failure guard, plain-language failure explanations

Not yet verified live:
- Stop on Render
- The GitHub push path after the latest prompt changes
- The website step limit (now 40 for PWA, matching web)

## Decisions needed before building

1. **Do we split at all?** One service is working. Split only if repo indexing crashes chat again.
2. **Which plan for the worker?** Render's free plan has no background workers. Check the current pricing page.
3. **Which account pays?** One account, one bill. Do not run one app across several free accounts.

## If we split: repo worker

Chat stops indexing directly. It writes a job, and the worker does the work.

Flow:
1. Chat receives a repo clone or zip upload.
2. Chat writes a document to `repo_jobs` in Mongo: `{user_id, kind, source, status: "queued"}`.
3. Worker polls `repo_jobs` for `queued`, sets `running`, clones or unzips, builds the index, saves it with `repo_index.save()`, sets `done` or `failed` with a reason.
4. Chat reads the status when the user asks, or on the next request.

Reuse `core/job_queue.py`. It already runs a worker loop inside the web process, so the worker can be the same loop in its own service.

## Environment variables

Shared by every service (same values):
- `MONGODB_URI`
- `OADSON_ENGINEER_TOKEN`

Chat service only:
- `GEMINI_API_KEY`, `OPENROUTER_API_KEY`
- `ANTHROPIC_API_KEY`, `ANTHROPIC_DAILY_CAP_USD`, `ANTHROPIC_TOTAL_CAP_USD`
- `DEEPSEEK_API_KEY` (optional; currently unfunded, so leave unset)
- `TELEGRAM_BOT_TOKEN`, `WHATSAPP_BRIDGE_SECRET`
- `ONESIGNAL_APP_ID`, `ONESIGNAL_REST_API_KEY`
- `CLOUDINARY_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
- `FRONTEND_BASE_URL` (logs warn it's missing; push deep links break without it)
- `BACKEND_URL`, `SKILLS_DIR`, `PLAYWRIGHT_BROWSERS_PATH`

Worker only:
- `GITHUB_TOKEN` (needed for clone)
- `MONGODB_URI`, `SKILLS_DIR`

Rules:
- Never commit `.env`. It's excluded from archives.
- Set each value in Render's dashboard, marked `sync: false` in `render.yaml`.
- Use the same `MONGODB_URI` on every service, or they won't share data.

## Laptop session checklist

1. Unzip the latest build and run `python -m pytest -q tests`. Expect 45 passing.
2. Deploy the chat service and check `/health`.
3. Test stop on Render: start a long task, press Stop, and look for `stop requested` in the logs.
4. Test the repo path on the website: clone a repo, ask for a change, confirm it pushes without asking again.
5. Read the first Claude log lines for `cache_read` and `cache_write`.
6. Only then decide on the worker.

## Risks

- Two services writing the same index can corrupt it. The worker must be the only writer.
- Caps are per process. Two chat instances would each spend up to the cap. Keep one chat instance.
- Moving the stop registry and repo index cache out of process memory is required before the sandbox can move out.

## Not doing

- Running one app across several free accounts.
- Splitting the sandbox before the stop registry and workspace ownership move to Mongo.
