# Aurora MVC documentation

Aurora MVC is an ASGI-first Python framework with MVC-friendly APIs. This documentation describes the unreleased Alpha 5 (`1.0.0a5`) release candidate. The latest release identified by the repository documentation is Alpha 3 (`1.0.0a3`); verify current publication status on PyPI. The existing SQLAlchemy-backed database API and six-command CLI remain active. The separate `aurora.aurorasql` package is experimental and opt-in, not the default backend or CLI implementation.

## Start here

- [Getting started](getting-started.md) — install locally, create and run an app
- [CLI reference](cli.md) — current commands and flags
- [Project templates and examples](project-templates.md) — three generated layouts and four repository examples
- [Routing and controllers](routing.md) — routes, actions, and handlers
- [Authentication and safe redirects](security/authentication-redirects.md) — optional guards and return paths
- [Database and migrations](database.md) — SQLite workflows and guarded MySQL operations through the six-command database CLI
- [Multilingual applications](multilingual.md) — locale configuration and URL design

## Contributors

- [Code style](development/CODE_STYLE.md)

The former Free Notes example and its historical documentation have been retired from the active repository. NoteFlow is maintained as a separate reference application. Historical changes remain accessible through Git history.
