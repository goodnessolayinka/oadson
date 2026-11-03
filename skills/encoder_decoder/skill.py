import base64
import urllib.parse
import binascii
from typing import Optional

class EncoderDecoderSkill:
    def __init__(self):
        pass
    
    # Base64
    def base64_encode(self, text: str) -> dict:
        """Encode text to Base64."""
        try:
            encoded = base64.b64encode(text.encode()).decode()
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "Base64",
                "message": "Text encoded to Base64"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def base64_decode(self, encoded: str) -> dict:
        """Decode Base64 text."""
        try:
            decoded = base64.b64decode(encoded).decode()
            return {
                "status": "success",
                "encoded": encoded,
                "decoded": decoded,
                "format": "Base64",
                "message": "Base64 decoded"
            }
        except binascii.Error:
            return {"status": "error", "reason": "Invalid Base64 string"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    # Hex
    def hex_encode(self, text: str) -> dict:
        """Encode text to hexadecimal."""
        try:
            encoded = text.encode().hex()
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "Hexadecimal",
                "message": "Text encoded to hex"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def hex_decode(self, hex_string: str) -> dict:
        """Decode hexadecimal to text."""
        try:
            # Remove spaces
            hex_string = hex_string.replace(" ", "")
            decoded = bytes.fromhex(hex_string).decode()
            return {
                "status": "success",
                "encoded": hex_string,
                "decoded": decoded,
                "format": "Hexadecimal",
                "message": "Hex decoded"
            }
        except ValueError:
            return {"status": "error", "reason": "Invalid hexadecimal string"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    # URL encoding
    def url_encode(self, text: str) -> dict:
        """Encode text for URL."""
        try:
            encoded = urllib.parse.quote(text)
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "URL Encoded",
                "message": "Text URL encoded"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def url_decode(self, encoded: str) -> dict:
        """Decode URL encoded text."""
        try:
            decoded = urllib.parse.unquote(encoded)
            return {
                "status": "success",
                "encoded": encoded,
                "decoded": decoded,
                "format": "URL Encoded",
                "message": "URL decoded"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    # ROT13
    def rot13_encode(self, text: str) -> dict:
        """ROT13 cipher (simple substitution)."""
        try:
            encoded = ""
            for char in text:
                if 'a' <= char <= 'z':
                    encoded += chr((ord(char) - ord('a') + 13) % 26 + ord('a'))
                elif 'A' <= char <= 'Z':
                    encoded += chr((ord(char) - ord('A') + 13) % 26 + ord('A'))
                else:
                    encoded += char
            
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "ROT13",
                "message": "ROT13 encoded"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def rot13_decode(self, encoded: str) -> dict:
        """ROT13 decode (same as encode for ROT13)."""
        return self.rot13_encode(encoded)
    
    # Binary
    def binary_encode(self, text: str) -> dict:
        """Encode text to binary."""
        try:
            encoded = " ".join(format(ord(char), "08b") for char in text)
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "Binary",
                "message": "Text encoded to binary"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def binary_decode(self, binary_string: str) -> dict:
        """Decode binary to text."""
        try:
            # Remove spaces and decode
            binary_string = binary_string.replace(" ", "")
            decoded = ""
            
            for i in range(0, len(binary_string), 8):
                byte = binary_string[i:i+8]
                if len(byte) == 8:
                    decoded += chr(int(byte, 2))
            
            return {
                "status": "success",
                "encoded": binary_string,
                "decoded": decoded,
                "format": "Binary",
                "message": "Binary decoded"
            }
        except ValueError:
            return {"status": "error", "reason": "Invalid binary string"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    # ASCII
    def ascii_encode(self, text: str) -> dict:
        """Convert text to ASCII codes."""
        try:
            encoded = " ".join(str(ord(char)) for char in text)
            return {
                "status": "success",
                "original": text,
                "encoded": encoded,
                "format": "ASCII",
                "message": "Text converted to ASCII codes"
            }
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def ascii_decode(self, ascii_string: str) -> dict:
        """Decode ASCII codes to text."""
        try:
            codes = ascii_string.split()
            decoded = "".join(chr(int(code)) for code in codes)
            
            return {
                "status": "success",
                "encoded": ascii_string,
                "decoded": decoded,
                "format": "ASCII",
                "message": "ASCII decoded"
            }
        except ValueError:
            return {"status": "error", "reason": "Invalid ASCII codes"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    def detect_encoding(self, text: str) -> dict:
        """Detect what encoding might be used."""
        try:
            detections = []
            
            # Check for Base64
            try:
                base64.b64decode(text)
                if len(text) % 4 == 0 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in text):
                    detections.append("Base64")
            except:
                pass
            
            # Check for hex
            try:
                int(text.replace(" ", ""), 16)
                if all(c in "0123456789abcdefABCDEF " for c in text):
                    detections.append("Hexadecimal")
            except:
                pass
            
            # Check for URL encoded
            if "%" in text and any(c in text for c in ["%20", "%21", "%2F"]):
                detections.append("URL Encoded")
            
            return {
                "status": "success",
                "text_preview": text[:50],
                "possible_encodings": detections,
                "message": f"Detected encodings: {', '.join(detections) if detections else 'None detected'}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute encoder_decoder skill with structured args.

    REWRITTEN: this had the worst version of the blind-dispatch bug in
    the whole codebase. dir()'s alphabetical order puts ascii_decode
    BEFORE ascii_encode, base64_decode, base64_encode, etc. — and
    ascii_encode(text) succeeds on virtually ANY input (every string can
    be converted to space-separated ASCII codes). That means almost
    every call, regardless of what encoding the user actually asked for,
    would have silently returned "converted to ASCII codes" instead of
    erroring — not a crash, a WRONG ANSWER that looked like a right one.
    Now the model picks the exact operation explicitly.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")
    text = args.get("text") or ""

    if not action:
        return {
            "status": "info",
            "message": "Encoder actions: base64_encode/decode, hex_encode/decode, url_encode/decode, rot13, binary_encode/decode, ascii_encode/decode, detect_encoding"
        }

    if not text:
        return {"status": "error", "reason": f"{action} requires 'text'"}

    try:
        skill = EncoderDecoderSkill()

        action_map = {
            "base64_encode": skill.base64_encode,
            "base64_decode": skill.base64_decode,
            "hex_encode": skill.hex_encode,
            "hex_decode": skill.hex_decode,
            "url_encode": skill.url_encode,
            "url_decode": skill.url_decode,
            "rot13": skill.rot13_encode,
            "binary_encode": skill.binary_encode,
            "binary_decode": skill.binary_decode,
            "ascii_encode": skill.ascii_encode,
            "ascii_decode": skill.ascii_decode,
            "detect_encoding": skill.detect_encoding,
        }

        handler = action_map.get(action)
        if not handler:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: {', '.join(action_map.keys())}"
            }

        return handler(text)

    except Exception as e:
        return {"status": "error", "reason": str(e)}
