---
name: release-docs (shared)
purpose: Shared README + project-notes update protocol used by /release. Single source of truth for how docs get updated at ship time.
---

# Release docs protocol

When releasing a change, treat the README, your project notes, and the commit as ONE atomic unit. Do not ship code without updating the docs that describe it.

## 1. Update README.md

Read the current README first to match its tone and structure.

Write a full subsection (or update an existing one) covering:
- **What** the feature or fix is
- **How** it works at a level a future reader needs (user-facing behavior, not implementation detail)
- **Why** it matters

If the README has a version number or changelog, bump it and date the entry.

A future reader should understand the change from `README.md` alone, without reading the diff. Not a one-line table tweak, a real subsection.

## 2. Update the project notes file

Most projects keep a working-notes file that assistant sessions and teammates read to get context (for Claude Code that is usually a `CLAUDE.md` in the repo root or a per-project data dir; adapt to whatever your project uses).

If your project has one, add the technical details a future session will need:
- New environment variables and where they are set
- New external dependencies and what they are for
- Schema, API, contract, or column changes
- Service IDs / project IDs / connection references
- Surprising behaviors discovered during implementation
- "If X breaks, look at Y" notes

Remove or update any "Planned" / "Pending" sections that this release just delivered.

If your project does not keep a notes file, skip this step.

## 3. Guardrails

- Never commit `.env`, credentials, API keys, service-account JSON, or anything with a token in it. If detected in the staged set, stop and report it.
- Never use `git add -A` or `git add .`. Stage specific files by name.
- Never skip hooks with `--no-verify` unless explicitly asked.
- Never amend a published commit. Create a new commit.
- If there are no changes to commit, say so and stop. Do not create an empty commit.

## 4. Writing style for README and notes

Keep the prose plain and human:
- No em dashes; use commas, periods, or parentheses.
- No marketing adjectives or significance inflation.
- No "Not just X, but also Y" constructions, no rule-of-three padding.
- Straight quotes only.
- Tables in markdown pipe format, never Unicode box-drawing characters (they break when copied into Google Docs, Word, Slack, or Notion).
