# Aurora MVC — Code Style and Commenting Policy

Status: pre-release working standard. This is a policy, not a claim that all source files have been reformatted.

## Philosophy

The visual complexity of code should match the conceptual complexity of the operation.

- Good names explain **what**.
- Good structure reveals **flow**.
- Comments explain **why**, including constraints, compatibility, assumptions and security boundaries.
- Docstrings explain **usage** of public-facing APIs.
- Type hints communicate **contracts** where useful.
- Tests verify **behavior**.

Prefer expressive Python, short straightforward expressions, multiline formatting for meaningful structure, clear names, and early returns where they reduce nesting. Use PEP 8 as a baseline. Avoid redundant comments, decorative headings, artificial one-line compression and noisy local type declarations.

## Change discipline

1. Keep style-only edits separate from behavior changes.
2. Do not rewrite tests to accommodate a formatting change.
3. Preserve public APIs, response codes, exception behavior, database safety and ASGI lifecycle semantics.
4. Use incremental batches with reviewable diffs and a full regression run.
5. Consider a formatter/linter only after selecting and pinning its configuration; never run an unreviewed whole-repository auto-fix.

## Current status

The first pass fixes missing method spacing in the core route modules and records this policy. The wider beautification pass remains **in progress**, not completed.
