# Gmail

## What It Does
Reads, searches, and sends Gmail emails using the user's Google OAuth token. Accesses inbox, unread messages, labels, and drafts. Can compose and send emails on behalf of the user.

## When to Use It
- User asks to "check email", "read inbox", "send email to X"
- User wants to search for specific emails by sender, subject, or keyword
- User wants to know unread count or recent messages

## When NOT to Use It
- User hasn't logged in with Google OAuth — return auth error
- User wants to delete emails — read/send only
- User wants calendar events → use google_calendar

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `unread` | list unread emails. |
| `count` | just the unread count. |
| `search` | find emails matching a query. Note: sending email is not yet supported by this skill. |

## Output Format
```json
{
  "status": "success",
  "email_count": 5,
  "messages": [
    {
      "sender": "github@github.com",
      "subject": "Security alert for your repository",
      "snippet": "We detected a new sign-in...",
      "timestamp": "2026-06-14T09:00:00Z",
      "unread": true
    }
  ]
}
```

## Constraints
- Requires valid Google OAuth token from user login
- Read access: up to 50 recent emails per query
- Send: plain text only (no attachments via OADSON)
- Cannot delete or permanently modify emails
- Token expires — re-login required if stale

## Examples
- "What emails do I have from Render about my deployment?"
- "Send a quick update to my collaborator a collaborator"
- "Show me unread messages from the last 24 hours"
