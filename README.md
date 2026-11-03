# OADSON

A self-hostable personal-AI framework. One orchestrator behind web, mobile, messaging, and terminal surfaces: it keeps conversational memory, runs a pluggable skill system (GitHub, Gmail, Calendar, search, and more), routes across several LLM providers with per-provider spend caps, and runs its own proactive messaging.

## Architecture

- `main.py` / `api/`: FastAPI app and REST routes (auth, chat, sessions, biometrics).
- `core/`: orchestrator, tool loop, provider router and registry, spend tracker, proactive agent.
- `memory/`: memory vault, manager, and reasoner.
- `skills/`: one folder per skill (`skill.py` plus `skill.md` / `manifest.json`).
- `database.py` / `core/db_router.py`: MongoDB access.

## Setup

1. Copy `.env.example` to `.env` and fill in the values you need. Blank values disable the feature that depends on them.
2. Install dependencies: `pip install -r requirements.txt`.
3. Run: `uvicorn main:app --reload`.

Never commit `.env`. Production configuration belongs in your host's environment settings.

## Configuration

Every runtime setting is read from environment variables. See `.env.example` for the full list, grouped by feature. Login is restricted to the emails listed in `OADSON_ALLOWED_EMAILS`.

## Status

Early-stage personal project. Expect rough edges and breaking changes.
