import subprocess
import os
from typing import Optional, List

class PortScannerSkill:
    def __init__(self):
        # Check if nmap is installed
        self.nmap_available = self._check_nmap()
    
    def _check_nmap(self) -> bool:
        """Check if nmap is available."""
        try:
            subprocess.run(["nmap", "--version"], capture_output=True, check=True)
            return True
        except:
            return False
    
    async def scan_host(self, host: str, ports: Optional[str] = None, aggressive: bool = False, confirm: bool = False) -> dict:
        """Scan a host for open ports."""
        if not confirm:
            return {
                "status": "pending_confirmation",
                "action": "scan_host",
                "host": host,
                "ports": ports or "common",
                "message": f"Scan {host} for open ports? Reply 'confirm' to proceed."
            }
        
        if not self._check_nmap():
            return {"status": "error", "reason": "nmap is not installed. Install with: sudo apt install nmap"}
        
        try:
            # Build nmap command
            cmd = ["nmap"]
            
            if aggressive:
                cmd.append("-A")  # Aggressive scan
            else:
                cmd.append("-sS")  # Stealth/SYN scan
            
            # Port specification
            if ports:
                cmd.extend(["-p", ports])
            else:
                cmd.append("-p-")  # All ports
            
            cmd.append("-T3")  # Timing template (normal)
            cmd.append(host)
            
            # Run scan with timeout
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": f"Scan failed: {result.stderr}"}
            
            # Parse output
            output = result.stdout
            open_ports = []
            
            for line in output.split("\n"):
                if "/tcp" in line or "/udp" in line:
                    parts = line.split()
                    if parts and "open" in line:
                        open_ports.append(line.strip())
            
            return {
                "status": "success",
                "host": host,
                "ports_scanned": ports or "all common",
                "open_ports": open_ports,
                "raw_output": output,
                "message": f"Found {len(open_ports)} open port(s)"
            }
        
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": "Scan timed out"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def scan_common_ports(self, host: str, confirm: bool = False) -> dict:
        """Scan only common ports (top 100)."""
        if not confirm:
            return {
                "status": "pending_confirmation",
                "action": "scan_common_ports",
                "host": host,
                "message": f"Scan {host} for common ports? Reply 'confirm' to proceed."
            }
        
        return await self.scan_host(host, ports="1-1000", confirm=True)
    
    async def scan_specific_ports(self, host: str, port_list: List[int], confirm: bool = False) -> dict:
        """Scan specific ports."""
        if not confirm:
            ports_str = ",".join(map(str, port_list))
            return {
                "status": "pending_confirmation",
                "action": "scan_specific_ports",
                "host": host,
                "ports": ports_str,
                "message": f"Scan {host} ports {ports_str}? Reply 'confirm' to proceed."
            }
        
        ports_str = ",".join(map(str, port_list))
        return await self.scan_host(host, ports=ports_str, confirm=True)
    
    async def version_detection(self, host: str, ports: Optional[str] = None, confirm: bool = False) -> dict:
        """Detect service versions on open ports."""
        if not confirm:
            return {
                "status": "pending_confirmation",
                "action": "version_detection",
                "host": host,
                "message": f"Detect service versions on {host}? Reply 'confirm' to proceed."
            }
        
        if not self._check_nmap():
            return {"status": "error", "reason": "nmap is not installed"}
        
        try:
            cmd = ["nmap", "-sV"]
            
            if ports:
                cmd.extend(["-p", ports])
            else:
                cmd.extend(["-p", "1-1000"])
            
            cmd.append(host)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": f"Scan failed: {result.stderr}"}
            
            return {
                "status": "success",
                "host": host,
                "version_detection": result.stdout,
                "message": "Version detection complete"
            }
        
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": "Scan timed out"}
        except Exception as e:
            return {"status": "error", "reason": str(e)}

# ── SKILL ENTRY POINT ──
async def run(payload):
    """
    Execute port_scanner skill with structured args.

    REWRITTEN: same blind try-every-method pattern as the other security
    skills, but with one extra concern — this skill's existing confirm=
    safety gate (correctly designed: scanning shouldn't fire on the first
    ask) MUST be preserved exactly, not bypassed for convenience. The
    structured schema makes confirm an explicit boolean the model has to
    consciously set true, with manifest.json instructing it to never
    assume confirmation — same two-step flow as before, just reachable
    via real actions instead of blind method-guessing.
    """
    args = payload.get("args", {}) or {}
    action = args.get("action")
    host = (args.get("host") or "").strip()
    confirm = bool(args.get("confirm", False))

    if not action:
        return {
            "status": "info",
            "message": "Port scanner actions: scan_common, scan_all, scan_specific (with ports), version_detection. All require host, and confirm=true to actually run."
        }

    if not host:
        return {"status": "error", "reason": f"{action} requires a 'host'"}

    try:
        skill = PortScannerSkill()
        ports = (args.get("ports") or "").strip() or None
        aggressive = bool(args.get("aggressive", False))

        if action == "scan_common":
            return await skill.scan_common_ports(host, confirm=confirm)

        elif action == "scan_all":
            return await skill.scan_host(host, ports=ports, aggressive=aggressive, confirm=confirm)

        elif action == "scan_specific":
            if not ports:
                return {"status": "error", "reason": "scan_specific requires 'ports'"}
            try:
                port_list = [int(p.strip()) for p in ports.split(",")]
            except ValueError:
                return {"status": "error", "reason": "ports must be a comma-separated list of numbers, e.g. '80,443,8080'"}
            return await skill.scan_specific_ports(host, port_list, confirm=confirm)

        elif action == "version_detection":
            return await skill.version_detection(host, ports=ports, confirm=confirm)

        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: scan_common, scan_all, scan_specific, version_detection"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
