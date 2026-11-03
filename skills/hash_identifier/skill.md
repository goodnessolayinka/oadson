# Hash Identifier

## What It Does
Identifies the algorithm behind an unknown hash string. Analyzes length, character set, prefix patterns, and structure to return likely algorithm matches with confidence scores.

## When to Use It
- User has an unidentified hash string and wants to know what it is
- User doing CTF recon on a captured hash
- User verifying what algorithm a system is using for storage
- First step before attempting hash_cracker

## When NOT to Use It
- User wants to crack the hash → use hash_cracker after identifying
- User wants to *generate* a hash — answer directly with code snippet
- String is clearly not a hash (too short, all letters)

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `identify` | detect the type of one hash. |
| `identify_batch` | detect types for multiple hashes at once. |
| `analyze` | AI-written deep-dive on one hash's characteristics. |
| `algorithm_info` | general info about a named algorithm (e.g. 'SHA256', 'bcrypt'). |

## Output Format
```json
{
  "hash": "5f4dcc3b5aa765d61d8327deb882cf99",
  "identified_types": [
    {"algorithm": "MD5", "confidence": 0.95},
    {"algorithm": "MD4", "confidence": 0.40}
  ],
  "length": 32,
  "character_set": "hex",
  "salted": false,
  "notes": "Classic MD5 — 32 hex chars, unsalted"
}
```

## Constraints
- Identification based on pattern matching — not 100% certain
- Some algorithms produce same-length hashes (MD5 vs NTLM both 32 hex)
- Salted hashes identified by prefix ($2b$, $apr1$, etc.)
- Cannot identify custom or proprietary hashing schemes

## Examples
- "Is this bcrypt or argon2?"
- "CTF: what's this 40-char hash?"
- "Does MongoDB store passwords as this format?"
