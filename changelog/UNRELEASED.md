# Unreleased

Changes accepted after `1.0.0a8` belong here until the next version is prepared. Keep entries short and developer-facing; review the list before every release.

## Added

- Maintain package-local Aurora model class exports on CLI creation/removal for MVC shared and Modular app models, preserving custom package code.
- Export `JSONResponse` from `aurora` without changing its existing import path or behavior.

- Support project-aware AuroraSQL CRUD without manual connection plumbing, plus owned `aurora.db.connection()` scopes and explicit `aurora.db.transaction()` blocks; preserve named-database isolation and existing explicit APIs.

- Add a `production` boolean to all generated project configurations; report development exceptions in server logs while keeping HTTP errors sanitized.

- Accept one positional mapping in `Model.create({...})` and `Model.using(connection).create({...})`; return native dictionaries.

## Changed

- Explain MVC/Modular model imports, ordinary CRUD, form/JSON request handling, controller HTTP verbs, and connection/transaction usage in the existing public guides.

- Return native dictionaries from model `create()` and retire keyword-based column input; supply one positional mapping instead (breaking change from Alpha 8).
- Convert supported returned dictionaries, lists, and materialized AuroraSQL read/JOIN records into JSON HTTP responses, with optional `(data, status)` shorthand.

- Support synchronous controller `request.form()` and `request.json()` through bounded, on-demand parsing without blocking the ASGI event loop; keep async parsing unchanged.
- Allow ordinary undefaulted required columns on verified empty MySQL/PostgreSQL tables, with a fresh emptiness check before DDL.
- Permit an ordinary undefaulted NOT NULL column on a verified empty SQLite table through a reviewed atomic rebuild; refuse populated tables.
- Support reviewed SQLite and PostgreSQL atomic mixed table transitions; MySQL creates replacement tables before the reviewed removal and reports uncertain partial DDL without automatic retry.
- Allow reviewed removal of child tables with outgoing foreign keys when surviving tables have no dependent references; retain destructive confirmations and integrity checks.

## Fixed

- Explain ineligible repeated SQLite initialization and direct ready databases to `aurora db init` without changing database state.

- Preserve Windows CRLF model-package files when reviewing and updating managed exports.

- Keep nested connection scopes inside an active transaction on the same owned connection, so outer rollback covers their writes; reject cross-thread scope reuse.

- Allow reviewed SQLite parent-table UNIQUE changes when incoming foreign keys reference unchanged columns, preserving child data and forward history.

## Removed or retired

- Retire the unused `Application(environment=...)` option and the redundant `AURORA_ENV` operational switch; project-local `production` now protects CLI and database modifications.
