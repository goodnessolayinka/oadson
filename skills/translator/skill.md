# Translator

## What It Does
Translates text between languages. Supports 100+ languages. Auto-detects source language if not specified. Returns translation with detected language and confidence.

## When to Use It
- User says "translate this to", "what does X mean in English", "translate from French"
- User working with multilingual content
- User wants to understand text in an unfamiliar language
- User writing messages for an international audience

## When NOT to Use It
- User wants code translated to another language — that's a coding task, not translation
- User wants technical documentation explained — use code_explainer
- Text is already in the target language

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `translate` | convert text from one language to another. |
| `detect_language` | identify what language text is in. |
| `translate_batch` | translate multiple texts at once. |
| `list_languages` | see supported language codes. |
| `transliterate` | convert between writing systems. |

## Output Format
```json
{
  "original_text": "Ẹ káàárọ̀",
  "translated_text": "Good morning",
  "source_language": "Yoruba",
  "source_language_code": "yo",
  "target_language": "English",
  "target_language_code": "en",
  "confidence": 0.97
}
```

## Constraints
- Nigerian languages (Yoruba, Igbo, Hausa) supported but accuracy varies
- Pidgin English has limited support
- Technical jargon may not translate accurately
- Idioms translated literally — flag if idiomatic meaning differs
- Max text: 5000 characters per request

## Examples
- "Translate my WhatsApp message to English"
- "How do I say 'the API is down' in French?"
- "Translate this Yoruba proverb"
