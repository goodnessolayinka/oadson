"""
backend_developer/skill.py

Generates a complete FastAPI backend (main.py, models.py, database.py,
routes per resource, requirements.txt) from a description of
resources/entities. Same pattern as website_developer: no LLM call
inside this skill — pure structural generation from the args Gemini
already extracted, then a REAL self-review (every generated .py file
is parsed with ast.parse — a genuine syntax guarantee, not a
heuristic checklist) before anything gets saved.
"""

import re
import ast
from datetime import datetime, timezone


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", text) or "backend"


def _pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\s-]+", name) if w)


def _default_resources() -> list:
    return [{
        "name": "item",
        "fields": [
            {"name": "title", "type": "str", "required": True},
            {"name": "description", "type": "str", "required": False, "default": ""},
            {"name": "done", "type": "bool", "required": False, "default": False},
        ],
    }]


PY_TYPE_MAP = {"str": "str", "int": "int", "float": "float", "bool": "bool", "datetime": "datetime"}


def _field_annotation(field: dict) -> str:
    py_type = PY_TYPE_MAP.get(field.get("type", "str"), "str")
    if not field.get("required", True):
        return f"Optional[{py_type}]"
    return py_type


def _field_default(field: dict) -> str:
    if field.get("required", True):
        return ""
    default = field.get("default")
    if default is None:
        return " = None"
    if isinstance(default, str):
        return f' = "{default}"'
    if isinstance(default, bool):
        return f" = {default}"
    return f" = {default}"


def _build_model_file(resources: list) -> str:
    imports = ["from pydantic import BaseModel", "from typing import Optional"]
    if any(f.get("type") == "datetime" for r in resources for f in r.get("fields", [])):
        imports.append("from datetime import datetime")

    blocks = []
    for res in resources:
        name = _pascal(res["name"])
        fields = res.get("fields") or []
        field_lines = "\n".join(
            f"    {f['name']}: {_field_annotation(f)}{_field_default(f)}" for f in fields
        ) or "    pass"

        blocks.append(f"""
class {name}Create(BaseModel):
{field_lines}


class {name}Update(BaseModel):
{chr(10).join(f"    {f['name']}: Optional[{PY_TYPE_MAP.get(f.get('type','str'),'str')}] = None" for f in fields) or "    pass"}


class {name}(BaseModel):
    id: str
{field_lines}
""")

    return "\n".join(imports) + "\n\n" + "\n".join(blocks)


def _build_database_file(project_name: str) -> str:
    return f'''"""
database.py — {project_name}
Async MongoDB via Motor. Set MONGODB_URI in your environment.
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "{_slugify(project_name).replace('-', '_')}")

client = AsyncIOMotorClient(MONGODB_URI)
db = client[DB_NAME]


async def init_db() -> None:
    await client.admin.command("ping")


async def close_db() -> None:
    client.close()
'''


def _build_routes_file(resource: dict) -> str:
    name = resource["name"]
    Name = _pascal(name)
    collection = f"{name}s"

    return f'''"""
routes_{name}.py — CRUD endpoints for {name}.
"""

from fastapi import APIRouter, HTTPException
from bson import ObjectId
from bson.errors import InvalidId

from database import db
from models import {Name}, {Name}Create, {Name}Update

router = APIRouter(prefix="/{collection}", tags=["{Name}"])


def _serialize(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def _object_id(item_id: str) -> ObjectId:
    # Was: ObjectId(item_id) called directly at each route below — an
    # invalid id (wrong length, non-hex chars) raises bson's InvalidId
    # uncaught, producing a raw 500 instead of a clean 400. A malformed
    # id in the URL is a client mistake, not a server error.
    try:
        return ObjectId(item_id)
    except (InvalidId, TypeError):
        raise HTTPException(400, f"'{{item_id}}' is not a valid {Name} id.")


@router.post("/", response_model={Name})
async def create_{name}(body: {Name}Create):
    result = await db["{collection}"].insert_one(body.model_dump())
    doc = await db["{collection}"].find_one({{"_id": result.inserted_id}})
    return _serialize(doc)


@router.get("/")
async def list_{name}s(limit: int = 50, skip: int = 0):
    cursor = db["{collection}"].find().skip(skip).limit(limit)
    return [_serialize(doc) async for doc in cursor]


@router.get("/{{item_id}}", response_model={Name})
async def get_{name}(item_id: str):
    doc = await db["{collection}"].find_one({{"_id": _object_id(item_id)}})
    if not doc:
        raise HTTPException(404, "{Name} not found")
    return _serialize(doc)


@router.patch("/{{item_id}}", response_model={Name})
async def update_{name}(item_id: str, body: {Name}Update):
    oid = _object_id(item_id)
    updates = {{k: v for k, v in body.model_dump().items() if v is not None}}
    if updates:
        await db["{collection}"].update_one({{"_id": oid}}, {{"$set": updates}})
    doc = await db["{collection}"].find_one({{"_id": oid}})
    if not doc:
        raise HTTPException(404, "{Name} not found")
    return _serialize(doc)


@router.delete("/{{item_id}}")
async def delete_{name}(item_id: str):
    result = await db["{collection}"].delete_one({{"_id": _object_id(item_id)}})
    if result.deleted_count == 0:
        raise HTTPException(404, "{Name} not found")
    return {{"status": "deleted", "id": item_id}}
'''


def _build_main_file(project_name: str, description: str, resources: list, auth_required: bool) -> str:
    resource_names = [r["name"] for r in resources]
    imports = "\n".join(f"from routes_{n} import router as {n}_router" for n in resource_names)

    auth_block = ""
    include_deps = ""
    fastapi_imports = "from fastapi import FastAPI"
    if auth_required:
        # Was: this dependency function was defined but never actually
        # applied to anything — every include_router() call ignored it
        # completely, so auth_required:true generated a backend where
        # nothing was actually protected despite the skill's own docs
        # claiming "a real dependency function, not just a comment".
        fastapi_imports = "from fastapi import FastAPI, Depends, Header, HTTPException"
        include_deps = ", dependencies=[Depends(require_auth)]"
        auth_block = '''

async def require_auth(authorization: str = Header(None)):
    """Bearer-token auth stub — replace the check below with real validation."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header.")
    token = authorization.removeprefix("Bearer ")
    if not token:
        raise HTTPException(401, "Empty token.")
    return token
'''
    includes = "\n".join(f"app.include_router({n}_router{include_deps})" for n in resource_names)

    return f'''"""
main.py — {project_name}

{description}

Generated by OADSON backend_developer skill. Structural starting
point — wire up real auth, validation, and business logic as needed.
"""

from contextlib import asynccontextmanager
{fastapi_imports}

from database import init_db, close_db
{imports}
{auth_block}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(title="{project_name}", lifespan=lifespan)

{includes}


@app.get("/health")
async def health():
    return {{"status": "ok"}}
'''


def _build_requirements() -> str:
    return "\n".join([
        "fastapi>=0.111.0",
        "uvicorn[standard]>=0.29.0",
        "motor>=3.4.0",
        "pydantic>=2.7.0",
        "python-dotenv>=1.0.0",
    ]) + "\n"


def _build_readme(project_name: str, description: str, resources: list) -> str:
    resource_list = "\n".join(f"- **{r['name']}** — full CRUD at `/{r['name']}s`" for r in resources)
    return f'''# {project_name}

{description}

## Resources

{resource_list}

## Run locally

```bash
pip install -r requirements.txt
export MONGODB_URI="mongodb://localhost:27017"
uvicorn main:app --reload
```

Then visit `/docs` for interactive API docs.
'''


def _self_review(files: dict) -> list:
    """Real syntax guarantee — every .py file must parse with ast.parse, not a heuristic."""
    issues = []
    for filename, content in files.items():
        if filename.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as e:
                issues.append(f"{filename}: syntax error — {e}")
    return issues


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    args = payload.get("args", {}) or {}
    action = args.get("action") or "build"

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    if action == "list_templates":
        return {
            "status": "success",
            "message": "Provide 'resources' as a list of {name, fields:[{name, type, required, default}]}. "
                       "Supported field types: str, int, float, bool, datetime. "
                       "If 'resources' is omitted, a single generic 'item' resource is generated.",
        }

    if action == "edit":
        from core.sandbox_edit import str_replace_sandbox_file

        file_path = (args.get("file_path") or "").strip()
        old_str = args.get("old_str")
        new_str = args.get("new_str")

        if not file_path:
            return {"status": "error", "reason": "edit requires 'file_path'"}
        if old_str is None:
            return {"status": "error", "reason": "edit requires 'old_str'"}
        if new_str is None:
            return {"status": "error", "reason": "edit requires 'new_str' (use an empty string to delete old_str)"}

        return str_replace_sandbox_file(user_id, file_path, old_str, new_str)

    if action != "build":
        return {"status": "error", "reason": f"Unknown action '{action}'. Use: build, edit, list_templates"}

    project_name = (args.get("project_name") or "").strip()
    if not project_name:
        return {"status": "error", "reason": "build requires 'project_name'"}

    description = (args.get("description") or "").strip() or f"Backend API for {project_name}."
    resources = args.get("resources") or _default_resources()
    auth_required = bool(args.get("auth_required", False))
    folder_name = (args.get("folder_name") or _slugify(project_name)).strip()

    # Normalize resource names (lowercase, no spaces) so generated
    # Python identifiers are always valid regardless of user input.
    for r in resources:
        r["name"] = re.sub(r"[^a-z0-9_]", "_", r["name"].lower().strip())
        if not r.get("fields"):
            r["fields"] = [{"name": "name", "type": "str", "required": True}]

    try:
        files = {
            "main.py": _build_main_file(project_name, description, resources, auth_required),
            "models.py": _build_model_file(resources),
            "database.py": _build_database_file(project_name),
            "requirements.txt": _build_requirements(),
            "README.md": _build_readme(project_name, description, resources),
        }
        for res in resources:
            files[f"routes_{res['name']}.py"] = _build_routes_file(res)

        review_issues = _self_review(files)
        if review_issues:
            # A syntax error here is a real bug in the template logic
            # itself (not a user-input problem, since resource names are
            # normalized above) — surface it clearly rather than saving
            # broken files silently.
            return {
                "status": "error",
                "reason": f"Generated code failed its own syntax check — this is a template bug, not saved: {'; '.join(review_issues)}",
            }

        try:
            from core.oadson_home import save_to_sandbox
        except ImportError:
            return {"status": "error", "reason": "Sandbox storage unavailable (core.oadson_home not found)"}

        saved_paths = []
        for filename, content in files.items():
            path = f"{folder_name}/{filename}"
            # WORKSPACE SPLIT 2026-08-28: generated code is scratch
            # work-in-progress, not a file the user sent — belongs in
            # workspace/, not the new inputs/ default. See
            # core/oadson_home.py's save_to_sandbox area= param.
            saved_path, save_error = save_to_sandbox(user_id, path, content, area="workspace")
            if save_error:
                return {"status": "error", "reason": f"Failed to save {filename}: {save_error}"}
            saved_paths.append(saved_path)

        return {
            "status": "success",
            "project_name": project_name,
            "folder": folder_name,
            "resources": [r["name"] for r in resources],
            "files_created": list(files.keys()),
            "saved_paths": saved_paths,
            "auth_required": auth_required,
            "message": (
                f"Built a FastAPI backend for '{project_name}' with {len(resources)} resource(s) "
                f"({', '.join(r['name'] for r in resources)}), saved to workspace folder '{folder_name}'. "
                "All generated files passed syntax validation. Run 'pip install -r requirements.txt' "
                "then 'uvicorn main:app --reload' to try it."
            ),
        }

    except Exception as e:
        return {"status": "error", "reason": str(e)}
