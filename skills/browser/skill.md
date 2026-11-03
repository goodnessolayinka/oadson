# Browser

## What It Does
Runs a real headless Chromium browser (Playwright) — navigate to pages,
click, type, extract data, take screenshots, run JavaScript. For actual
page interaction, not just fetching raw HTML — use this over
`@web_fetch`/`@web_search` when the page needs JavaScript to render, or
the task requires clicking/typing/logging in.

## Sessions are stateful — not a one-shot call
Unlike most skills, browser calls share a **persistent session per user**.
`navigate` opens a page and the session stays open across your following
calls — `click`, `type`, `read`, `extract`, `screenshot` all act on
whatever page the session is currently on, not a fresh one each time.

- The session auto-closes after **10 minutes of inactivity** — if it's
  been idle that long, the next action will need a fresh `navigate` first.
- Call `close` explicitly when you're done with a browser task, so the
  session doesn't sit open burning resources until the timeout.
- Call `status` if you're unsure whether a session is already open before
  deciding whether you need to `navigate` again.

## Setup Required — this may not be enabled at all
Browser needs either `BROWSERLESS_URL` (free tier, browserless.io) or
`BROWSER_ENABLED=true` on a Render plan with 1GB+ RAM — **check your
capability manifest for this turn** before assuming it's available; if
neither is configured, every action returns a clear setup message instead
of running. If a `navigate`/other action fails specifically because
Chromium binaries aren't installed, try `setup` once, then retry — don't
loop `setup` repeatedly if it keeps failing, that means the environment
genuinely doesn't have it configured and you should tell the user instead.

## Action Reference
| Action | Use for | Key params |
|---|---|---|
| `setup` | Install Chromium binaries — run once if navigate/etc fail with a missing-browser error | none |
| `navigate` | Go to a URL, opens/reuses the session, returns page title + text | `url` (required) |
| `read` | Get readable text from the current page (or a given `url`) | `url` (optional — defaults to current page) |
| `click` | Click an element | `selector` (CSS) or `text` (match by visible text) |
| `type` | Fill an input field | `selector`, `text`, `clear` (default true — clears field first) |
| `screenshot` | Save a screenshot to the sandbox, returns the file path | `full_page` (default false — viewport only) |
| `extract` | Pull structured data from the page | `what`: `'links'`, `'tables'`, `'headings'`, `'images'`, or any CSS selector |
| `scroll` | Scroll the page | `direction`: up/down/top/bottom, `amount` (pixels, default 500) |
| `run_script` | Execute arbitrary JavaScript, returns its result | `script` |
| `wait` | Wait for an element to appear or a fixed duration | `selector` or `ms` |
| `status` | Check whether a session is currently open for this user | none |
| `close` | Close the session — always do this when the browser task is finished | none |

## Typical Flow
```
1. browser action='navigate' url='https://example.com/login'
2. browser action='type' selector='#username' text='...'
3. browser action='type' selector='#password' text='...'
4. browser action='click' selector='#submit'
5. browser action='wait' selector='.dashboard'
6. browser action='extract' what='tables'
7. browser action='close'
```

## Constraints
- One session per user at a time — a new `navigate` while a session is
  already open reuses it (goes to the new URL in the same session), it
  does not open a second parallel session.
- Sessions live in memory on the current dyno process — not durable
  across a process restart.
- `run_script` executes real JS in the page context — treat it like any
  other code-execution capability, only for what the task actually needs.
