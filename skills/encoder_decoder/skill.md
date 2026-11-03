# Encoder / Decoder

## What It Does
Encodes and decodes data across multiple formats: Base64, Hex, URL encoding, Binary, ASCII, and Caesar cipher. Useful for CTF challenges, web security, and data format conversions.

## When to Use It
- User says "encode", "decode", "convert to base64", "what's this hex"
- User is working on a CTF or security lab
- User needs to encode data for web transmission
- User wants to inspect an obfuscated string

## When NOT to Use It
- User wants actual *encryption* (AES, RSA) — this is encoding, not encryption
- User wants to hash a value → use hash_identifier
- User needs URL shortening — different function

## Calling Convention
This is an `action`-based skill — call it with `action` set to the exact
operation needed, and `text` (the input):

| Action | What it does |
|---|---|
| `base64_encode` / `base64_decode` | Base64 either direction |
| `hex_encode` / `hex_decode` | Hex either direction |
| `url_encode` / `url_decode` | URL/percent-encoding either direction |
| `rot13` | ROT13 — same action encodes and decodes, it's its own inverse |
| `binary_encode` / `binary_decode` | Binary (0/1) either direction |
| `ascii_encode` / `ascii_decode` | ASCII code points either direction |
| `detect_encoding` | Guess what format an unknown string might be in |

## Output Format
```json
{
  "original": "hello world",
  "format": "base64",
  "result": "aGVsbG8gd29ybGQ=",
  "direction": "encode",
  "encoding_type": "UTF-8"
}
```

## Constraints
- Encoding formats: Base64, Hex, URL, ASCII, Binary, Caesar (ROT-N)
- No encryption — purely reversible encoding
- Input must be text (no binary file encoding)
- Caesar cipher: specify shift value or tries all 25

## Examples
- "What does this Base64 decode to?"
- "Encode my JWT payload to base64"
- "ROT13 this string: Hello World"
- "Convert hex 41 42 43 to text"
