import os
from typing import Optional

class TranslateSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)
    
    # Supported languages
    LANGUAGES = {
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "pt": "Portuguese",
        "ru": "Russian",
        "ja": "Japanese",
        "zh": "Chinese (Simplified)",
        "ko": "Korean",
        "ar": "Arabic",
        "hi": "Hindi",
        "bn": "Bengali",
        "yo": "Yoruba",
        "ha": "Hausa",
        "ig": "Igbo",
        "sw": "Swahili"
    }
    
    async def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> dict:
        """Translate text between languages."""
        try:
            # Map language codes
            source_name = self.LANGUAGES.get(source_lang.lower(), source_lang)
            target_name = self.LANGUAGES.get(target_lang.lower(), target_lang)
            
            if source_lang.lower() == "auto":
                prompt = f"""Translate this text to {target_name}:

{text}

Provide:
1. The translation
2. The detected source language
3. Any cultural notes (if applicable)"""
            else:
                prompt = f"""Translate this {source_name} text to {target_name}:

{text}

Provide only the translation, nothing else."""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "source_language": source_name,
                "target_language": target_name,
                "original_text": text,
                "translated_text": response.text,
                "message": f"Translated to {target_name}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def detect_language(self, text: str) -> dict:
        """Detect the language of text."""
        try:
            prompt = f"""Detect the language of this text and provide:
1. Language name
2. Language code (2-letter)
3. Confidence (high/medium/low)

Text: {text}"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "detection": response.text,
                "message": "Language detected"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def translate_batch(self, texts: list, target_lang: str = "en") -> dict:
        """Translate multiple texts at once."""
        try:
            target_name = self.LANGUAGES.get(target_lang.lower(), target_lang)
            
            texts_str = "\n".join([f"{i+1}. {t}" for i, t in enumerate(texts)])
            
            prompt = f"""Translate all of these texts to {target_name}:

{texts_str}

Provide translations in the same numbered format."""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "target_language": target_name,
                "count": len(texts),
                "translations": response.text,
                "message": f"Translated {len(texts)} text(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def list_languages(self) -> dict:
        """List all supported languages."""
        try:
            languages = []
            for code, name in self.LANGUAGES.items():
                languages.append({"code": code, "name": name})
            
            return {
                "status": "success",
                "languages": sorted(languages, key=lambda x: x["name"]),
                "count": len(languages),
                "message": f"{len(languages)} language(s) supported"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def transliterate(self, text: str, from_script: str, to_script: str) -> dict:
        """Convert text between writing systems (e.g., Latin to Cyrillic)."""
        try:
            prompt = f"""Convert this {from_script} text to {to_script}:

{text}

Provide only the converted text."""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "from_script": from_script,
                "to_script": to_script,
                "original": text,
                "converted": response.text,
                "message": "Text converted"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute translator skill with structured args.

    REWRITTEN: previously target_lang was never settable from run() —
    translate() always used its default ("en"), meaning every translation
    request silently translated TO English regardless of what language
    the user actually asked for. Now target_lang is a real, required-in-
    practice argument the model must specify.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Translator actions: translate (text/target_lang), detect_language (text), translate_batch (texts/target_lang), list_languages, transliterate (text/from_script/to_script)"
        }

    try:
        skill = TranslateSkill()

        if action == "translate":
            text = args.get("text") or ""
            if not text.strip():
                return {"status": "error", "reason": "translate requires 'text'"}
            target_lang = (args.get("target_lang") or "en").strip()
            source_lang = (args.get("source_lang") or "auto").strip()
            return await skill.translate(text, source_lang=source_lang, target_lang=target_lang)

        elif action == "detect_language":
            text = args.get("text") or ""
            if not text.strip():
                return {"status": "error", "reason": "detect_language requires 'text'"}
            return await skill.detect_language(text)

        elif action == "translate_batch":
            texts = args.get("texts")
            if not texts or not isinstance(texts, list):
                return {"status": "error", "reason": "translate_batch requires a 'texts' array"}
            target_lang = (args.get("target_lang") or "en").strip()
            return await skill.translate_batch(texts, target_lang=target_lang)

        elif action == "list_languages":
            return skill.list_languages()

        elif action == "transliterate":
            text = args.get("text") or ""
            from_script = (args.get("from_script") or "").strip()
            to_script = (args.get("to_script") or "").strip()
            if not text.strip() or not from_script or not to_script:
                return {"status": "error", "reason": "transliterate requires 'text', 'from_script', and 'to_script'"}
            return await skill.transliterate(text, from_script, to_script)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: translate, detect_language, translate_batch, list_languages, transliterate"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
