"""
website_developer/skill.py

Generates a complete standard-compliant static website (HTML/CSS/JS),
self-checks it against the quality checklist in skill.md, fixes what
it can automatically, and saves it into the user's sandbox workspace.

This skill does NOT call an LLM itself — it builds structurally from
the args Gemini already extracted (title, description, sections, theme).
Real creative copy comes from Gemini's own reasoning before calling this
skill (pass richer content via future args if needed). Without extra
content, section builders below produce sensible, relevant placeholder
copy tied to the given title/description — never Lorem Ipsum.
"""

import re
import uuid
import importlib.util
from pathlib import Path
from datetime import datetime, timezone

# skill.py is loaded directly by file path (not as a real package), so a
# relative import ("from .templates import ...") resolves against the
# wrong parent package and fails at runtime. Load templates.py by its
# own file path instead — same folder, guaranteed to exist alongside this file.
_templates_path = Path(__file__).parent / "templates.py"
_spec = importlib.util.spec_from_file_location("website_developer_templates", _templates_path)
_templates = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_templates)

resolve_theme = _templates.resolve_theme
resolve_sections = _templates.resolve_sections
DEFAULT_SECTIONS = _templates.DEFAULT_SECTIONS


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", text) or "site"


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── SECTION BUILDERS ──
# Each returns (html_fragment, css_fragment). Structurally sound and
# accessible by construction — no way to emit a broken section.

def _section_hero(title: str, description: str) -> tuple:
    html = f"""
  <section class="hero" id="hero">
    <div class="container">
      <h1>{_escape(title)}</h1>
      <p class="hero-subtitle">{_escape(description)}</p>
      <a href="#contact" class="btn btn-primary">Get Started</a>
    </div>
  </section>"""
    css = """
.hero { padding: 96px 24px; text-align: center; }
.hero h1 { font-size: clamp(2rem, 5vw, 3.5rem); margin-bottom: 16px; }
.hero-subtitle { font-size: 1.25rem; color: var(--text-muted); max-width: 640px; margin: 0 auto 32px; }
"""
    return html, css


def _section_features(description: str) -> tuple:
    items = [
        ("Fast", "Built for speed from the ground up."),
        ("Reliable", "Consistent performance you can depend on."),
        ("Simple", "No unnecessary complexity, just what you need."),
    ]
    cards = "\n".join(
        f'      <div class="feature-card"><h3>{_escape(t)}</h3><p>{_escape(d)}</p></div>'
        for t, d in items
    )
    html = f"""
  <section class="features" id="features">
    <div class="container">
      <h2>Why Choose Us</h2>
      <div class="feature-grid">
{cards}
      </div>
    </div>
  </section>"""
    css = """
.features { padding: 80px 24px; background: var(--surface); }
.feature-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 24px; margin-top: 40px; }
.feature-card { padding: 32px; background: var(--bg); border-radius: 12px; }
"""
    return html, css


def _section_about(title: str, description: str) -> tuple:
    html = f"""
  <section class="about" id="about">
    <div class="container">
      <h2>About {_escape(title)}</h2>
      <p>{_escape(description)}</p>
    </div>
  </section>"""
    css = ".about { padding: 80px 24px; max-width: 720px; margin: 0 auto; }"
    return html, css


def _section_services(description: str) -> tuple:
    services = ["Consulting", "Development", "Support"]
    cards = "\n".join(f'      <div class="service-card"><h3>{_escape(s)}</h3></div>' for s in services)
    html = f"""
  <section class="services" id="services">
    <div class="container">
      <h2>Our Services</h2>
      <div class="service-grid">
{cards}
      </div>
    </div>
  </section>"""
    css = """
.services { padding: 80px 24px; }
.service-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 32px; }
.service-card { padding: 24px; border: 1px solid var(--surface); border-radius: 10px; text-align: center; }
"""
    return html, css


def _section_projects() -> tuple:
    html = """
  <section class="projects" id="projects">
    <div class="container">
      <h2>Projects</h2>
      <div class="project-grid">
        <div class="project-card"><h3>Project One</h3><p>Short description goes here.</p></div>
        <div class="project-card"><h3>Project Two</h3><p>Short description goes here.</p></div>
        <div class="project-card"><h3>Project Three</h3><p>Short description goes here.</p></div>
      </div>
    </div>
  </section>"""
    css = """
.projects { padding: 80px 24px; background: var(--surface); }
.project-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px; margin-top: 32px; }
.project-card { padding: 24px; background: var(--bg); border-radius: 12px; }
"""
    return html, css


def _section_skills() -> tuple:
    skills = ["Python", "JavaScript", "Design", "Strategy"]
    tags = "\n".join(f'        <span class="skill-tag">{_escape(s)}</span>' for s in skills)
    html = f"""
  <section class="skills" id="skills">
    <div class="container">
      <h2>Skills</h2>
      <div class="skill-tags">
{tags}
      </div>
    </div>
  </section>"""
    css = """
.skills { padding: 80px 24px; }
.skill-tags { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 24px; }
.skill-tag { padding: 8px 16px; background: var(--surface); border-radius: 999px; font-size: 0.9rem; }
"""
    return html, css


def _section_testimonials() -> tuple:
    html = """
  <section class="testimonials" id="testimonials">
    <div class="container">
      <h2>What People Say</h2>
      <blockquote>"Exceptional experience from start to finish."<footer>— A satisfied client</footer></blockquote>
    </div>
  </section>"""
    css = """
.testimonials { padding: 80px 24px; text-align: center; background: var(--surface); }
.testimonials blockquote { font-size: 1.3rem; font-style: italic; max-width: 640px; margin: 24px auto 0; }
.testimonials footer { margin-top: 16px; font-size: 0.95rem; color: var(--text-muted); font-style: normal; }
"""
    return html, css


def _section_cta(title: str) -> tuple:
    html = f"""
  <section class="cta" id="cta">
    <div class="container">
      <h2>Ready to work with {_escape(title)}?</h2>
      <a href="#contact" class="btn btn-primary">Get in Touch</a>
    </div>
  </section>"""
    css = """
.cta { padding: 80px 24px; text-align: center; background: var(--primary); color: #fff; }
.cta .btn-primary { background: #fff; color: var(--primary); }
"""
    return html, css


def _section_contact() -> tuple:
    html = """
  <section class="contact" id="contact">
    <div class="container">
      <h2>Contact</h2>
      <form aria-label="Contact form">
        <label for="name">Name</label>
        <input type="text" id="name" name="name" required>
        <label for="email">Email</label>
        <input type="email" id="email" name="email" required>
        <label for="message">Message</label>
        <textarea id="message" name="message" rows="4" required></textarea>
        <button type="submit" class="btn btn-primary">Send Message</button>
      </form>
    </div>
  </section>"""
    css = """
.contact { padding: 80px 24px; max-width: 560px; margin: 0 auto; }
.contact form { display: flex; flex-direction: column; gap: 12px; margin-top: 24px; }
.contact input, .contact textarea { padding: 12px; border: 1px solid var(--surface); border-radius: 8px; font: inherit; }
"""
    return html, css


def _section_footer(title: str) -> tuple:
    year = datetime.now(timezone.utc).year
    html = f"""
  <footer class="site-footer">
    <div class="container">
      <p>&copy; {year} {_escape(title)}. All rights reserved.</p>
    </div>
  </footer>"""
    css = ".site-footer { padding: 32px 24px; text-align: center; color: var(--text-muted); font-size: 0.9rem; }"
    return html, css


def _section_sidebar() -> tuple:
    html = """
  <aside class="sidebar">
    <nav>
      <a href="#" class="active">Dashboard</a>
      <a href="#">Analytics</a>
      <a href="#">Settings</a>
    </nav>
  </aside>"""
    css = """
.sidebar { width: 220px; background: var(--surface); padding: 24px 16px; min-height: 100vh; }
.sidebar nav { display: flex; flex-direction: column; gap: 8px; }
.sidebar a { padding: 10px 12px; border-radius: 8px; color: var(--text); }
.sidebar a.active { background: var(--primary); color: #fff; }
"""
    return html, css


def _section_stats() -> tuple:
    stats = [("1,204", "Total Users"), ("98.2%", "Uptime"), ("342", "Active Sessions")]
    cards = "\n".join(
        f'      <div class="stat-card"><span class="stat-value">{v}</span><span class="stat-label">{l}</span></div>'
        for v, l in stats
    )
    html = f"""
  <section class="stats">
{cards}
  </section>"""
    css = """
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; padding: 24px; }
.stat-card { padding: 20px; background: var(--surface); border-radius: 10px; text-align: center; }
.stat-value { display: block; font-size: 1.8rem; font-weight: 700; }
.stat-label { font-size: 0.85rem; color: var(--text-muted); }
"""
    return html, css


def _section_header(title: str) -> tuple:
    html = f'<header class="site-header"><div class="container"><h2>{_escape(title)}</h2></div></header>'
    css = ".site-header { padding: 24px; border-bottom: 1px solid var(--surface); }"
    return html, css


def _section_chart_placeholder() -> tuple:
    html = '<div class="chart-placeholder" role="img" aria-label="Chart placeholder">Chart data goes here</div>'
    css = ".chart-placeholder { background: var(--surface); border-radius: 12px; padding: 60px; text-align: center; color: var(--text-muted); margin: 16px 24px; }"
    return html, css


def _section_table() -> tuple:
    html = ('<table class="data-table"><thead><tr><th>Name</th><th>Status</th></tr></thead>'
            '<tbody><tr><td>Sample Row</td><td>Active</td></tr></tbody></table>')
    css = (".data-table { width: 100%; border-collapse: collapse; margin: 16px 24px; } "
           ".data-table th, .data-table td { padding: 12px; text-align: left; border-bottom: 1px solid var(--surface); }")
    return html, css


def _section_featured_post(title: str, description: str) -> tuple:
    html = f'<article class="featured-post"><h2>Featured: Welcome to {_escape(title)}</h2><p>{_escape(description)}</p></article>'
    css = ".featured-post { padding: 48px 24px; }"
    return html, css


def _section_post_list() -> tuple:
    html = '<div class="post-list"><article><h3>First Post</h3><p>Post excerpt here.</p></article></div>'
    css = ".post-list article { padding: 24px; border-bottom: 1px solid var(--surface); }"
    return html, css


SECTION_BUILDERS = {
    "hero": lambda t, d: _section_hero(t, d),
    "features": lambda t, d: _section_features(d),
    "about": lambda t, d: _section_about(t, d),
    "services": lambda t, d: _section_services(d),
    "projects": lambda t, d: _section_projects(),
    "skills": lambda t, d: _section_skills(),
    "testimonials": lambda t, d: _section_testimonials(),
    "cta": lambda t, d: _section_cta(t),
    "contact": lambda t, d: _section_contact(),
    "footer": lambda t, d: _section_footer(t),
    "sidebar": lambda t, d: _section_sidebar(),
    "stats": lambda t, d: _section_stats(),
    "header": lambda t, d: _section_header(t),
    "chart_placeholder": lambda t, d: _section_chart_placeholder(),
    "table": lambda t, d: _section_table(),
    "featured_post": lambda t, d: _section_featured_post(t, d),
    "post_list": lambda t, d: _section_post_list(),
}


def _build_site(title: str, description: str, sections: list, theme: tuple) -> dict:
    bg, surface, text, primary, accent = theme
    html_parts = []
    css_parts = [f"""
:root {{
  --bg: {bg}; --surface: {surface}; --text: {text};
  --text-muted: {text}99; --primary: {primary}; --accent: {accent};
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: var(--bg); color: var(--text); line-height: 1.6; }}
.container {{ max-width: 1100px; margin: 0 auto; }}
h1, h2, h3 {{ line-height: 1.2; }}
a {{ color: var(--primary); text-decoration: none; }}
.btn {{ display: inline-block; padding: 14px 32px; border-radius: 8px; font-weight: 600; }}
.btn-primary {{ background: var(--primary); color: #fff; }}
"""]

    warnings = []
    for sec in sections:
        builder = SECTION_BUILDERS.get(sec)
        if not builder:
            warnings.append(f"Unknown section '{sec}' skipped.")
            continue
        html, css = builder(title, description)
        html_parts.append(html)
        css_parts.append(css)

    body_html = "\n".join(html_parts)
    full_css = "\n".join(css_parts)

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_escape(title)}</title>
  <meta name="description" content="{_escape(description)[:155]}">
  <link rel="stylesheet" href="style.css">
</head>
<body>
{body_html}
  <script src="script.js"></script>
</body>
</html>"""

    js = """// Smooth scroll for internal anchor links
document.querySelectorAll('a[href^="#"]').forEach(link => {
  link.addEventListener('click', e => {
    const target = document.querySelector(link.getAttribute('href'));
    if (target) {
      e.preventDefault();
      target.scrollIntoView({ behavior: 'smooth' });
    }
  });
});
"""

    return {
        "index.html": full_html,
        "style.css": full_css,
        "script.js": js,
        "warnings": warnings,
    }


def _self_review(files: dict) -> list:
    """Cheap structural checklist — catches real regressions without an LLM call."""
    issues = []
    html = files.get("index.html", "")

    if 'name="viewport"' not in html:
        issues.append("Missing viewport meta tag")
    if "<title>" not in html:
        issues.append("Missing <title>")
    if "<h1" not in html and "<h2" not in html:
        issues.append("No heading found")
    if re.search(r'<img(?![^>]*alt=)', html):
        issues.append("Image tag(s) missing alt attribute")
    if "lorem ipsum" in html.lower():
        issues.append("Placeholder Lorem Ipsum text found")

    return issues


async def run(payload: dict) -> dict:
    user_id = payload.get("user_id")
    args = payload.get("args", {}) or {}
    action = args.get("action") or "build"

    if not user_id:
        return {"status": "error", "reason": "User ID not available"}

    if action == "list_templates":
        return {
            "status": "success",
            "site_types": list(DEFAULT_SECTIONS.keys()),
            "frameworks": ["vanilla", "react-cdn", "react-vite"],
            "default_sections": DEFAULT_SECTIONS,
        }

    if action == "build_component":
        return await _build_react_component(payload, args, user_id)

    if action == "edit":
        return await _edit_file(payload, args, user_id)

    if action not in ("build",):
        return {"status": "error", "reason": f"Unknown action '{action}'. Use: build, build_component, edit, list_templates"}

    framework = (args.get("framework") or "vanilla").strip().lower()

    if framework == "react-cdn":
        return await _build_react_cdn(payload, args, user_id)

    if framework == "react-vite":
        return await _build_react_vite(payload, args, user_id)

    # ── vanilla fallback (existing path) ──
    title = (args.get("title") or "").strip()
    description = (args.get("description") or "").strip()
    # BUG FIXED 2026-08-14: site_type was used below (in
    # resolve_sections, in the returned "site_type" field, and in the
    # success message) but never actually read from args anywhere in
    # this function — every vanilla build() call hit an
    # UnboundLocalError on this undefined name, which is what surfaced
    # upstream as "passed unexpected value". resolve_sections() already
    # falls back to "landing" for an unrecognized/missing site_type, so
    # this is a safe default for old callers that never passed one.
    site_type = (args.get("site_type") or "landing").strip().lower()

    if not title:
        return {"status": "error", "reason": "build requires 'title'"}
    if not description:
        description = f"Welcome to {title}."

    sections = resolve_sections(site_type, args.get("sections"))
    theme = resolve_theme(args.get("color_theme"))
    folder_name = (args.get("folder_name") or _slugify(title)).strip()

    try:
        files = _build_site(title, description, sections, theme)
        review_issues = _self_review(files)

        if review_issues:
            html = files["index.html"]
            if "Missing viewport meta tag" in review_issues and "viewport" not in html:
                html = html.replace(
                    "<head>",
                    '<head>\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">'
                )
            files["index.html"] = html
            review_issues = _self_review(files)

        try:
            from core.oadson_home import save_to_sandbox
        except ImportError:
            return {"status": "error", "reason": "Sandbox storage unavailable (core.oadson_home not found)"}

        saved_paths = []
        for filename, content in files.items():
            if filename == "warnings":
                continue
            path = f"{folder_name}/{filename}"
            # WORKSPACE SPLIT 2026-08-28: generated site files are
            # scratch work-in-progress, not something the user sent —
            # belongs in workspace/, not the new inputs/ default. See
            # core/oadson_home.py's save_to_sandbox area= param.
            saved_path, save_error = save_to_sandbox(user_id, path, content, area="workspace")
            if save_error:
                return {"status": "error", "reason": f"Failed to save {filename}: {save_error}"}
            saved_paths.append(saved_path)

        # LIVE PREVIEW 2026-08-27 — "hosted the website not just code".
        # save_to_sandbox above only ever wrote to local disk on the
        # Render instance — which is ALSO ephemeral there (wiped on every
        # deploy/restart) even setting aside that nothing served it over
        # HTTP at all, hence the old message telling the user their only
        # real option was "push with @github to deploy". Also persisting
        # every generated file into db.file_sessions — the exact same
        # Mongo-backed store api/serve.py already serves from reliably —
        # under one shared bucket per build, with EXACT filenames
        # preserved (preserve_filename=True; the default random-prefix
        # behavior would break index.html's own relative
        # `<link href="style.css">` reference, since that only resolves
        # correctly if a file actually named style.css sits at that same
        # served path). A frontend iframe can now point straight at
        # preview_url and get a genuinely live, correctly-linked
        # multi-file page — not just inlined/static code.
        from core.inline_storage import save_inline
        db = payload.get("db")
        preview_bucket = f"site_{folder_name}_{uuid.uuid4().hex[:6]}"
        mime_map = {"index.html": "text/html; charset=utf-8", "style.css": "text/css; charset=utf-8", "script.js": "application/javascript; charset=utf-8"}
        preview_url = None
        if db is not None:
            for filename, content in files.items():
                if filename == "warnings":
                    continue
                url = await save_inline(
                    db, content.encode("utf-8"), filename,
                    mime_map.get(filename, "text/plain; charset=utf-8"),
                    bucket=preview_bucket, preserve_filename=True,
                )
                if filename == "index.html":
                    preview_url = url

        return {
            "status": "success",
            "site_type": site_type,
            "title": title,
            "folder": folder_name,
            "filename": f"{folder_name}/index.html",
            "html_content": files.get("index.html", ""),
            "files_created": [f for f in files if f != "warnings"],
            "sections_included": sections,
            "saved_paths": saved_paths,
            "preview_url": preview_url,
            "build_warnings": files.get("warnings", []),
            "review_issues_remaining": review_issues,
            "message": (
                f"Built a {site_type} site for '{title}' with {len(sections)} sections. "
                "Tap Preview to see it. Push with @github to deploy."
                + ("" if not review_issues else
                   f" Note: {len(review_issues)} minor issue(s): {'; '.join(review_issues)}")
            ),
        }

    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ── REACT BUILDERS ────────────────────────────────────────────────────────────

async def _edit_file(payload: dict, args: dict, user_id: str) -> dict:
    """
    ADDED 2026-08-31: before this, website_developer could only ever
    generate a brand new file tree — there was no way to change one
    thing in an existing file (a prior build, or a file pulled in via
    @github clone_to_workspace) without regenerating everything around
    it and losing any manual edits. This is the direct fix — see
    skill.md's "Editing an existing site" section for the intended
    @github <-> website_developer workflow this enables.

    Thin wrapper over core.sandbox_edit's shared primitive — no
    website_developer-specific logic needed here, editing an HTML/CSS/
    JS file isn't meaningfully different from editing any other text
    file in the sandbox.
    """
    from core.sandbox_edit import str_replace_sandbox_file

    file_path = (args.get("file_path") or "").strip()
    old_str = args.get("old_str")
    new_str = args.get("new_str")

    if not file_path:
        return {"status": "error", "reason": "edit requires 'file_path'"}
    if old_str is None:
        return {"status": "error", "reason": "edit requires 'old_str'"}
    if new_str is None:
        return {"status": "error", "reason": "edit requires 'new_str' (use an empty string to delete old_str)"}

    return str_replace_sandbox_file(user_id, file_path, old_str, new_str)


async def _get_sandbox(user_id: str):
    """Get the user's sandbox path."""
    from core.oadson_home import get_user_home
    return get_user_home(user_id)


async def _build_react_cdn(payload: dict, args: dict, user_id: str) -> dict:
    """
    Single HTML file with React 18 loaded from CDN.
    No npm, no build step — works immediately with @html_preview.
    """
    title = (args.get("title") or "").strip()
    description = (args.get("description") or "My App").strip()
    folder_name = _slugify(args.get("folder_name") or title or "react-app")

    if not title:
        return {"status": "error", "reason": "build requires 'title'"}

    sandbox = await _get_sandbox(user_id)
    folder = sandbox / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_escape(title)}</title>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f0f0f; color: #f0f0f0; min-height: 100vh; }}
    #root {{ display: flex; flex-direction: column; min-height: 100vh; }}
  </style>
</head>
<body>
  <div id="root"></div>

  <script type="text/babel">
    const {{ useState, useEffect }} = React;

    function App() {{
      const [count, setCount] = useState(0);

      return (
        <div style={{{{ padding: '2rem', maxWidth: '800px', margin: '0 auto' }}}}>
          <h1 style={{{{ fontSize: '2rem', marginBottom: '1rem', color: '#60a5fa' }}}}>
            {_escape(title)}
          </h1>
          <p style={{{{ color: '#9ca3af', marginBottom: '2rem' }}}}>
            {_escape(description)}
          </p>
          <button
            onClick={{() => setCount(c => c + 1)}}
            style={{{{
              background: '#3b82f6', color: '#fff', border: 'none',
              padding: '0.75rem 1.5rem', borderRadius: '8px',
              fontSize: '1rem', cursor: 'pointer'
            }}}}
          >
            Count: {{count}}
          </button>
        </div>
      );
    }}

    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(<App />);
  </script>
</body>
</html>"""

    (folder / "index.html").write_text(html, encoding="utf-8")

    return {
        "status": "success",
        "framework": "react-cdn",
        "title": title,
        "folder": folder_name,
        "filename": f"{folder_name}/index.html",
        "html_content": html,
        "files_created": ["index.html"],
        "saved_paths": [str(folder / "index.html")],
        "message": (
            f"Built React CDN app '{title}' → workspace/{folder_name}/index.html. "
            "Tap Preview to see it live. Push with @github to deploy."
        ),
    }


async def _build_react_vite(payload: dict, args: dict, user_id: str) -> dict:
    """
    Full Vite + React project scaffold.
    Generates: package.json, vite.config.js, index.html, src/main.jsx, src/App.jsx,
    src/components/, src/App.css, public/.
    User runs `npm install && npm run dev` to activate.
    """
    title = (args.get("title") or "").strip()
    description = (args.get("description") or "").strip()
    folder_name = _slugify(args.get("folder_name") or title or "react-app")

    if not title:
        return {"status": "error", "reason": "build requires 'title'"}

    sandbox = await _get_sandbox(user_id)
    folder = sandbox / folder_name
    (folder / "src" / "components").mkdir(parents=True, exist_ok=True)
    (folder / "public").mkdir(parents=True, exist_ok=True)

    pkg_name = folder_name.replace("-", "_")

    files = {
        "package.json": f'''{{\n  "name": "{pkg_name}",\n  "private": true,\n  "version": "0.1.0",\n  "type": "module",\n  "scripts": {{\n    "dev": "vite",\n    "build": "vite build",\n    "preview": "vite preview"\n  }},\n  "dependencies": {{\n    "react": "^18.3.1",\n    "react-dom": "^18.3.1"\n  }},\n  "devDependencies": {{\n    "@vitejs/plugin-react": "^4.3.1",\n    "vite": "^5.4.1"\n  }}\n}}''',

        "vite.config.js": '''import { defineConfig } from 'vite'\nimport react from '@vitejs/plugin-react'\n\nexport default defineConfig({\n  plugins: [react()],\n})''',

        "index.html": f'''<!DOCTYPE html>\n<html lang="en">\n  <head>\n    <meta charset="UTF-8" />\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n    <title>{_escape(title)}</title>\n  </head>\n  <body>\n    <div id="root"></div>\n    <script type="module" src="/src/main.jsx"></script>\n  </body>\n</html>''',

        "src/main.jsx": '''import React from 'react'\nimport ReactDOM from 'react-dom/client'\nimport App from './App.jsx'\nimport './App.css'\n\nReactDOM.createRoot(document.getElementById('root')).render(\n  <React.StrictMode>\n    <App />\n  </React.StrictMode>\n)''',

        "src/App.jsx": f'''import {{ useState }} from 'react'\nimport './App.css'\n\nfunction App() {{\n  const [count, setCount] = useState(0)\n\n  return (\n    <div className="app">\n      <h1>{_escape(title)}</h1>\n      <p>{_escape(description)}</p>\n      <button onClick={{() => setCount(c => c + 1)}}>\n        Count: {{count}}\n      </button>\n    </div>\n  )\n}}\n\nexport default App''',

        "src/App.css": '''* { box-sizing: border-box; margin: 0; padding: 0; }\nbody { font-family: system-ui, -apple-system, sans-serif; background: #0f0f0f; color: #f0f0f0; }\n.app { max-width: 800px; margin: 0 auto; padding: 2rem; }\nh1 { font-size: 2rem; margin-bottom: 1rem; color: #60a5fa; }\np { color: #9ca3af; margin-bottom: 2rem; }\nbutton { background: #3b82f6; color: #fff; border: none; padding: 0.75rem 1.5rem; border-radius: 8px; font-size: 1rem; cursor: pointer; }\nbutton:hover { background: #2563eb; }''',

        ".gitignore": "node_modules\ndist\n.env\n.env.local",
        "README.md": f"# {title}\n\n{description}\n\n## Setup\n\n```bash\nnpm install\nnpm run dev\n```",
    }

    saved_paths = []
    for rel_path, content in files.items():
        path = folder / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        saved_paths.append(str(path))

    return {
        "status": "success",
        "framework": "react-vite",
        "title": title,
        "folder": folder_name,
        "files_created": list(files.keys()),
        "saved_paths": saved_paths,
        "message": (
            f"Scaffolded Vite+React project '{title}' → workspace/{folder_name}/. "
            "Run: cd {folder_name} && npm install && npm run dev. "
            "Push to GitHub with @github to deploy on Vercel/Netlify."
        ),
    }


async def _build_react_component(payload: dict, args: dict, user_id: str) -> dict:
    """Generate a single reusable React component (.jsx)."""
    component_name = (args.get("component_name") or args.get("title") or "MyComponent").strip()
    description = (args.get("description") or "").strip()
    props_desc = (args.get("props") or "").strip()
    folder_name = _slugify(args.get("folder_name") or "components")

    sandbox = await _get_sandbox(user_id)
    folder = sandbox / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    props_comment = f" * Props: {props_desc}" if props_desc else " * No props required"
    safe_name = component_name.replace(" ", "")

    jsx = f'''/**
 * {safe_name}
 * {description}
{props_comment}
 */
import {{ useState }} from 'react'

/**
 * @param {{{{
 *   {props_desc or 'children: React.ReactNode'}
 * }}}} props
 */
function {safe_name}({{ {", ".join([p.split(":")[0].strip() for p in props_desc.split(",")]) if props_desc else "children"} }}) {{
  return (
    <div className="{safe_name.lower()}-wrapper">
      {{/* {description or "Component content here"} */}}
      {{children}}
    </div>
  )
}}

export default {safe_name}
'''

    file_name = f"{safe_name}.jsx"
    (folder / file_name).write_text(jsx, encoding="utf-8")

    return {
        "status": "success",
        "component": safe_name,
        "file": file_name,
        "folder": folder_name,
        "saved_path": str(folder / file_name),
        "message": (
            f"Created React component '{safe_name}' → workspace/{folder_name}/{file_name}. "
            f"Import with: import {safe_name} from './{folder_name}/{file_name}'"
        ),
    }
