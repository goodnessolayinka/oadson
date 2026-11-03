# document_reader

## What It Does
Reads PDFs, DOCX, PPTX, XLSX, CSV, and plain text/code files — either
already sitting in the sandbox or freshly uploaded/generated files
pulled in by URL. Covers full reads, large-document chunked/query-aware
reads, codebase-wide search, summarization, and Q&A over a document.

## Two very different starting points — check which one you're in

**Case A — the file is ALREADY in your sandbox.** This is the common
case for anything a user pasted as text in this chat: `attachment_processor`
saves it straight into `inputs/` before your turn even starts, and the
turn's objective tells you the exact `file_path` to use. **Go straight to
`read`/`read_chunked`/etc with that `file_path` — do NOT call
`fetch_upload` for these.** There is no `download_url` for a file that
was saved directly; guessing one (e.g. reconstructing a plausible-looking
`/files/serve/uploads/...` URL from a filename pattern you remember from
earlier) will fail, because that URL was never issued for this file in
the first place.

**Case B — you were given a real `download_url`** (a chat/WhatsApp/Drive
attachment's actual `attachment_url`, or a URL a prior skill's result
returned). Use `fetch_upload` with that **exact string**, copied — not
reconstructed from memory of what such URLs usually look like. This pulls
the file into the sandbox once; every action after that uses the plain
`file_path` it returns, not the URL again.

If you're not sure which case you're in, check the objective text for
this turn first — it says which one you have.

## Action Reference
| Action | Use for | Key params |
|---|---|---|
| `fetch_upload` | Pull a file into the sandbox by its real `download_url` — see Case B above | `download_url` (required, must be the exact real URL), `save_as` (optional) |
| `read` | Full content — small files only (<50 pages) | `file_path` |
| `read_chunked` | Query-aware chunked reading — **preferred for any PDF >10 pages or large file**; finds relevant sections instead of reading everything | `file_path`, `query` (required), `max_chunks` (default 5), `chunk_size` |
| `read_codebase` | Walk a directory, find relevant code files by query | `file_path` (dir), `query` (required), `file_extensions` |
| `summarize` | Content + a summarization pass | `file_path`, `summary_length`: short/medium/long |
| `answer_question` | Content + a specific question about it | `file_path`, `question` (required) |
| `extract_code` | Pull code blocks out of a document | `file_path` |
| `metadata` | File info only (size, type, page count) — no content | `file_path` |

## read_chunked — don't silently drop topics
If your `query` names several distinct topics/sections/modules at once
(e.g. asking about multiple chapters together), the default `max_chunks`
(5) can silently omit some of them from the result — that is NOT the same
as those topics being absent from the document. Check
`total_chunks_scanned` in the result and raise `max_chunks` to cover the
whole document when the query is genuinely multi-topic.

## Self-healing filename lookups
If the exact `file_path` you pass doesn't resolve, every action here
falls back to a narrow fuzzy match (dropped save-id prefix, small
timestamp drift) before failing — so a slightly-off filename usually
still works. It will NOT guess between two equally-plausible real files;
a genuinely ambiguous or wrong name still returns a clear "not found."

## Scanned/image-based PDFs — you may get page images back, not text
If a PDF has no extractable text at all (a scan, or a photo of a
document saved as PDF), `read`/`read_chunked` render its pages as
images instead of failing outright — up to 5 pages per call, attached
to the result alongside a short text note telling you this happened.
**Read those page images visually and answer from what's actually in
them** — don't guess content from the filename, and don't assume the
document is unreadable just because `content` is short; the real
content is in the attached images for that reply. Encrypted PDFs are
handled first (empty-password decrypt) before this even triggers, so
this only happens for genuinely scanned/image-only documents.

**Longer than 5 pages?** The result includes `has_more` and
`next_page_offset`. If `has_more` is true, call the same action again
with `vision_page_offset=<next_page_offset>` to render the next batch —
repeat until `has_more` is false to walk the whole document page range
by page range.

## Constraints
- `read` is unsuitable for large PDFs — use `read_chunked` instead of
  raising a size complaint back to the user.
- `read_codebase`'s `query` should be specific ("cybersecurity courses
  300 level", not "courses") — vague queries return vague/irrelevant
  matches.
- `fetch_upload` works with any real `download_url` — one issued by this
  server (an inline `/files/serve/...` URL) or a genuine external one
  (e.g. a Cloudinary URL, now that uploads can live there — see
  core/inline_storage.py). It still cannot fetch an arbitrary URL you
  weren't actually given — that's `@web_fetch`.
- Scanned-PDF page rendering is capped at 5 pages per call — use
  `vision_page_offset` (see above) to see further pages of a longer
  scanned document across repeat calls.
