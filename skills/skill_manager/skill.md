# Skill Manager

## What It Does
Lists, describes, and reports the status of all 54 OADSON skills. Shows which skills are active, what they require (API keys, OAuth), and their categories.

## When to Use It
- User says "what skills do you have", "list your skills", "show available skills"
- User wants to know what a specific skill does before using it
- User checking if a skill is active or what it needs to work
- User wants skills filtered by category

## When NOT to Use It
- User wants to actually *use* a skill — route to that skill directly
- User wants backend health → use system_status

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list` | all available skill names + short descriptions. |
| `describe` | full details (description, parameters) for one named skill. |
| `search` | find skills matching a keyword. |

## Output Format
```json
{
  "total_skills": 54,
  "skills": [
    {
      "name": "gmail",
      "category": "Communication",
      "description": "Read, send, manage Gmail",
      "status": "active",
      "requires": ["google_oauth_token"]
    }
  ],
  "categories": ["Communication", "Productivity", "Dev", "Security", "Utility"]
}
```

## Constraints
- Reflects skills loaded at server startup
- Status changes require server restart to reflect
- Cannot enable/disable individual skills at runtime
- Skill descriptions pulled from manifest.json

## Examples
- "Which skills don't need any API keys?"
- "Show me all communication skills"
- "Is the Shodan skill available?"
