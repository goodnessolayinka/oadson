"""
What this worker runs, and what needs your approval first.

Generated from the owner repo's classification (see docs/DESIGN.md) and copied to
the owner by scripts/sync_to_owner.py. Keep the two in step: the owner's tests
fail if they differ.
"""
ALLOWED_SKILLS = [
    "api_tester",
    "browser",
    "bug_analyzer",
    "calculator",
    "code_debugger",
    "code_explainer",
    "currency_converter",
    "cve_lookup",
    "dependency_audit",
    "dns_lookup",
    "docker_helper",
    "docs_fetcher",
    "encoder_decoder",
    "git_helper",
    "github",
    "github_daily_summary",
    "hash_cracker",
    "hash_identifier",
    "header_analyzer",
    "html_preview",
    "humanizer",
    "ip_geolocation",
    "json_formatter",
    "jwt_decoder",
    "nasa_weather",
    "news",
    "password_check",
    "port_scanner",
    "regex_builder",
    "shodan_search",
    "ssl_checker",
    "stackoverflow",
    "subdomain_enum",
    "temp_converter",
    "translator",
    "url_analyzer",
    "virustotal",
    "wayback",
    "weather_forecast",
    "web_fetch",
    "web_scraper",
    "web_search",
    "whois",
    "wikipedia",
    "youtube",
]

# Off unless the worker's WORKER_ENABLE_TERMINAL=true AND the owner's
# WORKER_ROUTE_TERMINAL=true. The terminal passes its environment to commands;
# see CHANGELOG for the accepted risk.
OPTIONAL_SKILLS = ["terminal"]

# Skills whose data lives on Render (your memory, database, messaging, Google/Spotify accounts,
# or the owner's scheduler). Their code is on the worker, but they answer 'runs on Render'.
OWNER_STATE_SKILLS = [
    "archive_creator",
    "backend_developer",
    "chat_history_search",
    "code_index",
    "document_reader",
    "file_generation",
    "file_session",
    "gmail",
    "google_calendar",
    "google_docs",
    "google_drive",
    "google_keep",
    "google_tasks",
    "ilana",
    "image_generation",
    "memory_manager",
    "note_taker",
    "plans",
    "proactive_config",
    "project_creator",
    "project_memory",
    "recon_report",
    "reminder",
    "session_log",
    "skill_creator",
    "skill_manager",
    "spotify",
    "sticker_reply",
    "style_manager",
    "system_status",
    "telegram",
    "threat_intel",
    "usage_analytics",
    "user_location",
    "voice_reply",
    "website_developer",
    "whatsapp_reader",
    "workspace",
]

# Built into the worker itself, not loaded from skills/.
BUILTIN_SKILLS = ["echo", "sleep", "count"]

# If the worker is unreachable, these never run on the owner. They are too heavy
# or touch the workspace, so the owner says "try again" instead.
NO_FALLBACK = ["browser", "github", "terminal"]

# Actions that change something outside the sandbox. They need the user's explicit
# approval (a message "approve <code>") before they run. Enforced on both sides.
# Approval removed on purpose (2026-10-30). Nothing waits for an "approve <code>" reply.
# To bring it back, list the skill and action pairs here again, e.g. ["github", "push_workspace"].
APPROVAL_REQUIRED = []

# Owner-side core modules the allowed skills import. Copied by sync_from_owner.py.
CORE_FILES = [
    "core/__init__.py",
    "core/code_validator.py",
    "core/failure_explainer.py",
    "core/oadson_home.py",
    "core/repo_index.py",
    "core/ssrf_guard.py",
]
