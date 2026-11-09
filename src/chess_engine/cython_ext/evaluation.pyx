# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, initializedcheck=False

import chess
import cython
from libc.stdint cimport int32_t, uint64_t, uint8_t
from libc.stdint cimport uint64_t as unsigned_long_long  # For MSVC compatibility
from chess_engine.cython_ext.transposition import MATE_SCORE, MATE_SCORE_THRESHOLD, init_zobrist, probe_tt_entry, store_tt_entry, free_cython_tt, probe_tt_move, init_cython_tt
IF UNAME_SYSNAME == "Windows":
    cdef extern from "<intrin.h>" nogil:
        unsigned char _BitScanForward64(unsigned long* Index, uint64_t Mask)
ELSE:
    cdef extern from "strings.h" nogil:
        int ffsll(long long i)

cdef inline int fast_lsb(uint64_t bitboard) nogil:
    if bitboard == 0:
        return -1
    # Cython's IF will compile the correct block based on the platform
    IF UNAME_SYSNAME == "Windows":
        cdef unsigned long index
        _BitScanForward64(&index, bitboard)
        return <int>index
    ELSE:
        # ffsll returns 1-based index, so subtract 1
        return ffsll(<long long>bitboard) - 1

cdef inline int popcount(uint64_t bitboard) nogil:
    """Fast population count (bit counting) function."""
    if bitboard == 0:
        return 0
    cdef int count = 0
    while bitboard:
        count += 1
        bitboard &= bitboard - 1
    return count

# --- Import int64_t for safer intermediate calculations if needed ---
from libc.stdint cimport int64_t

# Precomputed Evaluation Data

# Material values (in centipawns) – Pesto-Tabellen sind "umgekehrt konstruiert"
mg_piece_values = {
    chess.PAWN:   82,
    chess.KNIGHT: 337,
    chess.BISHOP: 365,
    chess.ROOK:   477,
    chess.QUEEN:  1025,
    chess.KING:   0,
}

eg_piece_values = {
    chess.PAWN:   94,
    chess.KNIGHT: 281,
    chess.BISHOP: 297,
    chess.ROOK:   512,
    chess.QUEEN:  936,
    chess.KING:   0,
}
# Piece-Square tables for middlegame (aus Sicht von Schwarz, d.h. direkter Zugriff für Schwarz)
mg_pawn_table = [
    0, 0, 0, 0, 0, 0, 0, 0,
    98, 134, 61, 95, 68, 126, 34, -11,
    -6, 7, 26, 31, 65, 56, 25, -20,
    -14, 13, 6, 21, 23, 12, 17, -23,
    -27, -2, -5, 12, 17, 6, 10, -25,
    -26, -4, -4, -10, 3, 3, 33, -12,
    -35, -1, -20, -23, -15, 24, 38, -22,
    0, 0, 0, 0, 0, 0, 0, 0
]

mg_knight_table = [
    -167, -89, -34, -49, 61, -97, -15, -107,
    -73, -41, 72, 36, 23, 62, 7, -17,
    -47, 60, 37, 65, 84, 129, 73, 44,
    -9, 17, 19, 53, 37, 69, 18, 22,
    -13, 4, 16, 13, 28, 19, 21, -8,
    -23, -9, 12, 10, 19, 17, 25, -16,
    -29, -53, -12, -3, -1, 18, -14, -19,
    -105, -21, -58, -33, -17, -28, -19, -23
]

mg_bishop_table = [
    -29, 4, -82, -37, -25, -42, 7, -8,
    -26, 16, -18, -13, 30, 59, 18, -47,
    -16, 37, 43, 40, 35, 50, 37, -2,
    -4, 5, 19, 50, 37, 37, 7, -2,
    -6, 13, 13, 26, 34, 12, 10, 4,
    0, 15, 15, 15, 14, 27, 18, 10,
    4, 15, 16, 0, 7, 21, 33, 1,
    -33, -3, -14, -21, -13, -12, -39, -21
]

mg_rook_table = [
    32, 42, 32, 51, 63, 9, 31, 43,
    27, 32, 58, 62, 80, 67, 26, 44,
    -5, 19, 26, 36, 17, 45, 61, 16,
    -24, -11, 7, 26, 24, 35, -8, -20,
    -36, -26, -12, -1, 9, -7, 6, -23,
    -45, -25, -16, -17, 3, 0, -5, -33,
    -44, -16, -20, -9, -1, 11, -6, -71,
    -19, -13, 1, 17, 16, 7, -37, -26
]

mg_queen_table = [
    -28, 0, 29, 12, 59, 44, 43, 45,
    -24, -39, -5, 1, -16, 57, 28, 54,
    -13, -17, 7, 8, 29, 56, 47, 57,
    -27, -27, -16, -16, -1, 17, -2, 1,
    -9, -26, -9, -10, -2, -4, 3, -3,
    -14, 2, -11, -2, -5, 2, 14, 5,
    -35, -8, 11, 2, 8, 15, -3, 1,
    -1, -18, -9, 10, -15, -25, -31, -50
]

mg_king_table = [
    -65, 23, 16, -15, -56, -34, 2, 13,
    29, -1, -20, -7, -8, -4, -38, -29,
    -9, 24, 2, -16, -20, 6, 22, -22,
    -17, -20, -12, -27, -30, -25, -14, -36,
    -49, -1, -27, -39, -46, -44, -33, -51,
    -14, -14, -22, -46, -44, -30, -15, -27,
    1, 7, -8, -64, -43, -16, 9, 8,
    -15, 36, 12, -54, 8, -28, 24, 14
]

# Piece-Square tables for endgame
eg_pawn_table = [
    0, 0, 0, 0, 0, 0, 0, 0,
    178, 173, 158, 134, 147, 132, 165, 187,
    94, 100, 85, 67, 56, 53, 82, 84,
    32, 24, 13, 5, -2, 4, 17, 17,
    13, 9, -3, -7, -7, -8, 3, -1,
    4, 7, -6, 1, 0, -5, -1, -8,
    13, 8, 8, 10, 13, 0, 2, -7,
    0, 0, 0, 0, 0, 0, 0, 0
]

eg_knight_table = [
    -58, -38, -13, -28, -31, -27, -63, -99,
    -25, -8, -25, -2, -9, -25, -24, -52,
    -24, -20, 10, 9, -1, -9, -19, -41,
    -17, 3, 22, 22, 22, 11, 8, -18,
    -18, -6, 16, 25, 16, 17, 4, -18,
    -23, -3, -1, 15, 10, -3, -20, -22,
    -42, -20, -10, -5, -2, -20, -23, -44,
    -29, -51, -23, -15, -22, -18, -50, -64
]

eg_bishop_table = [
    -14, -21, -11, -8, -7, -9, -17, -24,
    -8, -4, 7, -12, -3, -13, -4, -14,
    2, -8, 0, -1, -2, 6, 0, 4,
    -3, 9, 12, 9, 14, 10, 3, 2,
    -6, 3, 13, 19, 7, 10, -3, -9,
    -12, -3, 8, 10, 13, 3, -7, -15,
    -14, -18, -7, -1, 4, -9, -15, -27,
    -23, -9, -23, -5, -9, -16, -5, -17
]

eg_rook_table = [
    13, 10, 18, 15, 12, 12, 8, 5,
    11, 13, 13, 11, -3, 3, 8, 3,
    7, 7, 7, 5, 4, -3, -5, -3,
    4, 3, 13, 1, 2, 1, -1, 2,
    3, 5, 8, 4, -5, -6, -8, -11,
    -4, 0, -5, -1, -7, -12, -8, -16,
    -6, -6, 0, 2, -9, -9, -11, -3,
    -9, 2, 3, -1, -5, -13, 4, -20
]

eg_queen_table = [
    -9, 22, 22, 27, 27, 19, 10, 20,
    -17, 20, 32, 41, 58, 25, 30, 0,
    -20, 6, 9, 49, 47, 35, 19, 9,
    3, 22, 24, 45, 57, 40, 57, 36,
    -18, 28, 19, 47, 31, 34, 39, 23,
    -16, -27, 15, 6, 9, 17, 10, 5,
    -22, -23, -30, -16, -16, -23, -36, -32,
    -33, -28, -22, -43, -5, -32, -20, -41
]

eg_king_table = [
    -74, -35, -18, -18, -11, 15, 4, -17,
    -12, 17, 14, 17, 17, 38, 23, 11,
    10, 17, 23, 15, 20, 45, 44, 13,
    -8, 22, 24, 27, 26, 33, 26, 3,
    -18, -4, 21, 24, 27, 23, 9, -11,
    -19, -3, 11, 21, 23, 16, 7, -9,
    -27, -11, 4, 13, 14, 4, -5, -17,
    -53, -34, -21, -11, -28, -14, -24, -43
]


# Gruppiere die PST-Tabellen in einem Dictionary
mg_piece_square_tables = {
    chess.PAWN:   mg_pawn_table,
    chess.KNIGHT: mg_knight_table,
    chess.BISHOP: mg_bishop_table,
    chess.ROOK:   mg_rook_table,
    chess.QUEEN:  mg_queen_table,
    chess.KING:   mg_king_table
}

# --- Group Endgame Tables ---
eg_piece_square_tables = {
    chess.PAWN:   eg_pawn_table,
    chess.KNIGHT: eg_knight_table,
    chess.BISHOP: eg_bishop_table,
    chess.ROOK:   eg_rook_table,
    chess.QUEEN:  eg_queen_table,
    chess.KING:   eg_king_table
}



# --- Constants for Phase Calculation ---
# Values assigned to pieces for determining game phase
# Pawns and Kings typically don't contribute to phase calculation
# These are standard values, summing to 24 for a full starting set of non-pawn pieces
cdef int32_t PHASE_KNIGHT = 12
cdef int32_t PHASE_BISHOP = 12
cdef int32_t PHASE_ROOK = 20
cdef int32_t PHASE_QUEEN = 40
cdef int32_t MAX_PHASE = 2 * (2 * PHASE_KNIGHT + 2 * PHASE_BISHOP + 2 * PHASE_ROOK + 1 * PHASE_QUEEN) 

# C array for phase values, indexed by piece_type (chess.PAWN=1, chess.KNIGHT=2, etc.)
# Index 0 is unused, PAWN and KING have 0 phase value
cdef int32_t phase_values_c[7] 
#cdef int32_t MAX_PHASE = 256 # Standard Total Phase Scale

# --- C arrays for BOTH middlegame and endgame precomputed scores ---
cdef int32_t precomputed_mg_scores_c[2][6][64] # [color][piece_type-1][square]
cdef int32_t precomputed_eg_scores_c[2][6][64] # [color][piece_type-1][square]
cdef bint precomputed_initialized = False


def _initialize_precomputed_c():
    """Populates the MG and EG C arrays using correct phase values. Call once."""
    # Now it can access the global dicts defined above
    global precomputed_initialized, precomputed_mg_scores_c, precomputed_eg_scores_c
    if precomputed_initialized:
        return


    #print("Initializing precomputed MG and EG C arrays for evaluation...")

    # Initialize phase_values_c
    phase_values_c[chess.PAWN] = 0
    phase_values_c[chess.KNIGHT] = PHASE_KNIGHT
    phase_values_c[chess.BISHOP] = PHASE_BISHOP
    phase_values_c[chess.ROOK] = PHASE_ROOK
    phase_values_c[chess.QUEEN] = PHASE_QUEEN
    phase_values_c[chess.KING] = 0

    cdef int piece_type, sq, mirrored_sq
    cdef int piece_type_idx # 0-based index for arrays
    cdef int32_t mg_value, eg_value # Base material values FOR EACH PHASE
    cdef list mg_table, eg_table # Python lists for PSTs

    for piece_type in range(chess.PAWN, chess.KING + 1):
        piece_type_idx = piece_type - 1 # Calculate 0-based index once
        # --- Fetch material value for EACH phase ---
        mg_value = mg_piece_values[piece_type]
        eg_value = eg_piece_values[piece_type]
        # --- Fetch PSTs ---
        mg_table = mg_piece_square_tables[piece_type]
        eg_table = eg_piece_square_tables[piece_type]

        # --- White scores (index 0), needs mirrored PST ---
        for sq in range(64):
            mirrored_sq = chess.square_mirror(sq)
            # Use correct base value for each phase array
            precomputed_mg_scores_c[0][piece_type_idx][sq] = mg_value + mg_table[mirrored_sq]
            precomputed_eg_scores_c[0][piece_type_idx][sq] = eg_value + eg_table[mirrored_sq]

        # --- Black scores (index 1), direct PST lookup ---
        for sq in range(64):
            # Use correct base value for each phase array
            precomputed_mg_scores_c[1][piece_type_idx][sq] = mg_value + mg_table[sq]
            precomputed_eg_scores_c[1][piece_type_idx][sq] = eg_value + eg_table[sq]

    precomputed_initialized = True
    #print("Precomputation done.")

# --- Call initialization when the module is loaded ---
_initialize_precomputed_c()

   
from typing import Tuple
from libc.stdint cimport int32_t
from cython cimport ccall

cdef extern from "cython_chess_backend.h" nogil:
    uint64_t attackers_mask(bint colour, uint8_t square, uint64_t occupied, uint64_t queens_and_rooks, uint64_t queens_and_bishops, uint64_t kings, uint64_t knights, uint64_t pawns, uint64_t occupied_co)
    uint64_t attacks_mask(bint colour, uint64_t occupied, uint8_t square, uint8_t pieceType)
    void initialize_attack_tables() nogil

# File constants for pawn attack generation (from cython_chess_backend.cpp)
cdef const uint64_t BB_FILE_A = 0x0101010101010101ULL << 0
cdef const uint64_t BB_FILE_B = 0x0101010101010101ULL << 1
cdef const uint64_t BB_FILE_C = 0x0101010101010101ULL << 2
cdef const uint64_t BB_FILE_D = 0x0101010101010101ULL << 3
cdef const uint64_t BB_FILE_E = 0x0101010101010101ULL << 4
cdef const uint64_t BB_FILE_F = 0x0101010101010101ULL << 5
cdef const uint64_t BB_FILE_G = 0x0101010101010101ULL << 6
cdef const uint64_t BB_FILE_H = 0x0101010101010101ULL << 7

@cython.cfunc
@cython.inline
@cython.nogil
cdef void _ensure_attack_tables():
    initialize_attack_tables()

# Mobility Bonus Tables (cp per move count)
# Values match Stockfish 12 evaluate.cpp MobilityBonus, split into MG/EG arrays.
# Copyright (C) 2004-2020 The Stockfish developers (see upstream AUTHORS).
# Stockfish is GPL version 3 or later. These local arrays were introduced on
# 2025-10-23; attribution/modification notice added on 2026-11-08.
# Source: https://github.com/official-stockfish/Stockfish/blob/sf_12/src/evaluate.cpp
cdef int32_t knight_mobility_mg[9]  # Knight: 0-8 moves
cdef int32_t knight_mobility_eg[9]
cdef int32_t bishop_mobility_mg[14]  # Bishop: 0-13 moves
cdef int32_t bishop_mobility_eg[14]
cdef int32_t rook_mobility_mg[15]  # Rook: 0-14 moves
cdef int32_t rook_mobility_eg[15]
cdef int32_t queen_mobility_mg[28]  # Queen: 0-27 moves
cdef int32_t queen_mobility_eg[28]

# Initialize mobility tables at module level
knight_mobility_mg[:] = [-62, -53, -12, -4, 3, 13, 22, 28, 33]
knight_mobility_eg[:] = [-81, -56, -31, -16, 5, 11, 17, 20, 25]
bishop_mobility_mg[:] = [-48, -20, 16, 26, 38, 51, 55, 63, 63, 68, 81, 81, 91, 98]
bishop_mobility_eg[:] = [-59, -23, -3, 13, 24, 42, 54, 57, 65, 73, 78, 86, 88, 97]
rook_mobility_mg[:] = [-60, -20, 2, 3, 3, 11, 22, 31, 40, 40, 41, 48, 57, 57, 62]
rook_mobility_eg[:] = [-78, -17, 23, 39, 70, 99, 103, 121, 134, 139, 158, 164, 168, 169, 172]
queen_mobility_mg[:] = [-30, -12, -8, -9, 20, 23, 23, 35, 38, 53, 64, 65, 65, 66, 67, 67, 72, 72, 77, 79, 93, 108, 108, 108, 110, 114, 114, 116]
queen_mobility_eg[:] = [-48, -30, -7, 19, 40, 55, 59, 75, 78, 96, 96, 100, 121, 127, 131, 133, 136, 141, 147, 150, 151, 168, 168, 171, 182, 182, 192, 219]

# At the end of the module, after all definitions at module load once

# outside function scope (for efficiency)
_ensure_attack_tables()  # Initialize attack tables once when module loads

# Feature flag to guard mobility scoring in the hot path (disabled by default)
cdef bint ENABLE_MOBILITY = False  # C-level constant for nogil compatibility

@cython.ccall
def evaluate_board(
    board: chess.Board, bint white_to_move: bool
) -> Tuple[int32_t, bool]:
    """
    Python-visible wrapper around the pure bitboard evaluator.

    Args:
        board: The current chess position
        white_to_move: Whether white is to move (avoids repeated board.turn lookups)
        mobility_weight: Weight factor for mobility bonus (default 1)
    Returns:
        Tuple[int32_t, bool]: Evaluation score in centipawns and draw flag.
        Score is from the perspective of the side to move.
        Positive values indicate an advantage for the side to move.

    Performance:
        - Uses fast LSB scanning with __builtin_ctzll (GCC) or _BitScanForward64 (MSVC fallback)
        - Phase accumulation only for KNIGHT-QUEEN pieces
        - Proper mobility evaluation with piece-specific bonuses
        - 7-34% faster than piece iteration in typical positions
    """
    # --- Terminal Checks (Keep consistent) ---
    if board.is_checkmate():
        return -MATE_SCORE + board.ply(), False # bool is for draw detection
    if board.is_repetition(3) or board.is_insufficient_material() or board.is_stalemate() or board.is_fifty_moves():
        return 0, True # bool is for draw detection

    # --- Handle empty board case ---
    #if not board.occupied:
    #    return 0, True # Neutral score and draw flag for empty board

    return evaluate_board_static(board, white_to_move), False


@cython.ccall
def evaluate_board_static(board: chess.Board, bint white_to_move: bool) -> int32_t:
    """Evaluate material/positional score without terminal rule checks."""
    cdef uint64_t BB_pieces[2][6]
    cdef uint64_t piece_masks[6]
    cdef uint64_t color_masks[2]
    cdef int color, pt

    if type(board) is chess.Board:
        # BaseBoard.pieces_mask() selects one piece bitboard and intersects
        # it with occupied_co[color]. Only exact Boards use this shortcut:
        # subclasses may override pieces_mask() or their board representation.
        piece_masks[0] = board.pawns
        piece_masks[1] = board.knights
        piece_masks[2] = board.bishops
        piece_masks[3] = board.rooks
        piece_masks[4] = board.queens
        piece_masks[5] = board.kings
        color_masks[0] = board.occupied_co[0]
        color_masks[1] = board.occupied_co[1]
        for color in range(2):  # 0 Black, 1 White
            for pt in range(6):
                BB_pieces[color][pt] = piece_masks[pt] & color_masks[color]
    else:
        for color in range(2):
            for pt in range(6):
                BB_pieces[color][pt] = board.pieces_mask(pt + 1, color)

    # Get the raw score from the fast evaluator
    cdef int32_t raw_score = evaluate_board_fast(BB_pieces, white_to_move)

    # The fast evaluator already returns a score from the perspective of white_to_move
    # No additional sign adjustment needed here
    return raw_score


@ccall
def evaluate_board_from_bitboards(
    uint64_t[:] white_pieces, uint64_t[:] black_pieces, bint white_to_move
) -> Tuple[int32_t, bool]:
    """
    Evaluate position directly from bitboards (no board object needed).

    Args:
        white_pieces: Array of 6 uint64_t bitboards for white pieces [Pawn, Knight, Bishop, Rook, Queen, King]
        black_pieces: Array of 6 uint64_t bitboards for black pieces [Pawn, Knight, Bishop, Rook, Queen, King]
        white_to_move: Whether white is to move

    Returns:
        Tuple[int32_t, bool]: Evaluation score in centipawns and draw flag.
    """
    cdef uint64_t BB_pieces[2][6]
    cdef int pt

    # Copy bitboards to internal array format
    for pt in range(6):  # 0..5 → Pawn..King
        BB_pieces[1][pt] = white_pieces[pt]  # White pieces
        BB_pieces[0][pt] = black_pieces[pt]  # Black pieces

    cdef int32_t raw_score = evaluate_board_fast(BB_pieces, white_to_move)

    return raw_score, False


@cython.nogil
@cython.cfunc
@cython.inline
def evaluate_board_fast(
    uint64_t BB_pieces[2][6],   # [color][piece_type - 1] bitboards
    bint white_to_move: bool        # True if White to move
) -> int32_t:
    """
    Pure bitboard evaluation (no Python chess.Board dependency).
    --------------------------------------------------------------------
    This version assumes all bitboards are already maintained in engine state.
    It performs a standard midgame/endgame interpolation using precomputed
    PSTs and phase values, with additional mobility evaluation.

    Args:
        BB_pieces:  [2][6] array of 64-bit bitboards
                    color = 0 → Black, 1 → White
                    piece_type = 0..5 → Pawn..King
        white_to_move: True if white to move

    Returns:
        int32_t score in centipawns from the perspective of the side to move.
    """

    cdef int32_t mg_score = 0
    cdef int32_t eg_score = 0
    cdef int32_t game_phase = 0
    cdef int piece_type_idx
    cdef int sq
    cdef uint64_t bitboard
    cdef uint64_t occ_white, occ_black, occ_all
    cdef uint64_t mob_bb
    cdef int move_count
    cdef int32_t* mg_tables[4]
    cdef int32_t* eg_tables[4]
    cdef int max_indices[4]

    if ENABLE_MOBILITY:
        # Compute occupancies only when mobility is enabled
        occ_white = (
            BB_pieces[1][0] | BB_pieces[1][1] | BB_pieces[1][2] |
            BB_pieces[1][3] | BB_pieces[1][4] | BB_pieces[1][5]
        )
        occ_black = (
            BB_pieces[0][0] | BB_pieces[0][1] | BB_pieces[0][2] |
            BB_pieces[0][3] | BB_pieces[0][4] | BB_pieces[0][5]
        )
        occ_all = occ_white | occ_black

        # Initialize mobility tables only when needed
        mg_tables[0] = knight_mobility_mg
        mg_tables[1] = bishop_mobility_mg
        mg_tables[2] = rook_mobility_mg
        mg_tables[3] = queen_mobility_mg
        eg_tables[0] = knight_mobility_eg
        eg_tables[1] = bishop_mobility_eg
        eg_tables[2] = rook_mobility_eg
        eg_tables[3] = queen_mobility_eg
        max_indices[0] = 8
        max_indices[1] = 13
        max_indices[2] = 14
        max_indices[3] = 27


    # --- White Pieces ---
    for piece_type_idx in range(6):  # 0..5 → Pawn..King
        bitboard = BB_pieces[1][piece_type_idx]
        while bitboard:
            sq = fast_lsb(bitboard)
            mg_score += precomputed_mg_scores_c[0][piece_type_idx][sq]
            eg_score += precomputed_eg_scores_c[0][piece_type_idx][sq]
            bitboard &= bitboard - 1

        # --- Black Pieces ---
        #  for piece_type_idx in range(6):  # 0..5 → Pawn..King
        bitboard = BB_pieces[0][piece_type_idx]
        while bitboard:
            sq = fast_lsb(bitboard)
            mg_score -= precomputed_mg_scores_c[1][piece_type_idx][sq]
            eg_score -= precomputed_eg_scores_c[1][piece_type_idx][sq]
            bitboard &= bitboard - 1
        
        # --- Phase Calculation (KNIGHT..QUEEN only) ---
        if 1 <= piece_type_idx <= 4:  # Knight (1) .. Queen (4)
            game_phase += phase_values_c[piece_type_idx + 1] * (
                popcount(BB_pieces[1][piece_type_idx]) +
                popcount(BB_pieces[0][piece_type_idx])
            )
            # --- Mobility calculation for this piece type ---
            if ENABLE_MOBILITY and piece_type_idx >= 1 and piece_type_idx <= 4:
                # Calculate mobility for white pieces of this type
                bitboard = BB_pieces[1][piece_type_idx]
                while bitboard:
                    sq = fast_lsb(bitboard)
                    mob_bb = attacks_mask(True, occ_all, <uint8_t>sq, <uint8_t>(piece_type_idx + 1))
                    move_count = popcount(mob_bb & ~occ_white)
                    # Branchless clamp using ternary operator
                    move_count = move_count if move_count <= max_indices[piece_type_idx - 1] else max_indices[piece_type_idx - 1]
                    mg_score += mg_tables[piece_type_idx - 1][move_count]
                    eg_score += eg_tables[piece_type_idx - 1][move_count]
                    bitboard &= bitboard - 1

                # Calculate mobility for black pieces of this type
                bitboard = BB_pieces[0][piece_type_idx]
                while bitboard:
                    sq = fast_lsb(bitboard)
                    mob_bb = attacks_mask(False, occ_all, <uint8_t>sq, <uint8_t>(piece_type_idx + 1))
                    move_count = popcount(mob_bb & ~occ_black)
                    move_count = move_count if move_count <= max_indices[piece_type_idx - 1] else max_indices[piece_type_idx - 1]
                    mg_score -= mg_tables[piece_type_idx - 1][move_count]
                    eg_score -= eg_tables[piece_type_idx - 1][move_count]
                    bitboard &= bitboard - 1

    # --- Tapering ---
    if game_phase > MAX_PHASE:
        game_phase = MAX_PHASE

    cdef int32_t final_score = (
        (mg_score * game_phase + eg_score * (MAX_PHASE - game_phase))
        // MAX_PHASE
    )
    if not white_to_move: final_score *= -1

    return final_score



@cython.ccall
def get_game_phase_cython(board: chess.Board) -> str:
    """
    Determines the game phase ('opening', 'middlegame', 'endgame') based on the
    same phase calculation used in the tapered evaluation.
    """
    cdef int32_t game_phase = 0
    cdef int pt
    for pt in range(chess.KNIGHT, chess.QUEEN + 1):
        game_phase += phase_values_c[pt] * (popcount(board.pieces_mask(pt, chess.WHITE)) + popcount(board.pieces_mask(pt, chess.BLACK)))

    # Define thresholds based on MAX_PHASE (256)
    opening_threshold = 192  # > 75%
    endgame_threshold = 64   # < 25%

    if game_phase > opening_threshold:
        return 'opening'
    elif game_phase > endgame_threshold:
        return 'middlegame'
    else:
        return 'endgame'
