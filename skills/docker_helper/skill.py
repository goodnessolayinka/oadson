import subprocess
from typing import Optional

class DockerHelperSkill:
    def __init__(self):
        pass
    
    async def list_containers(self, all: bool = False) -> dict:
        """List Docker containers."""
        try:
            cmd = ["docker", "ps"]
            if all:
                cmd.append("-a")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "Docker not available or error listing containers"}
            
            lines = result.stdout.strip().split("\n")[1:]  # Skip header
            
            return {
                "status": "success",
                "containers": lines,
                "total": len(lines),
                "message": f"Found {len(lines)} container(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def list_images(self) -> dict:
        """List Docker images."""
        try:
            result = subprocess.run(
                ["docker", "images"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "Failed to list images"}
            
            lines = result.stdout.strip().split("\n")[1:]
            
            return {
                "status": "success",
                "images": lines,
                "total": len(lines),
                "message": f"Found {len(lines)} image(s)"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_logs(self, container_id: str, lines: int = 50) -> dict:
        """Get container logs."""
        try:
            result = subprocess.run(
                ["docker", "logs", "-n", str(lines), container_id],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": f"Container not found: {container_id}"}
            
            logs = result.stdout[:2000]
            
            return {
                "status": "success",
                "container_id": container_id,
                "logs": logs,
                "message": "Logs retrieved"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_stats(self, container_id: str) -> dict:
        """Get container resource stats."""
        try:
            result = subprocess.run(
                ["docker", "stats", "--no-stream", container_id],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": f"Container not found: {container_id}"}
            
            stats = result.stdout.strip()
            
            return {
                "status": "success",
                "container_id": container_id,
                "stats": stats,
                "message": "Stats retrieved"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def inspect_container(self, container_id: str) -> dict:
        """Inspect container details."""
        try:
            result = subprocess.run(
                ["docker", "inspect", container_id],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": f"Container not found: {container_id}"}
            
            import json
            data = json.loads(result.stdout)
            
            if data:
                container = data[0]
                return {
                    "status": "success",
                    "container_id": container_id,
                    "name": container.get("Name", "").strip("/"),
                    "state": container.get("State", {}).get("Status"),
                    "image": container.get("Config", {}).get("Image"),
                    "ports": container.get("NetworkSettings", {}).get("Ports", {}),
                    "message": "Container inspected"
                }
            
            return {"status": "error", "reason": "Could not parse container data"}
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}
    
    async def get_docker_info(self) -> dict:
        """Get Docker system info."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                return {"status": "error", "reason": "Docker not available"}
            
            info = result.stdout.strip()
            
            return {
                "status": "success",
                "info": info[:2000],
                "message": "Docker info retrieved"
            }
        
        except Exception as e:
            return {"status": "error", "reason": str(e)}


# ── SKILL ENTRY POINT ──
async def run(payload):
    """Execute docker_helper skill with structured args."""
    args = payload.get("args", {}) or {}
    action = args.get("action")

    if not action:
        return {
            "status": "info",
            "message": "Docker actions: list_containers, list_images, logs (container_id), stats (container_id), inspect (container_id), docker_info"
        }

    try:
        skill = DockerHelperSkill()

        if action == "list_containers":
            return await skill.list_containers(all=bool(args.get("all_containers", False)))
        elif action == "list_images":
            return await skill.list_images()
        elif action == "logs":
            container_id = (args.get("container_id") or "").strip()
            if not container_id:
                return {"status": "error", "reason": "logs requires a 'container_id'"}
            return await skill.get_logs(container_id, lines=int(args.get("log_lines") or 50))
        elif action == "stats":
            container_id = (args.get("container_id") or "").strip()
            if not container_id:
                return {"status": "error", "reason": "stats requires a 'container_id'"}
            return await skill.get_stats(container_id)
        elif action == "inspect":
            container_id = (args.get("container_id") or "").strip()
            if not container_id:
                return {"status": "error", "reason": "inspect requires a 'container_id'"}
            return await skill.inspect_container(container_id)
        elif action == "docker_info":
            return await skill.get_docker_info()
        else:
            return {
                "status": "error",
                "reason": f"Unknown action '{action}'. Must be one of: list_containers, list_images, logs, stats, inspect, docker_info"
            }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
