import os
from typing import Optional

class CodeExplainerSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)
    
    async def explain(self, code: str, language: Optional[str] = None) -> dict:
        """Explain what a code snippet does."""
        try:
            if not code.strip():
                return {"status": "error", "reason": "No code provided"}
            
            prompt = f"""Explain the following {language or 'code'} snippet in simple terms.
            
Code:
```{language or ''}
{code}
```

Provide:
1. What the code does (high-level overview)
2. Key components/functions (if applicable)
3. What it returns or outputs
4. Potential use cases
5. Any gotchas or important notes

Keep it concise and beginner-friendly."""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "language": language or "code",
                "explanation": response.text,
                "message": "Code explanation generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def explain_algorithm(self, code: str, focus: str = "complexity") -> dict:
        """Explain algorithm complexity and performance."""
        try:
            prompt = f"""Analyze the following code for {focus}:

```
{code}
```

Provide:
1. Time Complexity (Big O notation)
2. Space Complexity (Big O notation)
3. Algorithm efficiency rating (✓ good, ✗ poor, ? could be improved)
4. Optimization suggestions
5. Alternative approaches"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "focus": focus,
                "analysis": response.text,
                "message": "Algorithm analysis complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def detect_patterns(self, code: str) -> dict:
        """Detect design patterns in code."""
        try:
            prompt = f"""Identify design patterns and architectural patterns in this code:

```
{code}
```

List:
1. Detected patterns (if any)
2. How they're implemented
3. Whether they're well-used or could be improved
4. Similar patterns that might fit better"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "patterns": response.text,
                "message": "Pattern detection complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def refactor_suggestions(self, code: str) -> dict:
        """Suggest refactoring improvements."""
        try:
            prompt = f"""Suggest refactoring improvements for this code:

```
{code}
```

Provide:
1. Code quality issues (if any)
2. Readability improvements
3. DRY (Don't Repeat Yourself) violations
4. Specific refactoring suggestions with code examples
5. Priority level (must-fix, should-fix, nice-to-have)"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "suggestions": response.text,
                "message": "Refactoring analysis complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def security_review(self, code: str) -> dict:
        """Review code for security vulnerabilities."""
        try:
            prompt = f"""Security review for this code:

```
{code}
```

Analyze for:
1. Security vulnerabilities (SQLi, XSS, CSRF, etc.)
2. Unsafe patterns
3. Authentication/authorization flaws
4. Data exposure risks
5. Recommended fixes with severity levels (Critical/High/Medium/Low)"""
            
            response = self.client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "security_review": response.text,
                "message": "Security analysis complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute code_explainer skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    code = args.get("code") or ""

    if not action:
        return {
            "status": "info",
            "message": "Code explainer actions: explain, complexity, patterns, refactor, security (all require code)"
        }

    if not code.strip():
        return {"status": "error", "reason": f"{action} requires 'code'"}

    try:
        skill = CodeExplainerSkill()

        if action == "explain":
            return await skill.explain(code, language=args.get("language"))
        elif action == "complexity":
            return await skill.explain_algorithm(code, focus="complexity")
        elif action == "patterns":
            return await skill.detect_patterns(code)
        elif action == "refactor":
            return await skill.refactor_suggestions(code)
        elif action == "security":
            return await skill.security_review(code)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: explain, complexity, patterns, refactor, security"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
