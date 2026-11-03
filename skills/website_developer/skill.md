# Website Developer Skill

Expert-level website generation. Coordinates existing OADSON skills instead of
duplicating them — this skill is the carpenter, not a new set of tools.

## When to use
- User asks to build/create a website, landing page, portfolio, dashboard UI, or business site
- User describes a business/project and wants an online presence
- User wants a starter site to iterate on, not a single code snippet

## Actions
| Action | Use for |
|---|---|
| `build` (default) | Generate a full site/app — the main flow, documented below |
| `build_component` | Generate a single reusable React component instead of a whole site — see the `build_component action` section below |
| `list_templates` | Show available site types before committing to a `build` call |

## What it produces
A complete, self-contained static site: `index.html`, `style.css`, `script.js`,
saved into the user's sandbox workspace as a folder. Ready to:
- Preview via `@html_preview`
- Push to GitHub via `@github` (create_repo + create_file)
- Read/edit further via `@document_reader` or `@terminal`

## Standard applied to every generated site
This is non-negotiable baseline quality, applied automatically without the user
having to ask for it:

**Structure**
- Semantic HTML5 (`header`, `nav`, `main`, `section`, `footer`, not div-soup)
- One `index.html`, one `style.css`, one `script.js` — no inline styles/scripts
- Mobile-first responsive layout (flexbox/grid, no fixed pixel widths on containers)

**Design**
- Consistent spacing scale (multiples of 8px)
- Clear visual hierarchy — one primary heading, obvious CTA
- Real, readable placeholder copy relevant to the described business — never
  "Lorem ipsum" and never generic "Company Name" filler
- A cohesive color palette (3-4 colors max: primary, accent, background, text)
- System font stack by default unless a theme implies otherwise

**Accessibility**
- All images have `alt` text
- Sufficient color contrast (dark text on light background or vice versa, never
  low-contrast pairings)
- Interactive elements are real `button`/`a` tags, not clickable `div`s
- Logical heading order (one `h1`, nested `h2`/`h3`, never skipped levels)

**Performance / SEO basics**
- `<meta name="viewport">` always present
- `<title>` and `<meta name="description">` filled with real content
- No unused CSS classes, no dead JS

## Frameworks supported

**vanilla** (default) — plain HTML/CSS/JS. One `index.html`, one `style.css`, one `script.js`. Preview immediately via `@html_preview`.

**react-cdn** — single HTML file with React 18 + ReactDOM loaded from CDN (unpkg). No npm, no build step. Works in any browser, previewable immediately via `@html_preview`. Good for prototypes and lightweight interactive UIs.

**react-vite** — full Vite + React project scaffold: `package.json`, `vite.config.js`, `src/main.jsx`, `src/App.jsx`, `src/components/`. User runs `npm install && npm run dev` to activate. Good for real production React apps.

## build_component action

Generates a single reusable React component (`.jsx` file) with:
- Typed props (PropTypes or JSDoc)
- Clean functional component structure
- Scoped CSS module or Tailwind classes
- Usage example in comments

## edit action

Changes ONE specific part of a file that's already in the sandbox —
either something this skill built earlier, or a file pulled in via
`@github clone_to_workspace`. Does NOT regenerate the file — it makes
one targeted string replacement, same discipline as OADSON's own
editing tools: `old_str` must match the file's real, exact content in
exactly one place, or the edit is rejected (see the two examples below)
rather than guessing.

```
website_developer action='edit'
  file_path='king-chapel-admin/index.html'
  old_str='<div id="dashboard">'
  new_str='<div id="dashboard" hidden>'
```

**Read the file first if you're not certain of its exact current
content** — `@document_reader` or `@terminal cat` both work — rather
than reconstructing `old_str` from memory of what you generated
earlier or what a template usually looks like. A slightly-off guess at
whitespace or exact text is the single most common reason this action
fails.

## Working with an existing GitHub repo — the real workflow

Building a brand new site from scratch is `build`. But most real work
on an existing project is **changing** something in code that already
exists elsewhere — and that's a 3-skill loop, not a website_developer
job alone:

```
1. @github action='clone_to_workspace' owner='...' repo='...'
   → pulls the real repo into repos/{owner}__{repo}/

2. website_developer action='edit' (or @backend_developer action='edit',
   or @terminal for anything neither of those covers — see each skill's
   own scope)
   file_path='repos/{owner}__{repo}/index.html' ...
   → makes the actual change, in place, on the real cloned files

3. @github action='push_workspace' owner='...' repo='...' commit_message='...'
   → commits and pushes the change back
```

**Never regenerate a whole file with `build` to make one small change
to an existing project** — that discards everything else in the file
that `build` doesn't know about (custom edits, content the user or a
prior session added, anything outside this skill's own templates).
`build` is for genuinely new pages/sites; `edit` is for changing
something in a file that already has real content in it.

## Limitations
- `react-vite`: generates file structure only — OADSON can't run `npm install` server-side. User pushes to GitHub or runs locally.
- No image assets generated — uses CSS-only visuals, emoji, or placeholder blocks. Never fake `<img>` URLs that 404.

## After building

Files are saved to your sandbox workspace. To view the site:
- **Read/validate**: use `@html_preview` with `action='render_local'` and `path='folder/index.html'` — reads the file, shows text content and validation issues
- **Visual preview**: push to GitHub via `@github` and deploy to Vercel or Netlify (free, 1-click deploy)
- **Local**: use `@terminal` to `cat` the file or copy it to your local machine via git

`@html_preview` does NOT render HTML visually — it analyses the file. For actual browser preview, deploy it.
