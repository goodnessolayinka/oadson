# Git Helper

## What It Does
Provides Git command references, workflow guidance, and troubleshooting help. Generates git commands for common operations: status, commit, branch, merge, rebase, stash, remote management.

## When to Use It
- User asks how to do something in Git
- User wants the exact command for a Git operation
- User troubleshooting merge conflicts, detached HEAD, lost commits
- User learning Git workflows (feature branch, gitflow, trunk-based)

## When NOT to Use It
- User wants to interact with GitHub repos (PRs, issues, stars) → use github
- User wants to run Git commands live on OADSON's server — no shell access
- Question is general version control theory — answer directly

## Calling Convention
This is an `action`-based skill — call it with `action` set to one of:

| Action | What it does |
|---|---|
| `status` | working tree changes. |
| `log` | commit history. |
| `branches` | all local+remote branches. |
| `remote` | remote URLs. |
| `diff` | uncommitted changes. |
| `stash` | stashed changes list. |

## Output Format
```json
{
  "command": "git reset --soft HEAD~1",
  "explanation": "Moves HEAD back one commit but keeps changes staged",
  "alternatives": ["git reset --hard HEAD~1 (destructive)"],
  "use_cases": ["Undo accidental commit", "Amend before push"],
  "warning": "Don't use on pushed commits without force push"
}
```

## Constraints
- Command reference only — no live execution
- Covers Git 2.x standard commands
- GitHub-specific features (Actions, Pages) → use github skill
- Advice is for standard workflows — custom hooks/configs vary

## Examples
- "How do I push only one file?"
- "What's the difference between merge and rebase?"
- "How to set up upstream for my fork?"
- "Undo a git add before committing"
