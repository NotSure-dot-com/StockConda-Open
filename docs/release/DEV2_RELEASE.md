# StockConda `v0.2.3.5.dev2` — developer prerelease

This release continues the public `StockConda-Open` repository. It contains the already integrated private development changes since `dev1`, exported as selected source files without private Git history or archived experiments.

## Changes since `dev1`

- UCI Ponder search and `ponderhit`, including controlled stop and shutdown behavior.
- The PVS re-search condition avoids redundant work on an unreduced null-window move.
- UCI `info score mate` reports mate distance rather than a raw centipawn number.
- Opening-book option and finite/infinite-search handling fixes. The Book option remains off by default.
- `MaxDepth` defaults to the supported value 64. The ineffective `EvalBitboards` option is no longer advertised.
- The matching source distribution and locally built wheel include an identical package-local copy of the CC0 book. The Windows onedir package keeps its book beside the EXE.

These are implementation and interface changes. The earlier AP-04B/native-evaluation work was already in `dev1`. No new Elo gain or numerical strength rating is claimed for this head. The prior `dev1` 20-game LimitStrength pilot is not evidence for `dev2` or for the Windows binary.

## Artifacts and scope

The release assets are `StockConda-0.2.3.5.dev2-source.zip`, `StockConda-Windows-x64-onedir.zip`, and `SHA256SUMS`. Extract the full Windows ZIP and add `StockConda/StockConda.exe` to Arena as a UCI engine. It is unsigned. The source ZIP corresponds to the published tag and is also included in the Windows ZIP with source and dependency license notices. Source and Windows checks are recorded in the release audit; a clean Windows VM and a fresh automated Arena GUI test have not been performed.

StockConda's own source is offered under GPL version 3 only. Included cython-chess code retains its GPL version 3 or later notices; the Stockfish mobility-table attribution, python-chess dependency notice, and the owner-identified CC0 Stockfish Books provenance are in [third-party notices](../../THIRD_PARTY_NOTICES.md). The exact conversion of the opening-book Pickle remains unverified. The public repository contains no private development history.
