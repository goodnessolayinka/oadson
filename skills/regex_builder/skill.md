# Regex Builder

## What It Does
Builds, tests, and explains regular expressions. Given a description or test strings, generates the correct regex pattern. Given an existing pattern, explains what it matches and why.

## When to Use It
- User says "write a regex for", "build a pattern that matches", "explain this regex"
- User wants to validate emails, phone numbers, IPs, dates, etc.
- User has a regex that isn't matching and wants it debugged
- User learning regex and wants patterns explained

## When NOT to Use It
- User just needs a simple string check — answer directly with code
- User wants to search files with grep — give the command directly
- No regex task is present

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `build` | generate a pattern from a description. |
| `test` | check if a pattern matches a string. |
| `explain` | describe what an existing pattern does. |
| `extract` | find all matches in text. |
| `replace` | substitute matches in text. |
| `validate` | check correctness/efficiency of a pattern. |

## Output Format
```json
{
  "pattern": "^[0-9]{11}$",
  "language_flags": "Python: re.compile(r'^[0-9]{11}$')",
  "explanation": "Matches exactly 11 consecutive digits from start to end",
  "test_matches": ["08012345678 → MATCH", "0801234567 → NO MATCH"],
  "edge_cases": ["Doesn't account for +234 prefix format"],
  "alternatives": ["^(0|\\+234)[789][01]\\d{8}$ (more specific)"]
}
```

## Constraints
- Patterns generated for Python (re), JavaScript, and generic PCRE
- Very complex patterns explained section by section
- Lookahead/lookbehind supported but noted as engine-dependent
- No live execution against files — pattern testing only with provided strings

## Examples
- "Regex to validate university student ID format"
- "Match all MongoDB ObjectId patterns"
- "Extract all phone numbers from this text block"
- "Why doesn't my regex match 'test@test.co.uk'?"
