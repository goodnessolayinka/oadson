# Backend Developer Skill

Expert-level FastAPI backend generation. Mirrors website_developer's
pattern: structural generation, no LLM call inside the skill, real
self-review, save to sandbox.

## When to use
- User asks to build/create a backend, API, or server
- User describes data/resources they want to manage (tasks, users, products, etc.)
- User wants a real, runnable FastAPI starting point — not a single snippet

## Actions
| Action | Use for |
|---|---|
| `build` (default) | Generate a full backend project — the main flow, documented below |
| `list_templates` | Show what resource shapes are supported, before committing to a `build` call |

## What it produces
A complete FastAPI project saved into the user's sandbox workspace:
- `main.py` — app entry point, lifespan-managed DB connection, router wiring
- `models.py` — Pydantic Create/Update/Read models per resource
- `database.py` — async Motor MongoDB connection
- `routes_<resource>.py` — one file per resource, full CRUD (POST/GET/PATCH/DELETE)
- `requirements.txt`
- `README.md`

## Standard applied to every generated backend
- Full CRUD per resource: create, list (paginated), get one, update, delete
- Proper separation: models / routes / database in their own files, not one giant file
- Async throughout (Motor, not pymongo sync driver)
- 404s raised correctly for missing resources, not silent nulls
- Resource/field names normalized to valid Python identifiers regardless of
  how the user phrased them
- Optional Bearer-token auth stub when `auth_required: true` — a real
  dependency function, not just a comment saying "add auth here"

## Self-review — the real guarantee
Every generated `.py` file is parsed with `ast.parse()` before anything is
saved. This isn't a heuristic checklist like website_developer's — it's an
actual syntax guarantee. If any generated file fails to parse, nothing is
saved and the error is surfaced immediately as a template bug, never
silently written to the user's workspace broken.

## Limitations
- MongoDB only (Motor/async) — no SQL/Postgres/SQLAlchemy generation yet
- CRUD structure only — no business logic, relationships between resources,
  or complex queries generated (that's for the user or a follow-up request
  to add on top of the generated starting point)
- Auth stub is a real dependency function but does no actual token
  validation — it's a wiring point, not a security implementation
- No test files generated

## Workflow
1. Understand request → determine project_name, description, resources, auth_required
2. Generate main.py, models.py, database.py, one routes file per resource,
   requirements.txt, README.md
3. Parse every .py file with ast.parse — if any fails, stop and report
   the template bug rather than saving broken code
4. Save to sandbox
5. Return a summary + how to run it

## edit action

Changes ONE specific part of a file already in the sandbox — either
something this skill built earlier, or a file pulled in via
`@github clone_to_workspace`. Does NOT regenerate the file — a targeted
string replacement only. `old_str` must match the file's real, exact
content in exactly one place, or the edit is rejected rather than
guessing which occurrence you meant.

```
backend_developer action='edit'
  file_path='task-api/routes_task.py'
  old_str='@router.get("/tasks")'
  new_str='@router.get("/tasks", dependencies=[Depends(require_auth)])'
```

Read the file first (`@document_reader` or `@terminal cat`) if you're
not certain of its exact current content, rather than reconstructing
`old_str` from memory of the template.

## Working with an existing GitHub repo — the real workflow

`build` is for a genuinely new backend from scratch. Changing something
in a backend that already exists (your own prior build, or a real repo)
is a 3-skill loop:

```
1. @github action='clone_to_workspace' owner='...' repo='...'
2. backend_developer action='edit' file_path='repos/{owner}__{repo}/...' ...
   (or @website_developer action='edit' for frontend files in the same
   repo, or @terminal for anything neither dev-builder skill covers)
3. @github action='push_workspace' owner='...' repo='...' commit_message='...'
```

**Never regenerate a whole file with `build` to make one small change**
— that discards everything else `build`'s own templates don't know
about. `build` creates; `edit` changes.
