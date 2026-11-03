# Hash Cracker

## What It Does
Attempts to reverse hash values using dictionary attacks and rainbow table lookups. Identifies the hash type first, then tries to recover the original plaintext. Primarily useful for weak or common passwords.

## When to Use It
- User provides a hash and wants to find the original value
- User doing a CTF challenge involving hash cracking
- User testing whether a specific password hashes to a known value
- User auditing weak credential storage

## When NOT to Use It
- Hash is salted (bcrypt, argon2) — probabilistically uncrackable via dictionary
- User wants to *identify* hash type without cracking → use hash_identifier
- Clearly malicious intent — ethical use only

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `crack` | attempt one hash against the common-password list (requires explicit confirmation). |
| `crack_multiple` | attempt several hashes at once. |
| `estimate_difficulty` | general difficulty rating for a named hash algorithm, no actual cracking attempted. |

## Output Format
```json
{
  "hash": "5f4dcc3b5aa765d61d8327deb882cf99",
  "algorithm": "MD5",
  "cracked": true,
  "original_value": "password",
  "method": "dictionary",
  "crack_time_ms": 12
}
```

## Constraints
- Dictionary-based only — no brute force beyond common wordlists
- Salted hashes (bcrypt, scrypt, argon2) very unlikely to crack
- Strong unique passwords will not crack
- Ethical use only — do not use on credentials you don't own
- Results depend on wordlist coverage

## Examples
- "CTF: crack this MD5 hash"
- "Is 'admin123' in the crackable list?"
- "What algorithm is this and can it be reversed?"
