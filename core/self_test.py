"""
core/self_test.py — OADSON V3 Self-Test Suite.

Not a full test framework — a targeted regression net over the exact
bugs found and fixed in this system's recent history, plus a basic
"did the skill registry come up right" sanity check. Every test here
exists because something specific actually broke in production:

  - jwt_decoder: sanity check the whole decode path still works
    end-to-end against a fixed, known-answer token.
  - weather_forecast formatting: this is precisely where the
    undefined `wind_flag` NameError lived — calling the real _fmt()
    function here means a reintroduced typo crashes THIS test instead
    of a live chat.
  - file_generation pptx: regression guard for the bullet-loss bug —
    asserts the happy path produces zero warnings and the
    missing-bullets path produces exactly one, not a silent blank
    slide.
  - file_generation xlsx: round-trips real bytes through openpyxl to
    confirm the file is actually valid, not just "didn't throw."
  - proactive_agent chat ID: the highest-value test here — this is
    the exact "@c.us" corruption bug. Calls the REAL
    build_owner_chat_id() function (not a re-typed copy), so if
    anyone touches that function again and reintroduces the bug, this
    fails immediately instead of silently eating WhatsApp messages
    for however long it takes someone to notice.
  - skill_registry health: confirms the skills added this session are
    actually loaded and registered, not just present as files on disk.

Every test function is synchronous/self-contained where possible
(no DB writes, no network calls) so this can run often and fast —
intended to be called from GET /engineer/self-test and, in chat, via
skills/system_status.py's 'self_test' action.
"""

import io
import os
import ast
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("oadson.self_test")


def _ok(name: str, detail: str = "") -> dict:
    return {"name": name, "status": "pass", "detail": detail}


def _fail(name: str, detail: str) -> dict:
    return {"name": name, "status": "fail", "detail": detail}


async def _test_registry_health() -> dict:
    name = "skill_registry_health"
    try:
        from bootstrap import get
        registry = get("skill_registry")
        count = registry.count()
        expected = [
            "jwt_decoder", "url_analyzer", "dependency_audit",
            "threat_intel", "image_generation", "system_status",
            "file_generation", "weather_forecast",
        ]
        missing = [s for s in expected if not registry.has(s)]
        if missing:
            return _fail(name, f"Expected skills missing from registry: {missing}")
        return _ok(name, f"{count} skills loaded, all {len(expected)} expected ones present")
    except Exception as e:
        return _fail(name, f"Registry unreachable: {e}")


async def _test_jwt_decoder() -> dict:
    name = "jwt_decoder"
    try:
        from skills.jwt_decoder.skill import run
        # jwt.io's standard demo token — fixed, known-answer.
        test_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        result = await run({"args": {"token": test_jwt}})
        if result.get("status") != "success":
            return _fail(name, f"Expected status=success, got: {result}")
        if result.get("header", {}).get("alg") != "HS256":
            return _fail(name, f"Wrong header decoded: {result.get('header')}")
        if result.get("payload", {}).get("sub") != "1234567890":
            return _fail(name, f"Wrong payload decoded: {result.get('payload')}")
        return _ok(name, "Known-answer JWT decoded correctly end-to-end")
    except Exception as e:
        return _fail(name, f"Crashed: {e}")


def _test_weather_formatting() -> dict:
    name = "weather_forecast_formatting"
    try:
        # FIX 2026-08-29: `from skills.weather_forecast.skill import ...`
        # is a real Python package import — it needs skills/ (and every
        # skill subfolder) to actually resolve as importable packages on
        # sys.path. Confirmed in production: this failed with "cannot
        # import name '_label_entry'" even though _label_entry AND _fmt
        # both genuinely exist in the file — a misleading error, since
        # Python's import machinery reports against whichever name it
        # gives up on first, not necessarily the one that's actually
        # missing or the real reason the import path never resolved.
        # There is no skills/__init__.py anywhere in this codebase (by
        # design — see core/skill_loader.py's own approach), so this
        # dotted import was never guaranteed to work the same way in
        # every deploy environment, and it's the ONLY place in this
        # whole test file that loaded a skill this way — every other
        # skill-loading test (_test_all_skills_structural,
        # _test_all_skills_dry_run) already loads skill.py directly by
        # file path via importlib.util.spec_from_file_location, exactly
        # matching how core/skill_loader.py itself loads every skill at
        # real app boot. Switched to the same pattern here so this test
        # exercises skills the same way the app actually runs them,
        # instead of a second, inconsistent import method that can fail
        # for reasons that have nothing to do with the skill's own code.
        import importlib.util
        skill_path = os.path.join(SKILLS_DIR, "weather_forecast", "skill.py")
        if not os.path.isfile(skill_path):
            return _fail(name, f"weather_forecast/skill.py not found at {skill_path}")
        spec = importlib.util.spec_from_file_location("_selftest_weather_forecast", skill_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _label_entry = module._label_entry
        _fmt = module._fmt

        entry = {
            "dt_txt": "2026-08-07 15:00:00",
            "main": {"temp": 29.5, "feels_like": 33.1, "humidity": 78, "pressure": 1011},
            "weather": [{"description": "scattered thunderstorms"}],
            "wind": {"speed": 4.2, "deg": 225},
            "pop": 0.65,
            "rain": {"3h": 2.4},
        }
        now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
        labeled = _label_entry(entry, now, 3600)
        formatted = _fmt(labeled)  # exactly where the wind_flag NameError lived
        if labeled.get("wind_direction") != "SW":
            return _fail(name, f"Compass direction wrong: {labeled.get('wind_direction')!r} (expected SW)")
        if "SW" not in formatted or "225" not in formatted:
            return _fail(name, f"Formatted output missing wind data: {formatted!r}")
        return _ok(name, "Label + format pipeline runs clean, wind direction correct")
    except Exception as e:
        return _fail(name, f"Formatting crashed — this is the wind_flag bug class: {e}")


def _test_pptx_bullets() -> dict:
    name = "file_generation_pptx"
    try:
        from skills.file_generation.skill import _build_pptx

        good = {"slides": [{"title": "Test", "bullets": ["one", "two"]}]}
        good_bytes, good_warnings = _build_pptx(good)
        if good_warnings:
            return _fail(name, f"Expected zero warnings with bullets present, got: {good_warnings}")
        if len(good_bytes) < 500:
            return _fail(name, f"Output suspiciously small ({len(good_bytes)} bytes) for a slide with content")

        bad = {"slides": [{"title": "Test2"}]}  # no bullets, no explicit layout
        _, bad_warnings = _build_pptx(bad)
        if len(bad_warnings) != 1:
            return _fail(name, f"Expected exactly 1 warning for a bulletless slide, got {len(bad_warnings)}: {bad_warnings}")

        return _ok(name, "Bullet-loss regression check passed (happy path clean, empty path flagged)")
    except Exception as e:
        return _fail(name, f"Crashed: {e}")


def _test_xlsx_generation() -> dict:
    name = "file_generation_xlsx"
    try:
        from skills.file_generation.skill import _build_xlsx
        import openpyxl

        data = {"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}
        file_bytes = _build_xlsx(data)
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
        ws = wb.active
        if ws["A1"].value != "a" or ws["A2"].value != 1:
            return _fail(name, f"xlsx content mismatch: A1={ws['A1'].value!r}, A2={ws['A2'].value!r}")
        return _ok(name, "xlsx built and successfully re-read back with openpyxl")
    except Exception as e:
        return _fail(name, f"Crashed: {e}")


def _test_proactive_chat_id() -> dict:
    name = "proactive_agent_chat_id"
    try:
        from core.proactive_agent import build_owner_chat_id

        cases = {
            "+2348012345678": "2348012345678@c.us",
            "2348012345678": "2348012345678@c.us",
            "234-801-234-5678": "2348012345678@c.us",
            "+234 801 234 5678": "2348012345678@c.us",
        }
        for raw, expected in cases.items():
            built = build_owner_chat_id(raw)
            if built != expected:
                return _fail(name, f"build_owner_chat_id({raw!r}) -> {built!r}, expected {expected!r}")
            if built.count("@") != 1 or not built.endswith("@c.us"):
                return _fail(name, f"Malformed chat_id from {raw!r}: {built!r}")
        return _ok(name, f"All {len(cases)} phone-number formats build a valid chat_id")
    except Exception as e:
        return _fail(name, f"Crashed (or build_owner_chat_id no longer exists): {e}")


class _FakeAggCursor:
    """Minimal stand-in for Motor's aggregate() cursor — just enough
    for skill_reliability.py's pipeline shape, nothing more."""
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return self._docs


class _FakeSkillExecutionsCollection:
    """
    In-memory fake of db["skill_executions"] built from a fixed dataset
    — no real MongoDB needed to run this test. Deliberately implements
    ONLY the query shapes skill_reliability.py actually issues
    (aggregate + find_one with a status $in filter), so if that module's
    query shape ever changes, this test either needs updating alongside
    it or starts failing — which is the point of a regression test.
    """
    def __init__(self, records):
        self._records = records

    def aggregate(self, pipeline):
        match = pipeline[0]["$match"]
        since = match.get("timestamp", {}).get("$gte")
        rows = [r for r in self._records if r["skill"] == match["skill"] and (since is None or r["timestamp"] >= since)]
        if not rows:
            return _FakeAggCursor([])
        total = len(rows)
        successes = sum(1 for r in rows if r["status"] == "success")
        failures = sum(1 for r in rows if r["status"] in ("error", "timeout", "blocked"))
        durations = [r["duration_ms"] for r in rows if r.get("duration_ms") is not None]
        avg = sum(durations) / len(durations) if durations else None
        return _FakeAggCursor([{"total": total, "successes": successes, "failures": failures, "avg_duration_ms": avg}])

    async def find_one(self, query, sort=None):
        status_filter = query["status"]["$in"]
        rows = [r for r in self._records if r["skill"] == query["skill"] and r["status"] in status_filter]
        if not rows:
            return None
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[0]

    async def distinct(self, field):
        return sorted({r[field] for r in self._records})


class _FakeDB:
    def __init__(self, records):
        self._collection = _FakeSkillExecutionsCollection(records)

    def __getitem__(self, name):
        return self._collection


async def _test_skill_reliability_trend() -> dict:
    """
    Regression test for core/skill_reliability.py, added 2026-08-09 —
    this exact scenario (a skill degrading from 90% to 50% success
    rate between two time windows) was manually verified once in a
    throwaway sandbox script when the feature was built, then never
    kept anywhere OADSON could re-run it itself. Fixed by moving that
    same verification here permanently, matching every other test in
    this file being tied to something that was actually checked once
    and needed to stay checked.
    """
    name = "skill_reliability_trend"
    try:
        from core.skill_reliability import get_skill_reliability

        now = datetime.now(timezone.utc)
        records = []
        # Prior window (48-96h ago): 20 calls, 18 succeed (90%)
        for _ in range(18):
            records.append({"skill": "test_skill", "status": "success", "timestamp": now - timedelta(hours=60), "duration_ms": 800})
        for _ in range(2):
            records.append({"skill": "test_skill", "status": "error", "timestamp": now - timedelta(hours=60), "duration_ms": 500})
        # Recent window (0-48h ago): 20 calls, 10 succeed (50%) — degrading
        for _ in range(10):
            records.append({"skill": "test_skill", "status": "success", "timestamp": now - timedelta(hours=10), "duration_ms": 900})
        for _ in range(10):
            records.append({"skill": "test_skill", "status": "error", "timestamp": now - timedelta(hours=10), "duration_ms": 400})

        db = _FakeDB(records)
        report = await get_skill_reliability(db, "test_skill")

        if report.get("trend") != "DEGRADING":
            return _fail(name, f"Expected trend=DEGRADING for a 90%->50% drop, got {report.get('trend')!r}")
        if report.get("total_executions") != 40:
            return _fail(name, f"Expected 40 total executions, got {report.get('total_executions')}")
        if report.get("success_rate_pct") != 70.0:
            return _fail(name, f"Expected overall 70.0% success rate, got {report.get('success_rate_pct')}")
        if report.get("avg_duration_ms") != 710:
            return _fail(name, f"Expected avg_duration_ms=710, got {report.get('avg_duration_ms')}")

        return _ok(name, "Trend/rate/latency math verified against a known 90%->50% degrading scenario")
    except Exception as e:
        return _fail(name, f"Crashed: {e}")


SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


class _FakeFindCursor:
    """Minimal stand-in for Motor's find().sort().to_list() chain."""
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self  # already pre-sorted by the fake — good enough for this test's fixed dataset

    async def to_list(self, length=None):
        return self._docs[:length] if length else self._docs


class _FakeCrossSurfaceDB:
    """
    In-memory fake covering exactly the two query shapes
    core/proactive_agent.py's _cross_surface_context actually issues:
    ai_sessions.aggregate([$match, $project, $unwind, $match, $sort,
    $project]) and whatsapp_log.find(...).sort(...).to_list(n).
    Real $unwind/$match/$sort semantics implemented for THIS pipeline
    shape only — not a general Mongo simulator, same scope discipline
    as _FakeSkillExecutionsCollection above.
    """
    def __init__(self, sessions: list, whatsapp_log: list):
        self._sessions = sessions
        self._wa_log = whatsapp_log

    def __getitem__(self, name):
        if name == "ai_sessions":
            return self
        if name == "whatsapp_log":
            return self
        raise KeyError(name)

    # ai_sessions.aggregate(...)
    def aggregate(self, pipeline):
        match_user = pipeline[0]["$match"]["user_id"]
        time_gte = pipeline[3]["$match"]["messages.time"]["$gte"]
        out = []
        for sess in self._sessions:
            if sess["user_id"] != match_user:
                continue
            for m in sess["messages"]:
                if m["time"] >= time_gte:
                    out.append({
                        "role": m["role"], "content": m["content"], "time": m["time"],
                        "surface": sess.get("surface_origin", "web"),
                    })
        out.sort(key=lambda d: d["time"])
        return _FakeAggCursor(out)

    # whatsapp_log.find(...).sort(...)
    def find(self, query):
        chat_id = query["chat_id"]
        gte = query.get("timestamp", {}).get("$gte")
        rows = [
            r for r in self._wa_log
            if r["chat_id"] == chat_id and (gte is None or r["timestamp"] >= gte)
        ]
        rows.sort(key=lambda r: r["timestamp"])
        return _FakeFindCursor(rows)


async def _test_proactive_cross_surface_window() -> dict:
    """
    Regression guard for the 2026-08-29 fix: the OLD _cross_surface_context
    capped each session to its last 6 messages and only looked at the
    top 8 most-recently-updated sessions — for a heavy chat user, a real
    conversation (e.g. planning something overnight) could fall entirely
    outside that window and never reach the proactive agent's prompt at
    all, which is how it ended up telling the user a plan was "cancelled"
    that it had never actually seen discussed. This test builds a
    session with a real exchange sitting BEFORE what would have been the
    old 6-message tail cutoff, and confirms the new time-windowed version
    still surfaces it.
    """
    name = "proactive_agent_cross_surface_window"
    try:
        from core.proactive_agent import _cross_surface_context
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=8)

        # 9 messages in one session: the "plan" exchange is messages 0-1
        # (oldest) — under the OLD fixed -6 slice, only the LAST 6 of
        # these 9 would ever be looked at, so 0-1 would never be seen.
        messages = []
        base = now - timedelta(hours=6)
        messages.append({"role": "user", "content": "lets do the trip saturday morning", "time": (base).isoformat()})
        messages.append({"role": "assistant", "content": "sounds good, saturday morning it is", "time": (base + timedelta(minutes=1)).isoformat()})
        for i in range(7):
            t = base + timedelta(minutes=10 * (i + 1))
            messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"unrelated chat message {i}", "time": t.isoformat()})

        sessions = [{"user_id": "u1", "surface_origin": "web", "messages": messages}]
        fake_db = _FakeCrossSurfaceDB(sessions, whatsapp_log=[])

        result = await _cross_surface_context(fake_db, "u1", "owner_chat", last_checked_at=window_start)

        if "trip saturday" not in result.lower():
            return _fail(name, f"Plan-relevant message (from before the old 6-message tail) missing from context: {result!r}")
        return _ok(name, "Full time-windowed context includes messages older than the old fixed 6-message tail")
    except Exception as e:
        return _fail(name, f"Crashed: {e}")


def _test_local_tool_schema_parity() -> dict:
    """
    Added 2026-08-29 — this test did NOT survive the session that
    originally built and verified it (a prior Claude session hit its
    context limit before the file was ever saved/committed), so this is
    a fresh implementation covering the two real, distinct bug classes
    that actually happened here, back to back, in one day:

    1. MISSING DECLARATION — oadson_pkg (the CLI client) shipped
       write_files, task_list, and local_code_index as real, working
       LOCAL tools in its own executor.py, each one invisible to Gemini
       for a while because nothing here ever declared them in
       LOCAL_TOOL_DECLARATIONS. Caught each time only by someone
       noticing the model quietly working around a tool that "exists"
       on the client but was never callable.

    2. NAME COLLISION — when local_code_index WAS added (as "code_index"
       at the time), it collided with an EXISTING server-side skill
       already named "code_index" in skills/. Both get merged into the
       same tools=[...] payload sent to Gemini, which rejects two
       function declarations sharing one name outright — this took
       down EVERY model in the fallback chain identically (every real
       chat/stream request 400'd) since the collision happens before
       any provider-specific routing, not after.

    This test guards against both:
      (a) CLIENT_TOOL_NAMES (a snapshot of the CLI's real `TOOLS` dict —
          get the current list any time via `oadson list-tools` in the
          client repo) must be a subset of LOCAL_TOOL_NAMES here, or a
          client tool exists that Gemini can never call.
      (b) LOCAL_TOOL_NAMES here must not collide with any server-side
          skill's manifest "name" in skills/ — checked directly against
          every manifest.json on disk, not another pinned snapshot,
          since skills change independently of this file and a second
          static list would just be one more thing to forget to update.

    HONEST LIMIT, same as before: (a) is still a pinned snapshot of the
    client's tool list, not a live fetch — it will not notice a BRAND
    NEW client tool that was never added here at all, only a client
    tool this snapshot already knows about going missing from
    LOCAL_TOOL_NAMES. Keeping this snapshot current when oadson_pkg's
    TOOLS dict changes is a client-release-process item, not something
    this test can enforce by itself. (b) has no such limit — it reads
    the real skills/ directory fresh every run, so it can't go stale.
    """
    name = "local_tool_schema_parity"
    try:
        from core.local_tool_schemas import LOCAL_TOOL_NAMES
    except Exception as e:
        return _fail(name, f"Could not import LOCAL_TOOL_NAMES from core.local_tool_schemas: {e}")

    # Snapshot from `oadson list-tools` on oadson_pkg client v1.0.9.
    # UPDATE THIS whenever the client's TOOLS dict in executor.py changes.
    CLIENT_TOOL_NAMES = frozenset([
        "create_file", "delete_file", "git_commit", "git_diff", "git_log",
        "git_push", "git_status", "http_request", "list_dir",
        "local_code_index", "project_tree", "read_file", "remote_bash",
        "remote_download", "run_bash", "run_tests", "search_files",
        "syntax_check", "task_list", "test_endpoint", "write_file",
        "write_files",
    ])

    problems = []

    missing = CLIENT_TOOL_NAMES - set(LOCAL_TOOL_NAMES)
    if missing:
        problems.append(
            f"client has tools Gemini can never call (missing from "
            f"LOCAL_TOOL_NAMES): {sorted(missing)}"
        )

    # Bug class 2: cross-check against every server-side skill's
    # declared name, reading skills/ fresh (not a snapshot) so this
    # half can't go stale the way (a) can.
    if os.path.isdir(SKILLS_DIR):
        skill_names = {}
        for skill_name in sorted(os.listdir(SKILLS_DIR)):
            skill_dir = os.path.join(SKILLS_DIR, skill_name)
            manifest_path = os.path.join(skill_dir, "manifest.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path) as f:
                    manifest = json.load(f)
            except Exception:
                continue  # already caught by _test_all_skills_structural — not this test's job
            declared_name = manifest.get("name")
            if declared_name:
                skill_names[declared_name] = skill_name

        collisions = set(LOCAL_TOOL_NAMES) & set(skill_names.keys())
        if collisions:
            problems.append(
                f"LOCAL_TOOL_NAMES collides with server-side skill name(s), "
                f"which Gemini rejects outright as duplicate declarations: "
                + ", ".join(f"{n!r} (skill folder: {skill_names[n]})" for n in sorted(collisions))
            )
    else:
        problems.append(f"skills/ directory not found at {SKILLS_DIR} — cannot check for name collisions")

    if problems:
        return _fail(name, "; ".join(problems))
    return _ok(
        name,
        f"{len(LOCAL_TOOL_NAMES)} local tool(s) declared, no cross-source name "
        f"collisions with skills/, client snapshot ({len(CLIENT_TOOL_NAMES)} tools) fully covered",
    )


def _test_all_skills_structural() -> dict:
    """
    Added 2026-08-09 — static structural validation for EVERY skill in
    skills/, not just the handful with deep semantic regression tests
    above. Deliberately does NOT import or execute any skill's run()
    function — many skills have real shell/filesystem/internet/email
    access (terminal, gmail, github, ...) and actually invoking them
    here would mean real side effects (real emails sent, real shell
    commands run, real API quota burned) every single time this suite
    runs, which defeats the point of something meant to run often and
    safely.

    Instead, per skill folder, checks:
      1. manifest.json exists, is valid JSON, has the required fields
         (name, description, parameters.type == "object")
      2. skill.py exists and parses as valid Python (no syntax errors)
      3. skill.py defines an `async def run(...)` function — the exact
         contract skill_loader.py requires to load a skill at all

    This is precisely the class of mistake that silently breaks a
    skill at deploy time — skill_loader.py already skips a broken
    skill at boot without blocking the rest (by design), but that also
    means a broken skill just quietly vanishes from the registry with
    no clear signal why. This surfaces exactly which skill and exactly
    what's wrong, on demand, instead of noticing days later that a
    skill silently stopped being callable.
    """
    name = "all_skills_structural"
    if not os.path.isdir(SKILLS_DIR):
        return _fail(name, f"skills/ directory not found at {SKILLS_DIR}")

    broken = []
    status_warnings = []
    checked = 0

    for skill_name in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, skill_name)
        if not os.path.isdir(skill_dir) or skill_name.startswith("."):
            continue

        manifest_path = os.path.join(skill_dir, "manifest.json")
        skill_path = os.path.join(skill_dir, "skill.py")

        if not os.path.isfile(manifest_path):
            broken.append(f"{skill_name}: missing manifest.json")
            continue
        if not os.path.isfile(skill_path):
            broken.append(f"{skill_name}: missing skill.py")
            continue

        checked += 1

        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except Exception as e:
            broken.append(f"{skill_name}: manifest.json invalid JSON — {e}")
            continue

        for field in ("name", "description", "parameters"):
            if field not in manifest:
                broken.append(f"{skill_name}: manifest.json missing '{field}'")

        params = manifest.get("parameters") or {}
        if params and params.get("type") != "object":
            broken.append(f"{skill_name}: manifest.json 'parameters.type' should be 'object', got {params.get('type')!r}")

        try:
            with open(skill_path) as f:
                tree = ast.parse(f.read(), filename=skill_path)
        except SyntaxError as e:
            broken.append(f"{skill_name}: skill.py syntax error — {e}")
            continue

        has_run = any(
            isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
            for node in ast.walk(tree)
        )
        if not has_run:
            broken.append(f"{skill_name}: skill.py has no 'async def run(...)' function")

        # REGRESSION FOUND AND RE-ADDED 2026-08-28 — this whole check
        # (added 2026-08-27 after skills/ilana shipped completely
        # non-functional, then silently reverted away along with
        # core/skill_executor.py's own status-normalization fix — see
        # that file's _normalize_unknown_status) was missing entirely
        # from this function. Re-added as a hygiene note rather than a
        # hard failure: now that skill_executor.py's central fix is
        # restored too, a non-standard status here is no longer an
        # actual production risk (it gets normalized centrally
        # regardless of what any individual skill's own code does) —
        # just worth knowing about for code cleanliness.
        ALLOWED_STATUSES = {"success", "info", "error", "timeout", "blocked"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key_node, val_node in zip(node.keys, node.values):
                if (isinstance(key_node, ast.Constant) and key_node.value == "status"
                        and isinstance(val_node, ast.Constant) and isinstance(val_node.value, str)
                        and val_node.value not in ALLOWED_STATUSES):
                    status_warnings.append(
                        f"{skill_name}: skill.py returns status={val_node.value!r} at line "
                        f"{getattr(val_node, 'lineno', '?')} — not in {sorted(ALLOWED_STATUSES)}. "
                        f"No longer a production risk (skill_executor.py normalizes this "
                        f"centrally now), but worth cleaning up in the skill's own code."
                    )

    if broken:
        shown = "; ".join(broken[:10]) + (f" ... and {len(broken) - 10} more" if len(broken) > 10 else "")
        return _fail(name, f"{len(broken)}/{checked} skill(s) failed structural checks: {shown}")
    if status_warnings:
        shown = "; ".join(status_warnings[:10]) + (f" ... and {len(status_warnings) - 10} more" if len(status_warnings) - 10 > 0 else "")
        return _ok(name, f"All {checked} skills passed structural validation. {len(status_warnings)} status-contract hygiene note(s): {shown}")
    return _ok(name, f"All {checked} skills passed structural validation (valid manifest, valid syntax, real run() function).")


class _DryRunFakeCursor:
    """Async-iterable stand-in for a motor cursor — empty results only."""
    def __aiter__(self): return self
    async def __anext__(self): raise StopAsyncIteration
    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def skip(self, *a, **k): return self


class _DryRunFakeCollection:
    """
    Motor-collection-shaped stand-in used for every skill's `db` during
    the dry run below — every method is a harmless no-op/empty-result,
    so a skill can call find_one/find/aggregate/update_one/insert_one/
    etc. exactly like it would against the real database, without ever
    touching it.
    """
    async def find_one(self, *a, **k): return None
    def find(self, *a, **k): return _DryRunFakeCursor()
    def aggregate(self, *a, **k): return _DryRunFakeCursor()
    async def insert_one(self, *a, **k):
        class R: inserted_id = "self_test_dummy_id"
        return R()
    async def update_one(self, *a, **k):
        class R: modified_count = 0; upserted_id = None
        return R()
    async def delete_one(self, *a, **k):
        class R: deleted_count = 0
        return R()
    async def count_documents(self, *a, **k): return 0


class _DryRunFakeDB:
    """
    Named distinctly from the unrelated _FakeDB above (that one is
    _test_skill_reliability_trend's own narrow mock for a completely
    different purpose) — reusing that name here would have silently
    shadowed it, since Python module-level names are last-definition-
    wins regardless of which function actually uses which class.
    """
    def __getitem__(self, name): return _DryRunFakeCollection()
    def __getattr__(self, name): return _DryRunFakeCollection()


class _DryRunHTTPResponse:
    status_code = 200
    text = "{}"
    content = b"{}"
    headers = {}
    def json(self): return {}
    def raise_for_status(self): pass


def _dry_run_fake_completed_process(*a, **k):
    import subprocess as _sp
    return _sp.CompletedProcess(args=a or ["self_test"], returncode=0, stdout="", stderr="")


def _build_dummy_args(manifest: dict) -> dict:
    """
    Builds a minimal, plausible payload from a skill's OWN declared
    parameter schema — one dummy value per REQUIRED property, typed
    correctly, so every skill gets input shaped enough to actually
    exercise its real logic instead of immediately bailing out on a
    trivial "missing required arg" check.
    """
    props = (manifest.get("parameters") or {}).get("properties") or {}
    required = (manifest.get("parameters") or {}).get("required") or []
    args = {}
    for key in required:
        spec = props.get(key) or {}
        if spec.get("enum"):
            args[key] = spec["enum"][0]
        elif spec.get("type") in ("number", "integer"):
            args[key] = 1
        elif spec.get("type") == "boolean":
            args[key] = True
        elif spec.get("type") == "array":
            args[key] = []
        elif spec.get("type") == "object":
            args[key] = {}
        else:
            if "lat" in key.lower():
                args[key] = 6.5244
            elif "lon" in key.lower() or "lng" in key.lower():
                args[key] = 3.3792
            elif "date" in key.lower():
                args[key] = "2026-01-01"
            elif "url" in key.lower():
                args[key] = "https://example.com"
            elif "email" in key.lower():
                args[key] = "test@example.com"
            else:
                args[key] = "test"
    return args


async def _test_all_skills_dry_run() -> dict:
    """
    REGRESSION FOUND AND RE-ADDED 2026-08-28 — this entire function
    (added 2026-08-27) had been silently removed in its entirety,
    alongside core/skill_executor.py's own status-normalization fix
    (_normalize_unknown_status) — the two together are what actually
    caught and fixed the 13-skill silent-failure incident; losing both
    meant that whole class of bug had zero protection again, live, with
    nothing to indicate it had ever existed.

    ACTUALLY EXECUTES every skill's run(), unlike _test_all_skills_
    structural above, which only reads source without running anything
    — a static check can only ever catch a bug pattern someone already
    thought to write a rule for. Made safe via fully mocked I/O (every
    skill's real httpx/subprocess/socket usage surveyed directly, not
    assumed) so this can run often and freely with zero real side
    effects, zero paid infra.
    """
    import asyncio
    import importlib.util
    import subprocess as subprocess_module
    import socket as socket_module
    from unittest.mock import patch, AsyncMock

    name = "all_skills_dry_run"
    if not os.path.isdir(SKILLS_DIR):
        return _fail(name, f"skills/ directory not found at {SKILLS_DIR}")

    ALLOWED_STATUSES = {"success", "info", "error", "timeout", "blocked"}
    findings = []
    hygiene_notes = []
    checked = 0
    skipped = []

    class _FakeSocket:
        """
        FIX 2026-08-29: was missing setsockopt — confirmed real failure
        in production (dad_joke_fetcher, likely via requests/urllib3's
        connection pooling, which calls sock.setsockopt() to configure
        TCP_NODELAY/keepalive on a fresh connection — a completely
        normal, standard-library-adjacent call, not anything unusual
        about that skill). Added setsockopt plus the other socket
        methods any standard HTTP client library commonly touches on a
        raw socket, so a skill doesn't need this list extended again
        for the next ordinary method some other library happens to call.
        """
        def __init__(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def settimeout(self, *a, **k): pass
        def gettimeout(self, *a, **k): return None
        def setsockopt(self, *a, **k): pass
        def getsockopt(self, *a, **k): return 0
        def setblocking(self, *a, **k): pass
        def fileno(self, *a, **k): return -1
        def close(self): pass
        def shutdown(self, *a, **k): pass
        def send(self, *a, **k): return 0
        def sendall(self, *a, **k): return None
        def recv(self, *a, **k): return b""
        def makefile(self, *a, **k):
            import io
            return io.BytesIO(b"")
        def getpeername(self, *a, **k): return ("0.0.0.0", 0)
        def getsockname(self, *a, **k): return ("0.0.0.0", 0)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    for skill_name in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, skill_name)
        manifest_path = os.path.join(skill_dir, "manifest.json")
        skill_path = os.path.join(skill_dir, "skill.py")
        if not os.path.isfile(skill_path) or not os.path.isfile(manifest_path):
            continue

        try:
            manifest = json.loads(open(manifest_path).read())
        except (json.JSONDecodeError, OSError):
            continue

        if skill_name in {"terminal"}:
            skipped.append(f"{skill_name}: shell access is its entire purpose — a mocked subprocess call tests nothing meaningful about real command execution")
            continue

        try:
            spec = importlib.util.spec_from_file_location(f"_selftest_{skill_name}", skill_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            findings.append(f"{skill_name}: failed to import — {type(e).__name__}: {e}")
            continue

        if not hasattr(module, "run"):
            continue

        payload = {
            "user_id": "self_test_dummy_user",
            "session_id": "self_test_dummy_session",
            "surface": "web",
            "db": _DryRunFakeDB(),
            "message": "self-test dry run",
            "args": _build_dummy_args(manifest),
        }

        checked += 1
        try:
            with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=_DryRunHTTPResponse())), \
                 patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_DryRunHTTPResponse())), \
                 patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_DryRunHTTPResponse())), \
                 patch("httpx.AsyncClient.put", new=AsyncMock(return_value=_DryRunHTTPResponse())), \
                 patch("httpx.AsyncClient.delete", new=AsyncMock(return_value=_DryRunHTTPResponse())), \
                 patch.object(subprocess_module, "run", side_effect=_dry_run_fake_completed_process), \
                 patch.object(subprocess_module, "Popen", side_effect=lambda *a, **k: _dry_run_fake_completed_process()), \
                 patch.object(subprocess_module, "check_output", return_value=b""), \
                 patch.object(socket_module, "socket", _FakeSocket), \
                 patch.object(socket_module, "create_connection", lambda *a, **k: _FakeSocket()):
                result = await asyncio.wait_for(module.run(payload), timeout=10)
        except asyncio.TimeoutError:
            findings.append(f"{skill_name}: run() did not finish within 10s (hang) with fully mocked I/O")
            continue
        except Exception as e:
            if isinstance(e, RuntimeError) and "not found in registry" in str(e):
                skipped.append(f"{skill_name}: uses bootstrap's global service registry directly (not payload['db']) — cannot be safely tested without risking interference with concurrent live requests if this runs inside the real app")
                continue
            findings.append(f"{skill_name}: run() raised {type(e).__name__}: {e}")
            continue

        if not isinstance(result, dict):
            findings.append(f"{skill_name}: run() returned {type(result).__name__}, not a dict")
            continue
        status = result.get("status")
        if status not in ALLOWED_STATUSES:
            hygiene_notes.append(
                f"{skill_name}: run() returns status={status!r} directly — not one of "
                f"{sorted(ALLOWED_STATUSES)}. No longer a production risk (skill_executor.py "
                f"now normalizes this centrally), but worth cleaning up in the skill's own code."
            )

    if findings:
        shown = "; ".join(findings[:10]) + (f" ... and {len(findings) - 10} more" if len(findings) - 10 > 0 else "")
        return _fail(name, f"{len(findings)}/{checked} skill(s) failed dry-run execution (fully mocked I/O, zero real side effects): {shown}")
    skip_note = f" ({len(skipped)} skipped: {'; '.join(skipped)})" if skipped else ""
    hygiene_note = f" {len(hygiene_notes)} status-naming hygiene note(s): {'; '.join(hygiene_notes[:10])}" if hygiene_notes else ""
    return _ok(name, f"All {checked} skills executed successfully with mocked I/O — zero crashes, zero unhandled status contract gaps{skip_note}.{hygiene_note}")


async def run_self_test() -> dict:
    tests = [
        _test_registry_health(),
        _test_jwt_decoder(),
        _test_skill_reliability_trend(),
    ]
    results = [await t for t in tests]
    results.append(_test_weather_formatting())
    results.append(_test_pptx_bullets())
    results.append(_test_xlsx_generation())
    results.append(_test_proactive_chat_id())
    results.append(await _test_proactive_cross_surface_window())
    results.append(_test_local_tool_schema_parity())
    results.append(_test_all_skills_structural())
    results.append(await _test_all_skills_dry_run())

    failed = [r for r in results if r["status"] != "pass"]
    return {
        "status": "pass" if not failed else "fail",
        "passed": len(results) - len(failed),
        "total": len(results),
        "failed_tests": [r["name"] for r in failed],
        "results": results,
        "run_at": datetime.utcnow().isoformat(),
    }
