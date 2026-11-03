# Humanizer Skill

Rewrites text to eliminate AI writing patterns. Built around a comprehensive list of AI tells — vocabulary, structure, punctuation, tone — and trained to produce output that reads like a real person wrote it.

## Actions

### `humanize` (default)
Rewrite any text. Use `tone` to control the voice.

**Example:**
```
@humanizer humanize this essay introduction: [paste text]
```

### `scan`
Detect AI patterns in text without rewriting. Shows what would get flagged.

**Example:**
```
@humanizer scan this paragraph: [paste text]
```

### `section`
For large PDFs — humanize one section at a time with consistent voice.

**Example:**
```
@humanizer section "Methodology" [paste section text]
```

## Tones
- `natural` — default, whatever sounds most human for this content
- `casual` — contractions, short lines, informal
- `professional` — business-human, not robotic
- `academic` — real student/researcher voice
- `social` — punchy, social media ready

## What it eliminates
- "Delve into", "leverage", "utilize", "in the realm of"
- Em dashes used as style crutches
- Bullet lists for everything
- "Moreover", "Furthermore", "Additionally" openers
- "In conclusion", "To summarize"
- Robotic parallel structure
- Passive voice overuse
- Perfectly balanced "on the other hand" takes
- Every other AI vocabulary tell

## For large PDFs
Send section by section with `action=section` and `section_title`. The model maintains consistent voice across sections.
