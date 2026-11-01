# Aurora MVC — Alpha 9 cutover candidate

Aurora MVC is a lightweight, ASGI-first Python MVC framework.

**Status:** Unreleased, isolated Alpha 9 database-retirement candidate. This working tree is **not** an approved release or a published package. Its package metadata currently reports `1.0.0a5`; do not interpret that version as Alpha 9 publication.

The proposed cutover replaces the SQLAlchemy-backed `aurora.database` implementation with AuroraSQL while retaining the public `aurora.database` import namespace. The six database CLI commands are `init`, `plan`, `migrate`, `backup`, `restore`, and `history`. Database writes and restores remain subject to explicit safety gates; some restore operations are intentionally disabled.

**Verification:** The isolated candidate passed its complete automated regression (1,021 passed, 34 skipped, 247 subtests passed) on 2026-11-02, and Python source compilation passed. These results include SQLite-backed tests but do not establish live MySQL or PostgreSQL acceptance.

**Acceptance pending:** Live MySQL/PostgreSQL verification where available, recovery safety review, release-version decision, and explicit team approval before applying the replacement to the project checkout. The candidate must not be treated as production-ready.
