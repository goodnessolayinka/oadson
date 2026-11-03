"""
core/attachment_processor.py — OADSON V3 shared attachment handling.

Used by both api/chat.py and api/chat_stream.py so their file-handling
logic can't drift apart. Handles up to 20 files in one message:
  - Images: first one becomes the primary image_data/image_mime (real
    inline bytes sent to the vision model), the rest go into
    extra_images — the same mechanism built for WhatsApp multi-image
    album batching (see core/provider_router.py), reused here rather
    than building a second, separate multi-image path.
  - Text/code files: saved into the user's oadson_home sandbox (see
    core/oadson_home.py's save_to_sandbox) so @document_reader can
    genuinely read them, instead of the model guessing their contents
    from the filename — the actual bug this whole module exists to fix.
  - Archives: base64 content passed through as-is for the orchestrator/
    skills to handle (unchanged from the previous single-file behavior).
"""

MAX_ATTACHMENTS = 20


def _is_image(file_type: str) -> bool:
    return (file_type or "").startswith("image/")


# ADDED 2026-10-07 — web voice-note support. The image_data/image_mime
# slot below is NOT actually image-specific despite the name — it's
# Gemini's generic inline-data mechanism (mimeType + base64 bytes), and
# api/whatsapp.py's voice-note handler already proves this: it reuses
# this exact same image_data/image_mime pair for audio (`image_mime =
# mime or "audio/ogg"`), not a separate field. This function and its
# use below follow that same established convention instead of
# inventing a parallel audio_data/audio_mime pair — one inline-data slot,
# the mime type says what it actually contains. web/mobile recorders
# commonly produce audio/webm or audio/ogg; a few browsers may emit
# audio/mp4 or audio/mpeg depending on codec support, so this checks
# the general "audio/" prefix rather than a hardcoded format allowlist.
def _is_audio(file_type: str) -> bool:
    return (file_type or "").startswith("audio/")


def _is_archive(file_name: str, file_type: str) -> bool:
    archive_exts = (".zip", ".tar", ".gz", ".tgz", ".rar", ".7z")
    name = (file_name or "").lower()
    return name.endswith(archive_exts) or "zip" in (file_type or "") or "archive" in (file_type or "")


async def process_attachments(user_id: str, objective: str, attachments: list) -> dict:
    """
    Returns {"objective": str, "image_data": str|None, "image_mime": str|None,
             "extra_images": list, "archive_data": str|None, "archive_name": str|None}

    archive_data/archive_name only ever carries ONE archive (the first
    one seen) — multiple simultaneous archive uploads is a real edge
    case but not one worth building full multi-archive sandbox handling
    for right now; a second archive attached alongside others still gets
    noted in the objective text even though only the first is processed.
    """
    from core.oadson_home import save_to_sandbox
    import base64

    if not attachments:
        return {"objective": objective, "image_data": None, "image_mime": None,
                "extra_images": [], "archive_data": None, "archive_name": None}

    attachments = attachments[:MAX_ATTACHMENTS]

    image_data = None
    image_mime = None
    extra_images = []
    archive_data = None
    archive_name = None
    saved_notes = []
    skipped_notes = []

    for att in attachments:
        file_name = att.get("file_name") or "attachment"
        file_type = att.get("file_type") or ""

        if _is_image(file_type) and att.get("file_data"):
            if image_data is None:
                image_data = att["file_data"]
                image_mime = file_type
            else:
                extra_images.append({"data": att["file_data"], "mime": file_type, "filename": file_name})

            # ADDED 2026-08-31: previously an uploaded image ONLY ever
            # became image_data/extra_images — real bytes handed to the
            # model's vision input for THIS turn, but never written
            # anywhere on disk. That meant there was no way to get an
            # uploaded image into a GitHub repo at all: @github's
            # push_workspace (the one path that already correctly
            # handles binary — see its base64-blob fallback for
            # anything that fails a UTF-8 decode) reads from the local
            # sandbox filesystem, and nothing had ever put the image
            # there. The model could see it, but had no path to save
            # it. Fixed the same way archives are handled just above —
            # area="workspace" so it lands where @terminal/@github can
            # actually reach it, not the inputs/ default (which those
            # two skills don't look in — see save_to_sandbox's own
            # docstring on the inputs/workspace split).
            #
            # BUGFIX 2026-10-08 — this whole block was accidentally left
            # INSIDE the audio elif branch below when that branch was
            # added (same indentation level as that branch's body, not a
            # sibling of the outer if/elif) — meaning every audio
            # attachment ALSO ran through this image-save-and-label code
            # unconditionally, got written to disk a second time, and
            # got a confusing second "(image, also saved to your sandbox
            # workspace...)" note appended right after the correct
            # "(voice note...)" one. Moved back inside the image branch
            # where it was always meant to live — audio has its own,
            # separate handling below and never needs this at all, since
            # @github's push_workspace doesn't care about audio files
            # the same way it does images in a repo.
            try:
                raw_bytes = base64.b64decode(att["file_data"])
                img_saved_path, img_save_err = save_to_sandbox(user_id, file_name, raw_bytes, area="workspace")
                if img_saved_path:
                    saved_notes.append(
                        f"'{file_name}' (image, also saved to your sandbox workspace as "
                        f"'{img_saved_path}' — to add it to a GitHub repo: clone_to_workspace, "
                        f"copy/move it into the cloned repo folder with @terminal if needed, "
                        f"then push_workspace. You can already see this image directly in this "
                        f"turn without reading the saved file.)"
                    )
                # A failed sandbox save is non-fatal here — the model
                # can still SEE the image via image_data this turn even
                # if it couldn't also be persisted to disk; only note it
                # if the user might later expect to push it and find it
                # missing.
                elif img_save_err:
                    skipped_notes.append(f"{file_name} (could see it, but could not save a copy to sandbox: {img_save_err})")
            except Exception:
                # Malformed base64 etc — image_data above still has the
                # original string, so vision input for this turn is
                # unaffected either way; just skip the sandbox copy.
                pass

        elif _is_audio(file_type) and att.get("file_data"):
            # ADDED 2026-10-07 — web voice-note support, following the
            # same established image_data/image_mime reuse pattern
            # api/whatsapp.py's voice-note handler already uses for
            # audio (one inline-data slot, mime type says what it is —
            # see _is_audio's own docstring above for the full
            # reasoning). Only ONE audio clip per message is treated as
            # inline data — same reasoning archive_data below already
            # applies to archives: not worth building batch handling
            # for a rare case.
            #
            # Deliberately does NOT also run the image branch's
            # save-to-workspace-for-GitHub logic — that exists so an
            # uploaded IMAGE can be added to a repo via push_workspace;
            # a voice note isn't something that normally gets committed
            # to a repo the same way, and @github's push_workspace logic
            # has no special handling for audio anyway. If a real need
            # for "save my voice note into the repo" ever comes up, add
            # it here explicitly rather than assuming it from this
            # unrelated image-specific flow.
            if image_data is None:
                image_data = att["file_data"]
                image_mime = file_type
                saved_notes.append(
                    f"'{file_name}' (voice note, {file_type}) — transcribe and respond to what's said."
                )
            else:
                skipped_notes.append(
                    f"'{file_name}' (additional audio — only the first audio clip per message is processed; "
                    f"send separately if this needs its own response)"
                )

        elif _is_archive(file_name, file_type) and att.get("file_data"):
            if archive_data is None:
                # Was accepted into the request model and then never
                # read or used anywhere at all — a zip/tar attachment
                # silently went nowhere. Now actually decoded and saved
                # into the sandbox, same as everything else here.
                try:
                    raw_bytes = base64.b64decode(att["file_data"])
                    # WORKSPACE SPLIT 2026-08-28: an uploaded archive is
                    # meant to be unzipped and worked on via @terminal
                    # right after (see the instruction text below) —
                    # terminal only operates inside workspace/, not the
                    # new inputs/ default, so this MUST land in
                    # workspace/ or the unzip instruction below would
                    # point at a path terminal can't see.
                    saved_path, save_err = save_to_sandbox(user_id, file_name, raw_bytes, area="workspace")
                    if saved_path:
                        saved_notes.append(
                            f"'{file_name}' (archive, {len(raw_bytes)} bytes — "
                            f"saved to sandbox as '{saved_path}'. "
                            f"To unzip: use @terminal with query='unzip {saved_path} -d {saved_path.replace('.zip','').replace('.tar.gz','').replace('.tgz','')}'. "
                            f"The file is at relative path '{saved_path}' inside the sandbox — "
                            f"NEVER use an absolute path like /opt/render/... or /tmp/... directly)"
                        )
                        archive_data = att["file_data"]
                        archive_name = saved_path
                    else:
                        skipped_notes.append(f"{file_name} (could not be saved: {save_err})")
                except Exception as e:
                    skipped_notes.append(f"{file_name} (invalid archive data: {e})")
            else:
                skipped_notes.append(f"{file_name} (only one archive can be processed per message)")

        elif att.get("file_text"):
            # BUGFIX 2026-08-30: this file is written straight to the
            # sandbox's inputs/ dir RIGHT HERE — it never touches Mongo
            # and has no download_url. The old note below told the model
            # to "use @document_reader" with no further detail, so a few
            # tool-calls later (once the literal saved_path had scrolled
            # out of working context) the model would guess a plausible
            # /files/serve/uploads/pasted_<timestamp>.txt URL from
            # pattern memory and call fetch_upload with it — a URL that
            # never existed anywhere, since this path was never fetched,
            # only saved directly. Fixed at the source: the note now
            # names the exact action (read / read_chunked) and states
            # explicitly that fetch_upload is unnecessary here, instead
            # of leaving the model to reconstruct a URL that doesn't
            # exist for this branch.
            saved_path, save_err = save_to_sandbox(user_id, file_name, att["file_text"])
            if saved_path:
                # BUGFIX 2026-10-01 — was "already in your sandbox
                # inputs/ — call @document_reader ... file_path='{saved_path}'".
                # saved_path here is ALWAYS a bare filename (see
                # save_to_sandbox's `return candidate, None` — candidate
                # is just os.path.basename, never includes a directory
                # prefix), but mentioning "inputs/" in the same sentence
                # right next to file_path='{saved_path}' reads as an
                # instruction to construct the path as inputs/{saved_path}
                # — confirmed as the actual, reproducible production bug:
                # the model passed file_path='inputs/pasted_<ts>.txt' and
                # got "File not found", then on a LATER, unprompted retry
                # passed the bare file_path='pasted_<ts>.txt' and it
                # worked immediately. document_reader's _resolve() already
                # prepends the inputs/ directory itself (self.inputs /
                # file_path) — a caller-supplied "inputs/" prefix produces
                # a doubled inputs/inputs/... path that was never going to
                # exist. Now explicit that file_path must be the bare
                # filename with NO directory prefix, and drops "inputs/"
                # from the prose entirely so there's nothing nearby for
                # the model to reasonably (mis)read as part of the path.
                saved_notes.append(
                    f"'{saved_path}' ({len(att['file_text'])} chars, saved and ready to read — "
                    f"call @document_reader with action=read_chunked (or read, if short) and "
                    f"file_path='{saved_path}' EXACTLY AS SHOWN — no directory prefix, just "
                    f"that bare filename; document_reader finds it automatically. Do NOT use "
                    f"fetch_upload or any download_url for this file — none exists, it was "
                    f"saved directly, not fetched.)"
                )
            else:
                skipped_notes.append(f"{file_name} (could not be saved: {save_err})")

        elif att.get("cloudinary_url"):
            saved_notes.append(f"{file_name} — {att['cloudinary_url']} (URL only, not readable by tools)")

    if saved_notes:
        objective = (
            f"{objective}\n\n[Attached file(s): {'; '.join(saved_notes)}. "
            "Do not guess file contents from filenames — always read the actual content first.]"
        )
    if skipped_notes:
        objective = f"{objective}\n\n[Could not process: {'; '.join(skipped_notes)}.]"
    if len(attachments) > 1 and (image_data or extra_images):
        total_images = (1 if image_data else 0) + len(extra_images)
        if total_images > 1:
            objective = f"{objective}\n\n[{total_images} images attached — you can see all of them.]"

    # Save extra images so their URLs persist in session history.
    # Without this, only the primary image URL is stored — extras disappear on reload.
    # FIX 2026-08-11: `db` was referenced here but never passed in as a parameter
    # and not imported — caused NameError on every request with multiple attachments.
    # Fixed by fetching from bootstrap registry instead of expecting it as an argument,
    # same pattern as other skills that need DB access without it in their call signature.
    # CLOUDINARY REINSTATED 2026-09-03: uses save_upload() (Cloudinary-first,
    # inline fallback — see core/inline_storage.py) instead of save_inline()
    # directly, since a chat-attached image is a genuine user upload, the
    # same category as api/files.py's web uploads.
    extra_attachment_urls = []
    if extra_images:
        try:
            from bootstrap import get as _bootstrap_get
            _db = _bootstrap_get("db")
            from core.inline_storage import save_upload
            for img in extra_images:
                raw = base64.b64decode(img["data"])
                fname = img.get("filename") or f"image_{len(extra_attachment_urls)+2}.jpg"
                upload = await save_upload(_db, raw, fname, img["mime"], bucket="attachments")
                if upload.get("url"):
                    extra_attachment_urls.append({"url": upload["url"], "file_type": img["mime"], "filename": fname})
        except Exception:
            pass  # non-fatal — image still goes to Gemini, just won't persist

    return {
        "objective": objective,
        "image_data": image_data,
        "image_mime": image_mime,
        "extra_images": extra_images,
        "extra_attachment_urls": extra_attachment_urls,
        "archive_data": archive_data,
        "archive_name": archive_name,
    }
