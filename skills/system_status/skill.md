# System Status

## What It Does
Reports on OADSON's own backend health, configuration, and operational
history — server metrics, database connectivity, deploy environment, its
own regression test suite, and real per-skill success/failure history.
Distinct from `usage_analytics` (the USER's usage of OADSON) and
`port_scanner` (scanning EXTERNAL systems).

## Calling Convention — this is an `action`-based skill
Every call needs `action` set to one of the values below.

## Action Reference
| Action | Use for | Key params |
|---|---|---|
| `backend_status` | Quick API + database health summary | none |
| `metrics` | CPU/memory/disk usage snapshot | none |
| `api_stats` | Request/usage counts | none |
| `db_health` | Detailed database diagnostics (beyond the quick check in `backend_status`) | none |
| `environment` | Runtime config info (env vars present, deploy target, etc — non-secret) | none |
| `full_report` | Everything above combined into one report | none |
| `proactive_status` | Whether the autonomous WhatsApp agent's last send succeeded or failed, and why | none |
| `self_test` | Runs the regression test suite over known past bugs, reports pass/fail | none |
| `skill_reliability` | Real success/failure rates, trend (IMPROVING/STABLE/DEGRADING), and average latency — from actual execution history, not estimates | `skill_name` (optional — omit for a full ranked report across every skill) |

## When to Use It
- User asks about OADSON's own health, uptime, or whether something is
  configured correctly
- Debugging a slow/misbehaving OADSON instance
- Checking whether a specific skill (or all skills) has been reliable
  lately — `skill_reliability` uses real logged outcomes, so prefer this
  over guessing from a handful of recent turns you happened to see
- After a deploy, to confirm things came up healthy — `full_report` or
  `self_test`

## When NOT to Use It
- User wants to scan an external system/URL → `port_scanner`,
  `ssl_checker`, etc — not this
- User wants analytics on THEIR OWN usage of OADSON (message counts,
  costs) → `usage_analytics`
- User asking about Render's own deployment logs specifically → point
  them to the Render dashboard directly, this skill doesn't have those

## Constraints
- Reports OADSON's own server/process stats — never the user's device.
- `environment` reports config presence/shape, not secret values — never
  echo an actual API key or token back even if asked.
- `skill_reliability` is only as good as the execution history that's
  been logged so far — a skill added very recently may not have enough
  history yet for a meaningful trend.
