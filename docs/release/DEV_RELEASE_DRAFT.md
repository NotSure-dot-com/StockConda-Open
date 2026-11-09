# StockConda `0.2.3.5.dev1` — draft developer release notes

**Unreleased developer candidate. Source-only technical acceptance passed; publication requires separate user approval.**

## Intended scope

Source-based Linux developer build with Python 3.12 and locally compiled Cython/C++ extensions. Launch through UCI with `python src/chess_engine/stockconda.py` after installing the repository in a virtual environment. The opening book is disabled by default.

The local release export contains one owner-identified Stockfish Books-derived opening-book Pickle under CC0-1.0, plus the original GPL license for the included `cython-chess` code. StockConda's own release is GPL-3.0-only. It contains no historical Stockfish engine binary, private Git history, or archived match results. The private development repository retains its historical evidence.

## Current integration baseline

- Persistent UCI search worker, cancellation and shutdown behavior, corrected search time/depth composition, and TT repetition/50-move context are documented in the project work packages.
- PVS/QSearch move-ordering and evaluation improvements are present in the current source. The AP-04B runtime-policy file is kept LF on checkout through `.gitattributes`.
- The current source-export candidate passed 825 active tests, strict AP-04B mypy, and UCI book/no-book smoke. The earlier GitHub Correctness CI passed for PR #8. These results do not establish an Elo gain or a performance improvement for this proposed release.

## Known limits

- Tested installation platform for this draft: Ubuntu 24.04 x86-64, Python 3.12.3, GCC/G++ 13.3.0. Other platform builds have not been verified by this guide.
- Threefold repetition requires the move history supplied through UCI; a standalone FEN does not contain that history.
- The `Book` UCI option is disabled by default and loads the bundled Pickle when enabled. Missing or invalid book data falls back to normal search. No opening-book strength claim is made for this draft.
- The active suite excludes archived tests. CI does not run Elo, Fastchess, or performance gates.
- The source export and rebuilt sdist contain the Cython sources, book data, and license notices. A locally built wheel omits the book and is outside this source-based release scope.
- The owner chose GPLv3 for this release; upstream copyright notices remain intact. Package and UCI versions agree at `0.2.3.5.dev1`. Opening the private repository would require a separate historical binary/privacy review; the proposed artifact is the history-free source archive. See [release readiness](RELEASE_READINESS.md).
