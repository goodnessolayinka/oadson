"""
file_generation — V3 (rewritten July 2026).

Generates AND edits files, served via backend URL.

Key fixes:
  - docx: proper section/heading/paragraph structure, not just flat text
  - docx: edit_docx action opens existing docx from DB and appends/replaces
  - docx: sections support [{heading, level, paragraphs}] structure
  - pdf: multi-page support with proper line wrapping
  - all types: content stored in MongoDB, served via /files/serve/{session}/{filename}

Actions (via args.action or inferred from file_type):
  generate — create new file (default)
  edit_docx — open existing docx from DB, append new sections, re-save
"""

import os
import io
import json
import base64
import logging
import textwrap
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("oadson.skill.file_generation")

BACKEND_URL = os.getenv(
    "BACKEND_URL",
    os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000")
).rstrip("/")


def _serve_url(filename: str, session_id: str) -> str:
    return f"{BACKEND_URL}/api/v3/files/serve/{session_id}/{filename}"


def _get_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".md":   "text/markdown",
        ".txt":  "text/plain",
        ".py":   "text/x-python",
        ".js":   "application/javascript",
        ".ts":   "application/typescript",
        ".tsx":  "application/typescript",
        ".jsx":  "application/javascript",
        ".json": "application/json",
        ".html": "text/html",
        ".css":  "text/css",
        ".csv":  "text/csv",
        ".xml":  "application/xml",
        ".yaml": "text/yaml",
        ".yml":  "text/yaml",
        ".sh":   "text/x-sh",
        ".sql":  "text/x-sql",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(ext, "text/plain")


async def _store_text(db, user_id, session_id, filename, content):
    await db["file_sessions"].update_one(
        {"user_id": user_id, "session_id": session_id, "filename": filename},
        {"$set": {
            "user_id": user_id, "session_id": session_id,
            "filename": filename, "content": content,
            "size": len(content), "mime_type": _get_mime(filename),
            "uploaded_at": datetime.utcnow(),
        }},
        upsert=True
    )


async def _store_binary(db, user_id, session_id, filename, file_bytes, mime_type):
    encoded = base64.b64encode(file_bytes).decode()
    await db["file_sessions"].update_one(
        {"user_id": user_id, "session_id": session_id, "filename": filename},
        {"$set": {
            "user_id": user_id, "session_id": session_id,
            "filename": filename, "content": encoded,
            "mime_type": mime_type, "encoding": "base64",
            "uploaded_at": datetime.utcnow(),
        }},
        upsert=True
    )


async def _load_docx_bytes(db, user_id, session_id, filename) -> bytes | None:
    """Retrieve existing docx bytes from MongoDB."""
    doc = await db["file_sessions"].find_one({
        "user_id": user_id, "session_id": session_id, "filename": filename
    })
    if not doc:
        return None
    content = doc.get("content", "")
    if doc.get("encoding") == "base64":
        return base64.b64decode(content)
    return None


def _build_docx(data: dict, existing_bytes: bytes | None = None) -> bytes:
    """
    Build or extend a docx.

    data keys:
      title       — document title (heading level 0), only used for new docs
      sections    — list of {heading, level, paragraphs: [str], bold_lines: [str]}
      paragraphs  — flat list of str (fallback if no sections)
      append_only — if True and existing_bytes provided, only add sections (don't reset)
      table       — [[row], [row], ...] optional table to append

    Heading levels: 0=Title, 1=Heading 1, 2=Heading 2, 3=Heading 3
    """
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if existing_bytes:
        doc = Document(io.BytesIO(existing_bytes))
    else:
        doc = Document()

    # Title — only for new docs
    if not existing_bytes and data.get("title"):
        doc.add_heading(data["title"], level=0)

    # Structured sections
    sections = data.get("sections", [])
    if sections:
        for section in sections:
            heading = section.get("heading", "")
            level = int(section.get("level", 1))
            if heading:
                doc.add_heading(heading, level=level)
            for para in section.get("paragraphs", []):
                if para.strip():
                    doc.add_paragraph(para)
            # bold_lines — e.g. key points
            for bold_line in section.get("bold_lines", []):
                p = doc.add_paragraph()
                p.add_run(bold_line).bold = True

    elif data.get("paragraphs"):
        # Flat paragraph list fallback
        for para in data["paragraphs"]:
            if para.strip():
                # Detect heading markers like "# Heading" or "## Heading"
                if para.startswith("### "):
                    doc.add_heading(para[4:], level=3)
                elif para.startswith("## "):
                    doc.add_heading(para[3:], level=2)
                elif para.startswith("# "):
                    doc.add_heading(para[2:], level=1)
                else:
                    doc.add_paragraph(para)

    # Table
    if data.get("table"):
        table_data = data["table"]
        if table_data and len(table_data) > 0:
            t = doc.add_table(rows=len(table_data), cols=len(table_data[0]))
            t.style = "Table Grid"
            for i, row in enumerate(table_data):
                for j, cell in enumerate(row):
                    t.rows[i].cells[j].text = str(cell)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_pdf(data: dict) -> bytes:
    """Build a proper multi-page PDF with line wrapping."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        rightMargin=inch, leftMargin=inch,
        topMargin=inch, bottomMargin=inch
    )
    styles = getSampleStyleSheet()
    story = []

    if data.get("title"):
        story.append(Paragraph(data["title"], styles["Title"]))
        story.append(Spacer(1, 0.2 * inch))

    sections = data.get("sections", [])
    if sections:
        for section in sections:
            if section.get("heading"):
                level = int(section.get("level", 1))
                style = styles["Heading1"] if level == 1 else styles.get("Heading2", styles["Heading1"])
                story.append(Paragraph(section["heading"], style))
            for para in section.get("paragraphs", []):
                if para.strip():
                    story.append(Paragraph(para, styles["BodyText"]))
                    story.append(Spacer(1, 0.1 * inch))
    elif data.get("paragraphs"):
        for para in data["paragraphs"]:
            if para.strip():
                story.append(Paragraph(para, styles["BodyText"]))
                story.append(Spacer(1, 0.1 * inch))

    if data.get("table"):
        table_data = data["table"]
        t = Table(table_data)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR",  (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID",       (0, 0), (-1, -1), 0.5, colors.black),
            ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME",   (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE",   (0, 0), (-1, -1), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.lightgrey]),
        ]))
        story.append(t)

    doc.build(story)
    return buf.getvalue()


def _build_xlsx(data: dict) -> bytes:
    """
    Build an .xlsx workbook.

    data keys (either form works):
      sheets — [{name, headers: [str], rows: [[...]]}]  (multi-sheet, explicit)
      rows   — [{col: val, ...}, ...]  (single-sheet shortcut, same shape
               csv already uses — headers inferred from first row's keys)
      sheet_name — used only with the 'rows' shortcut. Default 'Sheet1'.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)  # default blank sheet — replaced by real ones below

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")

    def _write_sheet(ws, headers: list, rows: list):
        for col_idx, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=str(header))
            cell.font = header_font
            cell.fill = header_fill
        for row_idx, row in enumerate(rows, 2):
            for col_idx, value in enumerate(row, 1):
                ws.cell(row=row_idx, column=col_idx, value=value)
        for col_idx, header in enumerate(headers, 1):
            max_len = max([len(str(header))] + [len(str(r[col_idx - 1])) for r in rows if col_idx - 1 < len(r)] or [0])
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 50)

    sheets = data.get("sheets")
    if sheets:
        for sheet in sheets:
            ws = wb.create_sheet(title=(sheet.get("name") or "Sheet")[:31])
            headers = sheet.get("headers", [])
            rows = sheet.get("rows", [])
            _write_sheet(ws, headers, rows)
    else:
        rows = data.get("rows") or []
        if not rows:
            raise ValueError("xlsx requires either 'sheets' or 'rows'")
        headers = list(rows[0].keys()) if isinstance(rows[0], dict) else [f"Col{i+1}" for i in range(len(rows[0]))]
        table_rows = [
            [r.get(h, "") for h in headers] if isinstance(r, dict) else r
            for r in rows
        ]
        ws = wb.create_sheet(title=(data.get("sheet_name") or "Sheet1")[:31])
        _write_sheet(ws, headers, table_rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_pptx(data: dict) -> tuple[bytes, list[str]]:
    """
    Build a .pptx deck.

    data keys:
      title, subtitle   — title slide (only if at least one is set)
      slides — [{title, bullets: [str], layout: "title_content"|"section"|"title_only"}]

    Returns (file_bytes, warnings) — warnings flags any content slide that
    silently degraded to a bare title because 'bullets' came in empty.

    FIX 2026-08-06: caught in testing — a real deck came back with 3 of 4
    slides rendering as nothing but a title on a blank page (see
    coffee_presentation.pptx: "1. The Legend of Kaldi" with nothing below
    it) because the model called this with bullets=[] and no explicit
    layout, silently falling into the 'not bullets' branch below. That
    branch is legitimate for a DELIBERATE section-divider-style slide, so
    it isn't being removed — but it was invisible when it happened by
    accident. Now every such slide is tracked and surfaced back in the
    result so the caller (model) sees it happened and can re-call with
    real content, the same pass-through-so-it-can-self-correct pattern
    skill_executor.py already uses for skill errors.
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    section_layout = prs.slide_layouts[2]
    content_layout = prs.slide_layouts[1]
    blank_layout = prs.slide_layouts[6]

    warnings: list[str] = []

    if data.get("title") or data.get("subtitle"):
        slide = prs.slides.add_slide(title_layout)
        slide.shapes.title.text = data.get("title", "")
        if data.get("subtitle") and len(slide.placeholders) > 1:
            slide.placeholders[1].text = data["subtitle"]

    for i, slide_data in enumerate(data.get("slides", []), 1):
        layout_name = slide_data.get("layout", "title_content")
        heading = slide_data.get("title", "")
        bullets = slide_data.get("bullets", [])

        if layout_name == "section":
            slide = prs.slides.add_slide(section_layout)
            if slide.shapes.title:
                slide.shapes.title.text = heading
        elif layout_name == "title_only" or not bullets:
            if layout_name != "title_only":
                warnings.append(
                    f"Slide {i} ('{heading}') has no 'bullets' and no explicit layout='title_only' — "
                    f"it rendered as a bare title with nothing else on it. Re-call with bullets filled "
                    f"in if this slide was meant to have content."
                )
            slide = prs.slides.add_slide(blank_layout)
            box = slide.shapes.add_textbox(Inches(0.5), Inches(0.5), Inches(9), Inches(1.2))
            box.text_frame.text = heading
            box.text_frame.paragraphs[0].font.size = Pt(32)
            box.text_frame.paragraphs[0].font.bold = True
        else:
            slide = prs.slides.add_slide(content_layout)
            if slide.shapes.title:
                slide.shapes.title.text = heading
            body = slide.placeholders[1].text_frame
            body.clear()
            for bullet_idx, bullet in enumerate(bullets):
                p = body.paragraphs[0] if bullet_idx == 0 else body.add_paragraph()
                p.text = str(bullet)
                p.level = 0

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue(), warnings


async def run(payload: dict) -> dict:
    user_id   = payload.get("user_id")
    session_id = payload.get("session_id", "default")
    surface   = payload.get("surface", "web")
    args      = payload.get("args", {}) or {}

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    action    = (args.get("action") or "generate").strip().lower()
    file_type = (args.get("file_type") or "").strip().lower()
    filename  = (args.get("filename") or "").strip()
    content   = args.get("content") or payload.get("message", "")

    if not file_type:
        return {
            "status": "info",
            "message": (
                "file_generation: text, python, markdown, html, css, tsx, jsx, "
                "json, xml, csv, yaml, sh, docx, pdf, xlsx, pptx. "
                "For docx: pass data={title, sections:[{heading, level, paragraphs:[]}]} for structure. "
                "For xlsx: pass data={rows:[{col:val}]} for one sheet, or data={sheets:[{name, headers, rows}]} for multiple. "
                "For pptx: pass data={title, subtitle, slides:[{title, bullets:[str], layout}]}. "
                "action='edit_docx' to modify an existing docx."
            )
        }
    if not filename:
        return {"status": "error", "reason": "A 'filename' is required"}

    try:
        from bootstrap import get
        db = get("db")

        # ── EDIT EXISTING DOCX ──────────────────────────────────────────────
        if action == "edit_docx" or (action == "generate" and file_type == "docx" and args.get("edit_existing")):
            existing_bytes = await _load_docx_bytes(db, user_id, session_id, filename)
            if not existing_bytes:
                # Try without session scoping (uploaded files use different session_id)
                doc_record = await db["file_sessions"].find_one({
                    "user_id": user_id, "filename": filename,
                    "encoding": "base64"
                })
                if doc_record:
                    existing_bytes = base64.b64decode(doc_record.get("content", ""))

            data = args.get("data") or {}
            try:
                file_bytes = _build_docx(data, existing_bytes=existing_bytes)
            except Exception as e:
                return {"status": "error", "reason": f"docx edit failed: {e}"}

            await _store_binary(
                db, user_id, session_id, filename, file_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            url = _serve_url(filename, session_id)
            return {
                "status": "success",
                "filename": filename,
                "action": "edited",
                "existing_found": existing_bytes is not None,
                "download_url": url,
                "message": f"✅ *{filename}* {'updated' if existing_bytes else 'created'}\n📥 Download: {url}",
            }

        # ── TEXT-BASED TYPES ────────────────────────────────────────────────
        text_types = {
            "text", "python", "markdown", "html", "css",
            "tsx", "jsx", "xml", "yaml", "sh", "sql"
        }
        if file_type in text_types:
            if not content:
                # BUG FOUND 2026-08-21 — confirmed root cause of a real
                # report: "generate a random html file" led to
                # file_generation being called repeatedly (5x across 5
                # separate turns, per Render logs) with file_type='html'
                # and a filename, but no content — every single call hit
                # this exact branch and returned a clean {"status":
                # "error"} result. From the orchestrator's point of view
                # that's still a successfully-EXECUTED skill (no
                # exception, no crash), so nothing else in the pipeline
                # ever surfaced this as a failure — the model just kept
                # narrating around it and re-calling with the same
                # missing content instead of either generating real HTML
                # or telling the user what actually went wrong. The old
                # message here ("html requires 'content'") gave the model
                # nothing concrete to act on — it doesn't say WHAT content
                # should look like for this file_type, so a model that
                # didn't already know to synthesize one just kept retrying
                # the same broken call. This is far more directive: for
                # 'html' specifically (the reported case), it tells the
                # model exactly what a minimal valid document needs.
                if file_type == "html":
                    return {
                        "status": "error",
                        "reason": (
                            "file_type='html' requires 'content' — a complete HTML "
                            "document as a string (e.g. starting with <!DOCTYPE html> "
                            "through </html>). Call file_generation again with the "
                            "actual HTML you want in the 'content' field — do not "
                            "retry with the same empty call."
                        ),
                    }
                return {"status": "error", "reason": f"{file_type} requires 'content' — pass the actual file text in the 'content' field and call again."}
            await _store_text(db, user_id, session_id, filename, content)
            url = _serve_url(filename, session_id)
            tool_call = None
            if surface == "terminal":
                tool_call = {"tool": "write_file", "args": {"path": filename, "content": content}}
            return {
                "status": "success",
                "filename": filename,
                "file_type": file_type,
                "size": len(content),
                "download_url": url,
                "content": content,
                "tool_call": tool_call,
                "message": f"✅ *{filename}* ready\n📥 Download: {url}",
            }

        elif file_type == "json":
            data = args.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass
            out = json.dumps(data, indent=2)
            await _store_text(db, user_id, session_id, filename, out)
            url = _serve_url(filename, session_id)
            return {
                "status": "success", "filename": filename,
                "download_url": url, "content": out,
                "message": f"✅ *{filename}* ready\n📥 Download: {url}",
            }

        elif file_type == "csv":
            rows = args.get("rows")
            if not rows or not isinstance(rows, list):
                return {"status": "error", "reason": "csv requires a 'rows' list"}
            import csv as _csv
            buf = io.StringIO()
            writer = _csv.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
            out = buf.getvalue()
            await _store_text(db, user_id, session_id, filename, out)
            url = _serve_url(filename, session_id)
            return {
                "status": "success", "filename": filename, "rows": len(rows),
                "download_url": url,
                "message": f"✅ *{filename}* ready ({len(rows)} rows)\n📥 Download: {url}",
            }

        elif file_type == "docx":
            data = args.get("data") or {}
            try:
                file_bytes = _build_docx(data)
            except ImportError:
                return {"status": "error", "reason": "python-docx not installed. Run: pip install python-docx"}
            except Exception as e:
                return {"status": "error", "reason": f"docx generation failed: {e}"}

            await _store_binary(
                db, user_id, session_id, filename, file_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
            url = _serve_url(filename, session_id)
            return {
                "status": "success", "filename": filename,
                "download_url": url,
                "message": f"✅ *{filename}* ready\n📥 Download: {url}",
            }

        elif file_type == "pdf":
            data = args.get("data") or {}
            try:
                file_bytes = _build_pdf(data)
            except ImportError:
                return {"status": "error", "reason": "reportlab not installed. Run: pip install reportlab"}
            except Exception as e:
                return {"status": "error", "reason": f"PDF generation failed: {e}"}

            await _store_binary(db, user_id, session_id, filename, file_bytes, "application/pdf")
            url = _serve_url(filename, session_id)
            return {
                "status": "success", "filename": filename,
                "download_url": url,
                "message": f"✅ *{filename}* ready\n📥 Download: {url}",
            }

        elif file_type == "xlsx":
            data = args.get("data") or {}
            try:
                file_bytes = _build_xlsx(data)
            except ImportError:
                return {"status": "error", "reason": "openpyxl not installed. Run: pip install openpyxl"}
            except Exception as e:
                return {"status": "error", "reason": f"xlsx generation failed: {e}"}

            await _store_binary(
                db, user_id, session_id, filename, file_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            url = _serve_url(filename, session_id)
            return {
                "status": "success", "filename": filename,
                "download_url": url,
                "message": f"✅ *{filename}* ready\n📥 Download: {url}",
            }

        elif file_type == "pptx":
            data = args.get("data") or {}
            try:
                file_bytes, pptx_warnings = _build_pptx(data)
            except ImportError:
                return {"status": "error", "reason": "python-pptx not installed. Run: pip install python-pptx"}
            except Exception as e:
                return {"status": "error", "reason": f"pptx generation failed: {e}"}

            await _store_binary(
                db, user_id, session_id, filename, file_bytes,
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            )
            url = _serve_url(filename, session_id)
            message = f"✅ *{filename}* ready\n📥 Download: {url}"
            if pptx_warnings:
                message += "\n⚠️ " + " | ".join(pptx_warnings)
            return {
                "status": "success" if not pptx_warnings else "info",
                "filename": filename,
                "download_url": url,
                "warnings": pptx_warnings or None,
                "message": message,
            }

        else:
            return {
                "status": "error",
                "reason": f"Unknown file_type '{file_type}'. Valid: text, python, markdown, html, css, tsx, jsx, json, xml, csv, yaml, sh, docx, pdf, xlsx, pptx"
            }

    except Exception as e:
        logger.error(f"file_generation error: {e}", exc_info=True)
        return {"status": "error", "reason": str(e)}
