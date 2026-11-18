# Aurora MVC changelog

Keep the changelog while building Aurora, not after a release is finished.

- Record each meaningful developer-facing addition, change, fix, or retirement in [Unreleased](UNRELEASED.md) as the work is accepted. A short bullet is enough.
- Update the relevant bullet when the implementation changes. Don't record every commit, test run, or internal refactor that leaves developer behavior unchanged.
- Before committing a change, check whether the changelog needs an entry. If it doesn't, say why in the pull request or review notes.
- Before publishing a release, check every bullet against the shipping source and acceptance evidence. Remove items that didn't ship, write the GitHub release notes from the verified list, and save a dated, version-named copy here.
- After publication, clear the Unreleased sections for the next development cycle. Never change the already published version's file to rewrite history.

Start the versioned files with the **next release we actually publish**. We won't reconstruct previous alpha releases here; their published notes and Git history are still available.

Keep it short, specific, and natural. Never add private project notes, credentials, internal acceptance logs, or unreleased promises to a published changelog.
