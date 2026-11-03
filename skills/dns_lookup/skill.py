import socket
import subprocess
from typing import Optional, List

class DNSLookupSkill:
    def __init__(self):
        pass
    
    async def resolve_a(self, hostname: str) -> dict:
        """Resolve A record (IPv4)."""
        try:
            ip = socket.gethostbyname(hostname)
            
            return {
                "status": "success",
                "hostname": hostname,
                "record_type": "A",
                "ip_address": ip,
                "message": f"A record resolved to {ip}"
            }
        
        except socket.gaierror:
            return {"status": "error", "reason": f"Could not resolve {hostname}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def resolve_aaaa(self, hostname: str) -> dict:
        """Resolve AAAA record (IPv6)."""
        try:
            info = socket.getaddrinfo(hostname, None, socket.AF_INET6)
            ipv6_addresses = list(set([item[4][0] for item in info]))
            
            return {
                "status": "success",
                "hostname": hostname,
                "record_type": "AAAA",
                "ipv6_addresses": ipv6_addresses,
                "message": f"Found {len(ipv6_addresses)} IPv6 address(es)"
            }
        
        except socket.gaierror:
            return {"status": "error", "reason": f"No IPv6 records found for {hostname}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def resolve_mx(self, domain: str) -> dict:
        """Resolve MX records (mail servers)."""
        try:
            result = subprocess.run(
                ["dig", f"{domain}", "MX", "+short"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "dig command failed. Install: sudo apt install dnsutils"}
            
            mx_records = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    parts = line.split()
                    if len(parts) >= 2:
                        priority = parts[0]
                        server = parts[1].rstrip(".")
                        mx_records.append({"priority": priority, "server": server})
            
            return {
                "status": "success",
                "domain": domain,
                "record_type": "MX",
                "mx_records": mx_records,
                "message": f"Found {len(mx_records)} MX record(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def resolve_cname(self, hostname: str) -> dict:
        """Resolve CNAME record."""
        try:
            result = subprocess.run(
                ["dig", f"{hostname}", "CNAME", "+short"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "dig command failed"}
            
            cname = result.stdout.strip().rstrip(".")
            
            if not cname:
                return {"status": "error", "reason": f"No CNAME record found for {hostname}"}
            
            return {
                "status": "success",
                "hostname": hostname,
                "record_type": "CNAME",
                "cname": cname,
                "message": f"CNAME resolved to {cname}"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def resolve_ns(self, domain: str) -> dict:
        """Resolve NS records (nameservers)."""
        try:
            result = subprocess.run(
                ["dig", f"{domain}", "NS", "+short"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "dig command failed"}
            
            nameservers = [ns.rstrip(".") for ns in result.stdout.strip().split("\n") if ns]
            
            return {
                "status": "success",
                "domain": domain,
                "record_type": "NS",
                "nameservers": nameservers,
                "message": f"Found {len(nameservers)} nameserver(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def resolve_txt(self, domain: str) -> dict:
        """Resolve TXT records."""
        try:
            result = subprocess.run(
                ["dig", f"{domain}", "TXT", "+short"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "dig command failed"}
            
            txt_records = []
            for line in result.stdout.strip().split("\n"):
                if line:
                    txt_records.append(line.strip('"'))
            
            return {
                "status": "success",
                "domain": domain,
                "record_type": "TXT",
                "txt_records": txt_records,
                "message": f"Found {len(txt_records)} TXT record(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def reverse_dns(self, ip_address: str) -> dict:
        """Reverse DNS lookup (IP to hostname)."""
        try:
            hostname, _, _ = socket.gethostbyaddr(ip_address)
            
            return {
                "status": "success",
                "ip_address": ip_address,
                "hostname": hostname,
                "message": f"Reverse DNS: {ip_address} → {hostname}"
            }
        
        except socket.herror:
            return {"status": "error", "reason": f"No reverse DNS record for {ip_address}"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute dns_lookup skill with structured args."""
    args = payload.get("args", {}) or {}
    record_type = (args.get("record_type") or "").strip().upper()

    if not record_type:
        return {
            "status": "info",
            "message": "DNS record types: A, AAAA, MX, CNAME, NS, TXT, reverse"
        }

    try:
        skill = DNSLookupSkill()

        if record_type == "REVERSE":
            ip_address = (args.get("ip_address") or "").strip()
            if not ip_address:
                return {"status": "error", "reason": "reverse requires an 'ip_address'"}
            return await skill.reverse_dns(ip_address)

        hostname = (args.get("hostname") or "").strip()
        if not hostname:
            return {"status": "error", "reason": f"{record_type} requires a 'hostname'"}

        if record_type == "A":
            return await skill.resolve_a(hostname)
        elif record_type == "AAAA":
            return await skill.resolve_aaaa(hostname)
        elif record_type == "MX":
            return await skill.resolve_mx(hostname)
        elif record_type == "CNAME":
            return await skill.resolve_cname(hostname)
        elif record_type == "NS":
            return await skill.resolve_ns(hostname)
        elif record_type == "TXT":
            return await skill.resolve_txt(hostname)
        else:
            return {
                "status": "error",
                "reason": f"Unknown record_type '{record_type}'. Must be one of: A, AAAA, MX, CNAME, NS, TXT, reverse"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
