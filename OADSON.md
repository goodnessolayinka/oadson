# OADSON.md

This file tells OADSON how to work in this project. It's read fresh on
every message (no restart needed after editing) and injected into the
agent's context ahead of generic shell/git state.

> **Rule for you, the agent:** after finishing any real piece of work,
> update the "Current State" section yourself. Say what you built, what
> changed, and what's now true. Keep everything above "Current State"
> untouched unless the human explicitly tells you to change it.

## Project
OADSON_V3

## Conventions
- (e.g. "Use pytest, not unittest" / "2-space indent" / "no default exports")

## Commands
- Test:  (e.g. `pytest -q`)
- Lint:  (e.g. `ruff check .`)
- Run:   (e.g. `python main.py`)

## Danger zones
- (files/dirs OADSON should never touch without asking twice)

## Notes for OADSON
- (anything else worth knowing every session)

## Current State
Pulled the latest remote changes from origin/main, committed uncommitted updates in provider routing, spend tracker, tool loop, and test files, and successfully pushed OADSON_V3 to GitHub.
