# Developer release readiness (2026-11-08)

Historical acceptance snapshot for the source archive. Its statements about an unpublished candidate and the private development repository describe the situation on that date. This public repository starts from the reviewed history-free export, not from the private repository's history.

The current `v0.2.3.5.dev2` release assessment is in [DEV2_RELEASE.md](DEV2_RELEASE.md); the `dev1` counts and publication checklist below are historical.

Status: **GO for the history-free source-only developer candidate** after local technical and included-source review. Publication remains a separate user action; this document does not authorize a tag, package upload, repository visibility change, or installer.

## Proposed version and scope

The selected first developer release identifier is `0.2.3.5.dev1`, using the [Python development-release segment](https://packaging.python.org/en/latest/specifications/version-specifiers/#developmental-releases). `pyproject.toml`, `setup.py`, and both UCI identity lines now use that identifier. It is a local candidate, with no tag or publication yet.

The proposed artifact is a source-based Linux developer export without `.git` history, installed in a virtual environment with native extensions built locally. The documented platform is Ubuntu 24.04 x86-64, Python 3.12, and GCC/G++ 13.3. Windows/macOS builds, wheels, installers, GUI integration, and playing-strength claims are outside this initial scope. [Third-party notices](../../THIRD_PARTY_NOTICES.md) describe direct dependencies.

## Reviewed release boundaries

1. **Included-source audit.** The project owner selected GPL version 3 only for StockConda's own release. Package metadata records `GPL-3.0-only AND CC0-1.0` for the combined source artifact. The runtime dependency `chess` is [GPL-3.0-or-later](https://github.com/niklasf/python-chess#license); included modified [cython-chess](https://github.com/Ranuja01/cython-chess) files retain their GPL-3.0-or-later headers, copyright and dated modification notice. [Stockfish 12 mobility values](https://github.com/official-stockfish/Stockfish/blob/sf_12/src/evaluate.cpp) are attributed at the tables and in the third-party notices. The book remains CC0-1.0. The two active `.uci` fixtures trace to local regression and match records, also described in the notices. No further included third-party source or asset with a concrete unresolved license was identified in this reviewed export. The author alias is not a claim of legal ownership over upstream work.
2. **Release-export boundary.** One opening-book Pickle is retained, based on the owner's identification of [Stockfish Books `8moves_v3.pgn.zip`](https://github.com/official-stockfish/books/blob/master/8moves_v3.pgn.zip) and the upstream CC0-1.0 license. The exact Pickle conversion was not reproduced. The duplicate copy, historical Stockfish engine binaries, match data, and archived tests remain in the private repository/history but are excluded from the `.git`-free export. The selected-file manifest, independent hashes, and high-confidence path/token scan passed. Opening the existing repository would require separate rights and history review, including [Stockfish engine terms](https://github.com/official-stockfish/Stockfish#terms-of-use).
3. **Private-history boundary.** The existing repository history contains a Windows shortcut and absolute user-home paths. The proposed export excludes them and the history. No high-confidence token pattern was found in the earlier heuristic scan, but that scan is not a guarantee. Do not change repository visibility without a separate privacy review.
4. **Artifact and version.** Version identifiers are aligned at `0.2.3.5.dev1`. The tested code tree is commit `295564bd5953933882c00e49bf429875db6d150b`; the subsequent release-note update changes documentation only. The source archive remains untagged and unpublished. The wheel omits book data and is outside this release scope.

## Verified technical baseline

- The AP-04B LF checkout correction was merged in PR #8 as `2c4884f6d259ce873efff5e1cf749d00c99a9bc6`. Its isolated tested tree had 814 active tests passing, strict AP-04B mypy on nine files passing, and a UCI smoke passing. [Correctness CI run 42405839881](https://github.com/NotSure-dot-com/StockConda/actions/runs/42405839881) passed on the matching PR head. These are correctness checks, not strength or speed measurements.
- CI's active suite needs the controlled Python startup in [the workflow](../../.github/workflows/ci.yml). Historical tests in `provenance/` remain excluded.
- The additional engine mypy 2.4.1 command reports a `HistoryTable.get` override at `stockconda.py:146` on both the unchanged base and the LF-fix snapshot; it is separate from the nine-file AP-04B gate.
- A fresh editable install and native build from the local source-export candidate succeeded on the listed Linux platform. The controlled active suite passed 825 tests, the established strict type gate passed for nine files, and UCI smoke passed with the book present and temporarily absent. The 127-member sdist includes Cython `.pyx` sources, book, active test harness/fixtures/scripts, release notes, and all license texts. It built a wheel from its extracted sources, collected 825 tests, and passed the 11 Book tests plus UCI smoke after installation. The wheel omits book data and is not a supported release artifact under the source-based scope.
- The Book option is disabled by default. A targeted regression suite now checks that enabling it loads the bundled source-tree Pickle and publishes a legal book result, including from a foreign working directory; disabling, re-enabling, missing and damaged data, and illegal results use the normal search path as appropriate. No book-related Elo claim is made.

## Release checklist

- [x] Choose GPLv3 for the project's own release, replace the placeholder MIT license, and preserve upstream and book notices.
- [x] Review the included code and fixtures for concrete additional rights issues; document the Stockfish table, cython-chess modification, and local fixture origins. Preserve all upstream notices.
- [x] Confirm the local `.git`-free candidate contains one identified book copy, original upstream license texts, and no historical Stockfish engine binary, private paths, or unneeded match data; retain private original evidence.
- [x] Review the two retained UCI fixture origins and asset inventory. A public Git repository still needs a separate historical rights/privacy decision.
- [x] Select `0.2.3.5.dev1`, align package and UCI identifiers, and prepare dated draft release notes.
- [x] Build and test the local source-export candidate, including UCI, active correctness/type gates, and source/license inventory.
- [x] Rebuild and install from the tested source commit, verify dependency versions, source archive, license inventory, UCI book behavior, and the collected active tests. `MANIFEST.in` includes release notes in the sdist.
- [ ] Only after those decisions, consider a tag, upload, or visibility change as a separate authorized action.

See [draft release notes](DEV_RELEASE_DRAFT.md). Detailed local audit evidence is retained privately outside the release export.
