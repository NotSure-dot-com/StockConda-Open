# Third-party components and release scope

This source tree builds StockConda's Cython/C++ modules locally. It includes adapted code from the independent `cython-chess` project and one opening-book data file. It does not vendor installed Python dependency packages or Stockfish binaries. Installation resolves Python dependencies separately; their own packages carry their respective license texts. This inventory is not a substitute for reviewing the exact resolved distributions before publication.

## Included upstream source and data

- `src/chess_engine/cython_ext/cython_chess.pyx`, `cython_chess_backend.cpp`, and `cython_chess_backend.h` derive from [Ranuja Pinnaduwage's cython-chess](https://github.com/Ranuja01/cython-chess), itself based on python-chess. Their embedded headers state Copyright (C) 2025 Ranuja Pinnaduwage and GPL v3 or later. The local `.pyx` has project changes. The two backend files match upstream commit `af5ae0a014084d1654adc32ed568d2fe6e9c26e8` byte for byte; this comparison does not establish the precise revision originally imported. The [original upstream GPL license](third_party/licenses/cython-chess-GPL-3.0.txt) is preserved without alteration. This is a separate library from the Apache-licensed Cython compiler below.
- `opening_book.pkl` is identified by the project owner as converted from [Stockfish Books `8moves_v3.pgn.zip`](https://github.com/official-stockfish/books/blob/master/8moves_v3.pgn.zip). That repository supplies [CC0-1.0](third_party/licenses/stockfish-books-CC0-1.0.txt); its [original license](https://github.com/official-stockfish/books/blob/master/LICENSE) is included. Local code has generic PGN/book serialization and Pickle save/load methods, but no dedicated conversion recipe or exact source hash for this particular Pickle was found. The owner identification is provenance evidence, not a claim of byte-exact reproducibility. The old duplicate Pickle is excluded from the release export.
- The eight mobility arrays in `src/chess_engine/cython_ext/evaluation.pyx` match the middle-/endgame values of [Stockfish 12 `MobilityBonus`](https://github.com/official-stockfish/Stockfish/blob/sf_12/src/evaluate.cpp). Stockfish's original notice is Copyright (C) 2004-2020 The Stockfish developers ([AUTHORS](https://github.com/official-stockfish/Stockfish/blob/sf_12/AUTHORS)); its terms are GPL version 3 or later. This table was split into local arrays in commit `b1e3681` on 2025-10-23; the exact upstream import revision was not recorded. A dated modification and attribution notice appears next to the arrays. The engine binary and the rest of Stockfish's source are not included.

## Active test-fixture origin

- `tests/fixtures/history_depth_preflight.uci` freezes a 253-ply position from the project's local depth-16 preflight diagnostic, introduced with test commit `6266d6f` on 2026-09-09.
- `tests/fixtures/tt_history_long.uci` freezes the first eligible 120-ply prefix of game 9 from the project's local `tt_halfmove_120p4_20g_1` match PGN, introduced with test commit `1c713c2` on 2026-09-09. The private PGN is not in the export. These are move-only UCI test inputs; no external game annotation or PGN is packaged.

| Direct dependency | Use in this project | Upstream license source |
| --- | --- | --- |
| `chess` (python-chess) | Runtime board representation, move handling, and UCI client support in tests | [GPL-3.0-or-later](https://github.com/niklasf/python-chess#license) |
| NumPy | Build and opening-book module imports | [BSD-3-Clause with listed vendored components](https://github.com/numpy/numpy/blob/main/doc/source/license.rst) |
| Cython | Build of native extensions; also currently listed in runtime metadata | [Apache-2.0](https://cython.org/) |
| pandas | Currently listed in package dependencies; not imported by active engine source | [pandas license information](https://pandas.pydata.org/about/governance.html) |
| Matplotlib | Currently listed in package dependencies; not imported by active engine source | [Matplotlib license](https://matplotlib.org/stable/project/license.html) |

The private development repository contains historical Stockfish engine binaries and match data. They are excluded from the local release export. [Stockfish's terms](https://github.com/official-stockfish/Stockfish#terms-of-use) govern any separate redistribution of its engine binary. The private repository's existing history is not rewritten.

The project owner has chosen [GNU GPL version 3 only](LICENSE) for StockConda's own release. Package metadata uses `GPL-3.0-only AND CC0-1.0` to describe the combined source export: the book remains CC0-1.0, while the existing GPL-3.0-or-later notices in upstream `cython-chess` files remain intact. This is not a claim of copyright ownership over upstream code. The package author alias identifies the project in UCI and metadata; it does not prove ownership of all authored files.
