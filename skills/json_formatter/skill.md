# JSON Formatter

## What It Does
Parses, validates, and formats JSON data. Beautifies minified JSON, compresses formatted JSON, and surfaces syntax errors with line numbers. Also supports JSON-to-Python dict conversion.

## When to Use It
- User pastes messy or minified JSON and wants it readable
- User wants to validate if a JSON string is valid
- User needs to find a syntax error in JSON
- User wants to convert between JSON formats

## When NOT to Use It
- Data is YAML, TOML, or XML — different parsers needed
- User wants to query/filter JSON data — answer with jq command directly
- No JSON is provided

## Calling Convention
This is an `action`-based skill:

| Action | What it does |
|---|---|
| `format` / `prettify` | Pretty-print with indentation (same behavior) |
| `validate` | Check syntax only, no formatting |
| `minify` | Remove all whitespace |
| `schema` | Infer a JSON schema from sample data |
| `compare` | Diff two JSON objects |

## Output Format
```json
{
  "is_valid": true,
  "formatted": "{\n  \"name\": \"oga\",\n  \"level\": \"100L\"\n}",
  "minified": "{\"name\":\"oga\",\"level\":\"100L\"}",
  "error_message": null,
  "error_line": null,
  "key_count": 2,
  "compression_ratio": "34%"
}
```

## Constraints
- Max input size: 500KB
- Deeply nested structures (>20 levels) may truncate display
- Comments in JSON (JSONC) stripped before parsing
- Trailing commas removed automatically

## Examples
- "Format my MongoDB document response"
- "Why does JSON.parse() fail on this string?"
- "Minify this config for production"
- "Convert this JSON to a Python dict format"
