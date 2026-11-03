"""
skills/jwt_decoder/skill.py — OADSON V3 JWT Decoder.

Pure stdlib. Decoding a JWT never requires a secret — the header and
payload are just base64url JSON, unencrypted by design (that's what
JWTs are for: readable claims, verifiable signature). Only signature
VERIFICATION needs a secret, and only for HS*-family algorithms; RS/ES
verification needs a public key this skill doesn't have, so it's not
attempted.

Flags the classic misconfigurations a pentester/dev actually cares
about:
  - alg=none            → server may accept an unsigned token as valid
  - missing exp          → token never expires
  - exp already passed   → token is stale
  - HS256 signature verifiable against a supplied weak/guessed secret
"""

import base64
import hashlib
import hmac
import json
import time


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _decode_token(token: str) -> dict:
    parts = token.strip().split(".")
    if len(parts) != 3:
        return {"status": "error", "reason": f"Not a valid JWT — expected 3 dot-separated parts, got {len(parts)}."}

    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception as e:
        return {"status": "error", "reason": f"Header isn't valid base64url JSON: {e}"}

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as e:
        return {"status": "error", "reason": f"Payload isn't valid base64url JSON: {e}"}

    warnings = []
    alg = header.get("alg", "")

    if alg.lower() == "none":
        warnings.append("alg=none — if the server doesn't reject this explicitly, an attacker can forge a valid-looking token with any claims and no signature at all.")

    exp = payload.get("exp")
    now = int(time.time())
    expiry_status = None
    if exp is None:
        warnings.append("No 'exp' claim — this token never expires.")
    else:
        try:
            exp = int(exp)
            if exp < now:
                expiry_status = f"EXPIRED {now - exp}s ago"
                warnings.append(f"Token expired {now - exp}s ago.")
            else:
                expiry_status = f"valid for {exp - now}s more"
        except (TypeError, ValueError):
            warnings.append(f"'exp' claim isn't a valid timestamp: {exp!r}")

    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            nbf = int(nbf)
            if nbf > now:
                warnings.append(f"Token not valid yet — 'nbf' is {nbf - now}s in the future.")
        except (TypeError, ValueError):
            pass

    return {
        "status": "success",
        "header": header,
        "payload": payload,
        "algorithm": alg,
        "expiry_status": expiry_status,
        "warnings": warnings,
        "signature_verified": None,  # set below if a secret was supplied
        "_signature_b64": signature_b64,
        "_signing_input": f"{header_b64}.{payload_b64}",
    }


def _verify_hs(alg: str, signing_input: str, secret: str, signature_b64: str) -> bool:
    hash_fn = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}.get(alg.upper())
    if not hash_fn:
        return False
    expected = hmac.new(secret.encode(), signing_input.encode(), hash_fn).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    return hmac.compare_digest(expected_b64, signature_b64)


async def run(payload: dict) -> dict:
    args = payload.get("args", {}) or {}
    token = (args.get("token") or "").strip()
    secret = args.get("secret")

    if not token:
        return {"status": "error", "reason": "No token provided."}

    result = _decode_token(token)
    if result.get("status") != "success":
        return result

    alg = result["algorithm"]
    if secret and alg.upper().startswith("HS"):
        try:
            verified = _verify_hs(alg, result["_signing_input"], secret, result["_signature_b64"])
            result["signature_verified"] = verified
            if verified:
                result["warnings"].append(f"Signature verified against supplied secret — this secret works, meaning it's guessable/known. That's a finding in itself if it wasn't supposed to be public.")
        except Exception as e:
            result["signature_verified"] = None
            result["warnings"].append(f"Verification attempt failed: {e}")
    elif secret and not alg.upper().startswith("HS"):
        result["warnings"].append(f"alg={alg} isn't an HMAC algorithm — verifying it needs a public/private keypair, not a shared secret. Skipped.")

    # internal-only fields, don't surface to the model/user
    result.pop("_signature_b64", None)
    result.pop("_signing_input", None)

    return result
