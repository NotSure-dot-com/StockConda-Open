"""
Chess engine module with Cython-accelerated components.

This module provides a high-performance chess engine with UCI protocol support,
opening book functionality, and optimized evaluation functions.
"""

import logging
import chess
import chess.engine
from typing import Optional, Union

# Configure logger for this module
logger = logging.getLogger(__name__)

# Import the compiled Cython modules
try:
    from .cython_ext import (
        evaluate_board_piece_iter_phases,
        init_zobrist,
        init_cython_tt,
        free_cython_tt,
        store_tt_entry,
        probe_tt_entry,
        probe_tt_move,
    )
    # For backward compatibility
    from .cython_ext import evaluate_board_piece_iter_phases as evaluate_board
    
    __all__ = [
        'evaluate_board',
        'evaluate_board_piece_iter_phases',
        'init_zobrist',
        'init_cython_tt',
        'free_cython_tt',
        'store_tt_entry',
        'probe_tt_entry',
        'probe_tt_move',
        'find_best_move',
        'evaluate_board_stockfish',
    ]
    
except ImportError as e:
    logger.warning("Could not import Cython extensions: %s", e)
    logger.warning("Falling back to Python implementations (slower)")
    # Define fallback Python implementations here if needed
    __all__ = ['find_best_move', 'evaluate_board_stockfish']
    import warnings
    
    def evaluate_board(board: chess.Board) -> int:
        """Fallback evaluation function.
        
        Args:
            board: The chess position to evaluate
            
        Returns:
            int: The evaluation score in centipawns from White's perspective
            
        Raises:
            NotImplementedError: Always, since this is just a fallback
        """
        raise NotImplementedError("Cython evaluation not available")
    
    def evaluate_board_piece_iter_phases(board: chess.Board) -> int:
        """Fallback implementation that raises NotImplementedError.
        
        Args:
            board: The chess position to evaluate
            
        Returns:
            int: The evaluation score in centipawns from White's perspective
            
        Raises:
            NotImplementedError: Always, since this is just a fallback
        """
        raise NotImplementedError("Cython evaluation not available")
    
    # Add other fallback functions as needed

# Import the opening book module
from .opening_book import (
    OpeningBook, OpeningBookError, OpeningBookFileError, OpeningBookDataError,
    OpeningBookMoveError, load_opening_book
)

__all__ = [
    # Core engine functionality
    'find_best_move',
    'evaluate_board_stockfish',
    
    # Opening book functionality
    'OpeningBook',
    'OpeningBookError',
    'OpeningBookFileError',
    'OpeningBookDataError',
    'OpeningBookMoveError',
    'load_opening_book',

    # Evaluation functions
    'evaluate_board',
    'evaluate_board_piece_iter_phases',
    'init_zobrist',
    'init_cython_tt',
    'free_cython_tt',
    'store_tt_entry',
    'probe_tt_entry',
    'probe_tt_move',
]


def find_best_move(board: chess.Board,
                  max_depth: int = 4,
                  time_limit: Optional[float] = None,
                  color: Optional[chess.Color] = None) -> chess.Move:
    """
    Find the best move for the given board position.
    
    This is a convenience function that provides a simple interface to the
    StockConda chess engine. For more advanced usage, consider using the
    UCIEngine class directly.
    
    Args:
        board: The chess board position to analyze
        max_depth: Maximum search depth (default: 4)
        time_limit: Time limit in seconds (default: None for unlimited)
        color: The color to move (default: board.turn)
        
    Returns:
        chess.Move: The best move found by the engine
        
    Example:
        >>> import chess
        >>> from chess_engine import find_best_move
        >>> board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        >>> best_move = find_best_move(board, max_depth=3)
        >>> print(f"Best move: {best_move}")
    """
    # Import UCIEngine locally to avoid circular import
    from .stockconda import UCIEngine
    
    if color is None:
        color = board.turn
    
    # Create a temporary UCI engine instance
    engine = UCIEngine()
    
    # Set up the board position
    engine.board = board
    
    # Start the search and get the best move
    engine.start_search(time_limit, max_depth, False)
    
    # Return the best move found (should be set by the search)
    if engine.best_move_found is None:
        # Fallback: return a legal move if no move was found
        legal_moves = list(board.legal_moves)
        return legal_moves[0] if legal_moves else chess.Move.null()
    return engine.best_move_found


def evaluate_board_stockfish(board: chess.Board,
                           stockfish_engine: chess.engine.SimpleEngine) -> chess.engine.PovScore:
    """
    Evaluate a board position using Stockfish.
    
    This function provides a convenient interface to Stockfish for analysis
    purposes. It requires a Stockfish engine instance to be provided.
    
    Args:
        board: The chess board position to evaluate
        stockfish_engine: A Stockfish engine instance from python-chess
        
    Returns:
        chess.engine.PovScore: The evaluation from Stockfish's perspective
        
    Example:
        >>> import chess
        >>> import chess.engine
        >>> from chess_engine import evaluate_board_stockfish
        >>> board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        >>> with chess.engine.SimpleEngine.popen_uci("stockfish") as sf:
        ...     score = evaluate_board_stockfish(board, sf)
        ...     print(f"Stockfish evaluation: {score}")
    """
    return stockfish_engine.analyse(board, chess.engine.Limit(depth=10))["score"]
