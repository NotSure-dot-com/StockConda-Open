"""
@author: Ranuja Pinnaduwage

This file is part of cython-chess, a Cython-optimized modification of python-chess.

Description:
This file implements Cythonized versions of core python-chess components, improving performance 
with Cython and C++ while maintaining the same functionality.

Based on python-chess: https://github.com/niklasf/python-chess  
Copyright (C) 2025 Ranuja Pinnaduwage  
Licensed under the GNU General Public License (GPL) v3 or later.  

StockConda modification notice (2026-11-08): this file contains local move
generation and integration changes. The last earlier source change recorded in
the project history was 2026-09-17. The upstream notice and license remain.

This program is distributed in the hope that it will be useful,  
but WITHOUT ANY WARRANTY; without even the implied warranty of  
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  
See the GNU General Public License for more details.  

You should have received a copy of the GNU General Public License  
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

cimport cython
import chess
import itertools
from collections.abc import Iterator

# From the c++ standard library import vectors and strings
from libcpp.vector cimport vector
from libcpp.string cimport string

# Define C data types 
cdef extern from "stdint.h":
    ctypedef signed char int8_t
    ctypedef unsigned char uint8_t
    ctypedef unsigned long long uint64_t

# Import functions from the c++ file
cdef extern from "cython_chess_backend.h" nogil:
    
    void initialize_attack_tables()    
    
    void generate_piece_moves(vector[uint8_t] &startPos, vector[uint8_t] &endPos, uint64_t our_pieces, uint64_t pawnsMask, uint64_t knightsMask, uint64_t bishopsMask, uint64_t rooksMask, uint64_t queensMask, uint64_t kingsMask, uint64_t occupied_whiteMask, uint64_t occupied_blackMask, uint64_t occupiedMask, uint64_t from_mask, uint64_t to_mask)
    void generate_pawn_moves(vector[uint8_t] &startPos, vector[uint8_t] &endPos, vector[uint8_t] &promotions, uint64_t opposingPieces, uint64_t occupied, bint colour, uint64_t pawnsMask, uint64_t from_mask, uint64_t to_mask)
    uint64_t attackers_mask(bint colour, uint8_t square, uint64_t occupied, uint64_t queens_and_rooks, uint64_t queens_and_bishops, uint64_t kings, uint64_t knights, uint64_t pawns, uint64_t occupied_co)
    uint64_t slider_blockers(uint8_t king, uint64_t queens_and_rooks, uint64_t queens_and_bishops, uint64_t occupied_co_opp, uint64_t occupied_co, uint64_t occupied)
    uint64_t betweenPieces(uint8_t a, uint8_t b)
    uint64_t ray(uint8_t a, uint8_t b)

    uint8_t num_pieces(uint64_t bb)
    void scan_reversed(uint64_t bb, vector[uint8_t] &result)
    void scan_forward(uint64_t bb, vector[uint8_t] &result)
    uint64_t attacks_mask(bint colour, uint64_t occupied, uint8_t square, uint8_t pieceType)
    

cpdef initialize():
    initialize_attack_tables()    

def _generate_legal_moves_precomputed(object board, uint8_t king,
                                      uint64_t blockers, uint64_t checkers,
                                      uint64_t from_mask, uint64_t to_mask):
    cdef object move

    if checkers:
        for move in _generate_evasions(board, king, checkers, from_mask, to_mask):
            if board._is_safe(king, blockers, move):
                yield move
    else:
        for move in generate_pseudo_legal_moves(board, from_mask, to_mask):
            if board._is_safe(king, blockers, move):
                yield move


def generate_search_moves(object board, uint64_t from_mask, uint64_t to_mask):
    """Return check status and legal moves from one king-attack calculation."""
    cdef uint8_t king
    cdef uint64_t blockers
    cdef uint64_t checkers
    cdef uint64_t king_mask
    cdef int king_pos
    cdef bint board_turn = board.turn
    cdef bint opponent = not board_turn

    if board.is_variant_end():
        return False, iter(())

    king_mask = board.kings & board.occupied_co[board_turn]
    if king_mask:
        king_pos = king_mask.bit_length() - 1
        if king_pos < 0:
            return False, generate_pseudo_legal_moves(board, from_mask, to_mask)

        king = <uint8_t>king_pos
        blockers = slider_blockers(king, board.queens | board.rooks, board.queens | board.bishops, board.occupied_co[opponent], board.occupied_co[board_turn], board.occupied)
        checkers = attackers_mask(opponent, king, board.occupied, board.queens | board.rooks, board.queens | board.bishops, board.kings, board.knights, board.pawns, board.occupied_co[opponent])
        return bool(checkers), _generate_legal_moves_precomputed(
            board, king, blockers, checkers, from_mask, to_mask
        )

    return False, generate_pseudo_legal_moves(board, from_mask, to_mask)


def generate_legal_moves(object board, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    """Return an iterator over legal moves."""
    yield from generate_search_moves(board, from_mask, to_mask)[1]

def generate_legal_ep(object board, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    
    """
    Function to generate legal en passent moves through yielding

    Parameters:
    - board: The current board state
    - from_mask: The starting position mask
    - to_mask: The ending position mask

    Yields:
    - Legal en passent chess.Moves
    """
    
    cdef object move
    if board.is_variant_end():
        return
    
    for move in board.generate_pseudo_legal_ep(from_mask, to_mask):
        if not board.is_into_check(move):
            yield move

def generate_legal_captures(object board, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    
    """
    Function to generate legal captures through yielding

    Parameters:
    - board: The current board state
    - from_mask: The starting position mask
    - to_mask: The ending position mask

    Yields:
    - Legal capture chess.Moves
    """
    
    # Use itertools to yield moves from the legal moves function and en passent function        
    return itertools.chain(
        generate_legal_moves(board,from_mask, to_mask & board.occupied_co[not board.turn]),
        generate_legal_ep(board,from_mask, to_mask))        

def _generate_safe_evasions(object board, uint8_t king, uint64_t checkers, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    cdef object move
    cdef uint64_t blockers = slider_blockers(king, board.queens | board.rooks, board.queens | board.bishops, board.occupied_co[not board.turn], board.occupied_co[board.turn], board.occupied)
    for move in _generate_evasions(board, king, checkers, from_mask, to_mask):
        if board._is_safe(king, blockers, move):
            yield move

def _generate_legal_captures_unchecked(object board, uint8_t king, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    cdef uint64_t blockers = slider_blockers(king, board.queens | board.rooks, board.queens | board.bishops, board.occupied_co[not board.turn], board.occupied_co[board.turn], board.occupied)

    return _generate_legal_captures_precomputed(
        board, king, blockers, from_mask, to_mask
    )


def _generate_legal_captures_precomputed(object board, uint8_t king,
                                         uint64_t blockers,
                                         uint64_t from_mask,
                                         uint64_t to_mask):
    cdef object move
    cdef uint64_t source_mask
    cdef uint64_t unsafe_sources = blockers | (<uint64_t>1 << king)
    cdef uint64_t capture_to_mask = to_mask & board.occupied_co[not board.turn]

    for move in generate_pseudo_legal_moves(board, from_mask, capture_to_mask):
        source_mask = <uint64_t>1 << move.from_square
        # This helper is used only outside check. Occupied capture targets
        # exclude en passant; only king and slider-blocker sources need the
        # full python-chess safety callback.
        if not (unsafe_sources & source_mask) or board._is_safe(king, blockers, move):
            yield move

    yield from generate_legal_ep(board, from_mask, to_mask)


def _generate_legal_quiet_promotions_precomputed(
        object board, uint8_t king, uint64_t blockers,
        uint64_t from_mask, uint64_t to_mask):
    """Yield legal non-capture promotions for the QSearch noisy-move set."""
    cdef object move
    cdef uint64_t source_mask
    cdef uint64_t promotion_rank = chess.BB_RANK_8 if board.turn else chess.BB_RANK_1
    cdef uint64_t promotion_sources = (
        from_mask & board.pawns & board.occupied_co[board.turn]
    )
    cdef uint64_t quiet_promotion_targets = (
        to_mask & promotion_rank & ~board.occupied
    )

    for move in generate_pseudo_legal_moves(
            board, promotion_sources, quiet_promotion_targets):
        if move.promotion is None:
            continue
        source_mask = <uint64_t>1 << move.from_square
        if not (blockers & source_mask) or board._is_safe(
                king, blockers, move):
            yield move


def _generate_qsearch_noisy_moves_precomputed(
        object board, uint8_t king, uint64_t blockers,
        uint64_t from_mask, uint64_t to_mask):
    yield from _generate_legal_captures_precomputed(
        board, king, blockers, from_mask, to_mask
    )
    yield from _generate_legal_quiet_promotions_precomputed(
        board, king, blockers, from_mask, to_mask
    )


def _generate_qsearch_noisy_moves_unchecked(
        object board, uint8_t king, uint64_t from_mask, uint64_t to_mask):
    cdef uint64_t blockers = slider_blockers(
        king,
        board.queens | board.rooks,
        board.queens | board.bishops,
        board.occupied_co[not board.turn],
        board.occupied_co[board.turn],
        board.occupied,
    )
    return _generate_qsearch_noisy_moves_precomputed(
        board, king, blockers, from_mask, to_mask
    )


def _generate_qsearch_noisy_moves_without_king(
        object board, uint64_t from_mask, uint64_t to_mask):
    cdef object move
    for move in generate_legal_moves(board, from_mask, to_mask):
        if move.promotion is not None or board.is_capture(move):
            yield move


cdef bint _has_safe_pseudo_legal_move(object board, uint8_t king,
                                      uint64_t blockers, uint64_t from_mask,
                                      uint64_t to_mask):
    cdef object move
    for move in generate_pseudo_legal_moves(board, from_mask, to_mask):
        if board._is_safe(king, blockers, move):
            return True
    return False


def generate_qsearch_moves_with_status(object board, uint64_t from_mask,
                                       uint64_t to_mask,
                                       bint search_captures=True):
    """Return check status, legal-move existence and QSearch moves."""
    cdef uint8_t king
    cdef uint64_t blockers
    cdef uint64_t checkers
    cdef uint64_t king_mask
    cdef int king_pos
    cdef bint board_turn = board.turn
    cdef bint opponent = not board_turn
    cdef list moves

    king_mask = board.kings & board.occupied_co[board_turn]
    if not king_mask:
        moves = list(_generate_qsearch_noisy_moves_without_king(
            board, from_mask, to_mask)) if search_captures else []
        return False, next(generate_legal_moves(board, from_mask, to_mask), None) is not None, moves

    king_pos = king_mask.bit_length() - 1
    if king_pos < 0:
        moves = list(_generate_qsearch_noisy_moves_without_king(
            board, from_mask, to_mask)) if search_captures else []
        return False, next(generate_legal_moves(board, from_mask, to_mask), None) is not None, moves

    king = <uint8_t>king_pos
    blockers = slider_blockers(king, board.queens | board.rooks, board.queens | board.bishops, board.occupied_co[opponent], board.occupied_co[board_turn], board.occupied)
    checkers = attackers_mask(opponent, king, board.occupied, board.queens | board.rooks, board.queens | board.bishops, board.kings, board.knights, board.pawns, board.occupied_co[opponent])

    if checkers:
        moves = list(_generate_legal_moves_precomputed(
            board, king, blockers, checkers, from_mask, to_mask
        ))
        return True, bool(moves), moves

    if search_captures:
        moves = list(_generate_qsearch_noisy_moves_precomputed(
            board, king, blockers, from_mask, to_mask
        ))
        if moves:
            return False, True, moves
    else:
        moves = []

    return False, _has_safe_pseudo_legal_move(
        board, king, blockers, from_mask, to_mask
    ), moves

def generate_qsearch_moves(object board, uint64_t from_mask, uint64_t to_mask, bint search_captures=True):
    """
    Return the current check status and the legal moves required by QSearch.

    Checked positions yield every legal evasion. Other positions yield legal
    captures and quiet promotions. The king attack calculation is shared by
    the status and move selection so QSearch does not test for check twice.
    """
    cdef uint8_t king
    cdef uint64_t checkers
    cdef uint64_t king_mask
    cdef int king_pos
    cdef bint board_turn = board.turn
    cdef bint opponent = not board_turn

    king_mask = board.kings & board.occupied_co[board_turn]
    if not king_mask:
        if not search_captures:
            return False, None
        return False, _generate_qsearch_noisy_moves_without_king(
            board, from_mask, to_mask
        )

    king_pos = king_mask.bit_length() - 1
    if king_pos < 0:
        if not search_captures:
            return False, None
        return False, _generate_qsearch_noisy_moves_without_king(
            board, from_mask, to_mask
        )

    king = <uint8_t>king_pos
    checkers = attackers_mask(opponent, king, board.occupied, board.queens | board.rooks, board.queens | board.bishops, board.kings, board.knights, board.pawns, board.occupied_co[opponent])

    if checkers:
        return True, _generate_safe_evasions(
            board, king, checkers, from_mask, to_mask
        )

    if not search_captures:
        return False, None

    return False, _generate_qsearch_noisy_moves_unchecked(
        board, king, from_mask, to_mask
    )

def _generate_evasions(object board, uint8_t king, uint64_t checkers, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    
    """
    Function to generate evasions through yielding

    Parameters:
    - board: The current board state
    - king: The square where the current side's king is located
    - checkers: The mask for pieces checking the king 
    - from_mask: The starting position mask
    - to_mask: The ending position mask

    Yields:
    - Legal evasion chess.Moves
    """
    
    # Define mask for sliding pieces which are also checkers
    cdef uint64_t sliders = checkers & (board.bishops | board.rooks | board.queens)
    
    # Define mask to hold ray attacks towards the king
    cdef uint64_t attacked = 0
    
    # Define variable to hold squares to stop check through either blocking or capture
    cdef uint64_t target
    cdef uint8_t last_double
    cdef uint8_t checker
    cdef vector[uint8_t] attackedVec
    cdef vector[uint8_t] moveVec
    
    # Call c++ function to scan the sliders bitmask
    scan_reversed(sliders,attackedVec)
    cdef uint8_t size = attackedVec.size()
    
    # Acquire ray attacks 
    for i in range(size):
        attacked |= ray(king, attackedVec[i]) & ~chess.BB_SQUARES[int(attackedVec[i])]

    # Return moves the king can make to evade check
    if ((<uint64_t>1) << king) & from_mask:
        
        moveVec.clear()
        scan_reversed(chess.BB_KING_ATTACKS[king] & ~board.occupied_co[board.turn] & ~attacked & to_mask, moveVec)
        size = moveVec.size()
        for i in range(size):
            yield chess.Move(king, moveVec[i])    

    checker = chess.msb(checkers)
    
    if chess.BB_SQUARES[checker] == checkers:
        # Capture or block a single checker.
        target = betweenPieces(king, checker) | checkers

        yield from generate_pseudo_legal_moves(board, ~board.kings & from_mask, target & to_mask)
        
        # Capture the checking pawn en passant (but avoid yielding
        # duplicate moves).
        if board.ep_square and not chess.BB_SQUARES[board.ep_square] & target:
            last_double = board.ep_square + (-8 if board.turn == chess.WHITE else 8)
            if last_double == checker:
                yield from board.generate_pseudo_legal_ep(from_mask, to_mask)

def generate_pseudo_legal_moves(object board, uint64_t from_mask, uint64_t to_mask) -> Iterator[chess.Move]:
    
    """
    Function to generate pseudo legal moves through yielding

    Parameters:
    - board: The current board state
    - from_mask: The starting position mask
    - to_mask: The ending position mask

    Yields:
    - Legal evasion chess.Moves
    """
    
    # Define masks for our pieces and all pieces
    cdef uint64_t our_pieces = board.occupied_co[board.turn]
    cdef uint64_t all_pieces = board.occupied
    
    # Define vectors to hold different pseudo legal moves
    cdef vector[uint8_t] pieceVec
    cdef vector[uint8_t] pieceMoveVec
    cdef vector[uint8_t] pawnVec
    cdef vector[uint8_t] pawnMoveVec
    cdef vector[uint8_t] promotionVec

    cdef uint8_t outterSize
    cdef uint8_t innerSize
   
    # Call the c++ function to generate piece moves.
    generate_piece_moves(pieceVec, pieceMoveVec, our_pieces, board.pawns, board.knights, board.bishops, board.rooks, board.queens, board.kings, board.occupied_co[True], board.occupied_co[False], board.occupied, from_mask, to_mask)
    outterSize = pieceVec.size()  
    
    for i in range(outterSize):
        yield chess.Move(pieceVec[i], pieceMoveVec[i])
    
    # Call the c++ function to generate castling moves.
    if from_mask & board.kings:
        yield from board.generate_castling_moves(from_mask, to_mask)

    # The remaining moves are all pawn moves.
    cdef uint64_t pawns = board.pawns & board.occupied_co[board.turn] & from_mask
    if not pawns:
        return

    generate_pawn_moves(pawnVec, pawnMoveVec, promotionVec, board.occupied_co[not board.turn], board.occupied, board.turn, pawns, from_mask, to_mask)
    outterSize = pawnVec.size()  
    for i in range(outterSize):
        if (promotionVec[i] == 1):
            yield chess.Move(pawnVec[i], pawnMoveVec[i])
        else:
            yield chess.Move(pawnVec[i], pawnMoveVec[i], promotionVec[i])

    # Call the c++ function to generate en passant captures.
    if board.ep_square:
        yield from board.generate_pseudo_legal_ep(from_mask, to_mask)

def is_check(object board, uint64_t from_mask, uint64_t to_mask, bint board_turn) -> bint:
    """
    Determine if the current player is in check.

    Args:
        board: Chess board object with attributes
        from_mask: uint64_t bitmask for potential starting squares (currently unused)
        to_mask: uint64_t bitmask for potential target squares (currently unused)
        board_turn: bint indicating current player (0 for white, 1 for black)

    Returns:
        True if the current player's king is under attack, False otherwise

    Raises:
        ValueError: If the current player has no king on the board

    Note:
        from_mask and to_mask parameters are accepted for API compatibility
        but are currently not used in the check calculation
    """
        
    # Pre-compute opponent color 
    cdef bint opponent_color = not board_turn 

    # Acquire the current king position
    cdef uint64_t current_king_mask = board.kings & board.occupied_co[board_turn]
    cdef int king_pos
    cdef uint8_t king_square

    # Handle empty board case - no king present
    if current_king_mask == 0:
        return False  # Not in check if there's no king

    # Extract king position from bitmask
    king_pos = current_king_mask.bit_length() - 1
    if king_pos < 0 or king_pos > 63:
        return False  # Not in check if there's no king or invalid position
    king_square = <uint8_t>king_pos

    cdef uint64_t checkers = attackers_mask(opponent_color, king_square, board.occupied, board.queens | board.rooks, board.queens | board.bishops, board.kings, board.knights, board.pawns, board.occupied_co[opponent_color])

    # Return check status
    cdef bint is_currently_in_check = checkers != 0
    return is_currently_in_check

cpdef int count_legal_moves(object board, uint64_t from_mask, uint64_t to_mask):
    """
    Count the number of legal moves for the current position.

    Parameters:
    - board: The current board state
    - from_mask: The starting position mask
    - to_mask: The ending position mask

    Returns:
    - int: Number of legal moves
    """
    cdef int count = 0
    cdef object move
    for move in generate_legal_captures(board, from_mask, to_mask):
        count += 1
    return count

# Initialize module upon import
initialize()
