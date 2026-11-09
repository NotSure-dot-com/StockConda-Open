"""
Cython extensions for the chess engine.
"""

import chess

__all__ = []

# Import and expose functions from evaluation module
try:
    from .evaluation import evaluate_board_piece_iter_phases, evaluate_board_bitboard_phases
    __all__.extend(['evaluate_board_piece_iter_phases', 'evaluate_board_bitboard_phases'])
except ImportError:
    # Create stub functions that provide informative errors when Cython module is not compiled
    def evaluate_board_piece_iter_phases(board: chess.Board) -> int:
        raise ImportError(
            "evaluate_board_piece_iter_phases is not available. "
            "The Cython extension module needs to be compiled. "
            "Run 'python setup.py build_ext --inplace' from the project root."
        )

    def evaluate_board_bitboard_phases(board: chess.Board) -> int:
        raise ImportError(
            "evaluate_board_bitboard_phases is not available. "
            "The Cython extension module needs to be compiled. "
            "Run 'python setup.py build_ext --inplace' from the project root."
        )

# Import and expose functions from transposition module
try:
    from .transposition import (
        init_cython_tt,
        init_zobrist,
        free_cython_tt,
        store_tt_entry,
        probe_tt_entry,
        probe_tt_move
    )
    __all__.extend([
        'init_zobrist',
        'init_cython_tt',
        'free_cython_tt',
        'store_tt_entry',
        'probe_tt_entry',
        'probe_tt_move',
    ])
except ImportError as e:
    print(f"Warning: Could not import transposition module: {e}")

