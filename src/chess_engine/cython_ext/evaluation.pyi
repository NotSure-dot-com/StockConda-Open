from typing import Dict, List
from chess import Board

# Material values (in centipawns)
mg_piece_values: Dict[int, int]
eg_piece_values: Dict[int, int]

# Piece-Square Tables
mg_pawn_table: List[int]
mg_knight_table: List[int]
mg_bishop_table: List[int]
mg_rook_table: List[int]
mg_queen_table: List[int]
mg_king_table: List[int]

eg_pawn_table: List[int]
eg_knight_table: List[int]
eg_bishop_table: List[int]
eg_rook_table: List[int]
eg_queen_table: List[int]
eg_king_table: List[int]

# Combined piece-square tables
mg_piece_square_tables: Dict[int, List[int]]
eg_piece_square_tables: Dict[int, List[int]]

def evaluate_board_piece_iter_phases(board: Board) -> int:
    """
    DEPRECATED: Evaluation using piece iteration. Slower than bitboard version.
    Evaluation is from the perspective of the side to move.
    """
    ...

def evaluate_board(board: Board, white_to_move: bool) -> tuple[int, bool]:
    """
    Primary optimized evaluation using bitboard iteration and phase tapering.
    Evaluation is from the perspective of the side to move.
    """
    ...

def evaluate_board_static(board: Board, white_to_move: bool) -> int:
    """
    Optimized evaluation without terminal rule checks.
    Evaluation is from the perspective of the side to move.
    """
    ...

def evaluate_board_bitboard_phases(board: Board) -> int:
    """
    DEPRECATED: Optimized evaluation using bitboard iteration.
    Evaluation is from the perspective of the side to move.
    """
    ...

def get_game_phase_cython(board: Board) -> str:
    """
    Determines the game phase ('opening', 'middlegame', 'endgame').
    """
    ...
