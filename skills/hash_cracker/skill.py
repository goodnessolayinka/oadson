import hashlib
from typing import Optional, List

class HashCrackerSkill:
    def __init__(self):
        # Common wordlists (in production, load from file)
        self.common_passwords = [
            "password", "123456", "12345678", "qwerty", "abc123",
            "monkey", "letmein", "trustno1", "dragon", "baseball",
            "iloveyou", "master", "sunshine", "ashley", "bailey",
            "shadow", "123123", "654321", "superman", "qazwsx"
        ]
    
    def _hash_string(self, text: str, hash_type: str) -> str:
        """Generate hash of string."""
        hash_type = hash_type.lower()
        
        if hash_type == "md5":
            return hashlib.md5(text.encode()).hexdigest()
        elif hash_type == "sha1":
            return hashlib.sha1(text.encode()).hexdigest()
        elif hash_type == "sha256":
            return hashlib.sha256(text.encode()).hexdigest()
        elif hash_type == "sha512":
            return hashlib.sha512(text.encode()).hexdigest()
        
        return None
    
    async def crack_hash(self, hash_string: str, hash_type: str = "md5", confirm: bool = False) -> dict:
        """Attempt to crack a hash using wordlist."""
        if not confirm:
            return {
                "status": "pending_confirmation",
                "action": "crack_hash",
                "hash": hash_string[:20] + "...",
                "hash_type": hash_type,
                "message": f"Attempt to crack {hash_type.upper()} hash? Reply 'confirm' to proceed."
            }
        
        try:
            hash_string = hash_string.lower()
            
            # Try common passwords
            for password in self.common_passwords:
                test_hash = self._hash_string(password, hash_type)
                
                if test_hash and test_hash == hash_string:
                    return {
                        "status": "success",
                        "cracked": True,
                        "hash": hash_string,
                        "hash_type": hash_type,
                        "password": password,
                        "method": "wordlist",
                        "message": f"Hash cracked! Password: {password}"
                    }
            
            return {
                "status": "success",
                "cracked": False,
                "hash": hash_string,
                "hash_type": hash_type,
                "attempts": len(self.common_passwords),
                "message": f"Could not crack hash after {len(self.common_passwords)} attempts"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def crack_multiple(self, hashes: List[str], hash_type: str = "md5") -> dict:
        """Attempt to crack multiple hashes."""
        try:
            results = []
            
            for hash_str in hashes:
                hash_str = hash_str.lower()
                
                for password in self.common_passwords:
                    test_hash = self._hash_string(password, hash_type)
                    
                    if test_hash and test_hash == hash_str:
                        results.append({
                            "hash": hash_str,
                            "password": password,
                            "cracked": True
                        })
                        break
                else:
                    results.append({
                        "hash": hash_str,
                        "cracked": False
                    })
            
            cracked_count = sum(1 for r in results if r.get("cracked"))
            
            return {
                "status": "success",
                "total": len(hashes),
                "cracked": cracked_count,
                "results": results,
                "message": f"Cracked {cracked_count}/{len(hashes)} hash(es)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def estimate_difficulty(self, hash_type: str) -> dict:
        """Estimate difficulty of cracking a hash type."""
        try:
            difficulties = {
                "md5": {
                    "strength": "Weak",
                    "cracking_difficulty": "Easy with rainbow tables",
                    "gpu_hours": "< 1 hour (8-char password)",
                    "recommendation": "Do not use"
                },
                "sha1": {
                    "strength": "Weak",
                    "cracking_difficulty": "Easy with rainbow tables",
                    "gpu_hours": "1-24 hours (8-char password)",
                    "recommendation": "Deprecated"
                },
                "sha256": {
                    "strength": "Strong",
                    "cracking_difficulty": "Very difficult",
                    "gpu_hours": "> 1000 hours (8-char password)",
                    "recommendation": "Good for general use"
                },
                "sha512": {
                    "strength": "Very Strong",
                    "cracking_difficulty": "Extremely difficult",
                    "gpu_hours": "> 100000 hours",
                    "recommendation": "Excellent"
                }
            }
            
            info = difficulties.get(hash_type.lower())
            
            if info:
                return {
                    "status": "success",
                    "hash_type": hash_type,
                    "difficulty": info,
                    "message": f"{hash_type.upper()} - {info['strength']}"
                }
            
            return {"status": "error", "reason": f"Unknown hash type: {hash_type}"}
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute hash_cracker skill with structured args.

    NOTE: crack_multiple() has no confirm gate in the original code, while
    crack_hash() does. That inconsistency is preserved exactly as-is here
    rather than silently changed — crack_multiple was always callable
    without confirmation, so this rewrite doesn't add a restriction that
    wasn't there before. Worth knowing if you want that gate added later.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")
    hash_type = (args.get("hash_type") or "md5").strip()

    if not action:
        return {
            "status": "info",
            "message": "Hash cracker actions: crack (hash, confirm), crack_multiple (hashes), estimate_difficulty (hash_type)"
        }

    try:
        skill = HashCrackerSkill()

        if action == "crack":
            hash_value = (args.get("hash") or "").strip()
            confirm = bool(args.get("confirm", False))
            if not hash_value:
                return {"status": "error", "reason": "crack requires a 'hash'"}
            return await skill.crack_hash(hash_value, hash_type=hash_type, confirm=confirm)

        elif action == "crack_multiple":
            hashes = args.get("hashes")
            if not hashes or not isinstance(hashes, list):
                return {"status": "error", "reason": "crack_multiple requires a 'hashes' array"}
            return await skill.crack_multiple(hashes, hash_type=hash_type)

        elif action == "estimate_difficulty":
            return await skill.estimate_difficulty(hash_type)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: crack, crack_multiple, estimate_difficulty"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
