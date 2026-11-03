import re
import os
from typing import Optional

class HashIdentifierSkill:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        from google import genai
        self.client = genai.Client(api_key=self.api_key)
    
    # Hash patterns
    HASH_PATTERNS = {
        "MD5": (r"^[a-fA-F0-9]{32}$", 32),
        "SHA1": (r"^[a-fA-F0-9]{40}$", 40),
        "SHA224": (r"^[a-fA-F0-9]{56}$", 56),
        "SHA256": (r"^[a-fA-F0-9]{64}$", 64),
        "SHA384": (r"^[a-fA-F0-9]{96}$", 96),
        "SHA512": (r"^[a-fA-F0-9]{128}$", 128),
        "BLAKE2b": (r"^[a-fA-F0-9]{128}$", 128),
        "BLAKE2s": (r"^[a-fA-F0-9]{64}$", 64),
    }
    
    def identify(self, hash_string: str) -> dict:
        """Identify the type of hash."""
        try:
            hash_string = hash_string.strip().lower()
            
            # Remove common hash prefixes
            if hash_string.startswith("0x"):
                hash_string = hash_string[2:]
            
            possible_types = []
            
            # Check length and patterns
            for hash_type, (pattern, length) in self.HASH_PATTERNS.items():
                if re.match(pattern, hash_string):
                    possible_types.append(hash_type)
            
            # Additional checks
            if len(hash_string) == 27 and hash_string.startswith("$2"):
                possible_types.insert(0, "bcrypt")
            
            if hash_string.startswith("$argon2"):
                possible_types.insert(0, "Argon2")
            
            if "$" in hash_string and len(hash_string) > 20:
                possible_types.append("Unix crypt/PHP password_hash")
            
            if possible_types:
                return {
                    "status": "success",
                    "hash": hash_string[:20] + "...",
                    "hash_length": len(hash_string),
                    "possible_types": list(dict.fromkeys(possible_types)),
                    "most_likely": possible_types[0] if possible_types else "Unknown",
                    "message": f"Hash identified as {possible_types[0]}" if possible_types else "Could not identify hash type"
                }
            
            return {
                "status": "error",
                "reason": f"Could not identify hash type. Length: {len(hash_string)}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def analyze_hash(self, hash_string: str) -> dict:
        """Analyze hash with AI for detailed information."""
        try:
            identification = self.identify(hash_string)
            
            if identification["status"] != "success":
                return identification
            
            prompt = f"""Analyze this hash for security research purposes:

Hash (first 20 chars): {hash_string[:20]}...
Identified type: {identification.get('most_likely')}
Possible types: {', '.join(identification.get('possible_types', []))}

Provide:
1. Hash algorithm characteristics
2. Strength/weaknesses
3. Common use cases
4. Cracking difficulty
5. When to use it"""
            
            analysis = self.client.models.generate_content(
                # Was "gemini-2.5-flash-lite" — same deprecation fix as
                # model_router.py and password_check.
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            
            return {
                "status": "success",
                "identification": identification,
                "analysis": analysis.text,
                "message": "Hash analysis complete"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def identify_batch(self, hashes: list) -> dict:
        """Identify multiple hashes at once."""
        try:
            results = []
            
            for hash_str in hashes:
                result = self.identify(hash_str)
                results.append({
                    "hash": hash_str[:20] + "...",
                    "type": result.get("most_likely", "Unknown")
                })
            
            return {
                "status": "success",
                "total": len(hashes),
                "results": results,
                "message": f"Identified {len(hashes)} hash(es)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def get_hash_info(self, hash_type: str) -> dict:
        """Get information about a hash algorithm."""
        try:
            hash_info = {
                "MD5": {
                    "length": 32,
                    "output": "128 bits",
                    "strength": "Broken (cryptographically broken)",
                    "use_case": "Legacy systems, checksums",
                    "recommendation": "Do NOT use for passwords or security"
                },
                "SHA1": {
                    "length": 40,
                    "output": "160 bits",
                    "strength": "Weak (collision attacks exist)",
                    "use_case": "Legacy systems, checksums",
                    "recommendation": "Deprecated for cryptographic purposes"
                },
                "SHA256": {
                    "length": 64,
                    "output": "256 bits",
                    "strength": "Strong",
                    "use_case": "Digital signatures, integrity, TLS",
                    "recommendation": "Good for general use"
                },
                "SHA512": {
                    "length": 128,
                    "output": "512 bits",
                    "strength": "Very Strong",
                    "use_case": "Passwords, digital signatures, security",
                    "recommendation": "Excellent choice"
                },
                "bcrypt": {
                    "length": 60,
                    "output": "variable",
                    "strength": "Very Strong (salted + iterated)",
                    "use_case": "Password hashing",
                    "recommendation": "Excellent for passwords"
                },
                "Argon2": {
                    "length": "variable",
                    "output": "variable",
                    "strength": "Very Strong (memory-hard)",
                    "use_case": "Password hashing",
                    "recommendation": "Best for password security (2021)"
                }
            }
            
            info = hash_info.get(hash_type)
            
            if info:
                return {
                    "status": "success",
                    "hash_type": hash_type,
                    "info": info
                }
            
            return {"status": "error", "reason": f"No information for {hash_type}"}
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute hash_identifier skill with structured args.

    REWRITTEN: identify_batch() expects a real Python list, but the old
    loop only ever passed a single raw string positionally — that call
    would always fail (iterating a string's characters as if they were
    separate hashes, producing garbage, or the bare except swallowing a
    TypeError). Now the model passes a real array per manifest.json's
    schema.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Hash identifier actions: identify (hash), identify_batch (hashes array), analyze (hash), algorithm_info (hash_type)"
        }

    try:
        skill = HashIdentifierSkill()

        if action == "identify":
            hash_value = (args.get("hash") or "").strip()
            if not hash_value:
                return {"status": "error", "reason": "identify requires a 'hash'"}
            return skill.identify(hash_value)

        elif action == "identify_batch":
            hashes = args.get("hashes")
            if not hashes or not isinstance(hashes, list):
                return {"status": "error", "reason": "identify_batch requires a 'hashes' array"}
            return skill.identify_batch(hashes)

        elif action == "analyze":
            hash_value = (args.get("hash") or "").strip()
            if not hash_value:
                return {"status": "error", "reason": "analyze requires a 'hash'"}
            return await skill.analyze_hash(hash_value)

        elif action == "algorithm_info":
            hash_type = (args.get("hash_type") or "").strip()
            if not hash_type:
                return {"status": "error", "reason": "algorithm_info requires a 'hash_type'"}
            return skill.get_hash_info(hash_type)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: identify, identify_batch, analyze, algorithm_info"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
