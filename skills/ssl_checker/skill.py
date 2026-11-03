import socket
import ssl
from datetime import datetime
from typing import Optional

class SSLCheckerSkill:
    def __init__(self):
        pass
    
    async def check_certificate(self, hostname: str, port: int = 443) -> dict:
        """Check SSL certificate info for a hostname."""
        try:
            context = ssl.create_default_context()
            
            with socket.create_connection((hostname, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
                    bin_cert = ssock.getpeercert(binary_form=True)
                    
                    if not cert:
                        return {"status": "error", "reason": "No certificate found"}
                    
                    # Parse certificate details
                    subject = dict(x[0] for x in cert.get("subject", []))
                    issuer = dict(x[0] for x in cert.get("issuer", []))
                    
                    # Convert dates
                    not_before = datetime.strptime(cert.get("notBefore", ""), "%b %d %H:%M:%S %Y %Z")
                    not_after = datetime.strptime(cert.get("notAfter", ""), "%b %d %H:%M:%S %Y %Z")
                    
                    days_until_expiry = (not_after - datetime.utcnow()).days
                    
                    return {
                        "status": "success",
                        "hostname": hostname,
                        "port": port,
                        "certificate": {
                            "subject": subject,
                            "issuer": issuer,
                            "version": cert.get("version"),
                            "serial_number": cert.get("serialNumber"),
                            "valid_from": str(not_before),
                            "valid_until": str(not_after),
                            "days_until_expiry": days_until_expiry,
                            "is_expired": days_until_expiry < 0,
                            "alternative_names": cert.get("subjectAltName", [])
                        },
                        "message": f"Certificate valid for {days_until_expiry} more days"
                    }
        
        except ssl.SSLError as e:
            return {"status": "error", "reason": f"SSL error: {str(e)}"}
        except socket.timeout:
            return {"status": "error", "reason": f"Connection timed out to {hostname}:{port}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def check_expiry(self, hostname: str, port: int = 443) -> dict:
        """Quick expiry check for a certificate."""
        try:
            context = ssl.create_default_context()
            
            with socket.create_connection((hostname, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    cert = ssock.getpeercert()
                    
                    if not cert:
                        return {"status": "error", "reason": "No certificate found"}
                    
                    not_after = datetime.strptime(cert.get("notAfter", ""), "%b %d %H:%M:%S %Y %Z")
                    days_until_expiry = (not_after - datetime.utcnow()).days
                    
                    status = "expired" if days_until_expiry < 0 else "warning" if days_until_expiry < 30 else "valid"
                    
                    return {
                        "status": "success",
                        "hostname": hostname,
                        "cert_status": status,
                        "expires": str(not_after),
                        "days_remaining": max(0, days_until_expiry),
                        "message": f"Certificate {status}. {days_until_expiry} days remaining" if days_until_expiry >= 0 else "Certificate EXPIRED"
                    }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def check_chain(self, hostname: str, port: int = 443) -> dict:
        """Get SSL certificate chain."""
        try:
            context = ssl.create_default_context()
            
            with socket.create_connection((hostname, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                    der_cert = ssock.getpeercert(binary_form=True)
                    
                    if not der_cert:
                        return {"status": "error", "reason": "No certificate found"}
                    
                    # Import ssl_tools or use cryptography if available
                    try:
                        from cryptography import x509
                        from cryptography.hazmat.backends import default_backend
                        
                        cert = x509.load_der_x509_certificate(der_cert, default_backend())
                        
                        chain_info = {
                            "subject": cert.subject.rfc4514_string(),
                            "issuer": cert.issuer.rfc4514_string(),
                            "version": cert.version,
                            "not_valid_before": str(cert.not_valid_before),
                            "not_valid_after": str(cert.not_valid_after)
                        }
                        
                        return {
                            "status": "success",
                            "hostname": hostname,
                            "certificate_chain": [chain_info],
                            "message": "Certificate chain retrieved"
                        }
                    except ImportError:
                        return {
                            "status": "warning",
                            "message": "cryptography library not installed. Install: pip install cryptography"
                        }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def check_protocol_support(self, hostname: str, port: int = 443) -> dict:
        """Check which SSL/TLS protocols are supported."""
        try:
            protocols = {
                "TLS 1.3": ssl.TLSVersion.TLSv1_3,
                "TLS 1.2": ssl.TLSVersion.TLSv1_2,
                "TLS 1.1": ssl.TLSVersion.TLSv1_1,
                "TLS 1.0": ssl.TLSVersion.TLSv1,
            }
            
            supported = []
            
            for name, version in protocols.items():
                try:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.minimum_version = version
                    context.maximum_version = version
                    
                    with socket.create_connection((hostname, port), timeout=5) as sock:
                        with context.wrap_socket(sock, server_hostname=hostname) as ssock:
                            supported.append(name)
                except:
                    pass
            
            return {
                "status": "success",
                "hostname": hostname,
                "supported_protocols": supported,
                "message": f"Supported protocols: {', '.join(supported)}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute ssl_checker skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")
    hostname = (args.get("hostname") or "").strip()
    port = args.get("port") or 443

    if not action:
        return {
            "status": "info",
            "message": "SSL checker actions: full_check, expiry_check, chain, protocol_support (all require hostname)"
        }

    if not hostname:
        return {"status": "error", "reason": f"{action} requires a 'hostname'"}

    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 443

    try:
        skill = SSLCheckerSkill()

        if action == "full_check":
            return await skill.check_certificate(hostname, port=port)
        elif action == "expiry_check":
            return await skill.check_expiry(hostname, port=port)
        elif action == "chain":
            return await skill.check_chain(hostname, port=port)
        elif action == "protocol_support":
            return await skill.check_protocol_support(hostname, port=port)
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: full_check, expiry_check, chain, protocol_support"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
