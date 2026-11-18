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

## Indentation and generated files

- Use **four spaces per indentation level** for Python and all maintained JSON
  configuration, manifest, snapshot, and review artifacts. Never indent with tabs.
- JSON examples in public documentation and generated project files use the same
  four-space convention. Preserve valid Markdown/YAML syntax where their structure
  requires a different treatment; do not mechanically replace all whitespace.
- Apply this format to **new and modified files going forward**. Existing database
  histories, migration snapshots, reviewed digests, and backup manifests are
  immutable evidence: **never rewrite them solely to reindent**.
- Format-only work must not change database semantics, approval hashes, or
  historical interpretation. Verify generated files and the full regression.

## Documentation voice and examples

Write for another developer, not for an internal report. Use everyday language,
short explanations, and real commands or complete examples where they help. Speak
directly and say what works, what does not, and what to do next. Avoid bureaucratic,
robotic, repetitive, or promotional phrasing. Don't narrate milestones in a user guide.

Review examples against the actual source. Label unreleased functionality clearly;
never imply that a feature is available in the last published package just because
it works in the development checkout. Keep private engineering checkpoints out of
public documentation.

## Changelog and release notes

At each meaningful developer-facing change, update `changelog/UNRELEASED.md`
with a concise bullet. Make this part of the code-review and commit checklist;
when no entry is needed, explain why in the review. The release maintainer checks
the list against the shipping code, creates the published version's changelog,
and writes GitHub release notes from verified changes. No retrospective guessing.

## Current status

The wider source/test readability audit and retirement of historical modules are
separate reviewed cleanup work. The existence of this policy does not mean every
file already meets it.
