"""
core/repo_index.py — index a codebase so heavy turns send only relevant code.

Sources that get indexed:
  - a code archive uploaded in chat (api/chat_stream.py)
  - a repo cloned with the github skill's clone_to_workspace (skills/github/skill.py)
NOT indexed: github_daily_summary (commit/PR metadata only), workspace/read_file
(single files via the API), or terminal `git clone` run directly.

Per request, Claude gets:
  - a compact PROJECT MAP (files + top-level symbols, ~1.5K tokens) in the
    system prompt. Stable for the session, so it sits in the cached prefix.
  - the MAX snippets most relevant to the request (~3K tokens) in the latest
    user message.

Retrieval is BM25 over identifier words: no API calls, no cost, and it fits
code well because questions usually name a function, file, or concept.

PERSISTENCE (Mongo, via bootstrap's db; memory-only if db is absent):
  repo_indexes  one doc per index: files, symbols, owner user_id
  repo_chunks   one doc per chunk (keeps every doc well under Mongo's 16MB)
  repo_active   one doc per user: which index is the active repo
Ownership is enforced on load: an index only loads for the user who built it.
"""
import ast
import io
import logging
import math
import re
import zipfile
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("oadson.repo_index")

MAX_FILE_BYTES = 300_000
MAX_FILES = 800
MAX_CHUNKS = 4000               # bounds memory on the tight Render box and Mongo doc count
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".swift",
             ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".sql", ".sh", ".md",
             ".yaml", ".yml", ".toml", ".json", ".html", ".css"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", "target", ".idea", ".vscode", "coverage"}
WINDOW_LINES, WINDOW_OVERLAP = 60, 10
TOP_K = 6
SNIPPET_BUDGET_CHARS = 12_000
MAP_BUDGET_CHARS = 6_000
MAX_INDEXES_IN_MEMORY = 5
STOP = set("the and for with this that from into when what where which how why are was were "
           "not you your can use using has have any all but its one out get set new".split())

_SYM_RE = re.compile(
    r"^\s*(?:async\s+)?(?:def|class|function|export\s+(?:default\s+)?(?:async\s+)?function"
    r"|export\s+(?:const|class)|const|let|var|func|fn|struct|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M)


def _words(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOP]


@dataclass
class Chunk:
    file: str
    start: int
    end: int
    symbol: str
    text: str
    tf: Counter = field(default_factory=Counter)
    length: int = 0


class RepoIndex:
    def __init__(self, key: str, owner: str = ""):
        self.key = key
        self.owner = owner
        self.files: dict[str, int] = {}
        self.symbols: dict[str, list[str]] = {}
        self.chunks: list[Chunk] = []
        self.df: Counter = Counter()
        self.avg_len = 1.0

    # ---- ingest ----
    def add_file(self, path: str, text: str) -> None:
        if len(self.files) >= MAX_FILES or len(self.chunks) >= MAX_CHUNKS:
            return
        lines = text.splitlines()
        self.files[path] = len(lines)
        syms, spans = [], []
        if path.endswith(".py"):
            try:
                for node in ast.parse(text).body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        spans.append((node.lineno, node.end_lineno or node.lineno, node.name))
                        syms.append(node.name)
            except SyntaxError:
                pass
        if not spans:
            syms = [m.group(1) for m in _SYM_RE.finditer(text)]
        self.symbols[path] = syms[:40]

        if spans:
            for s, e, name in spans:
                body = "\n".join(lines[s - 1:e])
                for i in range(0, len(body), 4000):
                    self._add_chunk(path, s, e, name, body[i:i + 4000])
        else:
            step = WINDOW_LINES - WINDOW_OVERLAP
            for s in range(0, max(len(lines), 1), step):
                block = "\n".join(lines[s:s + WINDOW_LINES])
                if block.strip():
                    self._add_chunk(path, s + 1, min(s + WINDOW_LINES, len(lines)), "", block)

    def _add_chunk(self, path, start, end, symbol, text) -> None:
        if len(self.chunks) >= MAX_CHUNKS:
            return
        tf = Counter(_words(text))
        tf.update({w: 2 for w in _words(path)})       # file name words count extra
        if symbol:
            tf.update({w: 3 for w in _words(symbol)})  # function/class names count most
        self.chunks.append(Chunk(path, start, end, symbol, text, tf, sum(tf.values())))
        for w in tf:
            self.df[w] += 1

    def finalize(self) -> None:
        if self.chunks:
            self.avg_len = sum(c.length for c in self.chunks) / len(self.chunks)

    # ---- retrieval ----
    def search(self, query: str, k: int = TOP_K) -> list[Chunk]:
        q = set(_words(query))
        if not q or not self.chunks:
            return []
        n, k1, b = len(self.chunks), 1.5, 0.75
        scored = []
        for c in self.chunks:
            s = 0.0
            for w in q:
                f = c.tf.get(w, 0)
                if f:
                    idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                    s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * c.length / self.avg_len))
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]]

    # ---- what goes into the prompt ----
    def repo_map(self) -> str:
        header = f"PROJECT MAP ({len(self.files)} files; use the snippets for exact code):"
        lines, size = [header], len(header)
        paths = sorted(self.files)
        for i, path in enumerate(paths):
            syms = self.symbols.get(path) or []
            line = f"- {path} ({self.files[path]} lines)" + (f": {', '.join(syms[:8])}" if syms else "")
            if size + len(line) > MAP_BUDGET_CHARS:
                lines.append(f"... and {len(paths) - i} more files")
                break
            lines.append(line)
            size += len(line)
        return "\n".join(lines)

    def snippets_for(self, query: str) -> str:
        out, used = [], 0
        for c in self.search(query):
            head = f"--- {c.file}:{c.start}-{c.end}" + (f" ({c.symbol})" if c.symbol else "")
            block = f"{head}\n{c.text}"
            if used + len(block) > SNIPPET_BUDGET_CHARS:
                break
            out.append(block)
            used += len(block)
        return "\n\n".join(out)


# ---- in-memory registry ----
_indexes: "OrderedDict[str, RepoIndex]" = OrderedDict()
_active: dict[str, str] = {}   # user_id -> index key


def _register(idx: RepoIndex) -> RepoIndex:
    idx.finalize()
    _indexes[idx.key] = idx
    _indexes.move_to_end(idx.key)
    while len(_indexes) > MAX_INDEXES_IN_MEMORY:
        _indexes.popitem(last=False)
    return idx


def get(key: str) -> RepoIndex | None:
    idx = _indexes.get(key)
    if idx is not None:
        _indexes.move_to_end(key)
    return idx


def get_for_user(user_id: str) -> RepoIndex | None:
    """Sync lookup used on the tool-loop hot path. Loaded by resolve_user() at request start."""
    key = _active.get(user_id or "")
    return get(key) if key else None


# ---- building ----
def _want(path: str) -> bool:
    parts = Path(path).parts
    return not any(p in SKIP_DIRS for p in parts) and Path(path).suffix.lower() in CODE_EXTS


def ingest_zip(key: str, data: bytes, owner: str = "") -> RepoIndex:
    idx = RepoIndex(key, owner)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir() or not _want(info.filename) or info.file_size > MAX_FILE_BYTES:
                continue
            if len(idx.files) >= MAX_FILES:
                break
            idx.add_file(info.filename, z.read(info).decode("utf-8", errors="ignore"))
    return _register(idx)


def ingest_files(key: str, files: dict, owner: str = "") -> RepoIndex:
    """Index file contents already in memory (e.g. fetched via the GitHub API)."""
    idx = RepoIndex(key, owner)
    for path, text in files.items():
        if _want(path) and len(idx.files) < MAX_FILES:
            idx.add_file(path, text or "")
    return _register(idx)


def ingest_dir(key: str, root: str, owner: str = "") -> RepoIndex:
    idx = RepoIndex(key, owner)
    base = Path(root)
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(base))
        if not _want(rel) or p.stat().st_size > MAX_FILE_BYTES:
            continue
        if len(idx.files) >= MAX_FILES:
            break
        idx.add_file(rel, p.read_text(encoding="utf-8", errors="ignore"))
    return _register(idx)


# ---- persistence ----
async def save(db, idx: RepoIndex, user_id: str) -> None:
    """Make idx the user's active repo. Writes to Mongo when db is given; always keeps it in memory."""
    idx.owner = user_id
    _register(idx)
    _active[user_id] = idx.key
    if db is None:
        return
    try:
        await db["repo_chunks"].delete_many({"index_key": idx.key})
        await db["repo_indexes"].replace_one({"_id": idx.key}, {
            "_id": idx.key, "user_id": user_id,
            "files": [{"path": p, "lines": n, "symbols": idx.symbols.get(p, [])} for p, n in idx.files.items()],
            "avg_len": idx.avg_len, "n_chunks": len(idx.chunks),
            "updated": datetime.now(timezone.utc),
        }, upsert=True)
        docs = [{"index_key": idx.key, "file": c.file, "start": c.start, "end": c.end,
                 "symbol": c.symbol, "text": c.text, "tf": dict(c.tf), "length": c.length}
                for c in idx.chunks]
        for i in range(0, len(docs), 500):
            await db["repo_chunks"].insert_many(docs[i:i + 500])
        await db["repo_active"].replace_one({"_id": user_id}, {"_id": user_id, "key": idx.key}, upsert=True)
    except Exception as e:
        logger.error(f"repo index save failed (kept in memory for this process): {e}")


async def load(db, key: str, user_id: str) -> RepoIndex | None:
    if db is None:
        return None
    meta = await db["repo_indexes"].find_one({"_id": key})
    if not meta or meta.get("user_id") != user_id:   # ownership check
        return None
    idx = RepoIndex(key, user_id)
    for f in meta.get("files", []):
        idx.files[f["path"]] = f["lines"]
        idx.symbols[f["path"]] = f.get("symbols", [])
    async for d in db["repo_chunks"].find({"index_key": key}):
        c = Chunk(d["file"], d["start"], d["end"], d.get("symbol", ""), d["text"],
                  Counter(d.get("tf", {})), d.get("length", 0))
        idx.chunks.append(c)
        for w in c.tf:
            idx.df[w] += 1
    return _register(idx)


async def resolve_user(db, user_id: str) -> RepoIndex | None:
    """Restore a user's active repo after a restart. Cheap no-op if already in memory."""
    if get_for_user(user_id):
        return get_for_user(user_id)
    if db is None or not user_id:
        return None
    ptr = await db["repo_active"].find_one({"_id": user_id})
    if not ptr:
        return None
    idx = await load(db, ptr["key"], user_id)
    if idx:
        _active[user_id] = idx.key
    return idx


# ---- switching between indexed repos ----
_REPO_TOKEN = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
_SWITCH_INTENT = re.compile(r"\b(switch|use|work on|work with|open|go to|change to)\b", re.I)


def _label_of(key: str) -> str:
    """'<uid>:gh:acme/shop@main' -> 'acme/shop@main'; uploads get their key tail."""
    return key.split(":", 2)[-1]


async def list_repos(db, user_id: str) -> list[dict]:
    if db is None or not user_id:
        return []
    out = []
    async for d in db["repo_indexes"].find({"user_id": user_id}).sort("updated", -1):
        out.append({"key": d["_id"], "label": _label_of(d["_id"]), "files": len(d.get("files", [])),
                    "chunks": d.get("n_chunks", 0)})
    return out


async def maybe_switch(db, user_id: str, text: str) -> str | None:
    """
    If the message says to switch to / use an indexed repo by owner/repo name,
    make that repo the user's active repo. Only this user's own repos match,
    and nothing is switched on a plain mention without a switch-style verb.
    Returns the label switched to, or None.
    """
    if not text or not _SWITCH_INTENT.search(text) or db is None:
        return None
    names = {m.group(1).lower() for m in _REPO_TOKEN.finditer(text)}
    if not names:
        return None
    for repo in await list_repos(db, user_id):
        base = repo["label"].lower().split("@")[0]
        if base in names:
            idx = await load(db, repo["key"], user_id)
            if idx:
                _active[user_id] = idx.key
                return repo["label"]
    return None
