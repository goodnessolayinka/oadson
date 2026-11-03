"""
website_developer/templates.py

Defaults per site_type: which sections to include if the user didn't
specify, and base CSS variables per color theme direction.
No business logic — pure data the skill.py orchestrator reads from.
"""

DEFAULT_SECTIONS = {
    "landing":   ["hero", "features", "testimonials", "cta", "footer"],
    "portfolio": ["hero", "about", "projects", "skills", "contact", "footer"],
    "dashboard": ["sidebar", "header", "stats", "chart_placeholder", "table"],
    "business":  ["hero", "services", "about", "testimonials", "contact", "footer"],
    "blog":      ["header", "featured_post", "post_list", "sidebar", "footer"],
}

# Color themes: (background, surface, text, primary, accent)
THEMES = {
    "default":       ("#ffffff", "#f5f5f7", "#1d1d1f", "#0066ff", "#ff6b35"),
    "dark":          ("#0d0d0f", "#18181b", "#f4f4f5", "#6366f1", "#22d3ee"),
    "dark_minimal":  ("#0a0a0a", "#141414", "#e5e5e5", "#ffffff", "#888888"),
    "warm_playful":  ("#fff8f0", "#ffe8d6", "#3d2817", "#ff6b35", "#ffb84d"),
    "black_gold":    ("#0a0a0a", "#1a1a1a", "#f5f5f0", "#d4af37", "#8b6914"),
    "cool_professional": ("#f8fafc", "#eef2f7", "#0f172a", "#2563eb", "#64748b"),
}


def resolve_theme(color_theme: str | None) -> tuple:
    """Map a free-text theme hint to the closest built-in palette."""
    if not color_theme:
        return THEMES["default"]

    hint = color_theme.lower()
    if "dark" in hint and "minimal" in hint:
        return THEMES["dark_minimal"]
    if "dark" in hint:
        return THEMES["dark"]
    if "warm" in hint or "playful" in hint:
        return THEMES["warm_playful"]
    if "gold" in hint or "black and gold" in hint:
        return THEMES["black_gold"]
    if "professional" in hint or "cool" in hint or "corporate" in hint:
        return THEMES["cool_professional"]
    return THEMES["default"]


def resolve_sections(site_type: str, requested: list | None) -> list:
    if requested:
        return requested
    return DEFAULT_SECTIONS.get(site_type, DEFAULT_SECTIONS["landing"])
