# Aurora MVC 1.0.0a7 — release candidate checklist

**Status: PREPARATION ONLY — NOT PUBLISHED.** No Git tag, GitHub Release, PyPI upload, or production approval is implied by this document. Preserve the published `v1.0.0a6` tag.

## Candidate scope

- Milestone 4 AuroraSQL daily workflow: automatic shared/app model discovery in three generated layouts; one default database with optional aliases; reviewed SQLite schema updates and guarded MySQL/PostgreSQL Initial plus repeated successor migrations.
- One SQL-side validated model column `default=` contract, native linear migration history, safe backend-specific backups, and read-only history verification.
- Public AuroraSQL database commands remain exactly `init`, `plan`, `migrate`, `backup`, `restore`, and `history`.
- Public restore execution remains disabled; server explicit JSON schema-operation execution remains unavailable. Mixed/destructive and advanced dependent changes refuse. No live recovery rehearsal or production readiness claim.
- Release-only polish: version consistency, truthful CLI help, published-versus-candidate documentation, and distribution metadata.

## Historical acceptance before release polish

- Accepted Milestone 4 source baseline: `587b67ecc97335e438bf633e390f1b2c3ceb93f6`, clean/aligned with fetched `origin/main` on 2026-11-08.
- Batch B Windows Python 3.11.9: **1,405 passed / 37 skipped / 250 subtests**; Linux: **1,408 passed / 34 skipped / 250 subtests**.
- Dedicated disposable MySQL 8.0.33 and PostgreSQL 14.1: public reviewed Migration3 `VARCHAR(160)` to `VARCHAR(200)`, original data preserved, three-entry history and journal independently inspected, zero findings with `history --verify-models`.
- This does not certify native restore execution or production deployment. Do not replay those live DDL probes for release preparation.

## Final candidate gates — evidence must be recorded before publication

- [ ] Review complete replacement files and compare against `587b67e` using `git diff --check`; confirm only approved release polish.
- [ ] Confirm `1.0.0a7` is available on PyPI immediately before upload; do not overwrite an existing distribution.
- [ ] Full Linux regression on final candidate, compileall, README generator `--check`, and version tests.
- [ ] Full Windows Python 3.11 regression on final candidate, compileall and whitespace checks.
- [ ] Check relevant Python compatibility and CI where available; avoid asserting unexecuted matrix results.
- [ ] Build sdist and wheel from the candidate, inspect package contents and metadata, validate with Twine, and independently rebuild/install where practical.
- [ ] Clean wheel install into an isolated environment; smoke test CLI, all three `aurora new` layouts, and the six-command help/refusal contract.
- [ ] Check optional MySQL/PostgreSQL driver extras in isolated installs; do not run extra live DDL without separate approval.
- [ ] Review release notes, exact version strings, PyPI long description, immutable tag target, and GitHub release artifacts.
- [ ] Obtain explicit team approval for commit/push, then **separate publication authorization** for tag, GitHub Release and PyPI upload.
- [ ] After publication, independently verify PyPI metadata, downloadable artifacts, installed CLI, GitHub tag SHA, and remote branch alignment.
- [ ] Only then update governing documents with actual published SHA/artifact evidence.

## Handoff into Milestone 5

Use a verified published `1.0.0a7` alpha as the installable foundation for real-world usability testing. Broader NoteFlow/Aiomax integration, major API redesign, general-purpose ORM functions, optional MongoDB support, and new database commands are outside this release.
