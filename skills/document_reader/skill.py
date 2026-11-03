"""
document_reader/skill.py — OADSON V3

Reads documents and codebases from the user's workspace.

Key addition (v3.0): query-aware chunked reading for large documents.
Instead of reading a 228-page PDF and overflowing the context window,
read_chunked() splits the doc into pages/sections, scores each chunk
against the query using keyword overlap, and returns only the top N
most relevant chunks. For a "cybersecurity courses 300 level" query
on a 228-page PDF this means reading ~3 pages instead of 228.

read_codebase() walks a directory tree, reads code files by priority
(Python > JS > etc.), chunks by file, scores against query, returns
relevant files — handles entire codebases without context overflow.
"""

from pathlib import Path
from typing import Optional, List
import json
import csv
import io
import re
import logging
import base64

logger = logging.getLogger("oadson.skill.document_reader")


# ── Shared keyword scorer ─────────────────────────────────────────────────────

def _score_chunk(query: str, text: str) -> float:
    """
    Simple keyword overlap score — fast, no API call needed.
    Splits query into tokens, counts how many appear in text (case-insensitive).
    Returns 0.0–1.0.
    """
    if not query or not text:
        return 0.0
    q_tokens = set(re.findall(r'\w+', query.lower()))
    t_lower = text.lower()
    hits = sum(1 for tok in q_tokens if tok in t_lower and len(tok) > 2)
    return hits / max(len(q_tokens), 1)


class DocumentReaderSkill:
    def __init__(self, user_id: str):
        from core.oadson_home import get_user_home, get_inputs_dir, is_safe_path, is_safe_inputs_path
        if not user_id:
            raise ValueError("DocumentReaderSkill requires a user_id")
        self.user_id = user_id
        self.home = get_user_home(user_id)
        self.inputs = get_inputs_dir(user_id)
        self._is_safe_path = lambda p: is_safe_path(user_id, p)
        self._is_safe_inputs_path = lambda p: is_safe_inputs_path(user_id, p)
        self.supported_types = {
            ".pdf", ".docx", ".txt", ".py", ".js", ".ts", ".json", ".md",
            ".pptx", ".xlsx", ".xls", ".csv", ".html", ".xml", ".yaml", ".yml",
            ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb", ".php", ".sh",
            # FIX 2026-08-28: .tsx/.jsx/.vue/.svelte are plain text source
            # files exactly like .ts/.js — there was never a real reason
            # to reject them, and it broke every React/Vue/Svelte repo the
            # model tried to read (seen in production: "Unsupported file
            # type: .tsx" on a genuinely ordinary frontend file mid-fix).
            ".tsx", ".jsx", ".vue", ".svelte", ".mjs", ".cjs", ".scss", ".sass", ".less",
        }

    def _resolve(self, file_path: str):
        """
        WORKSPACE SPLIT 2026-08-28: files a user sends OADSON now land
        in inputs/ (see core/oadson_home.py), while repos/scratch work
        stays in workspace/ (self.home, unchanged). Callers here —
        read/read_chunked/summarize/etc — pass a bare filename or
        relative path with no idea which bucket it's actually in, so
        try inputs/ first (the common case for anything fetch_upload
        just pulled in), then fall back to workspace/ (repos, generated
        files) — whichever one actually has the file wins, with no
        change needed to any call site's arguments.

        SELF-HEAL 2026-08-30: an exact match is tried first, unchanged.
        If that misses, fall back to _fuzzy_find() before giving up —
        closes the actual production failure mode this was added for:
        a model reconstructing a filename from memory a few tool-calls
        after the attach (e.g. dropping the random 8-char save_inline
        prefix, or drifting the millisecond timestamp on a
        pasted_<ts>.txt name) got a hard "not found" even though the
        real file was sitting right there the whole time under a
        near-identical name. Every action (read, read_chunked,
        summarize, answer_question, extract_code, metadata) routes
        through this one method, so fixing it here fixes all of them
        at once rather than needing a fallback bolted onto each.

        WRITE-VISIBILITY RETRY 2026-09-02: confirmed in production —
        a file saved via save_to_sandbox moments earlier in the SAME
        turn (the model pastes text, is immediately told the exact
        saved_path, and calls document_reader on it right away) could
        still report "File not found" on the very first read attempt,
        then succeed on an immediate retry with the identical path —
        not a wrong path, not a wrong filename, the file genuinely
        wasn't visible to Path.exists() yet at the first check. A tiny
        blocking retry (max ~150ms total, only ever triggered on an
        exact-match miss, never on the fuzzy-match path) closes this
        without meaningfully slowing the common case where the file is
        already visible and resolves on the first check with zero
        added delay.
        """
        import time as _time

        def _exists_with_retry(candidate):
            if candidate.exists():
                return True
            for _ in range(3):
                _time.sleep(0.05)
                if candidate.exists():
                    return True
            return False

        if self._is_safe_inputs_path(file_path):
            candidate = self.inputs / file_path
            if _exists_with_retry(candidate):
                return str(candidate)
        if self._is_safe_path(file_path):
            candidate = self.home / file_path
            if _exists_with_retry(candidate):
                return str(candidate)

        healed = self._fuzzy_find(file_path)
        if healed:
            return healed

        # Nothing matched, exact or fuzzy — return the naive inputs/
        # path so the caller's existing "File not found: {file_path}"
        # error still fires with the right-looking path in it (same
        # behavior as before this method existed, for a genuinely
        # missing file).
        if self._is_safe_inputs_path(file_path):
            return str(self.inputs / file_path)
        if not self._is_safe_path(file_path):
            return None
        return str(self.home / file_path)

    def _fuzzy_find(self, file_path: str):
        """
        Look for the closest real match to `file_path` in inputs/ (then
        workspace/) when the exact name doesn't exist. Deliberately
        narrow — this is a recovery for near-miss filenames, not a
        general search:

          1. Strip a leading `<8-hex>_` save_inline-style prefix from
             the requested name, if present, and try matching either
             direction (requested-has-prefix-actual-doesn't, or vice
             versa) against every real filename with its own prefix
             stripped the same way.
          2. If the requested name looks like `pasted_<digits>.<ext>`
             (the paste-save convention — see attachment_processor.py),
             match any real file with the same prefix+extension whose
             digit run is within 5 minutes' worth of milliseconds of
             the requested one — covers a slightly mis-remembered
             timestamp without being so loose it could grab an
             unrelated paste from hours earlier.
          3. Otherwise, an exact case-insensitive match on the
             stripped base name.

        Only ever returns a path when exactly one candidate matches —
        if stripping/matching produces more than one plausible file,
        that's treated as genuinely ambiguous and left to fail with the
        normal "not found" error rather than silently guessing which
        one the model meant.
        """
        import re as _re

        requested = Path(file_path).name
        req_stem, req_ext = Path(requested).stem, Path(requested).suffix.lower()
        prefix_re = _re.compile(r"^[0-9a-f]{8}_(.+)$")
        req_bare = prefix_re.match(requested)
        req_bare_name = req_bare.group(1) if req_bare else requested

        candidates = []
        for directory in (self.inputs, self.home):
            try:
                if not directory.exists():
                    continue
                for entry in directory.iterdir():
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if name.lower() == requested.lower():
                        continue  # would have matched exactly above; not a fuzzy case
                    m = prefix_re.match(name)
                    bare_name = m.group(1) if m else name

                    if bare_name.lower() == req_bare_name.lower():
                        candidates.append(entry)
                        continue

                    ts_req = _re.match(r"^(pasted_)(\d+)(\.\w+)$", req_bare_name, _re.IGNORECASE)
                    ts_have = _re.match(r"^(pasted_)(\d+)(\.\w+)$", bare_name, _re.IGNORECASE)
                    if ts_req and ts_have and ts_req.group(3).lower() == ts_have.group(3).lower():
                        try:
                            if abs(int(ts_req.group(2)) - int(ts_have.group(2))) <= 5 * 60 * 1000:
                                candidates.append(entry)
                        except ValueError:
                            pass
            except Exception:
                continue

        # De-dupe (same resolved path could theoretically show up once
        # per directory loop iteration in edge cases) and require
        # exactly one unambiguous winner.
        unique = list({str(c): c for c in candidates}.values())
        if len(unique) == 1:
            logger.info(
                f"document_reader: fuzzy-matched requested '{file_path}' -> "
                f"'{unique[0]}' (self-heal, no exact file existed)"
            )
            return str(unique[0])
        return None

    # ── PDF ──────────────────────────────────────────────────────────────────
    #
    # MIGRATED 2026-09-03: PyPDF2 → pypdf, plus real encrypted-PDF handling.
    #
    # Root cause of the "PyCryptodome is required for AES algorithm" failure
    # on a genuine (non-scanned) contract PDF: this file called
    # PyPDF2.PdfReader(f) and went straight to page.extract_text() with NO
    # `reader.is_encrypted` check and NO `reader.decrypt(...)` call anywhere.
    # Many e-signature/export tools (DocuSign, Adobe "Protect PDF", Word's
    # own "Encrypt with password" export, etc.) apply PDF encryption with an
    # EMPTY user password — the file opens with no prompt in a normal PDF
    # viewer, but the content streams are still technically encrypted (often
    # AES-128/256) and require an explicit decrypt call before extraction
    # will work, even with an empty password. PyPDF2 is also no longer
    # maintained (pypdf is its successor, same original author, same API for
    # everything used here) and has known-flakier AES-256 support — even
    # with pycryptodome genuinely installed it can raise exactly this error
    # on some AES-256 documents that pypdf handles cleanly. requirements.txt
    # already listed BOTH PyPDF2 and pypdf (pypdf was added for some other
    # caller but this file kept using the older one) — this now actually
    # uses pypdf everywhere PDFs are read, and PyPDF2 can be dropped from
    # requirements.txt once this is confirmed working in production.
    #
    # _open_pdf_reader() below is the one place all three PDF entry points
    # go through, so the decrypt logic exists exactly once:
    #   1. Open with pypdf.PdfReader.
    #   2. If reader.is_encrypted, try reader.decrypt("") (empty password —
    #      covers the permissions-only-encryption case above, which is the
    #      overwhelming majority of "encrypted but not actually
    #      password-protected" PDFs).
    #   3. Distinguish the three real outcomes instead of collapsing them
    #      into one generic exception:
    #        - decrypt succeeded (or wasn't needed) → proceed normally
    #        - decrypt failed / returned no access → genuinely needs a real
    #          password this system doesn't have; say so plainly instead of
    #          a cryptic library error
    #        - Crypto/pycryptodome itself fails to import → distinct,
    #          actionable message (this is the one case that really is a
    #          missing server dependency, not a document problem)

    def _open_pdf_reader(self, file_path: str):
        """
        Returns (reader, None) on success, or (None, error_dict) on failure.
        error_dict is a ready-to-return {"status": "error", "reason": ...}.
        """
        try:
            import pypdf
        except ImportError:
            return None, {
                "status": "error",
                "reason": (
                    "pypdf is not installed on the server — add 'pypdf>=4.0.0' "
                    "to requirements.txt (it's already listed; this means the "
                    "installed environment is out of sync with requirements.txt "
                    "and needs a redeploy/rebuild)."
                ),
            }

        try:
            reader = pypdf.PdfReader(file_path)
        except Exception as e:
            return None, {"status": "error", "reason": f"Error opening PDF: {e}"}

        if getattr(reader, "is_encrypted", False):
            try:
                # Empty-password decrypt — see module comment above for why
                # this covers the common "encrypted but not really
                # password-protected" case. pypdf's decrypt() returns a
                # PasswordType enum; 0/NOT_DECRYPTED means it genuinely
                # failed (a real password is required).
                decrypt_result = reader.decrypt("")
                if not decrypt_result:
                    return None, {
                        "status": "error",
                        "reason": (
                            "This PDF is password-protected with a real password "
                            "(not just export-time permissions locking) — an empty "
                            "password did not unlock it. There is no way to read "
                            "its content without that password. Ask the user for "
                            "the PDF password, or ask them to re-export/print-to-PDF "
                            "a copy without a password."
                        ),
                    }
            except ImportError as e:
                # This is the genuine "server is missing a crypto dependency"
                # case — pypdf tried to import Crypto/pycryptodome internally
                # (needed for AES) and it isn't importable in this
                # environment, despite pycryptodome>=3.20.0 being listed in
                # requirements.txt. Surfaced distinctly from every other
                # error so this doesn't get misdiagnosed as "the document is
                # bad" — it's a deployment/build issue and needs a redeploy
                # with a clean dependency install, not a different PDF.
                return None, {
                    "status": "error",
                    "reason": (
                        f"This PDF uses AES encryption and the server's pypdf "
                        f"could not import the crypto backend it needs "
                        f"(pycryptodome) even though it's listed in "
                        f"requirements.txt: {e}. This is a server dependency "
                        f"problem, not a problem with the document — tell the "
                        f"user their PDF is fine, and this needs a clean "
                        f"redeploy/rebuild on the backend to actually install "
                        f"pycryptodome, not a different file from them."
                    ),
                }
            except Exception as e:
                return None, {
                    "status": "error",
                    "reason": f"Could not decrypt PDF: {e}",
                }

        return reader, None

    # ── Scanned/image PDF fallback: rasterize pages for vision ────────────────
    #
    # ADDED 2026-09-04. When a PDF is genuinely scanned/image-based (no
    # extractable text streams at all, confirmed only AFTER the encrypted-PDF
    # path above has already run — this is the case where extraction
    # succeeds but returns nothing, not the case where it fails), there is no
    # text to read. Two ways to actually solve that were considered:
    #   1. Real OCR (pytesseract + a Tesseract system binary) — rejected for
    #      now: Tesseract is a system-level binary, not a pip package, and
    #      awkward to guarantee on Render's free tier build environment.
    #   2. Render each page to an image and let the model itself read it via
    #      Gemini's vision input — CHOSEN. No system binary: PyMuPDF (the
    #      `fitz` package) is a pure pip wheel. It also authenticates
    #      empty-password-encrypted PDFs through MuPDF's own crypto, entirely
    #      independent of pypdf/pycryptodome — so this path even works if a
    #      scanned PDF is ALSO encrypted.
    #
    # This only rasterizes; it does not itself do vision OCR ("_do_scrape"-
    # style page images the model reads directly) — see tool_loop.py's
    # __vision_images__ handling for how these page images actually reach
    # Gemini as inline_data parts alongside this skill's functionResponse in
    # the SAME turn, so the model can read them in its very next reply
    # without a second round trip.
    MAX_VISION_PAGES = 5   # hard ceiling — each page becomes a real image the
                           # model has to process; this server also runs on a
                           # tight memory budget while rendering them
    VISION_DPI = 110       # ~850x1100px per US-letter page at this DPI — legible
                           # for the model, without ballooning base64 payload size

    def _rasterize_pdf_for_vision(self, file_path: str, max_pages: int = MAX_VISION_PAGES, start_page: int = 0) -> dict:
        """
        Returns {"status": "success", "images": [{"mime": "image/png", "data": <b64>}, ...],
        "pages_rendered": N, "page_range": "start-end (1-indexed)", "total_pages": M,
        "has_more": bool, "next_page_offset": int | None}
        or {"status": "error", "reason": ...}.

        ADDED 2026-09-04 (pagination): start_page (0-indexed) picks where
        rendering begins, so a scanned document longer than max_pages can be
        walked page-range by page-range across repeat calls instead of only
        ever showing its first MAX_VISION_PAGES pages — see run()'s
        'vision_page_offset' arg and read()/read_chunked()'s "has_more" /
        "next_page_offset" pass-through in their result for how the model
        is told to keep going.
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return {
                "status": "error",
                "reason": (
                    "This PDF appears to be scanned/image-based (no extractable text), and "
                    "PyMuPDF (the 'pymupdf' package) — needed to render its pages as images "
                    "for the model to read visually — is not installed on the server. It's "
                    "listed in requirements.txt; this needs a redeploy/rebuild to actually "
                    "install it."
                ),
            }

        try:
            from core.oadson_home import has_memory_headroom
            ok, headroom_mb = has_memory_headroom(min_mb=80)
            if not ok:
                return {
                    "status": "error",
                    "reason": (
                        f"Not enough free memory to render PDF pages as images right now "
                        f"({headroom_mb}MB available, need 80MB). Try again in a moment."
                    ),
                }
        except Exception:
            pass  # best-effort guard — never block on it failing to import/run

        try:
            doc = fitz.open(file_path)
            # MuPDF's own crypto — entirely separate from pypdf/pycryptodome
            # above, so this also covers a scanned PDF that's ALSO encrypted
            # with the common empty-user-password case.
            if doc.needs_pass:
                if not doc.authenticate(""):
                    doc.close()
                    return {
                        "status": "error",
                        "reason": "This PDF is password-protected with a real password — cannot render its pages either.",
                    }

            total_pages = doc.page_count
            start_page = max(0, min(start_page, max(total_pages - 1, 0)))
            end_page = min(total_pages, start_page + max_pages)
            zoom = self.VISION_DPI / 72.0  # PDF's native unit is 72 DPI
            matrix = fitz.Matrix(zoom, zoom)

            images = []
            for i in range(start_page, end_page):
                pix = doc[i].get_pixmap(matrix=matrix)
                images.append({"mime": "image/png", "data": base64.b64encode(pix.tobytes("png")).decode("ascii")})
            doc.close()

            has_more = end_page < total_pages
            return {
                "status": "success",
                "images": images,
                "pages_rendered": len(images),
                "page_range": f"{start_page + 1}-{end_page}",
                "total_pages": total_pages,
                "has_more": has_more,
                "next_page_offset": end_page if has_more else None,
            }
        except Exception as e:
            return {"status": "error", "reason": f"Could not render PDF pages as images: {e}"}

    def read_pdf(self, file_path: str) -> str:
        """Read entire PDF. Use read_pdf_chunked for large files."""
        reader, err = self._open_pdf_reader(file_path)
        if err:
            return f"Error reading PDF: {err['reason']}"
        try:
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            if not text.strip():
                return "[No extractable text — PDF may be scanned/image-based.]"
            return text
        except Exception as e:
            return f"Error reading PDF: {e}"

    def read_pdf_pages(self, file_path: str) -> list:
        """
        Read PDF page by page, return list of (page_num, text) tuples.
        Used by read_chunked for large PDF processing.
        """
        reader, err = self._open_pdf_reader(file_path)
        if err:
            return []
        pages = []
        try:
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append((i + 1, text))
        except Exception:
            pass
        return pages

    def read_pdf_pages_with_report(self, file_path: str) -> tuple[list, int, list]:
        """
        ADDED 2026-08-15. Same extraction as read_pdf_pages, but ALSO
        reports the real total page count and which specific pages
        failed to extract text.

        BUG FOUND: read_pdf_pages silently drops any page where
        extract_text() returns empty/whitespace-only text (a real,
        common failure mode for multi-column layouts, embedded fonts,
        image-heavy pages, or certain table formatting) — and callers
        derived "total_pages" from the LAST SURVIVING page number, not
        the PDF's actual page count. If extraction failed on, say,
        pages 31-43 of a 43-page PDF, the document would silently
        report as having fewer total pages than it really does, with
        zero indication anything was skipped — the model (and the user)
        would have no way to know some content was never actually read,
        only that it "wasn't found". This is the likely cause of a real
        user report: a document confirmed to contain specific module
        content that document_reader said was missing.
        Returns (pages, real_total_page_count, failed_page_numbers).

        MIGRATED 2026-09-03: now goes through _open_pdf_reader() like the
        other two PDF methods, so an encrypted PDF here fails with the
        same clear, distinguished error instead of silently returning
        ([], 0, []) — which previously looked identical to "this PDF has
        zero pages" with no indication encryption was ever the issue.
        """
        reader, err = self._open_pdf_reader(file_path)
        if err:
            return [], 0, []
        pages = []
        failed_pages = []
        real_total = 0
        try:
            real_total = len(reader.pages)
            for i, page in enumerate(reader.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if text.strip():
                    pages.append((i + 1, text))
                else:
                    failed_pages.append(i + 1)
        except Exception:
            pass
        return pages, real_total, failed_pages

    # ── DOCX ─────────────────────────────────────────────────────────────────

    def read_docx(self, file_path: str) -> str:
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            from docx.oxml.table import CT_Tbl
            from docx.oxml.text.paragraph import CT_P
        except ImportError:
            return "ERROR: python-docx not installed."
        try:
            doc = Document(file_path)
            parts = []
            for child in doc.element.body.iterchildren():
                if isinstance(child, CT_P):
                    para = Paragraph(child, doc)
                    if para.text.strip():
                        parts.append(para.text)
                elif isinstance(child, CT_Tbl):
                    table = Table(child, doc)
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        if any(cells):
                            parts.append(" | ".join(cells))
            text = "\n".join(parts)
            return text if text.strip() else "[Document appears empty.]"
        except Exception as e:
            return f"Error reading DOCX: {e}"

    # ── PPTX ─────────────────────────────────────────────────────────────────

    def read_pptx(self, file_path: str) -> str:
        try:
            from pptx import Presentation
        except ImportError:
            return "ERROR: python-pptx not installed."
        try:
            prs = Presentation(file_path)
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                slide_text = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            line = "".join(run.text for run in para.runs)
                            if line.strip():
                                slide_text.append(line)
                    if shape.has_table:
                        for row in shape.table.rows:
                            cells = [cell.text.strip() for cell in row.cells]
                            if any(cells):
                                slide_text.append(" | ".join(cells))
                parts.append(f"--- Slide {i} ---\n" + "\n".join(slide_text))
            return "\n\n".join(parts) if parts else "[No slides found.]"
        except Exception as e:
            return f"Error reading PPTX: {e}"

    # ── XLSX ─────────────────────────────────────────────────────────────────

    def read_xlsx(self, file_path: str) -> str:
        try:
            import openpyxl
        except ImportError:
            return "ERROR: openpyxl not installed."
        try:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            parts = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                parts.append(f"--- Sheet: {sheet_name} ---")
                row_count = 0
                for row in ws.iter_rows(values_only=True):
                    if row_count >= 200:
                        parts.append("[... truncated after 200 rows]")
                        break
                    if any(cell is not None for cell in row):
                        parts.append(" | ".join("" if c is None else str(c) for c in row))
                        row_count += 1
            return "\n".join(parts) if parts else "[Workbook has no data.]"
        except Exception as e:
            return f"Error reading XLSX: {e}"

    # ── Text-based formats ────────────────────────────────────────────────────

    def read_csv(self, file_path: str) -> str:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                reader = csv.reader(f)
                rows = []
                for i, row in enumerate(reader):
                    if i >= 500:
                        rows.append("[... truncated after 500 rows]")
                        break
                    rows.append(" | ".join(row))
            return "\n".join(rows) if rows else "[Empty CSV.]"
        except Exception as e:
            return f"Error reading CSV: {e}"

    def read_text(self, file_path: str) -> str:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception as e:
            return f"Error reading file: {e}"

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def _read_by_type(self, file_path: str, file_type: str) -> str:
        readers = {
            ".pdf": self.read_pdf,
            ".docx": self.read_docx,
            ".pptx": self.read_pptx,
            ".xlsx": self.read_xlsx,
            ".xls": self.read_xlsx,
            ".csv": self.read_csv,
        }
        if file_type in readers:
            return readers[file_type](file_path)
        return self.read_text(file_path)

    # ── read() — existing action, unchanged ───────────────────────────────────

    def read(self, file_path: str, vision_page_offset: int = 0) -> dict:
        resolved = self._resolve(file_path)
        if not resolved:
            return {"status": "error", "reason": "Path is outside sandboxed home directory"}
        path = Path(resolved)
        if not path.exists():
            return {"status": "error", "reason": f"File not found: {file_path}"}
        file_type = path.suffix.lower()
        if file_type not in self.supported_types:
            return {"status": "error", "reason": f"Unsupported file type: {file_type}"}
        content = self._read_by_type(resolved, file_type)
        # FIX 2026-09-03: read_pdf() (and the other typed readers) return
        # error text as a plain string on failure rather than raising —
        # this was always wrapped into status="success" regardless, which
        # meant skill_executor's PASSTHROUGH_STATUSES logic (see
        # core/skill_executor.py) had no way to tell the model this call
        # actually failed; the model only ever saw a "successful" read
        # whose content happened to be an error sentence. Most visible on
        # encrypted PDFs (the exact bug this migration fixes) but applied
        # to every reader's error string, not just PDF's.
        if isinstance(content, str) and content.startswith(("Error reading", "ERROR:")):
            return {"status": "error", "reason": content, "file_type": file_type, "file_name": path.name}
        # ADDED 2026-09-04: read_pdf() returns this exact sentinel for a
        # genuinely scanned/image-based PDF (decrypt succeeded, but there
        # are no text streams to extract at all) — fall back to rendering
        # pages as images for the model to read visually instead of just
        # reporting failure. See _rasterize_pdf_for_vision()'s docstring
        # and tool_loop.py's __vision_images__ handling for the other half
        # of this (how the images actually reach Gemini).
        if file_type == ".pdf" and content == "[No extractable text — PDF may be scanned/image-based.]":
            vision = self._rasterize_pdf_for_vision(resolved, start_page=vision_page_offset)
            if vision["status"] == "error":
                return {"status": "error", "reason": vision["reason"], "file_type": file_type, "file_name": path.name}
            more_note = (
                f" {vision['pages_rendered']} more page(s) remain — call again with "
                f"vision_page_offset={vision['next_page_offset']} to see them."
                if vision["has_more"] else " This was the last page range."
            )
            return {
                "status": "success",
                "content": (
                    f"[This PDF has no extractable text — it's scanned/image-based. "
                    f"Rendered pages {vision['page_range']} of {vision['total_pages']} total, "
                    f"attached to this result. Read them visually and answer from what you see in them."
                    f"{more_note}]"
                ),
                "file_type": file_type,
                "file_name": path.name,
                "file_size": path.stat().st_size,
                "total_pages": vision["total_pages"],
                "page_range_rendered": vision["page_range"],
                "has_more": vision["has_more"],
                "next_page_offset": vision["next_page_offset"],
                "__vision_images__": vision["images"],
            }
        return {
            "status": "success",
            "content": content,
            "file_type": file_type,
            "file_name": path.name,
            "file_size": path.stat().st_size,
        }

    # ── read_chunked() — NEW: query-aware large document reading ──────────────

    def read_chunked(self, file_path: str, query: str,
                     max_chunks: int = 5, chunk_size: int = 15, vision_page_offset: int = 0) -> dict:
        """
        Read a large document by splitting into chunks, scoring each chunk
        against the query, and returning only the top max_chunks most relevant.

        For PDFs: chunks are groups of chunk_size pages.
        For text/code: chunks are groups of chunk_size lines.
        For DOCX/PPTX: split into sections by headings or slides.

        This handles 228-page PDFs, large DOCX reports, and entire code files
        without hitting context window limits.

        BUG FIXED 2026-08-16 (real, recurring production evidence): a model
        asked "how many modules does this PDF have" answered "only Module 1
        and 2, no Module 3" — on a document that genuinely has 10 modules,
        confirmed earlier the same session. The document-grounding checker
        (core/document_grounding.py) correctly caught it as a fabrication,
        working exactly as designed — but the checker catching the SYMPTOM
        doesn't fix the CAUSE. The cause: read_chunked's relevance-ranked
        chunking is inherently QUERY-DEPENDENT — "how many modules" and
        "explain module 3" can score different chunks as most relevant, so
        two different turns can legitimately see two different partial
        VIEWS of the same document. A query whose top-ranked chunks happen
        to miss the page where "MODULE THREE" is headed will make the model
        truthfully report what it saw, which is wrong about the document as
        a whole. This is now fixed at the SOURCE rather than only caught
        after the fact: every read_chunked call independently scans the
        document's real heading structure (core/document_grounding.py's
        extract_document_structure — the same regex-based, non-model,
        can't-hallucinate-a-heading-that-isn't-there scanner already used
        by the grounding checker) and includes it in the result, ALWAYS,
        regardless of which chunks the relevance ranking happened to pick.
        The model now has the document's true table of contents on every
        single read_chunked call, not only when the query's relevance
        ranking happens to surface it.
        """
        resolved = self._resolve(file_path)
        if not resolved:
            return {"status": "error", "reason": "Path outside sandbox"}
        path = Path(resolved)
        if not path.exists():
            return {"status": "error", "reason": f"File not found: {file_path}"}

        file_type = path.suffix.lower()
        file_size_mb = path.stat().st_size / (1024 * 1024)

        # ── Query-independent structure scan (see docstring above) ──
        # Computed ONCE, up front, before any relevance-ranked chunking —
        # so it's identical no matter what query was asked. Best-effort:
        # a scan failure never blocks the actual read.
        document_structure = None
        try:
            from core.document_grounding import extract_document_structure
            # Only PDFs currently go through the page-based extraction
            # this needs; text-based structure scanning for DOCX/code
            # would need a different heading-detection pass and isn't
            # built yet — explicitly None (not silently guessed) for
            # those types rather than pretending coverage that isn't
            # there.
            if file_type == ".pdf":
                full_text_pages, _, _ = self.read_pdf_pages_with_report(resolved)
                full_text = "\n".join(t for _, t in full_text_pages)
                headings = extract_document_structure(full_text)
                modules = [h for h in headings if h["level"] == "module"]
                if modules:
                    document_structure = [
                        f"Module {h['number']}: {h['title'][:100]}" for h in modules
                    ]
        except Exception as e:
            logger.warning(f"document_reader structure scan failed (non-fatal): {e}")

        # ── PDF: page-by-page chunking ──
        if file_type == ".pdf":
            # FIX 2026-09-03: check for a real open/decrypt failure FIRST,
            # via the same _open_pdf_reader() every other PDF path uses —
            # read_pdf_pages_with_report() returns an indistinguishable
            # ([], 0, []) for "encrypted, couldn't decrypt" and "zero pages
            # extracted", so without this check an encrypted PDF here fell
            # through to the generic "may be image-based/scanned" message,
            # which is actively misleading (it isn't scanned, it's locked).
            _, open_err = self._open_pdf_reader(resolved)
            if open_err:
                return open_err

            pages, real_total_pages, failed_pages = self.read_pdf_pages_with_report(resolved)
            if not pages:
                # ADDED 2026-09-04: distinguish "genuinely scanned/image-based"
                # from other empty-extraction causes and fall back to the same
                # vision rendering read() uses, rather than a flat error —
                # see _rasterize_pdf_for_vision()'s docstring.
                vision = self._rasterize_pdf_for_vision(resolved, start_page=vision_page_offset)
                if vision["status"] == "success":
                    more_note = (
                        f" {vision['pages_rendered']} more page(s) remain — call again with "
                        f"vision_page_offset={vision['next_page_offset']} to see them."
                        if vision["has_more"] else " This was the last page range."
                    )
                    return {
                        "status": "success",
                        "content": (
                            f"[This PDF has no extractable text — it's scanned/image-based. "
                            f"Rendered pages {vision['page_range']} of {vision['total_pages']} total, "
                            f"attached to this result. Read them visually and answer the query "
                            f"'{query}' from what you see in them.{more_note}]"
                        ),
                        "total_pages": vision["total_pages"],
                        "page_range_rendered": vision["page_range"],
                        "has_more": vision["has_more"],
                        "next_page_offset": vision["next_page_offset"],
                        "__vision_images__": vision["images"],
                    }
                return {"status": "error", "reason": "Could not extract text from PDF — may be image-based/scanned."}

            total_pages = real_total_pages or (pages[-1][0] if pages else 0)

            # BUG FIXED 2026-08-15: a real user report confirmed this
            # exact failure mode — a PDF with Modules 1-10 fully present
            # was read as if it only contained Modules 1-2, because
            # PyPDF2 extraction silently lost most of the document's
            # pages and the previous fix only WARNED about it in the
            # note field, which the model still confidently ignored
            # ("the file does not appear to contain..."). When more than
            # 15% of the real page count failed to extract, this is now
            # a hard error instead of a soft warning — the model cannot
            # proceed to confidently answer questions about document
            # coverage on data this incomplete, it must say so plainly.
            extraction_loss_ratio = len(failed_pages) / real_total_pages if real_total_pages else 0
            if extraction_loss_ratio > 0.15:
                return {
                    "status": "error",
                    "reason": (
                        f"PDF text extraction failed for {len(failed_pages)} of {real_total_pages} pages "
                        f"({extraction_loss_ratio:.0%}) \u2014 too incomplete to reliably answer questions about "
                        f"this document's full contents. Only {len(pages)} pages extracted successfully "
                        f"(pages: {[p for p, _ in pages][:10]}{'...' if len(pages) > 10 else ''}). "
                        f"Do NOT tell the user specific content is 'missing' or 'not present' in the document "
                        f"based on this read \u2014 tell them extraction failed on most of it and ask them to "
                        f"re-export/re-scan the PDF as text-based (not scanned images), or try read_chunked "
                        f"again with a much larger chunk_size, or use OCR if this is a scanned document."
                    ),
                }

            # Group pages into chunks of chunk_size
            chunks = []
            for i in range(0, len(pages), chunk_size):
                group = pages[i:i + chunk_size]
                chunk_text = "\n".join(text for _, text in group)
                page_range = f"pages {group[0][0]}–{group[-1][0]}"
                score = _score_chunk(query, chunk_text)
                chunks.append((score, page_range, chunk_text))

            # Sort by relevance, take top N
            chunks.sort(key=lambda x: x[0], reverse=True)
            top = chunks[:max_chunks]

            # Sort top chunks back into document order for readability
            top.sort(key=lambda x: int(x[1].split()[1].split('–')[0]))

            sections = []
            for score, page_range, text in top:
                sections.append(f"[{page_range} | relevance: {score:.2f}]\n{text}")

            # BUG FIXED 2026-08-15: confirmed via a real failing case —
            # a 300+ question, 10-module PDF where Modules 6-10 ARE
            # present, but a query naming all five modules only pulled
            # back the top-scoring chunks and the model concluded those
            # modules were "not in the file". "Not in the top N most
            # relevant chunks" and "not in the document" are very
            # different claims; this warning stops the model from
            # conflating them when the query spans more topics than fit
            # in max_chunks.
            coverage_warning = ""
            if len(top) < len(chunks):
                coverage_warning = (
                    f"NOTE: only {len(top)} of {len(chunks)} total chunks were returned (ranked by relevance "
                    f"to your query) \u2014 the other {len(chunks) - len(top)} chunk(s) covering other pages "
                    f"were NOT included in this result and were NOT read by you. If your query asks about "
                    f"multiple distinct topics/sections, some of them may be in the unreturned chunks. Do not "
                    f"conclude a topic is absent from the document without either raising max_chunks to cover "
                    f"all {len(chunks)} chunks, or issuing a separate read_chunked call per topic. "
                )

            return {
                "status": "success",
                "file_name": path.name,
                "file_type": file_type,
                "total_pages": total_pages,
                "total_chunks_scanned": len(chunks),
                "chunks_returned": len(top),
                "query": query,
                # BUG FIXED 2026-08-15: this note said "out of N chunks
                # of {chunk_size} pages each" with no reminder of what
                # that means in total pages. On a 43-page PDF with
                # chunk_size=15, that's only 3 total chunks — so
                # chunks_returned=3 actually means the ENTIRE document
                # was scanned, not a 3-page slice. The model itself
                # misread this as "only read 3 pages" in a reply to the
                # user, when it had in fact read all 43 pages across
                # those 3 chunks. Spelling out the page math explicitly
                # here removes that ambiguity at the source.
                "note": (
                    f"Showing {len(top)} of {len(chunks)} total chunk(s) "
                    f"({chunk_size} pages per chunk) \u2014 this covers pages "
                    f"1\u2013{total_pages} of {total_pages} total. "
                    + ("This is the ENTIRE document; nothing was left unscanned. "
                       if len(top) == len(chunks) else coverage_warning)
                    + (f"WARNING: {len(failed_pages)} page(s) had NO extractable text and were SKIPPED "
                       f"(pages: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}) \u2014 this is a "
                       f"real gap, not empty content; those pages may be scanned/image-based or use a layout "
                       f"PyPDF2 can't parse. Do NOT conclude content is missing from the source document based "
                       f"on this reading alone if pages were skipped \u2014 say so explicitly to the user instead "
                       f"of stating the content 'does not appear' in the document."
                       if failed_pages else "")
                ),
                # ADDED 2026-08-16 — see this method's docstring for the
                # full real-world bug this fixes. document_structure is
                # scanned ONCE, independently of the query, from the
                # ENTIRE document's real headings — it is NOT limited to
                # whichever chunks the relevance ranking above happened to
                # select. If the model is asked "how many modules does
                # this have" and the top-ranked chunks miss the page where
                # a module header sits, this field still has the true
                # answer. ALWAYS trust this field over an impression
                # formed only from the chunk content below when answering
                # any question about the document's overall structure,
                # table of contents, or module/section count.
                "document_structure": document_structure if document_structure else (
                    "Not detected — this document may not use 'MODULE N' style headings, "
                    "or structure scanning is only implemented for PDFs currently."
                ),
                "content": "\n\n---\n\n".join(sections),
            }

        # ── DOCX: paragraph-level chunking ──
        elif file_type == ".docx":
            full_text = self.read_docx(resolved)
            if full_text.startswith("ERROR"):
                return {"status": "error", "reason": full_text}
            lines = full_text.split("\n")
            chunks = []
            for i in range(0, len(lines), chunk_size * 5):  # more lines per chunk for DOCX
                group = lines[i:i + chunk_size * 5]
                chunk_text = "\n".join(group)
                score = _score_chunk(query, chunk_text)
                chunks.append((score, f"section {i//( chunk_size*5)+1}", chunk_text))
            chunks.sort(key=lambda x: x[0], reverse=True)
            top = chunks[:max_chunks]
            sections = [f"[{label} | relevance: {score:.2f}]\n{text}" for score, label, text in top]
            return {
                "status": "success",
                "file_name": path.name,
                "file_type": file_type,
                "chunks_returned": len(top),
                "query": query,
                "content": "\n\n---\n\n".join(sections),
                # document_structure scanning (see read_chunked's docstring)
                # is currently PDF-only — explicitly saying so here rather
                # than omitting the field, so the model doesn't assume
                # coverage that doesn't exist for this file type.
                "document_structure": "Not available — structure scanning is currently implemented for PDF files only.",
            }

        # ── Text/code: line-based chunking ──
        else:
            try:
                with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
            except Exception as e:
                return {"status": "error", "reason": str(e)}

            chunks = []
            for i in range(0, len(lines), chunk_size):
                group = lines[i:i + chunk_size]
                chunk_text = "".join(group)
                line_range = f"lines {i+1}–{i+len(group)}"
                score = _score_chunk(query, chunk_text)
                chunks.append((score, line_range, chunk_text))

            chunks.sort(key=lambda x: x[0], reverse=True)
            top = chunks[:max_chunks]
            top.sort(key=lambda x: int(x[1].split()[1].split('–')[0]))
            sections = [f"[{label} | relevance: {score:.2f}]\n{text}" for score, label, text in top]
            return {
                "status": "success",
                "file_name": path.name,
                "file_type": file_type,
                "total_lines": len(lines),
                "chunks_returned": len(top),
                "query": query,
                "content": "\n\n---\n\n".join(sections),
            }

    # ── read_codebase() — NEW: directory-level code reading ───────────────────

    def read_codebase(self, dir_path: str, query: str,
                      max_chunks: int = 8,
                      file_extensions: list = None) -> dict:
        """
        Walk a directory tree, read code files, score against query,
        return the most relevant file sections.

        Priority order: .py > .js/.ts > .go/.rs > .json/.yaml > others.
        Skips: node_modules, __pycache__, .git, dist, build, venv.
        """
        resolved_dir = self._resolve(dir_path)
        if not resolved_dir:
            return {"status": "error", "reason": "Path outside sandbox"}
        dpath = Path(resolved_dir)
        if not dpath.exists() or not dpath.is_dir():
            return {"status": "error", "reason": f"Directory not found: {dir_path}"}

        # Default extension priority
        if not file_extensions:
            file_extensions = [
                ".py", ".js", ".ts", ".go", ".rs", ".java",
                ".c", ".cpp", ".h", ".rb", ".php", ".sh",
                ".json", ".yaml", ".yml", ".md", ".txt",
                ".html", ".css", ".sql",
            ]

        SKIP_DIRS = {"node_modules", "__pycache__", ".git", "dist",
                     "build", "venv", ".venv", ".mypy_cache", "coverage"}

        # Collect all matching files
        all_files = []
        for ext in file_extensions:
            for f in dpath.rglob(f"*{ext}"):
                # Skip hidden and blacklisted dirs
                if any(part in SKIP_DIRS or part.startswith('.') for part in f.parts):
                    continue
                all_files.append(f)

        if not all_files:
            return {"status": "error", "reason": f"No code files found in {dir_path}"}

        # Score each file against query
        scored_files = []
        for f in all_files:
            try:
                text = f.read_text(encoding='utf-8', errors='replace')
                score = _score_chunk(query, text)
                rel_path = str(f.relative_to(dpath))
                scored_files.append((score, rel_path, text))
            except Exception:
                continue

        scored_files.sort(key=lambda x: x[0], reverse=True)
        top = scored_files[:max_chunks]

        sections = []
        for score, rel_path, text in top:
            # Truncate very large individual files
            if len(text) > 8000:
                text = text[:8000] + f"\n... [truncated — {len(text)} chars total, showing first 8000]"
            sections.append(f"[{rel_path} | relevance: {score:.2f}]\n{text}")

        return {
            "status": "success",
            "directory": dir_path,
            "total_files_scanned": len(all_files),
            "files_returned": len(top),
            "query": query,
            "note": f"Showing {len(top)} most relevant files. Ask for specific files or a different query for more.",
            "content": "\n\n---\n\n".join(sections),
            "file_list": [r for _, r, _ in scored_files],  # full index for reference
        }

    # ── Existing actions ──────────────────────────────────────────────────────

    def summarize(self, file_path: str, summary_length: str = "medium") -> dict:
        result = self.read(file_path)
        if result["status"] != "success":
            return result
        word_count = len(result["content"].split())
        return {
            **result,
            "word_count": word_count,
            "instruction": f"Summarize this {result['file_type']} file ({word_count} words) in {summary_length} format.",
        }

    def answer_question(self, file_path: str, question: str) -> dict:
        result = self.read(file_path)
        if result["status"] != "success":
            return result
        return {
            **result,
            "question": question,
            "instruction": f"Answer this question based on the file content: {question}",
        }

    def extract_code(self, file_path: str) -> dict:
        result = self.read(file_path)
        if result["status"] != "success":
            return result
        content = result["content"]
        code_blocks = []
        if result["file_type"] == ".md":
            matches = re.findall(r"```(?:python)?\n(.*?)\n```", content, re.DOTALL)
            code_blocks = matches
        elif result["file_type"] == ".py":
            code_blocks = [content]
        return {
            **result,
            "code_blocks_found": len(code_blocks),
            "code": code_blocks,
        }

    def get_metadata(self, file_path: str) -> dict:
        resolved = self._resolve(file_path)
        if not resolved:
            return {"status": "error", "reason": "Path outside sandbox"}
        path = Path(resolved)
        if not path.exists():
            return {"status": "error", "reason": f"File not found: {file_path}"}
        stat = path.stat()

        # If it's a directory, return a tree summary
        if path.is_dir():
            files = list(path.rglob("*"))
            by_ext = {}
            for f in files:
                if f.is_file():
                    by_ext[f.suffix] = by_ext.get(f.suffix, 0) + 1
            return {
                "status": "success",
                "path": file_path,
                "type": "directory",
                "total_files": sum(by_ext.values()),
                "by_extension": by_ext,
            }

        return {
            "status": "success",
            "file_name": path.name,
            "file_type": path.suffix.lower(),
            "file_size": stat.st_size,
            "file_size_mb": round(stat.st_size / (1024 * 1024), 2),
            "is_supported": path.suffix.lower() in self.supported_types,
        }


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(payload):
    user_id = payload.get("user_id")
    args = payload.get("args", {}) or {}
    action = args.get("action")
    file_path = (args.get("file_path") or "").strip()
    query = (args.get("query") or "").strip()
    max_chunks = int(args.get("max_chunks") or 5)
    chunk_size = int(args.get("chunk_size") or 15)
    file_extensions = args.get("file_extensions") or None
    # ADDED 2026-09-04 — pagination for scanned/image PDF vision rendering
    # (see _rasterize_pdf_for_vision). 0-indexed; the model gets back
    # "next_page_offset" from a previous call's result and passes it here
    # to walk further into a document longer than one render batch.
    try:
        vision_page_offset = max(0, int(args.get("vision_page_offset") or 0))
    except (TypeError, ValueError):
        vision_page_offset = 0

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    if not action:
        return {
            "status": "info",
            "message": (
                "Actions: fetch_upload (pull an uploaded/generated file — identified by its "
                "download_url — directly into the sandbox with NO network call, use this FIRST "
                "for any file from chat/WhatsApp/file_generation before read/read_chunked), "
                "read (small files), read_chunked (large docs — needs query), "
                "read_codebase (directories — needs query), summarize, answer_question, "
                "extract_code, metadata. "
                "For any PDF > 10 pages or large file, use read_chunked with a specific query."
            )
        }

    if action == "fetch_upload":
        # ADDED 2026-08-15. Uploaded/generated files (chat attachments,
        # WhatsApp media, file_generation output) live ONLY in Mongo's
        # file_sessions collection, served over HTTP at
        # /api/v3/files/serve/{bucket}/{filename} — there was never a
        # way to get their bytes into the sandbox except curling that
        # URL. On this Render instance's single worker
        # (WEB_CONCURRENCY=1), a skill curling the app's OWN serve
        # endpoint deadlocks: the one worker running this skill is the
        # same worker that would need to be free to answer that curl,
        # so it hangs until timeout with zero bytes transferred. This
        # action reads the same Mongo document directly — no HTTP, no
        # deadlock — and writes the bytes into the sandbox so a normal
        # read/read_chunked call can follow immediately after.
        download_url = (args.get("download_url") or "").strip()
        save_as = (args.get("save_as") or "").strip()
        if not download_url:
            return {"status": "error", "reason": "fetch_upload requires 'download_url' (the URL from the chat attachment or a prior skill's result)"}

        # CLOUDINARY REINSTATED 2026-09-03: uploads can now live on
        # Cloudinary instead of our own Mongo file_sessions (see
        # core/inline_storage.py's save_upload()) — a Cloudinary URL is a
        # genuinely EXTERNAL host, so fetching it over real HTTP has none
        # of the single-worker self-curl deadlock risk the Mongo-direct
        # path below was built to avoid (that risk is specific to this
        # server curling ITSELF). Detected by absence of our own
        # "/files/serve/" path segment — anything else (Cloudinary, or any
        # other external URL a future storage backend might return) goes
        # through a plain download instead of the Mongo lookup.
        if "/files/serve/" not in download_url:
            import httpx as _httpx
            FETCH_MAX_BYTES = 20 * 1024 * 1024  # matches api/files.py's own upload cap
            try:
                async with _httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                    resp = await client.get(download_url)
                if resp.status_code != 200:
                    return {
                        "status": "error",
                        "reason": f"Fetching '{download_url}' returned HTTP {resp.status_code}.",
                    }
                raw_bytes = resp.content
                if len(raw_bytes) > FETCH_MAX_BYTES:
                    return {
                        "status": "error",
                        "reason": f"File at '{download_url}' is {len(raw_bytes)} bytes, over the {FETCH_MAX_BYTES}-byte fetch cap.",
                    }
                mime_type = resp.headers.get("content-type", "application/octet-stream")
            except Exception as e:
                return {"status": "error", "reason": f"Could not download '{download_url}': {e}"}

            target_name = save_as or download_url.rsplit("/", 1)[-1].split("?")[0] or "downloaded_file"
            try:
                from core.oadson_home import get_inputs_dir, is_safe_inputs_path
                if not is_safe_inputs_path(user_id, target_name):
                    return {"status": "error", "reason": f"'{target_name}' is not a safe path in the sandbox."}
                target_path = get_inputs_dir(user_id) / target_name
                with open(target_path, "wb") as f:
                    f.write(raw_bytes)
            except Exception as e:
                return {"status": "error", "reason": f"Could not write to sandbox: {e}"}

            return {
                "status": "success",
                "file_path": target_name,
                "size_bytes": len(raw_bytes),
                "mime_type": mime_type,
                "message": f"Downloaded and saved into sandbox as '{target_name}' ({len(raw_bytes)} bytes). Now use read/read_chunked/summarize on this file_path.",
            }

        # RESILIENCE FIX 2026-08-28: the model sometimes hands this a
        # paraphrased/half-remembered download_url instead of the exact
        # one from the conversation — one that doesn't split cleanly into
        # /files/serve/{bucket}/{filename}. Previously that was a hard
        # failure with a "Could not parse" error the model would then
        # retry verbatim over a dozen times (seen in production —
        # reflection count climbing on the exact same bad URL). Now try
        # the strict two-segment form first, and if that doesn't match,
        # fall back to pulling just the LAST path segment and looking it
        # up by filename alone (core.inline_storage.read_inline_by_filename
        # mirrors api/serve.py's own "search all sessions, most recent
        # wins" behavior for the single-segment /files/serve/{filename}
        # route) — same cost, one Mongo query either way, and it
        # recovers the file whenever a real filename is still in there
        # somewhere. Only report a parse error if there's no usable
        # filename at all.
        db = payload.get("db")
        if db is None:
            return {"status": "error", "reason": "No database handle available for this call."}

        from core.inline_storage import read_inline, read_inline_by_filename

        m = re.search(r"/files/serve/([^/]+)/([^/?#]+)", download_url)
        result = None
        bucket = None
        if m:
            bucket, filename = m.group(1), m.group(2)
            try:
                result = await read_inline(db, bucket, filename)
            except Exception as e:
                return {"status": "error", "reason": f"Could not read from storage: {e}"}

        if result is None:
            # Fallback: last path segment as a bare filename, regardless
            # of whether the strict regex matched above (covers both "no
            # slash after /files/serve/" and "bucket/filename didn't
            # actually exist under that bucket").
            m2 = re.search(r"/files/serve/(?:[^/]+/)?([^/?#]+)/?$", download_url)
            filename = m2.group(1) if m2 else download_url.rsplit("/", 1)[-1].split("?")[0]
            if not filename:
                return {"status": "error", "reason": f"Could not extract a filename from download_url: {download_url}"}
            try:
                result = await read_inline_by_filename(db, filename)
            except Exception as e:
                return {"status": "error", "reason": f"Could not read from storage: {e}"}

        if result is None:
            # SELF-HEAL 2026-08-30: before giving up, check whether a
            # file matching this name (exact or fuzzy) already exists
            # directly in the sandbox. This is the common case for
            # anything that went through attachment_processor.py's
            # file_text branch — it's saved straight into inputs/ and
            # was NEVER put into Mongo/file_sessions at all, so no
            # download_url for it ever existed to begin with. A model
            # calling fetch_upload here anyway (habit, or forgetting
            # that this particular file needs no fetch) used to hit a
            # hard, unrecoverable "not found" even though the real file
            # was sitting right there in the sandbox the whole time.
            # Reuses DocumentReaderSkill._resolve()/_fuzzy_find() so
            # this gets the exact same near-miss recovery (dropped hash
            # prefix, minor timestamp drift) as read/read_chunked/etc.
            try:
                skill = DocumentReaderSkill(user_id)
                healed_path = skill._resolve(filename)
                if healed_path and Path(healed_path).exists():
                    return {
                        "status": "success",
                        "file_path": Path(healed_path).name,
                        "message": (
                            f"'{filename}' was already in your sandbox (no fetch needed — "
                            "it was saved directly, not uploaded to storage). Use "
                            f"file_path='{Path(healed_path).name}' with read/read_chunked."
                        ),
                    }
            except Exception:
                pass

            return {
                "status": "error",
                "reason": (
                    f"No stored file found matching '{download_url}' — it may have expired, "
                    "or this download_url wasn't copied exactly from the conversation. "
                    "DO NOT retry with the same URL — re-check the exact attachment_url "
                    "from the user's message or the prior skill result that produced this "
                    "file, and pass that exact string instead. If this file was pasted as "
                    "text, it may not need fetch_upload at all — try read/read_chunked "
                    "directly with its filename."
                ),
            }

        raw_bytes, mime_type = result
        target_name = save_as or filename
        try:
            from core.oadson_home import get_inputs_dir, is_safe_inputs_path
            # WORKSPACE SPLIT 2026-08-28: a fetched upload is exactly the
            # "user sent OADSON a file" case — save into inputs/, not
            # workspace/, so it's never mixed in with cloned repos. See
            # core/oadson_home.py and _resolve()'s matching inputs-first
            # lookup above.
            if not is_safe_inputs_path(user_id, target_name):
                return {"status": "error", "reason": f"'{target_name}' is not a safe path in the sandbox."}
            target_path = get_inputs_dir(user_id) / target_name
            with open(target_path, "wb") as f:
                f.write(raw_bytes)
        except Exception as e:
            return {"status": "error", "reason": f"Could not write to sandbox: {e}"}

        return {
            "status": "success",
            "file_path": target_name,
            "size_bytes": len(raw_bytes),
            "mime_type": mime_type,
            "message": f"Fetched into sandbox as '{target_name}' ({len(raw_bytes)} bytes). Now use read/read_chunked/summarize on this file_path.",
        }

    if not file_path:
        return {"status": "error", "reason": f"{action} requires a 'file_path'"}

    # Block image files — OADSON has native vision
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"}
    if Path(file_path).suffix.lower() in image_exts:
        return {
            "status": "error",
            "reason": "document_reader cannot process images — use native vision capability instead."
        }

    try:
        skill = DocumentReaderSkill(user_id)

        if action == "read":
            return skill.read(file_path, vision_page_offset=vision_page_offset)
        elif action == "read_chunked":
            if not query:
                return {"status": "error", "reason": "read_chunked requires a 'query' — what are you looking for?"}
            return skill.read_chunked(file_path, query, max_chunks=max_chunks, chunk_size=chunk_size, vision_page_offset=vision_page_offset)
        elif action == "read_codebase":
            if not query:
                return {"status": "error", "reason": "read_codebase requires a 'query' — what are you looking for in the code?"}
            return skill.read_codebase(file_path, query, max_chunks=max_chunks, file_extensions=file_extensions)
        elif action == "summarize":
            return skill.summarize(file_path, (args.get("summary_length") or "medium"))
        elif action == "answer_question":
            q = (args.get("question") or "").strip()
            if not q:
                return {"status": "error", "reason": "answer_question requires a 'question'"}
            return skill.answer_question(file_path, q)
        elif action == "extract_code":
            return skill.extract_code(file_path)
        elif action == "metadata":
            return skill.get_metadata(file_path)
        else:
            return {"status": "error", "reason": f"Unknown action '{action}'."}

    except Exception as e:
        return {"status": "error", "reason": str(e)}
