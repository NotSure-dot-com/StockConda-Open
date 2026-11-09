from dataclasses import dataclass
from typing import Literal, Optional, Tuple, TypeAlias, Union, overload
from chess import Board, Move

# --- Type Aliases ---
NodeTypeStr: TypeAlias = Literal["EXACT", "LOWERBOUND", "UPPERBOUND"]
TTMoveResult: TypeAlias = Tuple[Optional[Move], int]

# --- Constants ---
MATE_SCORE: int = 100000
MATE_SCORE_THRESHOLD: int = MATE_SCORE - 500
Q_SEARCH_TT_DEPTH: int = 0
NO_MOVE_ENCODED: int = 0xFFFF

# Node type enums (for internal use)
NODE_TYPE_EXACT: int = 0
NODE_TYPE_LOWERBOUND: int = 1
NODE_TYPE_UPPERBOUND: int = 2

# --- Debug Mode ---
# Set to True to enable runtime type checking (slower but safer)
DEBUG: bool = False

@dataclass(frozen=True)
class TTProbeResult:
    """Result of a transposition table probe.
    
    Attributes:
        value: The evaluation score from the transposition table
        depth: The search depth of the stored entry
        node_type: The type of node ("EXACT", "LOWERBOUND", or "UPPERBOUND")
        best_move: The best move from this position, if any
    """
    value: int
    depth: int
    node_type: NodeTypeStr
    best_move: Optional[Move]

def init_zobrist() -> None:
    """Initialize Zobrist hashing tables."""
    ...

def zobrist_hash(board: Board) -> int:
    """Calculate Zobrist hash for the given board position."""
    ...

def seed_history_cy(position_key: int) -> int:
    """Seed a repetition-history tag from a position key."""
    ...

def advance_history_cy(parent_history: int, child_key: int, zeroing: bool) -> int:
    """Advance a repetition-history tag, or reseed after a zeroing move."""
    ...

def tt_context_from_board(board: Board) -> tuple[int, int]:
    """Return the current position key and ordered reversible-history tag."""
    ...

def init_cython_tt(table_size_bytes: int) -> None:
    """
    Initialize the transposition table with the given size in bytes.
    
    Args:
        table_size_bytes: Size of the transposition table in bytes.
    """
    ...

def free_cython_tt() -> None:
    """Free the memory used by the transposition table."""
    ...

def tt_layout_cy() -> tuple[int, int, int]:
    """Return entry bytes, slot count, and allocated bytes without mutation."""
    ...

def store_tt_entry_cy(
    board_hash: int,
    depth: int,
    value: int,
    node_type_enum: int,
    best_move_obj: object,
    current_ply: int,
    halfmove_clock: int | None = None,
    history_context: object = None
) -> None:
    """Store an entry with explicit clock and repetition-history context.

    A reusable value requires a valid halfmove clock and a history-context
    integer in the inclusive range 0..2**64-1. Missing or invalid context keeps
    only the position-compatible move hint.
    
    Args:
        board_hash: Zobrist hash of the board position (64-bit unsigned)
        depth: Search depth (0-255)
        value: Evaluation score (32-bit signed integer)
        node_type_enum: Node type (0=EXACT, 1=LOWERBOUND, 2=UPPERBOUND)
        best_move_obj: Best move from this position (chess.Move or None)
        current_ply: Current ply in the search (0-65535)
        
    Raises:
        ValueError: If node_type_enum is invalid (in debug mode)
        TypeError: If best_move_obj is not None or chess.Move (in debug mode)
    """
    ...

def probe_tt_entry_cy(
    board_hash: int,
    required_depth: int,
    current_ply: int,
    halfmove_clock: int | None = None,
    history_context: object = None
) -> Optional[TTProbeResult]:
    """Probe a value only with exact valid clock and history contexts.

    ``history_context`` must be an integer in the inclusive range 0..2**64-1.
    Missing or invalid context cannot return a score, although the separate
    move-only probe remains available.
    
    Args:
        board_hash: Zobrist hash of the board position (64-bit unsigned)
        required_depth: Minimum required search depth (0-255)
        current_ply: Current ply in the search (0-65535)
        
    Returns:
        TTProbeResult if found and depth is sufficient, None otherwise
    """
    ...

def probe_tt_move_cy(board_hash: int) -> TTMoveResult:
    """Probe the transposition table for a move (Cython implementation).
    
    Args:
        board_hash: Zobrist hash of the board position (64-bit unsigned)
        
    Returns:
        Tuple of (best_move, depth) if found, (None, 0) otherwise
    """
    ...

def store_tt_entry(
    board: Board,
    depth: int,
    value: int,
    node_type: NodeTypeStr,
    best_move: Optional[Move] = None,
    *,
    tt_context: tuple[int, int] | None = None
) -> None:
    """Store an entry in the transposition table (Python wrapper).

    If supplied, ``tt_context`` must describe this same current board. Omitting
    it reconstructs the context from the board and its retained move stack.
    
    Args:
        board: The chess position to store
        depth: Search depth (will be clamped to 0-255)
        value: Evaluation score (will be clamped to 32-bit signed int)
        node_type: Node type ("EXACT", "LOWERBOUND", or "UPPERBOUND")
        best_move: Best move from this position, if any
        
    Returns:
        None
    """
    ...

def probe_tt_entry(
    board: Board,
    depth: int,
    *,
    tt_context: tuple[int, int] | None = None
) -> Optional[TTProbeResult]:
    """Probe the transposition table for an entry (Python wrapper).

    If supplied, ``tt_context`` must describe this same current board. Omitting
    it reconstructs the context from the board and its retained move stack.
    
    Args:
        board: The chess position to probe
        depth: Minimum required search depth (0-255)
        
    Returns:
        TTProbeResult if found and depth is sufficient, None otherwise
    """
    ...

def probe_tt_move(board: Board, *, board_hash: int | None = None) -> TTMoveResult:
    """Probe the transposition table for a move (Python wrapper).
    
    Args:
        board: The chess position to probe
        
    Returns:
        Tuple of (best_move, depth) if found, (None, 0) otherwise
    """
    ...
