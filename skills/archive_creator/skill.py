"""
archive_creator/skill.py

Creates a zip archive from files in the user's sandbox and serves it via
the inline file storage endpoint (/files/serve/) instead of Cloudinary.
Cloudinary is disabled by default (USE_CLOUDINARY=false) — the old version
hard-coded upload_to_cloudinary and always got a 404.

Also handles the case where the model passes an entire cloned repo directory
path (e.g. 'repos/owner__reponame') — recursively adds all files in that
directory, not just a flat list of individual files.
"""

import zipfile
import io
import os
from pathlib import Path


class ArchiveCreatorSkill:
    def __init__(self, user_id: str, db=None):
        from core.oadson_home import get_user_home, is_safe_path
        self.user_id = user_id
        self.db = db
        self.home = get_user_home(user_id)
        self._is_safe_path = lambda p: is_safe_path(user_id, p)

    async def create_zip(self, files: list, archive_name: str = "archive.zip") -> dict:
        """
        Create a zip from files/directories in the user's sandbox.
        Paths can be individual files OR directories (recursively zipped).
        Saves via inline_storage and returns a /files/serve/ URL.
        """
        if not files:
            return {"status": "error", "reason": "No files provided."}

        if not archive_name.endswith(".zip"):
            archive_name += ".zip"

        buffer = io.BytesIO()
        included = []
        skipped = []

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in files:
                # Normalize: strip leading slashes so it stays relative
                file_path = file_path.lstrip("/")

                if not self._is_safe_path(file_path):
                    skipped.append({"file": file_path, "reason": "outside sandbox"})
                    continue

                full_path = self.home / file_path
                if not full_path.exists():
                    skipped.append({"file": file_path, "reason": "not found"})
                    continue

                if full_path.is_dir():
                    # Recursively add directory contents
                    dir_files_added = 0
                    for sub in full_path.rglob("*"):
                        if sub.is_file():
                            # Preserve directory structure inside zip
                            arcname = str(sub.relative_to(self.home))
                            zf.write(sub, arcname=arcname)
                            dir_files_added += 1
                    if dir_files_added > 0:
                        included.append(f"{file_path}/ ({dir_files_added} files)")
                    else:
                        skipped.append({"file": file_path, "reason": "empty directory"})
                else:
                    zf.write(full_path, arcname=Path(file_path).name)
                    included.append(file_path)

        if not included:
            return {
                "status": "error",
                "reason": "No valid files were included in the archive.",
                "skipped": skipped,
            }

        buffer.seek(0)
        zip_bytes = buffer.read()

        # Save via inline_storage — no Cloudinary needed
        try:
            from core.inline_storage import save_inline
            backend_url = os.getenv("BACKEND_URL", "").rstrip("/")
            download_url = await save_inline(
                self.db,
                zip_bytes,
                archive_name,
                "application/zip",
                bucket="archives",
            )
            # save_inline returns a relative path or full URL
            if download_url and not download_url.startswith("http"):
                download_url = f"{backend_url}{download_url}"
        except Exception as e:
            # Fallback: save to sandbox and tell user where it is
            save_path = self.home / archive_name
            save_path.write_bytes(zip_bytes)
            download_url = None
            fallback_path = str(save_path.relative_to(self.home))

        result = {
            "status": "success",
            "archive_name": archive_name,
            "size_bytes": len(zip_bytes),
            "size_kb": round(len(zip_bytes) / 1024, 1),
            "files_included": included,
            "files_skipped": skipped,
            "message": f"Archive '{archive_name}' created with {len(included)} item(s) — {round(len(zip_bytes)/1024, 1)}KB.",
        }

        if download_url:
            result["download_url"] = download_url
            result["filename"] = archive_name
        else:
            result["sandbox_path"] = fallback_path
            result["message"] += f" Saved to sandbox: {fallback_path}. Use @terminal to access it."

        return result


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    args = payload.get("args", {}) or {}
    db = payload.get("db")

    files = args.get("files")
    archive_name = (args.get("archive_name") or "archive.zip").strip()

    if not user_id:
        return {"status": "error", "reason": "user_id not available."}
    if not files or not isinstance(files, list):
        return {
            "status": "error",
            "reason": (
                "'files' array is required. Pass relative paths from the sandbox root, "
                "e.g. ['repos/owner__reponame'] to zip a whole cloned repo."
            ),
        }

    try:
        # Try to get db from bootstrap if not in payload
        if not db:
            try:
                from bootstrap import get as _get
                db = _get("db")
            except Exception:
                pass

        skill = ArchiveCreatorSkill(user_id=user_id, db=db)
        return await skill.create_zip(files, archive_name=archive_name)
    except Exception as e:
        return {"status": "error", "reason": str(e)}
