# Aurora MVC — Code Style and Commenting Policy

Status: **mandatory for every new or modified source and test file**. Existing files will be audited in a separate behavior-preserving maintainability milestone; this is not a claim that all files already comply.

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
4. Consolidate related changes into meaningful, reviewable milestones. Run focused tests while implementing and full regression at milestone gates.
5. Consider a formatter/linter only after selecting and pinning its configuration; never run an unreviewed whole-repository auto-fix.

## Naming, spacing, and review

- Name modules, classes, functions, and tests for **stable responsibilities and observable behavior**, never temporary batch, stage, alpha, or checkpoint labels.
- Use PEP 8 spacing: two blank lines between top-level definitions, one between class methods, and clear separation between meaningful logical blocks. Avoid compressed one-line control flow.
- Add concise comments for non-obvious intent, compatibility, database safety, and lifecycle invariants; do not narrate obvious assignments.
- Apply these standards to every change, including a few new lines. When an old file is touched, improve the changed region without mixing broad formatting changes with behavioral fixes.
- Supply complete replacement files for every changed source/test file during collaborative implementation. Preserve safety gates and public compatibility.

## Current status

The wider source/test readability audit and renaming of historical batch/alpha test modules are **deferred to a dedicated post-publication milestone**. No claim of repository-wide completion is made.
