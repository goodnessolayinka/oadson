# Password Checker

## What It Does
Evaluates password strength using entropy analysis, common pattern detection, and breach database checks. Returns a score, failure reasons, and improvement suggestions. Does not store the password.

## When to Use It
- User says "is my password strong", "check this password", "is this secure"
- User wants to know why a password is weak
- User wants to verify against breach databases (HaveIBeenPwned-style)
- User choosing a new password and wants guidance

## When NOT to Use It
- User wants a password *generated* — generate one directly
- User asking general password advice — answer directly
- User wants to crack someone else's password → use hash_cracker

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `analyze_strength` | score and feedback for a given password. |
| `generate` | create a strong random password. |
| `check_breach` | check if a password has appeared in known data breaches. |
| `suggest` | AI-generated memorable password ideas for a described context (e.g. 'for my email account'). |

## Output Format
```json
{
  "password": "[REDACTED for output]",
  "strength": "moderate",
  "score": 62,
  "entropy_bits": 48.3,
  "issues": [
    "Contains predictable date pattern",
    "Common substitution: @ for a"
  ],
  "suggestions": [
    "Add more random characters",
    "Avoid personal info like year"
  ],
  "is_breached": false,
  "estimated_crack_time": "3 days (GPU)"
}
```

## Constraints
- Password analyzed locally — not logged or stored
- Breach check via k-anonymity (only hash prefix sent, never full password)
- Strength levels: very_weak | weak | moderate | strong | very_strong
- Entropy calculated on actual character set used
- No guarantee against targeted attacks (personal info guessing)

## Examples
- "Is 'Password123' actually safe?"
- "Generate and check a password for my new account"
- "Why does my password manager say this is weak?"
