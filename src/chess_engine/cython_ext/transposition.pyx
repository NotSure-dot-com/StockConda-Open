# cython: language_level=3
# cython: boundscheck=False, wraparound=False, nonecheck=False, cdivision=True

# === Python Imports (Combine all needed) ===
import chess
import random
from dataclasses import dataclass

# === C Imports (Combine all needed) ===
from libc.stdlib cimport malloc, free
from libc.string cimport memset
from libc.stdint cimport uint64_t, uint32_t, int32_t, uint16_t, uint8_t

# ==============================================================
# === ZOBRIST HASHING CODE (Copied from your Zobrist cell) ===
# ==============================================================

# --- Zobrist Keys (Global C variables) ---
cdef uint64_t c_zobrist_keys[64][6][2]
cdef uint64_t c_zobrist_castling[4]
cdef uint64_t c_zobrist_en_passant[8]
cdef uint64_t c_zobrist_side
cdef bint zobrist_initialized = False

# --- Zobrist Helper mapping functions ---
# Making these inline can sometimes help the C compiler optimize

cdef int piece_type_to_index(int piece_type):
    return piece_type - 1

cdef int color_to_index(bint color):
    # Use direct integer values for colors if possible
    # Assuming chess.BLACK is 1 and chess.WHITE is 0 (or vice-versa)
    # return 1 if color == chess.BLACK else 0 # Relies on Python object comparison
    return <int>color # Directly cast bool to int (True->1, False->0) - Check if this matches chess color constants!

# --- Zobrist init_zobrist (Python def function) ---
# This still needs to be callable from Python to initialize keys
def init_zobrist():
    global zobrist_initialized, c_zobrist_side
    global c_zobrist_keys, c_zobrist_castling, c_zobrist_en_passant
    cdef int square, pt_idx, c_idx, file_idx, piece_type
    cdef bint color # Use bint for boolean color

    if not zobrist_initialized:
        random.seed(42)
        for square in range(64):
            for piece_type in range(1, 7): # chess.PIECE_TYPES are 1-6
                pt_idx = piece_type_to_index(piece_type)
                # Iterate through boolean colors directly
                for color in [False, True]: # False=WHITE, True=BLACK
                    c_idx = color_to_index(color)
                    c_zobrist_keys[square][pt_idx][c_idx] = <uint64_t>random.getrandbits(64)

        for i in range(4): c_zobrist_castling[i] = <uint64_t>random.getrandbits(64)
        for file_idx in range(8): c_zobrist_en_passant[file_idx] = <uint64_t>random.getrandbits(64)
        c_zobrist_side = <uint64_t>random.getrandbits(64)
        zobrist_initialized = True
    #print("info Zobrist keys initialized.", flush=True) # Confirmation

# --- Zobrist Hashing Function (Make it cpdef for C speed) ---
# Takes Python board object, returns C integer
cpdef uint64_t zobrist_hash(object board_obj) except? 0: # Add except? 0 for error check
    """
    Calculates the Zobrist hash for the given board state using C arrays.
    Uses the board's helper methods for castling rights. (Cythonized version)
    """
    # Cast board object for type safety if needed, or access attributes directly
    #cdef chess.Board board = <chess.Board>board_obj # Cast the input object
    cdef object board = board_obj # Use 'object' for the board within C context
    cdef uint64_t hash_value = 0
    cdef int square, pt_idx, c_idx, ep_file
    cdef uint64_t occupied = board.occupied
    cdef uint64_t occupied_white = board.occupied_co[chess.WHITE]
    cdef uint64_t square_mask
    cdef uint64_t pawns = board.pawns
    cdef uint64_t knights = board.knights
    cdef uint64_t bishops = board.bishops
    cdef uint64_t rooks = board.rooks
    cdef uint64_t queens = board.queens

    # Ensure initialization happened (can be removed if guaranteed elsewhere)
    if not zobrist_initialized:
        # Decide: raise error or initialize lazily? Initializing here might be slow.
        # Best practice: Ensure init_zobrist() is called once globally before search starts.
         #print("info Warning: zobrist_hash called before init_zobrist!", flush=True) # Avoid lazy init inside hash function if possible
         # init_zobrist() # Avoid lazy init inside hash function if possible
         return 0 # Return default/error value

    # Match piece_type_at()/piece_at() without building a Python dictionary or
    # Piece objects. Their type priority and WHITE-else-BLACK color rule are
    # preserved, including the KING fallback for an occupied unusual square.
    for square in range(64):
        square_mask = <uint64_t>1 << square
        if not (occupied & square_mask):
            continue
        if pawns & square_mask:
            pt_idx = 0
        elif knights & square_mask:
            pt_idx = 1
        elif bishops & square_mask:
            pt_idx = 2
        elif rooks & square_mask:
            pt_idx = 3
        elif queens & square_mask:
            pt_idx = 4
        else:
            pt_idx = 5
        c_idx = 1 if occupied_white & square_mask else 0
        hash_value ^= c_zobrist_keys[square][pt_idx][c_idx]

    # Castling rights (using board methods - Python calls)
    if board.has_kingside_castling_rights(chess.WHITE): hash_value ^= c_zobrist_castling[0]
    if board.has_queenside_castling_rights(chess.WHITE): hash_value ^= c_zobrist_castling[1]
    if board.has_kingside_castling_rights(chess.BLACK): hash_value ^= c_zobrist_castling[2]
    if board.has_queenside_castling_rights(chess.BLACK): hash_value ^= c_zobrist_castling[3]

    # En passant square (Python attribute access)
    ep_square = board.ep_square
    if ep_square is not None:
        ep_file = chess.square_file(ep_square) # Python call
        hash_value ^= c_zobrist_en_passant[ep_file]

    # Side to move (Python attribute access)
    if board.turn == chess.BLACK: # Use constant if possible
        hash_value ^= c_zobrist_side

    return hash_value

# SplitMix64 finalizer operations from Sebastiano Vigna's public-domain
# implementation: https://prng.di.unimi.it/splitmix64.c. The seed and rolling
# step domains below are custom and are not a cryptographic hash construction.
cdef inline uint64_t mix_history(uint64_t value) noexcept:
    value = (value ^ (value >> 30)) * <uint64_t>0xbf58476d1ce4e5b9
    value = (value ^ (value >> 27)) * <uint64_t>0x94d049bb133111eb
    return value ^ (value >> 31)

cpdef uint64_t seed_history_cy(uint64_t position_key):
    return mix_history(position_key ^ <uint64_t>0x243f6a8885a308d3)

cpdef uint64_t advance_history_cy(uint64_t parent_history,
                                  uint64_t child_key, bint zeroing):
    if zeroing:
        return seed_history_cy(child_key)
    return mix_history(parent_history ^ child_key ^ <uint64_t>0x9e3779b97f4a7c15)

cpdef tuple tt_context_from_board(object board):
    # The board/stack is copied once at the search boundary, never per node.
    scratch = board.copy(stack=True)
    keys = [zobrist_hash(scratch)]
    while scratch.move_stack:
        move = scratch.pop()
        # Test against the position BEFORE the actual move.
        if scratch.is_zeroing(move):
            break
        keys.append(zobrist_hash(scratch))
    # Module-wide wraparound=False: do not use negative list indices here.
    history = seed_history_cy(keys[len(keys) - 1])
    for child_key in reversed(keys[:len(keys) - 1]):
        history = advance_history_cy(history, child_key, False)
    return keys[0], history

# ==============================================================
# === TRANSPOSITION TABLE CODE (Starts Here) ===================
# ==============================================================

# --- Constants ---
cdef uint8_t NODE_TYPE_EXACT = 0
cdef uint8_t NODE_TYPE_LOWERBOUND = 1
cdef uint8_t NODE_TYPE_UPPERBOUND = 2
cdef uint16_t NO_MOVE_ENCODED = 0xFFFF
MATE_SCORE = 10000
MATE_SCORE_THRESHOLD = MATE_SCORE - 1000
Q_SEARCH_TT_DEPTH = 0
C_KNIGHT = chess.KNIGHT # Defined once

# --- C Struct for TT Entry ---
cdef struct TTEntryC:
    uint64_t signature
    uint64_t history_context
    int32_t value
    uint8_t depth
    uint8_t node_type
    uint16_t best_move_encoded
    uint16_t store_ply
    uint32_t halfmove_context # clock + 1; zero means no reusable value context

# --- Global C variables for the TT ---
cdef TTEntryC* c_transposition_table = NULL
cdef uint64_t C_TABLE_SIZE = 0
cdef uint64_t C_TABLE_INDEX_MASK = 0

# --- Helper: Move Encoding/Decoding ---
# (Definitions as before - using object, checking isinstance, etc.)
cpdef uint16_t encode_move(object move_obj) except? 0xFFFF:
    cdef object move;
    cdef uint16_t encoded_move=NO_MOVE_ENCODED;
    cdef int from_sq,to_sq,promotion
    if move_obj is None: return NO_MOVE_ENCODED
    if not isinstance(move_obj, chess.Move): return NO_MOVE_ENCODED
    move=move_obj; from_sq=move.from_square; to_sq=move.to_square
    promotion=move.promotion if move.promotion is not None else 0
    encoded_move = from_sq | (to_sq << 6) | (promotion << 12)
    return encoded_move

cpdef object decode_move(uint16_t encoded_move):
    if encoded_move == NO_MOVE_ENCODED: return None
    cdef int from_sq, to_sq, promotion_type;
    cdef object promotion = None
    from_sq = encoded_move & 0x3F; to_sq = (encoded_move >> 6) & 0x3F
    promotion_type = (encoded_move >> 12)
    if promotion_type >= C_KNIGHT: promotion = promotion_type
    try: return chess.Move(from_sq, to_sq, promotion=promotion)
    except ValueError: return None

# --- Helper: Mate Score Adjustment (Cython versions) ---
cdef inline int32_t c_adjust_mate_score_for_store(int32_t value, uint16_t ply):
    cdef int32_t abs_val = abs(value)
    if abs_val > MATE_SCORE_THRESHOLD: return value + ply if value > 0 else value - ply
    return value
cdef inline int32_t c_adjust_mate_score_for_probe(int32_t value, uint16_t ply):
    cdef int32_t abs_val = abs(value)
    if abs_val > MATE_SCORE_THRESHOLD: return value - ply if value > 0 else value + ply
    return value

# --- TT Initialization and Cleanup ---
# (init_cython_tt and free_cython_tt definitions - ensure C_TABLE_SIZE assignment is on new line)
def init_cython_tt(uint64_t table_size_bytes):
    global c_transposition_table, C_TABLE_SIZE, C_TABLE_INDEX_MASK
    if c_transposition_table != NULL: free(c_transposition_table); c_transposition_table = NULL
    cdef size_t entry_size = sizeof(TTEntryC)
    if entry_size == 0: raise MemoryError("Could not determine size of TTEntryC struct.")
    C_TABLE_SIZE = table_size_bytes // entry_size # Assign on separate line
    if C_TABLE_SIZE == 0: raise MemoryError(f"Requested memory {table_size_bytes} bytes is too small.")
    cdef uint64_t power_of_2_size = 1
    while (power_of_2_size * 2) <= C_TABLE_SIZE: power_of_2_size *= 2
    if power_of_2_size != C_TABLE_SIZE: C_TABLE_SIZE = power_of_2_size
    C_TABLE_INDEX_MASK = C_TABLE_SIZE - 1
    c_transposition_table = <TTEntryC*> malloc(C_TABLE_SIZE * entry_size)
    if c_transposition_table == NULL: C_TABLE_SIZE = 0; C_TABLE_INDEX_MASK = 0; raise MemoryError("Malloc failed")
    memset(c_transposition_table, 0, C_TABLE_SIZE * entry_size)
    #print(f"info Successfully initialized Cython TT with {C_TABLE_SIZE} entries.", flush=True) # Confirmation

def free_cython_tt():
    global c_transposition_table, C_TABLE_SIZE, C_TABLE_INDEX_MASK
    if c_transposition_table != NULL: free(c_transposition_table); c_transposition_table = NULL; C_TABLE_SIZE = 0; C_TABLE_INDEX_MASK = 0

cpdef tuple tt_layout_cy():
    return sizeof(TTEntryC), C_TABLE_SIZE, C_TABLE_SIZE * sizeof(TTEntryC)

# --- Core Cython TT Functions ---
# (store_tt_entry_cy, probe_tt_entry_cy, probe_tt_move_cy definitions as before)
cdef inline uint32_t encode_halfmove_context(object clock):
    # Validate before the C cast: Python board attributes can hold large or
    # malformed values. Never let an unknown/overflowed context match clock 0.
    if type(clock) is not int or clock < 0 or clock >= 4294967295:
        return 0
    return <uint32_t>clock + <uint32_t>1

cdef inline bint valid_history_context(object history):
    return type(history) is int and 0 <= history < 18446744073709551616

cpdef void store_tt_entry_cy(uint64_t board_hash, uint8_t depth, int32_t value, uint8_t node_type_enum, object best_move_obj, uint16_t current_ply, object halfmove_clock=None, object history_context=None):
    cdef uint64_t index;
    cdef TTEntryC* entry_ptr;
    cdef int32_t adjusted_value;
    cdef uint16_t encoded_move
    if c_transposition_table == NULL: return
    index = board_hash & C_TABLE_INDEX_MASK; entry_ptr = &c_transposition_table[index]
    if entry_ptr.signature == 0 or depth >= entry_ptr.depth:
        adjusted_value = c_adjust_mate_score_for_store(value, current_ply); encoded_move = encode_move(best_move_obj)
        entry_ptr.signature = board_hash; entry_ptr.value = adjusted_value; entry_ptr.depth = depth
        entry_ptr.node_type = node_type_enum; entry_ptr.best_move_encoded = encoded_move
        entry_ptr.store_ply = current_ply
        entry_ptr.history_context = <uint64_t>history_context if valid_history_context(history_context) else 0
        entry_ptr.halfmove_context = encode_halfmove_context(halfmove_clock) if valid_history_context(history_context) else 0

@dataclass
class TTProbeResult: # Python dataclass for return
    value: int; depth: int; node_type: str; best_move: chess.Move | None

cpdef object probe_tt_entry_cy(uint64_t board_hash, uint8_t required_depth, uint16_t current_ply, object halfmove_clock=None, object history_context=None):
    cdef uint64_t index;
    cdef TTEntryC* entry_ptr;
    cdef int32_t adjusted_value;
    cdef object decoded_move;
    cdef str node_type_str
    if c_transposition_table == NULL: return None
    if not valid_history_context(history_context): return None
    cdef uint32_t context = encode_halfmove_context(halfmove_clock)
    if context == 0: return None
    index = board_hash & C_TABLE_INDEX_MASK; entry_ptr = &c_transposition_table[index]
    if entry_ptr.signature != board_hash: return None
    if entry_ptr.halfmove_context != context: return None
    if entry_ptr.history_context != <uint64_t>history_context: return None
    if entry_ptr.depth < required_depth: return None
    adjusted_value = c_adjust_mate_score_for_probe(entry_ptr.value, current_ply)
    decoded_move = decode_move(entry_ptr.best_move_encoded)
    if entry_ptr.node_type == NODE_TYPE_EXACT: node_type_str = "EXACT"
    elif entry_ptr.node_type == NODE_TYPE_LOWERBOUND: node_type_str = "LOWERBOUND"
    elif entry_ptr.node_type == NODE_TYPE_UPPERBOUND: node_type_str = "UPPERBOUND"
    else: node_type_str = "UNKNOWN"
    return TTProbeResult(value=adjusted_value, depth=entry_ptr.depth, node_type=node_type_str, best_move=decoded_move)

cpdef tuple probe_tt_move_cy(uint64_t board_hash):
    cdef uint64_t index;
    cdef TTEntryC* entry_ptr;
    cdef object decoded_move
    if c_transposition_table == NULL: return (None, 0)
    index = board_hash & C_TABLE_INDEX_MASK; entry_ptr = &c_transposition_table[index]
    if entry_ptr.signature != board_hash: return (None, 0)
    if entry_ptr.best_move_encoded == NO_MOVE_ENCODED: return (None, 0)
    decoded_move = decode_move(entry_ptr.best_move_encoded)
    return (decoded_move, entry_ptr.depth)

# --- Python Wrapper Functions ---
# These now directly call the cpdef functions defined ABOVE in this cell

def store_tt_entry(board: chess.Board, depth: int, value: int, node_type: str,
                   best_move: chess.Move | None = None, *, tt_context=None):
    cdef uint8_t node_type_enum
    cdef uint64_t board_hash
    cdef uint16_t current_ply
    if node_type == "EXACT": node_type_enum = NODE_TYPE_EXACT
    elif node_type == "LOWERBOUND": node_type_enum = NODE_TYPE_LOWERBOUND
    elif node_type == "UPPERBOUND": node_type_enum = NODE_TYPE_UPPERBOUND
    else: return
    if depth < 0 or depth > 255: depth = 255
    if tt_context is None:
        tt_context = tt_context_from_board(board)
    board_hash, history_context = tt_context
    current_ply = <uint16_t>board.ply()
    # Calls the cpdef store_tt_entry_cy defined in THIS cell
    store_tt_entry_cy(board_hash, <uint8_t>depth, <int32_t>value, node_type_enum,
                      best_move, current_ply, board.halfmove_clock, history_context)

def probe_tt_entry(board: chess.Board, depth: int, *, tt_context=None) -> TTProbeResult | None:
    cdef uint64_t board_hash
    cdef uint16_t current_ply
    if depth < 0 or depth > 255: return None
    if tt_context is None:
        tt_context = tt_context_from_board(board)
    board_hash, history_context = tt_context
    current_ply = <uint16_t>board.ply()
    # Calls the cpdef probe_tt_entry_cy defined in THIS cell
    return probe_tt_entry_cy(board_hash, <uint8_t>depth, current_ply,
                             board.halfmove_clock, history_context)

def probe_tt_move(board: chess.Board, *, board_hash=None) -> tuple[chess.Move | None, int]:
    if board_hash is None:
        board_hash = zobrist_hash(board)
    # Calls the cpdef probe_tt_move_cy defined in THIS cell
    return probe_tt_move_cy(board_hash)
