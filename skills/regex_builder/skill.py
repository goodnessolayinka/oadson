import re
import os
from typing import Optional, List

class RegexBuilderSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)
    
    async def build_regex(self, description: str) -> dict:
        """Build a regex pattern from description."""
        try:
            prompt = f"""Create a regex pattern for: {description}

Provide:
1. The regex pattern (between backticks)
2. Explanation of what it matches
3. Test examples (what it matches and doesn't match)
4. Alternative approaches (if applicable)
5. Performance notes"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "description": description,
                "suggestion": response.text,
                "message": "Regex pattern suggestion generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def test_regex(self, pattern: str, test_string: str) -> dict:
        """Test a regex pattern against a string."""
        try:
            compiled = re.compile(pattern)
            matches = compiled.findall(test_string)
            search = compiled.search(test_string)
            
            return {
                "status": "success",
                "pattern": pattern,
                "test_string": test_string,
                "matches": matches,
                "match_found": bool(search),
                "first_match": search.group(0) if search else None,
                "match_groups": search.groups() if search else None,
                "message": f"Found {len(matches)} match(es)"
            }
        
        except re.error as e:
            return {"status": "error", "reason": f"Invalid regex: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def explain_regex(self, pattern: str) -> dict:
        """Explain what a regex pattern does."""
        try:
            prompt = f"""Explain this regex pattern in detail:

Pattern: {pattern}

Provide:
1. What the pattern matches (overall)
2. Break down each component:
   - Character classes
   - Quantifiers
   - Groups
   - Anchors
3. Example matches
4. Example non-matches
5. Use cases"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "pattern": pattern,
                "explanation": response.text,
                "message": "Regex explanation generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def extract_matches(self, pattern: str, text: str, group_names: Optional[List[str]] = None) -> dict:
        """Extract all matches from text using regex."""
        try:
            compiled = re.compile(pattern)
            matches = compiled.finditer(text)
            
            results = []
            for match in matches:
                result = {
                    "full_match": match.group(0),
                    "span": match.span(),
                    "groups": match.groups()
                }
                if group_names and match.groupdict():
                    result["named_groups"] = match.groupdict()
                results.append(result)
            
            return {
                "status": "success",
                "pattern": pattern,
                "total_matches": len(results),
                "matches": results[:50],
                "truncated": len(results) > 50,
                "message": f"Extracted {len(results)} match(es)"
            }
        
        except re.error as e:
            return {"status": "error", "reason": f"Invalid regex: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def replace_with_regex(self, pattern: str, text: str, replacement: str) -> dict:
        """Replace text using regex pattern."""
        try:
            compiled = re.compile(pattern)
            result = compiled.sub(replacement, text)
            
            changes = len(compiled.findall(text))
            
            return {
                "status": "success",
                "pattern": pattern,
                "original": text,
                "result": result,
                "replacements_made": changes,
                "message": f"Made {changes} replacement(s)"
            }
        
        except re.error as e:
            return {"status": "error", "reason": f"Invalid regex: {str(e)}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def validate_input(self, pattern: str, description: str = "") -> dict:
        """Validate if a pattern is correct and efficient."""
        try:
            # Try to compile
            re.compile(pattern)
            
            prompt = f"""Review this regex pattern for quality:

Pattern: {pattern}
Purpose: {description}

Provide:
1. Is the pattern correct? ✓/✗
2. Is it efficient?
3. Potential issues?
4. Optimization suggestions?
5. Alternative approaches?"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "pattern": pattern,
                "valid": True,
                "review": response.text,
                "message": "Pattern validation complete"
            }
        
        except re.error as e:
            return {
                "status": "error",
                "pattern": pattern,
                "valid": False,
                "reason": f"Invalid regex: {str(e)}"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute regex_builder skill with structured args.

    REWRITTEN: test_regex (pattern + test_string) and replace_with_regex
    (pattern + text + replacement) need MULTIPLE arguments — the old
    single-string blind dispatch could never have supplied these
    correctly even by accident, since it only ever passed one raw
    message as one positional arg.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Regex actions: build (description), test (pattern/test_string), explain (pattern), extract (pattern/text), replace (pattern/text/replacement), validate (pattern)"
        }

    try:
        skill = RegexBuilderSkill()

        if action == "build":
            description = (args.get("description") or "").strip()
            if not description:
                return {"status": "error", "reason": "build requires 'description'"}
            return await skill.build_regex(description)

        elif action == "test":
            pattern = args.get("pattern") or ""
            test_string = args.get("test_string") or ""
            if not pattern:
                return {"status": "error", "reason": "test requires 'pattern'"}
            return skill.test_regex(pattern, test_string)

        elif action == "explain":
            pattern = args.get("pattern") or ""
            if not pattern:
                return {"status": "error", "reason": "explain requires 'pattern'"}
            return await skill.explain_regex(pattern)

        elif action == "extract":
            pattern = args.get("pattern") or ""
            text = args.get("text") or ""
            if not pattern or not text:
                return {"status": "error", "reason": "extract requires 'pattern' and 'text'"}
            return skill.extract_matches(pattern, text)

        elif action == "replace":
            pattern = args.get("pattern") or ""
            text = args.get("text") or ""
            replacement = args.get("replacement", "")
            if not pattern or not text:
                return {"status": "error", "reason": "replace requires 'pattern' and 'text'"}
            return skill.replace_with_regex(pattern, text, replacement)

        elif action == "validate":
            pattern = args.get("pattern") or ""
            if not pattern:
                return {"status": "error", "reason": "validate requires 'pattern'"}
            return await skill.validate_input(pattern, description=args.get("description", ""))

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: build, test, explain, extract, replace, validate"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
