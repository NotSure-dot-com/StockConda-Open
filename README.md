# StockConda Chess Engine

StockConda is a UCI chess engine written in Python with Cython/C++ extensions for move generation, evaluation, and its transposition table. Its search uses principal variation search, late move reductions, and move ordering heuristics. See [the UCI search lifecycle](docs/architecture/UCI_SEARCH_LIFECYCLE.md) for more detail.

The [v0.2.3.5.dev1 developer pre-release](https://github.com/NotSure-dot-com/StockConda-Open/releases/tag/v0.2.3.5.dev1) provides a Windows x64 onedir ZIP and the matching source ZIP. Extract the whole Windows ZIP, keep `_internal` beside `StockConda.exe`, and select the EXE as a UCI engine in Arena. The EXE is unsigned. See [Windows onedir](docs/release/WINDOWS_ONEDIR.md) for the build recipe and remaining validation limits.

## Install and start on Linux

The steps below were checked on Ubuntu 24.04 x86-64 with Python 3.12.3 and GCC/G++ 13.3.0. You need Git, Python with `venv` and development headers, a C/C++ compiler, pip, and access to a Python package index. Cloning requires access to this repository. Other platforms and Python versions have not been verified by this guide.

Run these commands in a Bash shell:

```bash
git clone https://github.com/NotSure-dot-com/StockConda-Open.git
cd StockConda-Open
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -c 'import chess_engine.cython_ext.cython_chess, chess_engine.cython_ext.evaluation, chess_engine.cython_ext.transposition'
python src/chess_engine/stockconda.py
```

For the history-free source ZIP, extract it and enter its `StockConda-0.2.3.5.dev1/` directory, then run the commands above starting at `python3 -m venv .venv`. For the sdist, extract `chess_engine-0.2.3.5.dev1.tar.gz`, enter its top-level directory, and use the same steps. Both contain the book and build sources; the wheel is outside this release scope.

`pip install -e .` builds and installs the native extensions. A separate `setup.py build_ext --inplace` step is not needed for this install path. The final command starts an interactive UCI process. Enter these commands one line at a time:

```text
uci
isready
position startpos
go depth 2
```

Wait for a `bestmove` line, then enter `quit`. The engine identifies itself as `StockConda v0.2.3.5.dev1 by Real`. The opening book is disabled by default. The included `opening_book.pkl` can be enabled with `setoption name Book value true`; the engine then loads the bundled source-tree book and uses a legal book move when available. Missing or unreadable book data leaves normal search available. The [third-party notices](THIRD_PARTY_NOTICES.md) record its user-identified source. A UCI-compatible chess GUI can launch the same Python command from the repository root with the virtual environment active.

## Tests

The active tests are in `tests/`. Historical tests and match evidence are retained in the private development repository and are not part of the release export or active suite.

To run a checked subset of the active UCI and repetition tests from the repository root in the same Bash environment:

```bash
python -m pip install -e '.[dev]'
STOCKCONDA_TEST_SOURCE="$PWD/src" PYTHONPATH="$PWD/src" python -m pytest -q tests/test_uci_async_stop.py tests/test_repetition_history.py
```

The release source passed 825 active tests in a controlled Linux environment. The full-suite command uses a controlled Python startup required by the AP-04B runtime-policy tests; copy that command from [CI](.github/workflows/ci.yml) when reproducing the full check. Neither the subset nor the suite establishes playing strength. The search checks threefold repetition using the position history it receives. A standalone FEN does not include earlier positions, so provide the move history through UCI when repetition matters.

## Playing strength and limits

In one local 20-game pilot at 120+1, a Linux build of the published source scored 13 wins, 2 draws, and 5 losses against Stockfish 19 with `UCI_LimitStrength=true` and `UCI_Elo=2000`. Ten openings were played with colors swapped. The raw score gives an informal estimate of **about 2150 on Stockfish's `UCI_Elo` setting scale**. The sample is small; this is neither a FIDE rating nor a measurement of the Windows binary.

Arena 3.5.1 registered and played with the Windows build. The user also reported manual Arena play with `Book` and `EvalBitboards` selected. `EvalBitboards` is advertised as a UCI option but its value is not read by the current search path, so selecting it does not activate bitboard evaluation. A clean Windows VM check remains outstanding.

## License

StockConda's own release is offered under [GNU GPL version 3 only](LICENSE). The package license expression also records the separately [CC0-licensed opening book](THIRD_PARTY_NOTICES.md); it does not relicense that data or the included upstream code. The UCI author name and package author alias do not establish copyright ownership over upstream work. See the [third-party notices](THIRD_PARTY_NOTICES.md). The release tag preserves the exact source ZIP content. `RELEASE_EXPORT_MANIFEST.json` describes that tagged snapshot; later documentation changes on `main` do not alter the published binaries or source archive.
