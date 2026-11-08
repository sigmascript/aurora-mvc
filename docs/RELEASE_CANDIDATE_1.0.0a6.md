# Aurora MVC 1.0.0a6 — release candidate checklist

**Historical record — subsequently published.** This checklist captures the original pre-publication planning for `1.0.0a6`, which was later published on GitHub and PyPI with immutable tag `v1.0.0a6` at `9e16812`. Unchecked boxes below reflect the **original preparation snapshot**, not today's release status. For the current `1.0.0a7` candidate, see [its separate checklist](RELEASE_CANDIDATE_1.0.0a7.md). Do not modify the historical tag.

## Scope

- Integrated AuroraSQL is the canonical `aurora.database` backend; retired SQLAlchemy backend and tests removed.
- Six database CLI commands retained, with supported operations guarded by explicit safety contracts.
- Public docs corrected for cutover and disabled restore execution.
- No broad test renaming or source beautification in this release; those are post-release work.

## Evidence already supplied by team

- Git main cutover commit: `0bb5c0c`.
- Local full regression: **1,021 passed, 35 skipped, 247 subtests passed** on 2026-11-02.
- Separate authorized live MySQL/PostgreSQL acceptance: **33 passed** (team report).
- Reinstalled editable package metadata lists `Jinja2` only as a mandatory dependency.

## Required release gates — not yet verified for this prepared ZIP

- [ ] Compare replacements against current working tree and review `git diff`.
- [ ] Confirm the proposed `1.0.0a6` version is unused on PyPI.
- [ ] Run full regression after applying replacements; compile source and `git diff --check`.
- [ ] Build wheel and sdist with `python -m build`.
- [ ] Validate both artifacts with `python -m twine check dist/*`.
- [ ] Test clean installation of the built wheel in a fresh virtual environment and exercise CLI/project generation.
- [ ] Review packaged files, dependencies, license, README, and final docs.
- [ ] Obtain explicit approval for commit, push, tag, GitHub release, and PyPI upload.
- [ ] Verify uploaded release from a fresh installation after publication.

## Next milestones

1. Naming/readability cleanup and audit of all intentional skips.
2. Aiomax-focused migration coverage (additive changes first; safe alteration/rename contracts next).
3. Guarded backup and restore acceptance, including disposable recovery testing.
4. Requirements-driven framework essentials and Aiomax integration (sessions, authentication, multilingual routing, ergonomics).

Permanent principles: **Simple, Developer-Friendly, Secure, Reliable**. Complete replacement files; meaningful consolidated milestones; focused tests and milestone regressions; live DB safety; explicit release approval.
