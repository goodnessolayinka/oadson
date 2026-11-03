# HTML Preview

## What It Does
Renders HTML content inline within OADSON's chat interface. Strips malicious scripts, sanitizes input, and displays the visual result in a sandboxed iframe.

## When to Use It
- User pastes raw HTML and wants to see how it renders
- User building a web component and wants a quick preview
- User wants to test HTML email templates
- User debugging CSS or layout issues visually

## When NOT to Use It
- User wants to *fetch* HTML from a URL → use web_fetch
- User wants generated HTML written to disk → use file_generation
- Content requires JavaScript execution to render properly — limited support

## Calling Convention
This is an `action`-based skill:

| Action | What it does | Needs |
|---|---|---|
| `fetch` | Download HTML from a public URL | `url` |
| `render_local` | Read an HTML file from the user's sandbox | `path` |
| `validate` | Analyse a raw HTML string for syntax issues | `html` |
| `extract_text` | Pull readable text out of a raw HTML string | `html` |
| `extract_links` | Pull all links out of a raw HTML string | `html` |
| `extract_meta` | Pull meta tags out of a raw HTML string | `html` |

`fetch`/`render_local` GET you the HTML; `validate`/`extract_*` ANALYSE HTML
you already have — fetch or render first, then pass that result's html into
one of the extract/validate actions if you need to.

## Output Format
Returns rendered HTML inside a sandboxed iframe in the chat interface.

```json
{
  "status": "success",
  "sanitized": true,
  "scripts_removed": 0,
  "rendered": true,
  "viewport": "375x667"
}
```

## Constraints
- Scripts (JavaScript) are sandboxed — limited execution
- External resources (CDN, fonts) may not load in sandbox
- Max HTML size: 50KB
- CSS animations supported
- No localStorage or cookies in preview sandbox

## Examples
- "Does this card component look right?"
- "Preview my landing page hero section"
- "Show how this Bootstrap form renders"
