"""Type stubs for cython_chess module."""

import chess
from typing import Iterator, Optional, Tuple

def gives_check(board: chess.Board, move: chess.Move) -> bool:
    """
    A faster, Cythonized version of board.gives_check(move).
    """
    ...

def initialize() -> None:
    """Initialize the attack tables for move generation."""
    ...

def generate_legal_moves(board: chess.Board, from_mask: int, to_mask: int) -> Iterator[chess.Move]:
    """Generate all legal moves for the current position.
    
    Args:
        board: The current chess board state
        from_mask: Bitmask of source squares
        to_mask: Bitmask of target squares
    """
    ...

def generate_search_moves(
    board: chess.Board, from_mask: int, to_mask: int
) -> Tuple[bool, Iterator[chess.Move]]:
    """Return check status and legal moves; consume before mutating board."""
    ...

def generate_legal_ep(board: chess.Board, from_mask: int, to_mask: int) -> Iterator[chess.Move]:
    """Generate all legal en passant moves.
    
    Args:
        board: The current chess board state
        from_mask: Bitmask of source squares
        to_mask: Bitmask of target squares
    """
    ...

def generate_legal_captures(board: chess.Board, from_mask: int, to_mask: int) -> Iterator[chess.Move]:
    """Generate all legal capture moves.
    
    Args:
        board: The current chess board state
        from_mask: Bitmask of source squares
        to_mask: Bitmask of target squares
    """
    ...

def generate_qsearch_moves(
    board: chess.Board,
    from_mask: int,
    to_mask: int,
    search_captures: bool = True,
) -> Tuple[bool, Optional[Iterator[chess.Move]]]:
    """Return check status and all moves QSearch must consider."""
    ...

def generate_qsearch_moves_with_status(
    board: chess.Board,
    from_mask: int,
    to_mask: int,
    search_captures: bool = True,
) -> Tuple[bool, bool, list[chess.Move]]:
    """Return check status, legal-move existence and QSearch moves."""
    ...

def _generate_evasions(
    board: chess.Board, 
    king: int, 
    checkers: int, 
    from_mask: int, 
    to_mask: int
) -> Iterator[chess.Move]:
    """Generate all evasion moves when in check.
    
    Args:
        board: The current chess board state
        king: The square index of the king
        checkers: Bitmask of checking pieces
        from_mask: Bitmask of source squares
        to_mask: Bitmask of target squares
    """
    ...

def generate_pseudo_legal_moves(board: chess.Board, from_mask: int, to_mask: int) -> Iterator[chess.Move]:
    """Generate all pseudo-legal moves.
    
    Args:
        board: The current chess board state
        from_mask: Bitmask of source squares
        to_mask: Bitmask of target squares
    """
    ...

def is_check(board: chess.Board, from_mask: int, to_mask: int) -> bool:
    """
    Determine if the current player is in check.

    Args:
        board: Chess board object
        from_mask: Bitmask for potential starting squares (currently unused)
        to_mask: Bitmask for potential target squares (currently unused)

    Returns:
        True if the current player's king is under attack, False otherwise
    """
    ...

# Cython-specific type hints for internal use
if False:
    def attackers_mask(
        colour: bool, 
        square: int, 
        occupied: int, 
        queens_and_rooks: int, 
        queens_and_bishops: int, 
        kings: int, 
        knights: int, 
        pawns: int, 
        occupied_co: int
    ) -> int: ...
    
    def slider_blockers(
        king: int, 
        queens_and_rooks: int, 
        queens_and_bishops: int, 
        occupied_co_opp: int, 
        occupied_co: int, 
        occupied: int
    ) -> int: ...
    
    def betweenPieces(a: int, b: int) -> int: ...
    def ray(a: int, b: int) -> int: ...
    def num_pieces(bb: int) -> int: ...
    def scan_reversed(bb: int, result: list[int]) -> None: ...
    def scan_forward(bb: int, result: list[int]) -> None: ...
    def attacks_mask(colour: bool, occupied: int, square: int, pieceType: int) -> int: ...
