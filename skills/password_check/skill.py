import os
from typing import Optional

class PasswordCheckSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)
    
    def analyze_strength(self, password: str) -> dict:
        """Analyze password strength."""
        try:
            score = 0
            feedback = []
            
            # Length check
            if len(password) >= 8:
                score += 1
            else:
                feedback.append("Use at least 8 characters")
            
            if len(password) >= 12:
                score += 1
            
            if len(password) >= 16:
                score += 1
            
            # Lowercase check
            if any(c.islower() for c in password):
                score += 1
            else:
                feedback.append("Add lowercase letters")
            
            # Uppercase check
            if any(c.isupper() for c in password):
                score += 1
            else:
                feedback.append("Add uppercase letters")
            
            # Numbers check
            if any(c.isdigit() for c in password):
                score += 1
            else:
                feedback.append("Add numbers")
            
            # Special characters check
            special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
            if any(c in special_chars for c in password):
                score += 2
            else:
                feedback.append("Add special characters")
            
            # No common patterns
            common_patterns = ["123", "abc", "qwerty", "password"]
            if not any(pattern in password.lower() for pattern in common_patterns):
                score += 1
            else:
                feedback.append("Avoid common patterns")
            
            # Determine strength level
            if score <= 2:
                strength = "Very Weak"
            elif score <= 4:
                strength = "Weak"
            elif score <= 6:
                strength = "Fair"
            elif score <= 8:
                strength = "Good"
            else:
                strength = "Excellent"
            
            return {
                "status": "success",
                "strength": strength,
                "score": score,
                "max_score": 12,
                "feedback": feedback,
                "message": f"Password strength: {strength}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def generate_password(self, length: int = 16, special_chars: bool = True) -> dict:
        """Generate a strong password."""
        try:
            import random
            import string
            
            lowercase = string.ascii_lowercase
            uppercase = string.ascii_uppercase
            digits = string.digits
            special = "!@#$%^&*()_+-=[]{}|;:,.<>?"
            
            # Ensure mix of character types
            password = [
                random.choice(lowercase),
                random.choice(uppercase),
                random.choice(digits)
            ]
            
            if special_chars:
                password.append(random.choice(special))
                chars = lowercase + uppercase + digits + special
            else:
                chars = lowercase + uppercase + digits
            
            # Fill rest with random chars
            password += [random.choice(chars) for _ in range(length - len(password))]
            
            # Shuffle
            random.shuffle(password)
            generated = "".join(password)
            
            # Analyze the generated password
            analysis = self.analyze_strength(generated)
            
            return {
                "status": "success",
                "password": generated,
                "strength": analysis.get("strength"),
                "message": "Strong password generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def check_breach(self, password: str) -> dict:
        """Check if password appears in known breaches (using Have I Been Pwned API)."""
        try:
            import hashlib
            import httpx
            
            # Hash the password with SHA-1
            sha1_hash = hashlib.sha1(password.encode()).hexdigest().upper()
            prefix = sha1_hash[:5]
            suffix = sha1_hash[5:]
            
            # Query HaveIBeenPwned API
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.pwnedpasswords.com/range/{prefix}",
                    timeout=10,
                    headers={"User-Agent": "OADSON-Security"}
                )
                
                if response.status_code == 200:
                    hashes = response.text.split("\n")
                    
                    for hash_line in hashes:
                        hash_suffix, count = hash_line.split(":")
                        if hash_suffix.strip() == suffix:
                            return {
                                "status": "warning",
                                "breached": True,
                                "count": int(count),
                                "message": f"Password found in {count} breaches! Do not use."
                            }
                
                return {
                    "status": "success",
                    "breached": False,
                    "message": "Password not found in known breaches"
                }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def suggest_password(self, description: str) -> dict:
        """Get password suggestion from AI."""
        try:
            prompt = f"""Suggest a strong, memorable password for: {description}

Requirements:
1. 16-20 characters
2. Mix of uppercase, lowercase, numbers, special characters
3. No dictionary words
4. No patterns like 123 or abc

Provide 3 different options."""
            
            response = self.client.models.generate_content(
                # Was "gemini-2.5-flash-lite" — confirmed via web search to
                # be in active deprecation alongside gemini-2.5-pro (see
                # model_router.py for the full citation/reasoning). Using
                # the confirmed-current gemini-3.1-flash-lite instead,
                # consistent with the rest of the app's model references.
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "suggestions": response.text,
                "message": "Password suggestions generated"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute password_check skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Password actions: analyze_strength, generate, check_breach (all need password except generate), suggest (needs description)"
        }

    try:
        skill = PasswordCheckSkill()

        if action == "analyze_strength":
            password = args.get("password") or ""
            if not password:
                return {"status": "error", "reason": "analyze_strength requires a 'password'"}
            return skill.analyze_strength(password)

        elif action == "generate":
            length = int(args.get("length") or 16)
            special_chars = bool(args.get("special_chars", True))
            return await skill.generate_password(length=length, special_chars=special_chars)

        elif action == "check_breach":
            password = args.get("password") or ""
            if not password:
                return {"status": "error", "reason": "check_breach requires a 'password'"}
            return await skill.check_breach(password)

        elif action == "suggest":
            description = (args.get("description") or "").strip()
            if not description:
                return {"status": "error", "reason": "suggest requires a 'description'"}
            return await skill.suggest_password(description)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: analyze_strength, generate, check_breach, suggest"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
