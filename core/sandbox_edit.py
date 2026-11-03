"""
core/sandbox_edit.py — shared file-edit primitive for skills that need
to modify an EXISTING file at a fixed path within the sandbox, as
opposed to core.oadson_home.save_to_sandbox's job (saving something
NEW — a fresh upload or a freshly generated file — with basename-only,
no-clobber-by-rename semantics that are exactly right for that but
wrong here).

Built 2026-08-31 for website_developer's and backend_developer's new
`edit` actions — see each skill's skill.md for the workflow this
supports: @github clone_to_workspace -> edit here (or a freshly built
site/backend) -> @github push_workspace. Before this, both skills could
only ever generate a brand new file tree; there was no way to change
ONE thing in an existing file without regenerating everything around
it and losing any manual edits made outside OADSON.

Deliberately minimal — read, overwrite, and a strict unique-match
str_replace, matching the same "old_str must match exactly once, no
silent multi-replace" discipline as OADSON's own str_replace tool, for
the same reason: an edit that could silently apply to more than one
spot (or apply to a different spot than the model meant) is worse than
one that fails loudly and asks for a more specific match.
"""

from pathlib import Path
from core.oadson_home import get_user_home, is_safe_path


def read_sandbox_file(user_id: str, rel_path: str) -> tuple[str | None, str | None]:
    """
    Returns (content, None) on success, or (None, error_message).
    rel_path is relative to the workspace root (get_user_home) — e.g.
    'my-site/index.html', 'repos/owner__repo/src/App.jsx'.
    """
    if not is_safe_path(user_id, rel_path):
        return None, f"'{rel_path}' is outside the sandbox — use a relative path within your workspace."
    full = get_user_home(user_id) / rel_path
    if not full.exists():
        return None, f"'{rel_path}' does not exist in your sandbox. Check the path — did you mean to build it first, or clone a repo that contains it?"
    if not full.is_file():
        return None, f"'{rel_path}' is a directory, not a file."
    try:
        return full.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, f"'{rel_path}' isn't a text file — can't edit it as text."
    except Exception as e:
        return None, f"failed to read '{rel_path}': {e}"


def write_sandbox_file(user_id: str, rel_path: str, content: str, must_exist: bool = False) -> tuple[str | None, str | None]:
    """
    Overwrites (or creates, if must_exist=False) rel_path with content,
    AT THAT EXACT PATH — unlike save_to_sandbox, this never renames to
    avoid a clobber and never strips subdirectories from the path. That
    is the entire point of this function: an edit must land on the same
    file it read from, not a sibling with a suffix appended.

    Returns (rel_path, None) on success, or (None, error_message).
    """
    if not is_safe_path(user_id, rel_path):
        return None, f"'{rel_path}' is outside the sandbox — use a relative path within your workspace."
    full = get_user_home(user_id) / rel_path
    if must_exist and not full.exists():
        return None, f"'{rel_path}' does not exist — nothing to edit. Use the build action to create it first."
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return rel_path, None
    except Exception as e:
        return None, f"failed to write '{rel_path}': {e}"


def str_replace_sandbox_file(user_id: str, rel_path: str, old_str: str, new_str: str) -> dict:
    """
    Read rel_path, replace exactly one occurrence of old_str with
    new_str, write it back. Fails loudly (no write happens) if old_str
    matches zero or more-than-one times — same discipline as OADSON's
    own str_replace tool, for the same reason: silently picking one of
    several matches (or applying to none) is a worse failure mode than
    asking for a more specific match.

    Returns a result dict shaped like every other skill action result
    ({"status": ..., ...}) rather than a bare tuple, since this is
    called directly from each skill's run() as a full action handler,
    not just a helper another function post-processes.
    """
    content, err = read_sandbox_file(user_id, rel_path)
    if err:
        return {"status": "error", "reason": err}

    count = content.count(old_str)
    if count == 0:
        return {
            "status": "error",
            "reason": (
                f"old_str not found in '{rel_path}'. It must match the file's actual content "
                "exactly, including whitespace — read the file first if you're not certain of "
                "the exact text, don't guess or reconstruct it from memory."
            ),
        }
    if count > 1:
        return {
            "status": "error",
            "reason": (
                f"old_str matches {count} places in '{rel_path}' — it must match exactly once. "
                "Include more surrounding text (a nearby unique line, a specific attribute value) "
                "to narrow it down to the one spot you actually mean to change."
            ),
        }

    new_content = content.replace(old_str, new_str, 1)
    saved_path, write_err = write_sandbox_file(user_id, rel_path, new_content, must_exist=True)
    if write_err:
        return {"status": "error", "reason": write_err}

    return {
        "status": "success",
        "file_path": saved_path,
        "message": f"Edited '{rel_path}' — replaced 1 occurrence.",
    }
