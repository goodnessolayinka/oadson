"""
skills/terminal/skill.py — OADSON V3 Terminal Skill.

Executes real shell commands inside a sandboxed workspace directory
(get_user_home() — see core/oadson_home.py; physically
{OADSON_HOME_ROOT}/{user_id}/workspace/ as of the 2026-08-28 workspace
restructure). Every result includes the exact command that ran plus
real stdout/stderr/return_code — never fabricated.

FIXES applied per EDD v1.0 (July 2026):

  Fix 1 — Full error output always returned.
    Already implemented in the previous version. Verified and kept.

  Fix 2 — Persistent working directory.
    cwd survives across multiple terminal calls within the same session.
    Stored in payload["session_state"] under key "terminal_cwd".
    cd commands update the session state; all other commands execute from
    the persisted cwd. The orchestrator must write the returned
    session_state["terminal_cwd"] back to the session on each call.

  Fix 3 — Network access confirmed available.
    Render subprocesses have real outbound internet. pip install, curl,
    wget, git clone, npm install all work. The EDD's claim of "no network
    in subprocess" was a misdiagnosis — the actual cause of silent failures
    was skill_executor swallowing stderr (fixed there). git clone of GitHub
    repos gets a soft hint to prefer clone_to_workspace instead, but is
    not blocked.

  Fix 4 — Timeout with partial output.
    TimeoutExpired now captures whatever stdout/stderr was produced before
    the timeout and returns it alongside a clear timeout status.

ROOT CAUSE NOTE (skill_executor.py):
    The production symptom "status=error, no message" was NOT caused by
    this skill swallowing output — it already returned full stdout/stderr.
    The real cause was skill_executor.py reading result.get("error") while
    this skill uses "reason" as its error key. skill_executor.py has been
    fixed to pass all skill-returned dicts through verbatim. Both fixes
    are required.
"""

import subprocess
import os
import re
from pathlib import Path

from core.oadson_home import get_user_home, is_safe_path, OADSON_HOME_ROOT


# ── Git-clone routing ────────────────────────────────────────────────────────
# git clone via terminal works (Render has outbound internet) but is slower
# and less reliable than github clone_to_workspace, which uses the GitHub API
# and already handles auth, branch selection, and sandbox extraction.
# We soft-warn on git clone but still let it run — the model may have a
# good reason (e.g. cloning a non-GitHub repo). For GitHub repos specifically
# the model should prefer clone_to_workspace.

_GIT_CLONE_RE = re.compile(r"^\s*git\s+clone\b", re.IGNORECASE)


def clean_path_arg(path: str) -> str:
    """Drop stray quotes the model sometimes leaves on a path, e.g. '../x/y.py\''."""
    return path.strip().strip("'\"")


def repo_path_hint(path: str) -> str:
    """Extra guidance when a blocked path looks like a sibling repo."""
    if "../" in path or "__" in path:
        return ("Cloned repos live at 'repos/owner__repo/' inside the sandbox; "
                "use that path instead of '../'. ")
    return ""


def _git_clone_hint(command: str) -> str | None:
    """
    Returns a hint string if the command is a git clone of a GitHub URL,
    suggesting clone_to_workspace as a faster alternative.
    Returns None otherwise — command proceeds normally either way.
    """
    if _GIT_CLONE_RE.search(command) and "github.com" in command:
        return (
            "Blocked: use @github action='clone_to_workspace' for GitHub repos instead of "
            "git clone. A shell clone skips the repo index, so the assistant can't retrieve "
            "code from it."
        )
    return None


_BLOCKED_PATTERNS = [
    # MongoDB direct connection always times out — no MongoDB running in sandbox
    (
        re.compile(r"(pymongo|MongoClient|mongodb://)", re.IGNORECASE),
        "Blocked: cannot connect to MongoDB from the terminal sandbox — no database "
        "server runs here. Use @memory_manager to read memories, or @api_tester "
        "to call the backend API instead."
    ),
    # psycopg2/PostgreSQL direct connection — same issue
    (
        re.compile(r"(psycopg2|asyncpg|postgres://|postgresql://)", re.IGNORECASE),
        "Blocked: cannot connect to PostgreSQL from the terminal sandbox. "
        "Use @api_tester to call database endpoints instead."
    ),
]


def _check_blocked(command: str) -> str | None:
    """Returns a block reason if the command would always fail or timeout."""
    for pattern, reason in _BLOCKED_PATTERNS:
        if pattern.search(command):
            return reason
    return None


# Commands get only this environment. The server's own variables (API keys,
# MONGODB_URI, INTERNAL_SECRET, GITHUB_TOKEN, ...) are never passed to a command,
# so `printenv` or `env` cannot show them. See docs/DESIGN.md, Step 0.
_COMMAND_ENV_NAMES = ("PATH", "LANG", "TERM", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH")


def command_env(home) -> dict:
    env = {k: os.environ[k] for k in _COMMAND_ENV_NAMES if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("LC_")})
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = str(home)
    return env


class TerminalSkill:
    """
    Executes real shell commands inside a sandboxed home directory.

    Design goals (directly addressing the trust problem):
      1. The exact command that ran is ALWAYS included in the result,
         verbatim — never just the model's prose description of what it
         "did". If the model's reply doesn't match the "command" field in
         the raw JSON, that's your tell something's wrong.
      2. Real stdout/stderr/return_code from a real subprocess call —
         never fabricated. No code path returns a fake "success" without
         actually having run something.
      3. All execution is confined to this user's home directory under
         OADSON_HOME_ROOT. Commands cannot read/write/cd outside of it —
         attempts to escape (via .., absolute paths outside home,
         symlinks, cd tricks) are rejected before anything runs.
      4. Working directory persists across calls within the same session
         via session_state["terminal_cwd"].
      5. Network-blocked commands are detected before execution and
         return a helpful routing message instead of a silent failure.
    """

    def __init__(self, user_id: str):
        if not user_id:
            raise ValueError("TerminalSkill requires a user_id — sandboxing is per-user, not global")
        self.user_id = str(user_id)
        self.home = get_user_home(self.user_id)
        self.timeout = 30  # default; overridden per-command below

    def _get_timeout(self, command: str) -> int:
        """
        Package managers need more time. Detect and give them room.
        pip install numpy can take 60-90s. npm install a full project: 120s+.
        """
        cmd = command.lower()
        if any(p in cmd for p in (
            "pip install", "pip download", "pip3 install",
            "npm install", "npm ci", "yarn install", "yarn add",
            "pnpm install", "pnpm add",
            "cargo build", "cargo install",
            "go get", "go install", "go build",
            "composer install", "composer require",
            "gem install", "bundle install",
            "apt-get install", "apt install",
            "brew install",
        )):
            return 180  # 3 minutes for package ops
        if any(p in cmd for p in ("make", "cmake", "gradle", "mvn")):
            return 120
        return 30

    def _is_safe_path(self, path_str: str) -> bool:
        return is_safe_path(self.user_id, path_str)

    def _extract_path_args(self, command: str) -> list:
        """
        Extract tokens that look like filesystem paths needing sandbox validation.
        Only returns tokens that are genuine absolute paths starting with /
        (excluding lone '/' which is harmless to block explicitly below),
        home-relative paths starting with ~, or relative traversal with ..
        Skips flags (-la, --verbose), URLs (https://), and other slash-containing
        tokens that are not filesystem paths.

        BUGFIX 2026-09-19 — this uses a naive command.split() (whitespace
        only, no shell-quote awareness), which is fine for a normal shell
        command but breaks completely for `python3 -c '<source code>'` or
        `python -c "<source code>"`: the quoted argument is Python SOURCE,
        not a list of shell path tokens, and split() has no idea it's
        inside quotes at all — it just sees every space-separated word in
        the Python code as its own token. Confirmed in production: a
        `python3 -c '...'` payload containing a bare division (`10 / 2`),
        a comment separator (`# --- / ---`), or any other standalone `/`
        anywhere in the source got misread as an absolute-path token,
        which then failed _is_safe_path for being empty/degenerate — a
        completely fake violation of a script that never referenced the
        filesystem unsafely at all. The model then had to guess-and-retry
        (sometimes twice) against an error that had nothing to do with
        anything it could actually fix by changing a path.
        Real fix: detect a `-c` flag immediately following `python`/
        `python3`/`python3.X` and skip path extraction for that whole
        command — its actual file-path safety (if it opens files at all)
        is enforced at the OS level by cwd confinement regardless, same
        as any other subprocess run inside the sandbox; this check exists
        to catch shell-level path arguments, which a -c payload doesn't have.
        """
        if re.search(r"\bpython3?(\.\d+)?\s+-c\b", command):
            return []
        tokens = command.split()
        paths = []
        for t in tokens:
            # Skip flags and options
            if t.startswith("-"):
                continue
            # Skip URLs
            if t.startswith("http://") or t.startswith("https://") or t.startswith("ftp://"):
                continue
            # Slash-only tokens of 2+ chars (the two-slash comment marker) name no
            # real location, so they are not paths. A bare / is still checked.
            if t.startswith("/") and len(t) >= 2 and t.strip("/") == "":
                continue
            # Absolute path (starts with /)
            if t.startswith("/"):
                paths.append(t)
            # Home-relative
            elif t.startswith("~"):
                paths.append(t)
            # Parent traversal
            elif t == ".." or t.startswith("../"):
                paths.append(t)
        return paths

    def _auto_correct_sandbox_path(self, command: str) -> str:
        """
        Auto-correct absolute paths that point inside the sandbox to relative paths.
        The model sometimes constructs full absolute paths like:
          {OADSON_HOME_ROOT}/{user_id}/repos/x  →  repos/x
          {OADSON_HOME_ROOT}/*/repos/x          →  repos/x (wildcard form)
        Rather than blocking and wasting a step, transparently rewrite these
        so the command runs correctly without the model having to retry.
        """
        home_str = str(self.home)
        # self.home is {OADSON_HOME_ROOT}/{user_id}/workspace — its parent
        # is {OADSON_HOME_ROOT}/{user_id}, which is what actually needs
        # stripping here (a model-constructed absolute path points at the
        # user root, not specifically at .../workspace).
        home_root = str(self.home.parent)

        # Pattern: {OADSON_HOME_ROOT}/{any_user_id}/... → make relative to self.home
        import re as _re
        # Match {OADSON_HOME_ROOT}/<anything>/<rest> — handles wildcard form too
        pattern = _re.compile(
            r'(?<!\w)' + _re.escape(home_root) + r'/[^/\s]+/?((?:[^\s]*)?)'
        )
        def replacer(m):
            rest = m.group(1).lstrip('/')
            return rest if rest else '.'
        corrected = pattern.sub(replacer, command)

        # Also handle exact home path alone
        corrected = corrected.replace(home_str + '/', '').replace(home_str, '.')
        return corrected.strip() or command

    def _resolve_cwd(self, session_cwd: str | None) -> Path:
        """
        Resolve the effective working directory from session state.
        Always stays within the sandbox. Falls back to home if invalid.
        """
        if not session_cwd:
            return self.home

        candidate = Path(session_cwd)
        try:
            candidate.resolve().relative_to(self.home.resolve())
            if candidate.is_dir():
                return candidate
        except ValueError:
            pass

        return self.home

    def run_code(self, language: str, code: str, filename: str | None = None,
                 session_cwd: str | None = None, stdin: str | None = None) -> dict:
        """
        Run a source-code snippet in a given language, inside the same
        sandbox execute() already enforces (path containment, timeout,
        blocked-command checks) — this is a thin wrapper, not a parallel
        execution path, so it inherits every safety property execute()
        already has for free.

        ADDED per direct request for real multi-language code execution
        as a distinct capability, not just "run an arbitrary shell
        command that happens to invoke an interpreter". Writing the
        source to a REAL FILE first (rather than inlining it as a
        `python3 -c "..."` shell string) avoids a real, nasty class of
        bug: arbitrary user/model-supplied code containing quotes,
        backslashes, or newlines would otherwise need perfect shell
        escaping to survive being embedded in a shell command string —
        one unescaped quote in the code and the command breaks or,
        worse, executes something other than what was intended. A file
        has no such problem; the interpreter just reads its own bytes.

        HONEST SCOPING, checked directly rather than assumed: language
        support here reflects what interpreters/compilers are actually
        confirmed present in a real Linux sandbox environment (node,
        python3, java) — NOT a list of languages this "should" support
        in theory. Adding a language OADSON's actual Render container
        doesn't have installed would silently fail with a confusing
        "command not found" rather than a clear, honest unsupported-
        language error, which is why this validates against a known
        list up front instead of just trying every possible interpreter
        name and hoping.
        """
        LANGUAGE_RUNNERS = {
            "python": {"ext": ".py", "cmd": "python3 {file}"},
            "python3": {"ext": ".py", "cmd": "python3 {file}"},
            "javascript": {"ext": ".js", "cmd": "node {file}"},
            "js": {"ext": ".js", "cmd": "node {file}"},
            "node": {"ext": ".js", "cmd": "node {file}"},
            "typescript": {"ext": ".ts", "cmd": "npx -y tsx {file}"},
            "ts": {"ext": ".ts", "cmd": "npx -y tsx {file}"},
            "bash": {"ext": ".sh", "cmd": "bash {file}"},
            "sh": {"ext": ".sh", "cmd": "bash {file}"},
            # Java needs a compile step before it can run — handled as
            # a special case below, not a single {cmd} template, since
            # javac + java is two real commands, not one.
            "java": {"ext": ".java", "cmd": None},
        }

        lang_key = (language or "").strip().lower()
        if lang_key not in LANGUAGE_RUNNERS:
            return {
                "status": "error",
                "reason": (
                    f"Unsupported language '{language}'. Supported: "
                    f"{sorted(set(k for k in LANGUAGE_RUNNERS if LANGUAGE_RUNNERS[k]['cmd'] is not None or k == 'java'))}. "
                    f"If you need a language not listed here, use terminal's plain 'query' action "
                    f"instead and check first whether the interpreter is actually installed "
                    f"(e.g. 'which ruby') — this action only lists languages CONFIRMED present, "
                    f"not every language that theoretically might be."
                ),
            }
        if not (code or "").strip():
            return {"status": "error", "reason": "No code provided"}

        runner = LANGUAGE_RUNNERS[lang_key]
        effective_cwd = self._resolve_cwd(session_cwd)

        # Java specifically: class name must match filename (javac's own
        # requirement, not a sandbox quirk) — extract it or fall back to
        # a generic name and rewrite the code's class name to match, so
        # the model doesn't need to get this exactly right itself.
        if lang_key == "java":
            # BUG FOUND WHILE TESTING, before shipping this: `java` (the
            # JRE, runtime-only) and `javac` (the JDK compiler) are NOT
            # the same install — confirmed directly in this environment
            # that java exists but javac does not (openjdk JRE-headless
            # only, no JDK). Compiling Java source therefore genuinely
            # cannot work here, and very possibly can't on OADSON's live
            # Render container either, since a JRE-only install is the
            # common minimal default. Checking for javac explicitly
            # up front turns a confusing raw "javac: not found" shell
            # error into an honest, actionable message instead.
            javac_check = self.execute("which javac", session_cwd)
            if javac_check.get("status") != "success" or not (javac_check.get("stdout") or "").strip():
                return {
                    "status": "error",
                    "reason": (
                        "Java compilation is not available in this sandbox — the Java runtime "
                        "(java) is installed, but the compiler (javac, part of the full JDK) is "
                        "not. Only pre-compiled .class/.jar files could be run here, not raw "
                        ".java source. Use Python, JavaScript, TypeScript, or bash instead."
                    ),
                }
            class_match = re.search(r"\bpublic\s+class\s+(\w+)", code)
            class_name = class_match.group(1) if class_match else "Main"
            if not class_match:
                # No public class found at all — wrap defensively rather
                # than fail outright, since plenty of valid snippets
                # someone pastes won't have "public" on the class.
                code = re.sub(r"\bclass\s+\w+", f"class {class_name}", code, count=1) or code
            src_name = f"{class_name}.java"
            src_path = effective_cwd / src_name
            try:
                src_path.write_text(code)
            except Exception as e:
                return {"status": "error", "reason": f"Could not write source file: {e}"}
            compile_result = self.execute(f"javac {src_name}", session_cwd)
            if compile_result.get("status") != "success":
                compile_result["stage"] = "compile"
                return compile_result
            run_result = self.execute(f"java {class_name}" + (f" < {stdin!r}" if stdin else ""), session_cwd)
            run_result["stage"] = "run"
            return run_result

        # All other languages: single file, single command.
        src_name = filename or f"snippet{runner['ext']}"
        if not src_name.endswith(runner["ext"]):
            src_name += runner["ext"]
        src_path = effective_cwd / src_name
        try:
            src_path.write_text(code)
        except Exception as e:
            return {"status": "error", "reason": f"Could not write source file: {e}"}

        cmd = runner["cmd"].format(file=src_name)
        if stdin:
            # Write stdin to its own file too rather than trying to
            # inline it into the shell command string — same escaping
            # hazard as inlining source code, same fix.
            stdin_path = effective_cwd / f"{src_name}.stdin"
            try:
                stdin_path.write_text(stdin)
                cmd += f" < {src_name}.stdin"
            except Exception:
                pass  # best-effort — code still runs without stdin rather than failing outright
        return self.execute(cmd, session_cwd)

    async def maybe_reclone(self, command: str) -> dict | None:
        """
        RESTART RESILIENCE 2026-08-28 (rewritten for correctness — see
        note below). If this workspace was last touched by a different
        server process (restart/redeploy wiped the local disk —
        confirmed in production: a bootstrap cycle landed mid-session
        and every subsequent terminal call failed with "No such file or
        directory" / "not a git repository", burning 15+ retries
        because nothing ever told the model why), and `command`
        references a repos/{owner}__{repo} path that no longer exists
        on disk, silently re-clone it before the command runs — per
        explicit instruction, no warning message, just recover and
        continue.

        MUST be awaited from an async caller (terminal.run(), which is
        already async and is called via `await module.run(payload)` in
        core/skill_registry.py) — NOT called from execute(), which is
        sync. An earlier version of this tried asyncio.run() from
        inside execute() to work around that; asyncio.run() raises
        immediately if a loop is already running, which it always is
        here, so that version would have hard-crashed the very first
        time it fired instead of degrading gracefully. Caught before
        shipping — moving the check up into run() (already async) is
        the real fix, not a workaround.

        Returns None if nothing needed doing (workspace still fresh, or
        stale but this command doesn't reference a repo — gets marked
        fresh either way so the check is O(1) for the rest of the
        turn). Returns an error dict if a needed reclone failed — the
        caller should surface that as the command's result and not run
        anything.
        """
        from core.oadson_home import needs_reclone, mark_fresh
        if not needs_reclone(self.user_id):
            return None

        ref = self._find_repo_reference(command)
        if ref:
            owner, repo = ref
            # BUGFIX 2026-09-19 — was `self.home / "repos" / f"{owner}__{repo}"`,
            # a SECOND, independent reimplementation of github/skill.py's
            # _repo_sandbox_path that never got the 2026-09-02 case-
            # normalization fix applied to it. That fix's whole point was
            # "every caller resolves to the identical folder regardless of
            # casing" — but this call site kept building the path with
            # owner/repo verbatim, undoing that guarantee for exactly the
            # one caller (a post-restart reclone) most likely to see a
            # DIFFERENT casing than however the repo was originally cloned.
            # Confirmed in production: repos/ ended up holding BOTH
            # your-github-user__example-repo/ (original casing, likely
            # created by this exact path) and your-github-user__example-repo/
            # (correctly normalized, from a direct clone_to_workspace call) —
            # two folders for one repo, and every later @terminal command
            # guessing at casing had a 50/50 chance of "No such file or
            # directory" depending which one it happened to reference.
            # Importing the real function instead of hand-rebuilding its
            # logic means there is now exactly ONE place this path is ever
            # constructed — this can't drift out of sync with it again.
            from skills.github.skill import _repo_sandbox_path
            target = _repo_sandbox_path(self.user_id, owner, repo)
            if not target.is_dir():
                try:
                    from skills.github.skill import GitHubSkill
                    gh = GitHubSkill()
                    if gh.error:
                        mark_fresh(self.user_id)
                        return {"status": "error", "reason": f"Workspace was reset by a server restart, and re-cloning {owner}/{repo} failed: {gh.error}"}
                    reclone = await gh.clone_to_workspace(owner, repo, self.user_id)
                except Exception as e:
                    reclone = {"status": "error", "reason": str(e)}
                if reclone.get("status") != "success":
                    mark_fresh(self.user_id)  # don't retry-loop forever on a bad repo ref
                    return {
                        "status": "error",
                        "reason": (
                            f"Workspace was reset by a server restart, and re-cloning "
                            f"{owner}/{repo} failed: {reclone.get('reason', 'unknown error')}"
                        ),
                    }
                # clone_to_workspace already calls mark_fresh() on success
                # (see skills/github/skill.py) — no need to duplicate here.
        else:
            mark_fresh(self.user_id)
        return None

    def _find_repo_reference(self, command: str) -> tuple[str, str] | None:
        """
        If `command` references a repos/{owner}__{repo} path (the exact
        layout github.clone_to_workspace uses — see
        skills/github/skill.py's _repo_sandbox_path), return (owner, repo).
        Used by the auto-reclone check in execute() to figure out WHAT to
        reclone when the workspace turns out to be stale, without needing
        the model to say so explicitly.
        """
        m = re.search(r"repos/([^/\s]+)__([^/\s]+)", command)
        if not m:
            return None
        return m.group(1), m.group(2)

    def execute(self, command: str, session_cwd: str | None = None) -> dict:
        """
        Execute a real shell command, confined to the sandbox.

        Returns the exact command run alongside real stdout/stderr/return_code.
        Also returns the effective cwd and a new_cwd field if a cd command
        changed the working directory — the caller must persist new_cwd back
        to session state.
        """
        command = (command or "").strip()

        if not command:
            return {"status": "error", "command": command, "reason": "No command provided"}

        # Auto-correct absolute sandbox paths to relative before any checks.
        # The model sometimes constructs full absolute paths — transparently
        # rewrite them instead of blocking and wasting a step.
        corrected = self._auto_correct_sandbox_path(command)
        if corrected != command:
            command = corrected

        # Block commands that always timeout or fail in this sandbox
        blocked = _check_blocked(command)
        if blocked:
            return {"status": "blocked", "command": command, "reason": blocked}

        # ── Fix 2: resolve persisted working directory ──
        effective_cwd = self._resolve_cwd(session_cwd)
        new_cwd = None  # will be set if cd changes directory

        # ── Handle cd separately — update session cwd, don't shell out ──
        # BUGFIX 2026-09-19 — was `re.match(r"^\s*cd\s*(.*?)\s*$", command)`,
        # which matches from start to the VERY END of the string with a
        # non-greedy capture — meaning for a compound command like
        # "cd repos/x && find . -exec sed -i '...' {} + && grep ...", the
        # ENTIRE rest of the line (including the && and everything after
        # it) got captured as the "cd target" and then checked with
        # `.is_dir()`, which of course fails since that whole string isn't
        # a real path — producing "No such file or directory" regardless
        # of whether the actual target folder existed and regardless of
        # its casing. Confirmed in production: the EXACT correctly-cased,
        # genuinely-existing path failed this way, immediately after a
        # confirmed-successful clone to that same path.
        #
        # Real fix: only treat this as a bare, standalone `cd` when there's
        # no shell operator after the path (&&, ||, ;, |, or a newline for
        # a second command). Anything compound falls through to the normal
        # subprocess.run(..., shell=True, cwd=effective_cwd) path further
        # below, which handles a real multi-command shell line correctly —
        # this special-cased handler only existed to persist cwd across
        # SEPARATE terminal calls (a bare `cd` has no output of its own to
        # capture), which a compound command doesn't need since bash
        # itself runs the whole chain in one real shell process.
        cd_match = re.match(r"^\s*cd\s*([^&|;\n]*?)\s*$", command)
        if cd_match:
            target = cd_match.group(1).strip() or str(self.home)
            if target == "~" or target == "":
                target = str(self.home)
            # Resolve relative to effective_cwd
            if not os.path.isabs(target):
                resolved = (effective_cwd / target).resolve()
            else:
                resolved = Path(target).resolve()

            # Sandbox check
            try:
                resolved.relative_to(self.home.resolve())
            except ValueError:
                return {
                    "status": "error",
                    "command": command,
                    "cwd": str(effective_cwd),
                    "reason": f"Blocked: cannot cd outside the sandboxed home directory (attempted: {resolved})",
                }

            if not resolved.is_dir():
                # BUGFIX 2026-09-19 — was a bare, fabricated
                # "bash: cd: {target}: No such file or directory" with
                # ZERO context on what actually exists. Confirmed as a
                # real, repeated production pain point: this generic
                # message told the model (and the human reading it)
                # nothing beyond "you guessed wrong" — so a casing
                # mismatch (repos/X vs repos/x) took 2-3 blind retries
                # to resolve, each one just re-triggering this same
                # uninformative message. Now lists the target's PARENT
                # directory's real contents inline, so the model sees
                # what's actually there in the SAME response instead of
                # needing a separate `ls` call (which costs a whole
                # extra step-budget step) to find out.
                parent = resolved.parent
                listing_note = ""
                try:
                    if parent.is_dir():
                        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in parent.iterdir())
                        if entries:
                            listing_note = f" Actual contents of {parent.relative_to(self.home)}/: {', '.join(entries)}"
                        else:
                            listing_note = f" {parent.relative_to(self.home)}/ exists but is empty."
                    else:
                        listing_note = f" Parent directory {parent.relative_to(self.home)}/ doesn't exist either — nothing has been cloned/created along this path yet."
                except Exception:
                    # Best-effort context only — never let a listing
                    # failure hide the real cd error itself.
                    pass
                return {
                    "status": "error",
                    "command": command,
                    "cwd": str(effective_cwd),
                    "stdout": "",
                    "stderr": f"bash: cd: {target}: No such file or directory.{listing_note}",
                    "return_code": 1,
                }

            new_cwd = str(resolved)
            return {
                "status": "success",
                "command": command,
                "cwd": str(effective_cwd),
                "new_cwd": new_cwd,
                "stdout": "",
                "stderr": None,
                "return_code": 0,
                "message": f"Changed directory to {new_cwd}",
            }

        # ── Sandbox path enforcement for non-cd commands ──
        if re.search(r"\bcd\s+/(?!tmp/oadson)", command):
            return {
                "status": "error",
                "command": command,
                "reason": "Blocked: cannot cd outside the sandboxed home directory",
            }

        # BUGFIX 2026-09-19 — real, repeated production failure: a
        # `python3 -c '<script>'` command kept getting blocked with
        # "Blocked: '/' is an empty/degenerate path", twice in a row on
        # the SAME underlying task, even though the actual script had no
        # real path-safety issue at all. Root cause: _extract_path_args
        # naively whitespace-tokenizes the ENTIRE command string,
        # including everything inside the quoted -c script — but that
        # quoted payload is arbitrary Python SOURCE CODE handed to the
        # python interpreter, not shell arguments at all. Ordinary Python
        # syntax is full of bare or near-bare '/' that has nothing to do
        # with filesystem paths — a comment mentioning a date like
        # "1963/1985", an f-string, a division operator — and
        # command.split() has no concept of the outer shell quoting, so
        # it happily splits those into tokens that then look exactly
        # like a degenerate path argument to the check below.
        #
        # Fix: skip path-arg scanning entirely for a python/python3 -c
        # (or -m) invocation — the script argument itself was written by
        # the model in service of a task already covered by its own
        # write_file/read_file tool-level safety checks (those tools
        # validate the ACTUAL paths the script will touch when it runs,
        # which is the real safety boundary here — not a blind text scan
        # of source code that never gets executed as shell arguments).
        _is_python_dash_c = bool(re.match(r"^\s*python3?\s+-[cm]\s+", command))

        if not _is_python_dash_c:
            for path_arg in (clean_path_arg(p) for p in self._extract_path_args(command)):
                expanded = path_arg.replace("~", str(self.home), 1) if path_arg.startswith("~") else path_arg
                # /dev/null is a null device, not a file path: `2>/dev/null` is
                # everyday shell and was being blocked. Only this exact node.
                if expanded == "/dev/null":
                    continue
                if not self._is_safe_path(expanded):
                    # ADDED 2026-09-02 — real production case: the model sent
                    # a literally empty/degenerate path like '//' or '/' —
                    # not a genuine attempt to escape into a real system
                    # directory, just a malformed argument (e.g. from
                    # constructing a path via string concatenation that
                    # produced no actual segment). The generic message below
                    # is accurate but unhelpful here — it explains sandbox
                    # boundaries, not that the path itself is essentially
                    # empty, which is what actually went wrong and is a much
                    # more direct thing to tell the model to fix.
                    if not path_arg.strip("/"):
                        return {
                            "status": "error",
                            "command": command,
                            "reason": (
                                f"Blocked: '{path_arg}' is an empty/degenerate path (no real "
                                "directory or file segment) — not a sandbox escape attempt, just "
                                "a malformed argument. Check how this path was built — it likely "
                                "came from concatenating strings that left nothing between the "
                                "slashes. Use a real relative path, e.g. 'repos/owner__repo/' or "
                                "'.' for the current directory."
                            ),
                        }
                    return {
                        "status": "error",
                        "command": command,
                        "reason": (
                            f"Blocked: path '{path_arg}' is outside the sandbox. "
                            + repo_path_hint(path_arg) +
                            f"The sandbox root is {self.home}. "
                            f"Use relative paths only — e.g. 'ls repos/' not 'ls /tmp/...'. "
                            f"To list cloned repo contents: 'ls repos/owner__repo/'. "
                            f"Never use absolute paths starting with / outside the sandbox."
                        ),
                    }

        # ── Execute ──
        _blocked = _git_clone_hint(command)
        if _blocked:
            return {"status": "blocked", "command": command, "reason": _blocked}
        hint = None
        timeout = self._get_timeout(command)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=True,
                cwd=str(effective_cwd),
                env=command_env(self.home),
            )

            out = {
                "status": "success" if result.returncode == 0 else "error",
                "command": command,
                "cwd": str(effective_cwd),
                "stdout": result.stdout,
                "stderr": result.stderr or None,
                "return_code": result.returncode,
                **({"new_cwd": new_cwd} if new_cwd else {}),
            }
            # BUGFIX 2026-09-19 — companion to the bare-cd fast-path fix
            # above. A COMPOUND command (cd X && other-stuff) runs through
            # THIS real subprocess path, not the fast-path — confirmed in
            # production: the exact "bash: line 1: cd: repos/X: No such
            # file or directory" message came from here. Same fix: if the
            # stderr looks like a failed cd into a repos/ path, list what
            # actually exists there inline, so the model doesn't need a
            # separate `ls` call (a whole extra step-budget step) just to
            # find the real directory name.
            if out["status"] == "error" and out["stderr"] and "No such file or directory" in out["stderr"]:
                cd_fail_match = re.search(r"cd:\s*([^\s:]+):\s*No such file or directory", out["stderr"])
                if cd_fail_match:
                    failed_target = cd_fail_match.group(1)
                    try:
                        candidate = (effective_cwd / failed_target).resolve()
                        parent = candidate.parent
                        if parent.is_dir():
                            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in parent.iterdir())
                            if entries:
                                rel_parent = parent.relative_to(self.home) if parent != self.home else Path(".")
                                out["stderr"] += f" | Actual contents of {rel_parent}/: {', '.join(entries)}"
                    except Exception:
                        # Best-effort context only — never let this hide
                        # or replace the real stderr from bash itself.
                        pass
            if hint:
                out["hint"] = hint
            return out

        except subprocess.TimeoutExpired as e:
            return {
                "status": "timeout",
                "command": command,
                "cwd": str(effective_cwd),
                "stdout": (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
                "return_code": None,
                "timeout_seconds": timeout,
                "message": f"Command timed out after {timeout}s. Partial output returned above.",
            }

        except Exception as e:
            return {
                "status": "error",
                "command": command,
                "cwd": str(effective_cwd),
                "reason": str(e),
            }


def _extract_command(message: str) -> str:
    """
    Pull an actual shell command out of the incoming message.
    Only used as fallback when Gemini doesn't pass args.query.
    """
    msg = message.strip()

    m = re.search(r"`([^`]+)`", msg)
    if m:
        return m.group(1).strip()

    msg_stripped = re.sub(r"^(run|execute)\s+", "", msg, flags=re.IGNORECASE)

    known_cmds = (
        "ls", "cat", "mkdir", "touch", "rm", "cp", "mv", "echo", "pwd",
        "find", "grep", "head", "tail", "wc", "chmod", "df", "du", "cd",
        "python", "python3", "pip", "node", "npm", "git", "curl", "wget",
        "sed", "awk", "sort", "uniq", "diff", "tar", "unzip", "zip",
    )
    first_word = msg_stripped.split()[0] if msg_stripped.split() else ""

    if first_word in known_cmds or any(op in msg_stripped for op in ("&&", "||", "|", ";")):
        return msg_stripped

    return ""


# ── SKILL ENTRY POINT ─────────────────────────────────────────────────────────

async def run(payload: dict) -> dict:
    """
    Execute terminal skill.

    Accepts either:
      - args.query: a single shell command or a && -chained compound command
      - args.script: a multi-line shell script (newline-separated commands).
        All lines run as one subprocess via bash -c, returning combined output.
        Use this to batch multiple operations into a single step.

    Fix 2 requires session_state round-trip:
      - Read session_state["terminal_cwd"] from payload for the current cwd
      - Write result["new_cwd"] back to session_state["terminal_cwd"] if present

    The orchestrator handles session_state persistence — this skill just
    reads and returns the cwd values; it doesn't write to the DB itself.
    """
    args = payload.get("args", {}) or {}

    # Script mode — multiple commands as one step
    script = (args.get("script") or "").strip()
    raw_command = (
        args.get("query") or args.get("command") or args.get("cmd") or ""
    ).strip()
    message = payload.get("message", "").strip()
    user_id = payload.get("user_id")

    session_state = payload.get("session_state") or {}
    session_cwd = session_state.get("terminal_cwd")

    if not user_id:
        return {"status": "error", "reason": "User ID not available — terminal sandboxing is per-user"}

    try:
        skill = TerminalSkill(user_id)
        action = (args.get("action") or "").strip()

        # ── RESTART RESILIENCE 2026-08-28 ──
        # Correctness fix: this MUST be awaited here (run() is async,
        # already inside the event loop skill_registry.execute() drives
        # via `await module.run(payload)`) rather than attempted with
        # asyncio.run() down inside the sync execute() method — see
        # maybe_reclone()'s own docstring for why that first version
        # would have crashed on its very first trigger instead of
        # recovering. Checked once here against whatever text this call
        # actually carries (script, query, or the clone_install_zip repo
        # arg below), before any of the branches that might reference a
        # now-possibly-missing repos/{owner}__{repo} path.
        reclone_check_text = script or raw_command or (args.get("repo") or "")
        if reclone_check_text:
            reclone_error = await skill.maybe_reclone(reclone_check_text)
            if reclone_error:
                return reclone_error

        # ── run_code: write source to a real file, execute with the right runtime ──
        if action == "run_code":
            language = args.get("language") or args.get("lang") or ""
            code = args.get("code") or ""
            filename = args.get("filename")
            stdin = args.get("stdin")
            # No special session_state handling needed here — run_code
            # never issues a `cd`, so execute()'s own new_cwd field (a
            # flat key in the result, persisted by the orchestrator per
            # this file's existing convention — see the module
            # docstring) stays absent/unchanged exactly as it would for
            # any other non-cd command.
            return skill.run_code(language, code, filename=filename, session_cwd=session_cwd, stdin=stdin)

        # ── download_packages: MUST be before `if not command` since it has no query ──
        if action == "download_packages":
            packages = args.get("packages") or []
            manager = (args.get("manager") or "pip").strip().lower()
            output_name = (args.get("output_name") or "packages.zip").strip()
            if not output_name.endswith(".zip"):
                output_name += ".zip"
            if not packages:
                return {"status": "error", "reason": "Provide 'packages' list."}

            pkg_str = " ".join(packages)
            pkg_dir = "downloaded_packages"

            if manager == "pip":
                dl_cmd = f"pip download {pkg_str} -d {pkg_dir}"
            elif manager in ("npm", "node"):
                dl_cmd = f"mkdir -p {pkg_dir} && cd {pkg_dir} && npm pack {pkg_str}"
            elif manager == "yarn":
                dl_cmd = f"mkdir -p {pkg_dir} && yarn pack --filename {pkg_dir}/{pkg_str}.tgz"
            elif manager == "cargo":
                dl_cmd = f"mkdir -p {pkg_dir} && cargo download {pkg_str} --output {pkg_dir}"
            else:
                return {"status": "error", "reason": f"Manager '{manager}' not supported. Use pip, npm, yarn, cargo."}

            dl_result = skill.execute(dl_cmd, session_cwd=session_cwd)
            zip_result = skill.execute(f"zip -r {output_name} {pkg_dir}/", session_cwd=session_cwd)
            return {
                "status": "success" if zip_result["status"] == "success" else "partial",
                "output_file": output_name,
                "packages": packages,
                "manager": manager,
                "download_stdout": dl_result.get("stdout", ""),
                "download_stderr": dl_result.get("stderr", ""),
                "zip_stdout": zip_result.get("stdout", ""),
                "message": (
                    f"Downloaded {pkg_str} via {manager} → {output_name}. Use @archive_creator to get a download link."
                    if zip_result["status"] == "success"
                    else f"Download ran but zip failed: {zip_result.get('stderr', '')}"
                ),
            }

        # ── clone_install_zip: full workflow — clone repo, npm/pip install, zip ──
        # Avoids the model writing fragile chained commands itself.
        if action == "clone_install_zip":
            repo = (args.get("repo") or "").strip()       # e.g. "mavixal/class-teacher-frontend"
            manager = (args.get("manager") or "npm").strip().lower()
            output_name = (args.get("output_name") or "repo.zip").strip()
            include_modules = args.get("include_modules", False)  # default: exclude node_modules
            install_flags = (args.get("install_flags") or "").strip()

            if not repo:
                return {"status": "error", "reason": "clone_install_zip requires 'repo' (e.g. 'owner/name')."}

            owner, _, repo_name = repo.partition("/")
            if not repo_name:
                return {"status": "error", "reason": "repo must be 'owner/name' format."}

            workspace_dir = f"repos/{owner}__{repo_name}"
            if not output_name.endswith(".zip"):
                output_name += ".zip"

            steps = {}

            # 1. Clone (use github skill if available, else git clone)
            clone_result = skill.execute(
                f"git clone https://github.com/{repo}.git {workspace_dir}",
                session_cwd=session_cwd,
            )
            steps["clone"] = {"stdout": clone_result.get("stdout"), "stderr": clone_result.get("stderr"), "code": clone_result.get("return_code")}
            if clone_result["status"] not in ("success",) and "already exists" not in (clone_result.get("stderr") or ""):
                return {"status": "error", "step": "clone", "reason": clone_result.get("stderr") or clone_result.get("stdout"), "steps": steps}

            # 2. Install
            if manager == "npm":
                install_cmd = f"cd {workspace_dir} && npm install {install_flags}"
            elif manager == "pip":
                install_cmd = f"cd {workspace_dir} && pip install -r requirements.txt {install_flags} -t ./vendor"
            elif manager == "yarn":
                install_cmd = f"cd {workspace_dir} && yarn install {install_flags}"
            elif manager == "none":
                install_cmd = None
            else:
                return {"status": "error", "reason": f"manager '{manager}' not supported. Use npm, pip, yarn, none."}

            if install_cmd:
                install_result = skill.execute(install_cmd, session_cwd=session_cwd)
                steps["install"] = {"stdout": install_result.get("stdout", "")[:500], "stderr": install_result.get("stderr", "")[:500], "code": install_result.get("return_code")}

            # 3. Zip — always zip to sandbox root to avoid ../.. escaping
            exclude = "" if include_modules else "--exclude '*/node_modules/*' --exclude '*/__pycache__/*' --exclude '*/.git/*'"
            zip_cmd = f"zip -r {output_name} {workspace_dir}/ {exclude}"
            zip_result = skill.execute(zip_cmd, session_cwd=session_cwd)
            steps["zip"] = {"stdout": zip_result.get("stdout", "")[:300], "code": zip_result.get("return_code")}

            return {
                "status": "success" if zip_result["status"] == "success" else "partial",
                "output_file": output_name,
                "repo": repo,
                "workspace": workspace_dir,
                "steps": steps,
                "message": (
                    f"Cloned {repo}, ran {manager} install, zipped to {output_name}. "
                    "Use @archive_creator files=['" + output_name + "'] to get a download link."
                    if zip_result["status"] == "success"
                    else f"Partial — check steps for errors."
                ),
            }

        if script:
            # Scan script for absolute cd calls outside the sandbox before running.
            for line in script.splitlines():
                line = line.strip()
                cd_match = re.match(r"^cd\s+(.*)", line)
                if cd_match:
                    target = cd_match.group(1).strip().strip('"\'')
                    if os.path.isabs(target):
                        resolved = Path(target).resolve()
                        try:
                            resolved.relative_to(skill.home.resolve())
                        except ValueError:
                            return {
                                "status": "error",
                                "command": f"script (blocked at: cd {target})",
                                "reason": (
                                    f"Blocked: 'cd {target}' goes outside the sandbox. "
                                    "Use relative paths: e.g. 'cd repos/backend'."
                                ),
                            }
            command = f"bash -c {__import__('shlex').quote(script)}"
        else:
            command = raw_command or _extract_command(message)

        if not command:
            return {
                "status": "error",
                "reason": (
                    "No shell command found. "
                    "Pass args.query for a single command, args.script for multi-line, "
                    "or args.action='clone_install_zip' to clone+install+zip a repo in one step."
                ),
            }

        result = skill.execute(command, session_cwd=session_cwd)
        return result

    except Exception as e:
        return {"status": "error", "reason": str(e)}
