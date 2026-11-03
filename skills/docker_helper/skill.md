# Docker Helper

## What It Does
Provides Docker command references, Dockerfile generation, docker-compose templates, and troubleshooting guidance. Explains containerization concepts and best practices.

## When to Use It
- User asks how to build, run, or push a Docker image
- User wants a Dockerfile or docker-compose.yml generated
- User is troubleshooting container issues
- User learning Docker for the first time

## When NOT to Use It
- User wants to *execute* Docker commands — OADSON has no shell access
- User wants Kubernetes orchestration — beyond this skill's scope
- User just wants general Linux help → answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `list_containers` | running (or all) containers. |
| `list_images` | local images. |
| `logs` | a container's recent logs. |
| `stats` | live resource usage. |
| `inspect` | full container details. |
| `docker_info` | Docker system/daemon info. |

## Output Format
```json
{
  "command": "docker build -t myapp:latest .",
  "explanation": "Builds the image from current directory Dockerfile...",
  "dockerfile": "FROM python:3.11-slim\n...",
  "best_practices": ["Use slim base images", "Layer cache optimization"],
  "troubleshooting": "Check logs with: docker logs <container_id>"
}
```

## Constraints
- Reference and generation only — no execution
- Supports Docker Engine, Docker Compose v2
- Kubernetes guidance is limited
- Generated Dockerfiles are starting points — review before production

## Examples
- "Containerize my Render-deployed FastAPI backend"
- "What's the difference between CMD and ENTRYPOINT?"
- "My container exits immediately after starting — why?"
- "docker-compose for Python + MongoDB Atlas"
