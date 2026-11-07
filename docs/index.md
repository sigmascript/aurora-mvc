# Aurora MVC documentation

Aurora MVC is an ASGI-first Python web framework with MVC-friendly APIs. This documentation accompanies the **`1.0.0a6` release candidate** following the Alpha 9 AuroraSQL cutover. Publication is pending verification and approval.

The integrated `aurora.database` namespace now uses **AuroraSQL**. The former SQLAlchemy-backed database implementation has been retired. Database features are alpha-stage and backend-specific; consult the CLI help and safety guide before writing schemas.

## Start here

- [Getting started](getting-started.md) — installation, project creation, development server
- [CLI reference](cli.md) — commands and safety boundaries
- [Project templates and examples](project-templates.md) — generated layouts and examples
- [Application configuration](application-configuration.md) — validated defaults, time zones, limits, and read-only constants
- [Routing and controllers](routing.md) — routes, actions, and handlers
- [Authentication and safe redirects](security/authentication-redirects.md) — request guards and safe return paths
- [Database and migrations](database.md) — AuroraSQL, configuration, supported guarded operations
- [Multilingual applications](multilingual.md) — locale configuration and URL design

## Contributors and historical reference

- [Code style](development/CODE_STYLE.md) — mandatory standards for new and modified code
- [Alpha 8 compatibility notes (historical)](aurorasql-alpha8-compatibility.md) — pre-cutover snapshot, not current implementation guidance

NoteFlow is maintained separately as a reference application. Older implementation details remain in Git history.
