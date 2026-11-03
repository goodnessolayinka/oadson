# Engineer mode

A read-only diagnostic mode inside OADSON. You see a problem, you report it, Engineer reads the code and tells you what it thinks is wrong and how to check it. It changes nothing.

## Use it

- `/engineer on`: stay in Engineer mode for this chat. Send `/engineer off` to leave.
- `report: <what you see>`: ask once, without switching modes. Attach a screenshot if it helps.
- `/engineer status`: show the mode.

The reply has five parts: what it thinks is wrong, where (file and lines), a proposed change as text, how to check it, and what it could not verify.

## What it can and cannot do

- Can: search the project folder, read files, and list the folder's structure. All of this runs on the worker.
- Can: read an attached screenshot (only images from the upload host, up to 4 MB).
- Cannot: commit, push, merge, deploy, run the tests, touch memory, mail, Telegram, or any other personal tool.
- Cannot: read code on Render if the worker is offline. It says so instead.

## Setup

1. The project folder must be on the worker. Upload the project zip once in chat, or clone the repo with the GitHub skill. That makes it the active folder.
2. Set `ENGINEER_ENABLED=true` (the default). Set `ENGINEER_DAILY_CAP_USD` (default 1.00) and `ENGINEER_TOTAL_CAP_USD` (default 2.00). Engineer spend also counts toward the normal Anthropic totals.

## Limits

- Model: Claude Haiku 5.5 only.
- At most 6 model steps per report. Each step is a single read.
- When Engineer's budget is used up, it pauses until midnight UTC. Reports are still saved.

## Reports

Each `report:` is saved to `engineer_reports` with status `open`. Nothing acts on it automatically.

## Not built yet

- Running the tests on the worker.
- Writing a branch and proposing a change as a pull request.
- Applying a fix. That will be a separate step you ask for, with approval.
