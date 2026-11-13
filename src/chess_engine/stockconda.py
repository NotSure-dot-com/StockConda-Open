from collections import deque
import chess
import sys
import time
import threading # For handling 'stop' command asynchronously
import atexit
import math
import gc
from chess_engine.uci_search_worker import SearchTask, SearchWorker
from collections import deque
from typing import Dict, List, Tuple, Iterator, Literal, TextIO, TypedDict, Unpack, TypeVar, NamedTuple, Sequence, overload

from chess_engine.cython_ext import cython_chess
from chess_engine.cython_ext.transposition import MATE_SCORE, MATE_SCORE_THRESHOLD, advance_history_cy, init_zobrist, probe_tt_entry, store_tt_entry, free_cython_tt, probe_tt_move, init_cython_tt, tt_context_from_board, zobrist_hash
from chess_engine.cython_ext.evaluation import evaluate_board, evaluate_board_static, get_game_phase_cython
from chess_engine.opening_book import load_opening_book

# Global debug flag - set to True to enable debug prints
DEBUG = False


def _board_from_fen_moves(fen: str, moves: tuple[str, ...]) -> chess.Board:
    """Restore an owned board including all history supplied in the snapshot."""
    board = chess.Board(fen)
    for move in moves:
        board.push_uci(move)
    return board


def _child_tt_context(board: chess.Board, parent: tuple[int, int],
                      zeroing: bool, depth: int) -> tuple[int, int] | None:
    if depth <= 0:
        return None
    key = zobrist_hash(board)
    return key, advance_history_cy(parent[1], key, zeroing)


class PrintOptions(TypedDict, total=False):
    sep: str | None
    end: str | None
    file: TextIO | None
    flush: bool


HistoryKey = tuple[int, int, bool]
DefaultValue = TypeVar('DefaultValue')


class _OrderingSnapshot(NamedTuple):
    history: tuple[tuple[int, ...], ...]
    killers: tuple[chess.Move | None, chess.Move | None]


class _HistoryCellView:
    def __init__(self, table: 'HistoryTable', source: int, target: int) -> None:
        self._table = table
        self._source = source
        self._target = target

    def __getitem__(self, turn: bool | int) -> int:
        return self._table._planes[int(turn)][self._source][self._target]

    def __setitem__(self, turn: bool | int, value: int) -> None:
        self._table.set_value(turn, self._source, self._target, value)


class _HistoryRowView:
    def __init__(self, table: 'HistoryTable', source: int) -> None:
        self._table = table
        self._source = source

    def __getitem__(self, target: int) -> _HistoryCellView:
        return _HistoryCellView(self._table, self._source, target)


class HistoryTable(dict[HistoryKey, int]):
    """Two color planes with a compatibility view for legacy 3D indexing."""

    def __init__(self) -> None:
        self._planes = [[[0 for _ in range(64)] for _ in range(64)] for _ in range(2)]
        self._snapshots: list[list[tuple[int, ...] | None]] = [
            [None for _ in range(64)] for _ in range(2)
        ]
        self._row_views = tuple(_HistoryRowView(self, source) for source in range(64))
        super().__init__()

    @classmethod
    def from_legacy(cls, array: list[list[list[int]]]) -> 'HistoryTable':
        table = cls()
        for source in range(64):
            for target in range(64):
                cell = array[source][target]
                table._planes[0][source][target] = cell[0]
                table._planes[1][source][target] = cell[1]
        return table

    @overload
    def __getitem__(self, key: HistoryKey) -> int: ...
    @overload
    def __getitem__(self, key: int) -> _HistoryRowView: ...
    def __getitem__(self, key: HistoryKey | int) -> int | _HistoryRowView:
        if isinstance(key, tuple):
            source, target, turn = key
            return self._planes[int(turn)][source][target]
        return self._row_views[key]

    def __setitem__(self, key: HistoryKey, value: int) -> None:
        source, target, turn = key
        self.set_value(turn, source, target, value)

    def plane(self, turn: bool | int) -> tuple[tuple[int, ...], ...]:
        """Return an immutable public view of one color's history values."""
        return tuple(tuple(row) for row in self._planes[int(turn)])

    def _plane(self, turn: bool | int) -> list[list[int]]:
        """Return the mutable internal plane for latency-sensitive read access."""
        return self._planes[int(turn)]

    def set_value(self, turn: bool | int, source: int, target: int, value: int) -> None:
        color = int(turn)
        self._planes[color][source][target] = value
        self._snapshots[color][source] = None

    def add(self, turn: bool | int, source: int, target: int, delta: int) -> None:
        color = int(turn)
        self._planes[color][source][target] += delta
        self._snapshots[color][source] = None

    def snapshot(self, turn: bool | int, occupied: int) -> tuple[tuple[int, ...], ...]:
        color = int(turn)
        plane = self._planes[color]
        cached = self._snapshots[color]
        rows: list[tuple[int, ...]] = [()] * 64
        remaining = occupied
        while remaining:
            source_bit = remaining & -remaining
            source = source_bit.bit_length() - 1
            row = cached[source]
            if row is None:
                row = tuple(plane[source])
                cached[source] = row
            rows[source] = row
            remaining ^= source_bit
        return tuple(rows)

    @overload
    def get(self, key: HistoryKey) -> int: ...
    @overload
    def get(self, key: HistoryKey, default: DefaultValue) -> int | DefaultValue: ...
    def get(self, key: HistoryKey, default: DefaultValue | int = 0) -> int | DefaultValue:
        try:
            return self[key]
        except IndexError:
            return default

    def clear(self) -> None:
        self._planes = [[[0 for _ in range(64)] for _ in range(64)] for _ in range(2)]
        self._snapshots = [[None for _ in range(64)] for _ in range(2)]

    def decay(self) -> None:
        """Age all history scores by shifting them right by 1."""
        for plane in self._planes:
            for row in plane:
                for target in range(64):
                    row[target] >>= 1
        self._snapshots = [[None for _ in range(64)] for _ in range(2)]

    # Legacy dict subclass returns a generator, not a dict_items view.
    # Preserve that public behavior; only the superclass return contract differs.
    def items(self) -> Iterator[tuple[HistoryKey, int]]:  # type: ignore[override]
        for turn, plane in enumerate(self._planes):
            for source, row in enumerate(plane):
                for target, value in enumerate(row):
                    if value != 0:
                        yield ((source, target, bool(turn)), value)

    def __len__(self) -> int:
        return sum(value != 0 for plane in self._planes for row in plane for value in row)

class TimeManager:
    """Manages time allocation for chess moves based on UCI time controls."""

    def __init__(self) -> None:
        self.wtime: float | None = None
        self.btime: float | None = None
        self.winc: float = 0.0
        self.binc: float = 0.0
        self.movestogo: int | None = None

    def set_time_controls(self, wtime_ms: int | None, btime_ms: int | None,
                         winc_ms: int | None, binc_ms: int | None,
                         movestogo: int | None) -> None:
        """Set time controls in milliseconds, convert to seconds."""
        self.wtime = wtime_ms / 1000.0 if wtime_ms is not None else None
        self.btime = btime_ms / 1000.0 if btime_ms is not None else None
        self.winc = winc_ms / 1000.0 if winc_ms is not None else 0.0
        self.binc = binc_ms / 1000.0 if binc_ms is not None else 0.0
        self.movestogo = movestogo

    def get_time_limit(self, is_white_turn: bool, phase: str = 'middlegame', num_moves: int = 30) -> float | None:
        """Calculate time limit for the current move with intelligent allocation."""
        if self.wtime is None and self.btime is None:
            return None

        my_time = self.wtime if is_white_turn else self.btime
        my_inc = self.winc if is_white_turn else self.binc

        if my_time is None:
            return None

        moves_to_go = max(1, self.movestogo if self.movestogo is not None else 30)  # default assumption, prevent division by zero
        time_per_move = my_time / moves_to_go
        allocated_time = time_per_move + my_inc * 0.8  # use 80% of increment

        # Adjust for game phase
        phase_multipliers = {'opening': 1.3, 'middlegame': 1.1, 'endgame': 0.9}
        allocated_time *= phase_multipliers.get(phase, 1.0)

        # Adjust for position complexity (number of legal moves)
        if num_moves > 40:
            allocated_time *= 1.3  # complex position
        elif num_moves < 10:
            allocated_time *= 0.8  # simple position

        # As a safeguard, never use more than 80% of the remaining time for a single move.
        # The primary time allocation is handled by the moves_to_go logic.
        max_time = my_time * 0.8
        return min(allocated_time, max_time)


class UCIEngine:
    PIECE_VALUES: dict[int | None, int] = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 20
    }


    # Parameters for LMR formula (using the previous version's formula with 1.7 divisor)
    MAX_LMR_DEPTH = 64  # Max depth for precomputing the table
    MAX_MOVES_PER_POSITION = 256  # A large enough number for legal moves
    init_zobrist() #Initialize zobrist hashing

    def _new_history_table(self) -> HistoryTable:
        """Create a new zero-initialized history table."""
        return HistoryTable()

    def __init__(self) -> None:
        self._output_lock = threading.Lock()
        self.board = chess.Board()
        self.tt_size_bytes = 16 * 1024 * 1024
        self.use_opening_book = False
        self.opening_book = None
        if self.use_opening_book:
            self.opening_book = load_opening_book()
        self.use_transposition_table = True
        self._tt_freed = False
        self._shutdown_started = False
        self._active_cancel: threading.Event | None = None
        self._active_task: SearchTask | None = None
        self.ponder_enabled = False
        self._ponder_clock_armed = False
        if self.use_transposition_table:
            init_cython_tt(self.tt_size_bytes)
            atexit.register(self._shutdown)
        self.searching = False
        self.stop_flag = False
        self.best_move_found: chess.Move | None = None
        self.history_table = self._new_history_table()
        self.killer_moves: list[list[chess.Move | None]] = [[None, None] for _ in range(self.MAX_LMR_DEPTH)]
        self.time_manager = TimeManager()
        self.max_search_depth = self.MAX_LMR_DEPTH # Default maximum search depth for performance stability
        self.max_depth_limit: int | None = self.MAX_LMR_DEPTH # UCI MaxDepth option

        # --- State for sophisticated stop_flag_check ---
        self._stop_check_counter = 0
        self._stop_check_threshold = 1 # Initial threshold
        self._search_start_time: float = 0.0 # Monotonic seconds; initialized per search
        self._search_info_start_time: float = 0.0 # Total ponder + normal search duration
        self._search_time_limit: float | None = None # To be set in start_search
        self._last_time_check_time: float = 0.0 # To track when the last actual time check happened

        self.lmr_reduction_table = self.precompute_lmr_table(
            self.MAX_LMR_DEPTH,
            self.MAX_MOVES_PER_POSITION,
            2,  # base_reduction
            1   # move_index_multiplier
        )
        # PVS indexes these factors at every supported remaining search depth.
        self.history_factors = self.precompute_history_factors(self.MAX_LMR_DEPTH)
        self._search_worker = SearchWorker(self._execute_uci_task)

    def _uci_print(self, *args: object, **kwargs: Unpack[PrintOptions]) -> None:
        """Write one complete UCI line without interleaving search-thread output."""
        with self._output_lock:
            print(*args, **kwargs)

    def _stop_active_search(self) -> bool:
        """Finalize the current task before mutating shared engine resources."""
        self._search_worker.cancel_and_wait()
        return True

    def _execute_uci_task(self, task: SearchTask) -> None:
        self._active_cancel = task.cancelled
        self._active_task = task
        self.stop_flag = False
        self.searching = True
        try:
            self._run_search_and_report_bestmove(
                task.time_limit, task.depth_limit, task.infinite, task.fen, task.moves)
        finally:
            self.searching = False
            self._active_cancel = None
            self._active_task = None

    def _cancel_requested(self) -> bool:
        event = getattr(self, "_active_cancel", None)
        return self.stop_flag or (event is not None and event.is_set())

    def execute_search_task(self, time_limit: float | None, depth_limit: int | None,
                            infinite_search: bool, board_fen: str | None = None,
                            moves: tuple[str, ...] = ()) -> tuple[chess.Move | None, int]:
        """Search an isolated position, preserving its known repetition history.

        Explicit FEN callers may supply subsequent UCI moves; a bare FEN has
        no known history. Without a FEN, copy the engine board with its stack.
        """
        
        # Reset search state
        self._stop_check_counter = 0
        self._stop_check_threshold = 1
        self._search_start_time = time.monotonic()
        self._search_info_start_time = self._search_start_time
        task = self._active_task
        hit_at = task.hit_at if task is not None and task.ponder else None
        self._ponder_clock_armed = hit_at is not None
        if hit_at is not None:
            self._search_start_time = hit_at
        self._search_time_limit = (
            None if task is not None and task.ponder and hit_at is None
            else time_limit
        )
        self._last_time_check_time = self._search_start_time

        self.history_table.decay()
        
        board = (self.board.copy() if board_fen is None
                 else _board_from_fen_moves(board_fen, moves))
        root_context = tt_context_from_board(board)
        
        # Search implementation
        current_depth = 1
        max_depth = depth_limit if depth_limit is not None else self.max_depth_limit if self.max_depth_limit is not None else self.MAX_LMR_DEPTH
        if infinite_search:
            max_depth = self.MAX_LMR_DEPTH
        max_depth = min(max_depth, self.MAX_LMR_DEPTH)
        
        best_move_so_far = None
        best_value_so_far = -MATE_SCORE
        
        while current_depth <= max_depth and not self._cancel_requested():
            try:
                value, move = self.pvs_search(
                    board, current_depth, -MATE_SCORE, MATE_SCORE, 0, 0,
                    tt_context=root_context)
            except Exception as e:
                raise RuntimeError(f"search error at depth {current_depth}: {e}") from e

            # Never replace a completed result with an interrupted iteration.
            if value is None or self._cancel_requested():
                break

            if move is not None:
                best_move_so_far = move
                best_value_so_far = value

                # Send info for current depth
                elapsed = time.monotonic() - self._search_info_start_time
                if abs(value) > MATE_SCORE_THRESHOLD:
                    # Mate scores encode the absolute board ply. UCI reports
                    # signed moves to mate from the current search position.
                    plies_to_mate = MATE_SCORE - abs(value) - board.ply()
                    moves_to_mate = (plies_to_mate + 1) // 2
                    score = f"mate {moves_to_mate if value > 0 else -moves_to_mate}"
                else:
                    score = f"cp {value}"
                self._uci_print(f"info depth {current_depth} score {score} time {int(elapsed * 1000)} pv {move.uci()}", flush=True)

            if self.stop_flag:
                break
            
            current_depth += 1
        
        return best_move_so_far, best_value_so_far

    def run(self) -> None:
        """Main loop to read and process UCI commands."""
        self._uci_print("StockConda v0.2.3.5.dev2 by Real", flush=True)
        try:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    break
                line = line.strip()
                # Strip surrounding quotes if present (PowerShell echo issue)
                if line.startswith('"') and line.endswith('"'):
                    line = line[1:-1]
                if not line:
                    continue

                parts = line.split()
                command = parts[0]

                if command == "uci":
                    self.handle_uci()

                elif command == "setoption":
                    self.handle_setoption(parts[1:])
                elif command == "isready":
                    self.handle_isready()
                elif command == "ucinewgame":
                    self.handle_ucinewgame()
                elif command == "position":
                    self.handle_position(parts[1:])
                elif command == "go":
                    self.handle_go(parts[1:])
                elif command == "stop":
                    self.handle_stop()
                elif command == "ponderhit":
                    self.handle_ponderhit()
                elif command == "quit":
                    self.handle_quit()
                    break # Exit the loop

        finally:
            self._shutdown()


    def handle_uci(self) -> None:
        if DEBUG:
            self._uci_print("info debug: handling uci command", flush=True)
        self._uci_print("id name StockConda v0.2.3.5.dev2 by Real", flush=True)
        self._uci_print("id author ReallyNot Sure", flush=True)
        self._uci_print("option name Hash type spin default 16 min 1 max 65536", flush=True)  # Hash size in MB
        self._uci_print("option name Book type check default false", flush=True)
        self._uci_print("option name Ponder type check default false", flush=True)
        self._uci_print(f"option name MaxDepth type spin default {self.MAX_LMR_DEPTH} min 1 max {self.MAX_LMR_DEPTH}", flush=True)
        self._uci_print("uciok", flush=True)
        if DEBUG:
            self._uci_print("info debug: uciok sent", flush=True)

    def handle_setoption(self, parts: list[str]) -> None:
        if not self._stop_active_search():
            return
        if len(parts) >= 4 and parts[0] == "name" and parts[2] == "value":
            option_name = parts[1]
            option_value = " ".join(parts[3:])
            
            if option_name == "Hash":
                try:
                    mb = int(option_value)
                    self.tt_size_bytes = mb * 1024 * 1024
                    if self.use_transposition_table and 1 <= mb <= 65536:
                        init_cython_tt(self.tt_size_bytes)
                    else:
                        self._uci_print(f"info string Invalid hash size: {option_value}", flush=True)
                except ValueError:
                    self._uci_print(f"info string Invalid hash size: {option_value}", flush=True)
                
            elif option_name == "Book":
                self.use_opening_book = False
                if option_value.lower() in ("true", "1", "yes"):
                    try:
                        self.opening_book = load_opening_book()
                    except Exception as exc:
                        self.opening_book = None
                        self._uci_print(f"info string Opening book unavailable: {exc}; using search", flush=True)
                    else:
                        if self.opening_book is None:
                            self._uci_print("info string Opening book unavailable; using search", flush=True)
                        else:
                            self.opening_book.exhausted = False
                            self.use_opening_book = True
                            self._uci_print("info string Opening book enabled", flush=True)
                else:
                    self._uci_print("info string Opening book disabled", flush=True)
            elif option_name == "Ponder":
                self.ponder_enabled = option_value.lower() in ("true", "1", "yes")
                self._uci_print(f"info string Ponder {'enabled' if self.ponder_enabled else 'disabled'}", flush=True)
            elif option_name == "MaxDepth":
                try:
                    depth = int(option_value)
                    if 1 <= depth <= self.MAX_LMR_DEPTH:
                        self.max_depth_limit = depth
                        self._uci_print(f"info string Maximum search depth set to {depth}", flush=True)
                    else:
                        self._uci_print(f"info string Invalid max depth: {option_value} (must be 1-{self.MAX_LMR_DEPTH})", flush=True)
                except ValueError:
                    self._uci_print(f"info string Invalid max depth: {option_value}", flush=True)
            else:
                self._uci_print(f"info string Unknown option: {option_name}", flush=True)
        else:
            self._uci_print("info string Invalid setoption command", flush=True)
            if len(parts) >= 4:
                self._uci_print(f"info string Option name: {parts[1]}", flush=True)
                self._uci_print(f"info string Option value: {parts[3:]}", flush=True)
        self._uci_print("readyok", flush=True)

    def handle_isready(self) -> None:
        if DEBUG:
            self._uci_print("info debug: handling isready command", flush=True)
        self._uci_print("readyok", flush=True)
        if DEBUG:
            self._uci_print("info debug: readyok sent", flush=True)

    def handle_ucinewgame(self) -> None:
        if not self._stop_active_search():
            return
        if DEBUG:
            self._uci_print("info received ucinewgame command", flush=True)

        self.board.reset()

        if self.opening_book is not None:
            self.opening_book.exhausted = False

        self.clear_search_tables()
        self.searching = False
        self.stop_flag = False
        self.best_move_found = None
        if DEBUG:
            self._uci_print("info ucinewgame command processed", flush=True)

    def handle_position(self, params: list[str]) -> None:
        if not self._stop_active_search():
            return
        previous_root = self.board.root().fen(en_passant='fen')
        previous_moves = tuple(self.board.move_stack)
        if DEBUG:
            self._uci_print(f"info debug: handling position command with params: {params}", flush=True)
        if len(params) < 1:
            self._uci_print("info Invalid position command", flush=True)
            return

        if params[0] == "startpos":
            self.board.reset()
            if DEBUG:
                self._uci_print("info position set to startpos", flush=True)
            # Handle moves if they follow startpos
            if len(params) > 1 and params[1] == "moves":
                moves_list = params[2:]
                if DEBUG:
                    self._uci_print(f"info applying moves: {moves_list}", flush=True)
                for move_uci in moves_list:
                    try:
                        move = self.board.parse_uci(move_uci)
                        self.board.push(move)
                    except ValueError:
                        self._uci_print(f"info Invalid move in position command: {move_uci}", flush=True)
                        break # Stop applying moves on error

        elif params[0] == "fen":
            if DEBUG:
                self._uci_print("info received fen command", flush=True)
            try:
                moves_index = params.index("moves")
                fen_string = ' '.join(params[1:moves_index])
                moves_list = params[moves_index + 1:]
            except ValueError:
                fen_string = ' '.join(params[1:])
                moves_list = []

            try:
                self.board.set_fen(fen_string)
                if DEBUG:
                    self._uci_print(f"info position set by fen: {fen_string}", flush=True)
                if moves_list:
                    if DEBUG:
                        self._uci_print(f"info applying moves after fen: {moves_list}", flush=True)
                    for move_uci in moves_list:
                        try:
                            move = self.board.parse_uci(move_uci)
                            self.board.push(move)
                        except ValueError:
                            self._uci_print(f"info Invalid move after fen: {move_uci}", flush=True)
                            break # Stop applying moves on error

            except ValueError:
                self._uci_print(f"info Invalid fen string: {fen_string}", flush=True)
        else:
            self._uci_print(f"info Unknown position parameter: {params[0]}", flush=True)
        if (self.opening_book is not None and (
                self.board.root().fen(en_passant='fen') != previous_root
                or tuple(self.board.move_stack[:len(previous_moves)]) != previous_moves)):
            self.opening_book.exhausted = False
        if DEBUG:
            self._uci_print("info debug: position command processed", flush=True)

    def handle_go(self, params: list[str]) -> None:
        if DEBUG:
            self._uci_print(f"info debug: handling go command with params: {params}", flush=True)
        # Parse UCI parameters
        param_dict = {}
        i = 0
        while i < len(params):
            try:
                if params[i] in ['wtime', 'btime', 'winc', 'binc', 'movestogo', 'depth', 'movetime']:
                    param_dict[params[i]] = int(params[i + 1])
                    i += 2
                elif params[i] == 'infinite':
                    param_dict['infinite'] = True
                    i += 1
                elif params[i] == 'ponder':
                    param_dict['ponder'] = True
                    i += 1
                else:
                    i += 1  # skip unknown parameters
            except (IndexError, ValueError):
                self._uci_print(f"info go command: Invalid parameter {params[i] if i < len(params) else 'at end'}", flush=True)
                i += 1

        # Determine search mode
        time_limit: float | None = None
        depth_limit: int | None = None
        infinite_search = False
        ponder_search = bool(param_dict.get('ponder', False))

        if 'depth' in param_dict:
            depth_limit = param_dict['depth']
            if DEBUG:
                self._uci_print(f"info go command: depth_limit set to {depth_limit}", flush=True)

        if 'movetime' in param_dict:
            time_limit = param_dict['movetime'] / 1000.0  # convert ms to seconds
            if DEBUG:
                self._uci_print(f"info go command: time_limit set to {time_limit} seconds", flush=True)
        elif 'infinite' in param_dict and depth_limit is None:
            infinite_search = True
            if DEBUG:
                self._uci_print("info go command: infinite search enabled", flush=True)
        elif depth_limit is None or any(
            key in param_dict for key in ('wtime', 'btime', 'winc', 'binc', 'movestogo')
        ):
            # Use time manager for UCI time controls
            self.time_manager.set_time_controls(
                param_dict.get('wtime'),
                param_dict.get('btime'),
                param_dict.get('winc'),
                param_dict.get('binc'),
                param_dict.get('movestogo')
            )
            phase = self.get_game_phase()
            num_moves = len(list(self.board.legal_moves))
            time_limit = self.time_manager.get_time_limit(self.board.turn, phase, num_moves)
            if time_limit is not None:
                if DEBUG:
                    self._uci_print(f"info go command: calculated time_limit {time_limit:.3f} seconds (phase: {phase}, moves: {num_moves})", flush=True)
            else:
                if DEBUG:
                    self._uci_print("info go command: no time controls specified, defaulting to infinite search", flush=True)
                infinite_search = depth_limit is None

        if not self._stop_active_search():
            return

        self.best_move_found = None
        # FEN alone loses repetitions. Serialize before admission so neither
        # the mutable board nor its mutable Move objects cross thread ownership.
        self._search_worker.submit(
            self.board.root().fen(en_passant='fen'), time_limit, depth_limit, infinite_search,
            moves=tuple(move.uci() for move in self.board.move_stack),
            ponder=ponder_search)

    def _run_search_and_report_bestmove(
        self, time_limit: float | None, depth_limit: int | None,
        infinite_search: bool, board_fen: str, moves: tuple[str, ...] = ()
    ) -> None:
        """Prepare a legal result before the one terminal write for this task."""
        root = _board_from_fen_moves(board_fen, moves)
        best_move = None
        task = self._active_task
        if (not infinite_search and not (task is not None and task.ponder)
                and self.use_opening_book and self.opening_book is not None
                and not self.opening_book.exhausted):
            book_move = None
            try:
                book_move = self.opening_book.get_move(root, temperature=1)
            except Exception as exc:
                self._uci_print(f"info string book lookup failed: {exc}; using search", flush=True)
            if book_move is not None:
                if isinstance(book_move, chess.Move) and root.is_legal(book_move):
                    best_move = book_move
                    self._uci_print(f"info string book move {book_move.uci()}", flush=True)
                else:
                    self._uci_print("info string illegal book move; using search", flush=True)
        if best_move is None:
            try:
                best_move, _ = self.execute_search_task(
                    time_limit, depth_limit, infinite_search, board_fen, moves)
            except OSError:
                # I/O/pipe failures are not recoverable in this process.
                raise
            except Exception as exc:
                self._uci_print(f"info string search error: {exc}", flush=True)
        if best_move is not None and (
            not isinstance(best_move, chess.Move) or best_move not in root.legal_moves
        ):
            self._uci_print("info string illegal bestmove; using legal fallback", flush=True)
            best_move = None
        if best_move is None:
            best_move = next(iter(root.legal_moves), None)
        line = "bestmove 0000" if best_move is None else f"bestmove {best_move.uci()}"
        if best_move is not None:
            try:
                pv = self.reconstruct_pv(root, best_move, max_len=2)
                after = root.copy()
                after.push(best_move)
                if len(pv) >= 2 and pv[1] in after.legal_moves:
                    line += f" ponder {pv[1].uci()}"
            except Exception as exc:
                self._uci_print(f"info string ponder unavailable: {exc}", flush=True)
        # An output failure is fatal to the controller; never attempt a second result.
        if task is not None:
            task.wait_for_release()
        self._uci_print(line, flush=True)
        self.best_move_found = best_move

    def precompute_lmr_table(self, max_depth: int, max_moves: int, base_reduction: int, move_index_multiplier: int) -> list[list[int]]:
        """
        Precomputes the Late Move Reduction (LMR) table based on depth and move index.
        Uses the formula: int(log(depth) * log(move_index + 1) / 1.7)
        """
        
        table = [[0 for _ in range(max_moves)] for _ in range(max_depth + 1)]
        
        for depth in range(1, max_depth + 1):
            for move_index in range(max_moves):
                # Calculate reduction using the previous version's formula
                reduction = int(math.log(depth) * math.log(move_index + 1) / 1.7)
                
                # Clamp the reduction to avoid negative or excessive values
                # Using depth-2 to be consistent with the previous version
                reduction = min(reduction, depth - 2) if depth > 2 else 0
                reduction = max(0, reduction)  # Ensure non-negative
                
                table[depth][move_index] = reduction
                
        return table

    def precompute_history_factors(self, max_depth: int = 15) -> dict[str, list[int]]:
        """
        Precomputes history heuristic factor values for different depths and factor types.
        Returns a dictionary structure for better readability and maintainability.
        
        Args:
            max_depth: Maximum depth to precompute factors for (default 15)
            
        Returns:
            Dictionary with keys 'beta_cutoff', 'alpha_improve', 'regular_update'
            Each value contains factors for depths 0 through max_depth inclusive.
        """
        factors = {
            'beta_cutoff': [0] * (max_depth + 1),
            'alpha_improve': [0] * (max_depth + 1),
            'regular_update': [0] * (max_depth + 1)
        }
        
        for depth in range(max_depth + 1):
            # Beta cutoff formula
            factors['beta_cutoff'][depth] = depth**3 + depth**2 + 10
            
            # Alpha improvement formula
            factors['alpha_improve'][depth] = depth**2 + depth * 3
            
            # Regular updates formula
            factors['regular_update'][depth] = depth * (depth + 1) // 2
            
        return factors
    
    def get_game_phase(self) -> str:
        """Determine the current game phase using the Cython extension."""
        return get_game_phase_cython(self.board)

    def reconstruct_pv(self, board: chess.Board, first_move: chess.Move, max_len: int = 64) -> List[chess.Move]:
        """
        Reconstruct the Principal Variation (PV) from the transposition table.

        Args:
            board: The current board position
            first_move: The best move found at the root
            max_len: Maximum length of PV to reconstruct

        Returns:
            List of moves representing the PV
        """
        pv = []
        temp_board = board.copy()

        if first_move and temp_board.is_legal(first_move):
            pv.append(first_move)
            temp_board.push(first_move)

            while len(pv) < max_len:
                tt_move, _ = probe_tt_move(temp_board)
                if not tt_move or not temp_board.is_legal(tt_move):
                    break
                pv.append(tt_move)
                temp_board.push(tt_move)

        return pv

    def start_search(self, time_limit: float | None, depth_limit: int | None, infinite_search: bool) -> None:
        # Direct Python searches need the same clock domain as recursive checks.
        self._stop_check_counter = 0
        self._stop_check_threshold = 1
        self._search_start_time = time.monotonic()
        self._search_time_limit = time_limit
        self._last_time_check_time = self._search_start_time

        if (not infinite_search and self.opening_book is not None
                and not self.opening_book.exhausted and self.use_opening_book):
            if book_move := self.opening_book.get_move(self.board, temperature = 1):
                self._uci_print(f"info string book move {book_move.uci()}", flush=True)
                self._uci_print(f"bestmove {book_move.uci()}", flush=True)
                return
        
        current_depth = 1
        # Use depth_limit from go command, or a default max if infinite
        max_depth = depth_limit if depth_limit is not None else self.max_depth_limit if self.max_depth_limit is not None else self.MAX_LMR_DEPTH
        if infinite_search:
            max_depth = self.MAX_LMR_DEPTH
        max_depth = min(max_depth, self.MAX_LMR_DEPTH)

        self.history_table.decay()
        
        if DEBUG:
            self._uci_print(f"info search started, max_depth: {max_depth}, time_limit: {time_limit}, infinite: {infinite_search}", flush=True)
            self._uci_print(f"info debug: board position: {self.board.fen()}", flush=True)

        # Reset per-search counters (removed for performance)
        best_move_so_far = None # Keep track of the best move from the *last completed* iteration
        best_value_so_far = -MATE_SCORE # Keep track of the value for info command
        
        # Reset killer moves 
        self.killer_moves = [[None, None] for _ in range(self.MAX_LMR_DEPTH)]
        root_context = tt_context_from_board(self.board)


        # Iterative Deepening
        while current_depth <= max_depth and not self.stop_flag:
            #self._uci_print(f"info starting search at depth {current_depth}", flush=True)
            
            # Aspiration windows: Use narrow window around previous best value for depth >= 3
            if current_depth >= 3 and best_value_so_far != -MATE_SCORE:
                delta = 50  # ±50 centipawns aspiration window
                alpha = best_value_so_far - delta
                beta = best_value_so_far + delta
            else:
                alpha = -MATE_SCORE
                beta = MATE_SCORE

            # Create a single board copy for this iteration
            root_board = self.board.copy()

            # Call the PVS search function with aspiration window
            value, move = self.pvs_search(
                root_board,
                current_depth,
                alpha,
                beta,
                tt_context=root_context,
             )

            # Handle aspiration window failures
            if value is not None and move is not None:
                if value <= alpha or value >= beta:
                    if DEBUG:
                        self._uci_print(f"info string debug: aspiration window fail at depth {current_depth}, re-searching", flush=True)
                    value, move = self.pvs_search(
                        self.board,
                        current_depth,
                        -MATE_SCORE,
                        MATE_SCORE,
                        tt_context=root_context,
                    )

            # Check if the search was stopped by time/stop_flag
            if value is None or move is None: # ist it or or AND time break usually returns NONE, None
                break 
           
            best_move_so_far = move
            best_value_so_far = value

            # Extract PV and score for the info command
            # This requires re-searching the best line from the TT or storing the full PV.
            # For simplicity here, we will re-construct a basic PV from the best move found
            # at this depth using the TT if available. A full PV requires deeper logic.
            # Also, the score reported should typically be the value found at this depth.

            # Reconstruct PV using the new helper method
            pv_moves = self.reconstruct_pv(self.board, best_move_so_far, max_len=current_depth)
            pv_string = " ".join(move.uci() for move in pv_moves)

            # Per-depth metrics accumulation removed for performance

            # The score is the value found by the search at this depth

            # Format score for UCI (cp for centipawns, mate for checkmate)
            score_type = "cp"
            score_value = value
            
            # Check for mate score (using a large absolute value threshold)
            if abs(value) > MATE_SCORE_THRESHOLD:  # Assuming a large value indicates checkmate
                score_type = "mate"
                # Calculate plies to mate from root perspective
                # At leaf nodes, mate score is MATE_SCORE - plies_to_mate_from_leaf
                # At root, we need to adjust by current ply to get distance from root
                if value > 0:  # Winning mate
                    plies_to_mate = (MATE_SCORE - value - self.board.ply())
                    moves_to_mate = (plies_to_mate + 1) // 2  # Round up for positive
                else:  # Losing mate
                    plies_to_mate = (MATE_SCORE + value - self.board.ply())
                    moves_to_mate = -(plies_to_mate // 2)  # Round down for negative
                score_value = moves_to_mate
            #else:
            #    # Convert to centipawns for non-mate scores
            #    score_value = int(value * 100)

            # Send concise info line (minimize I/O overhead)
            total_depth_time = int((time.monotonic() - self._search_start_time) * 1000)
            self._uci_print(f"info depth {current_depth} score {score_type} {score_value} time {total_depth_time} pv {pv_string}", flush=True)

            if self.stop_flag:
                break

            current_depth += 1
            #self._uci_print(f"info current_depth {current_depth}")


        self.searching = False

        # Report the best move found from the *last completed* iteration
        # Use best_move_so_far which stores the result from the *last successful* depth
        if DEBUG:
            self._uci_print("info debug: search finished, reporting bestmove", flush=True)

        if best_move_so_far:
            # Reconstruct PV to get ponder move if available
            pv_moves = self.reconstruct_pv(self.board, best_move_so_far, max_len=2)
            if len(pv_moves) >= 2:
                ponder_move = pv_moves[1]
                self._uci_print(f"bestmove {best_move_so_far.uci()} ponder {ponder_move.uci()}", flush=True)
            else:
                self._uci_print(f"bestmove {best_move_so_far.uci()}", flush=True)
        else:
            first_move = next(cython_chess.generate_legal_moves(self.board, chess.BB_ALL, chess.BB_ALL), None)
            if first_move:
                self._uci_print(f"bestmove {first_move.uci()}", flush=True)
            else:
                self._uci_print("bestmove 0000", flush=True)

        if DEBUG:
            self._uci_print("info debug: bestmove sent", flush=True)

    def _prepare_pvs_moves(
        self, board: chess.Board, ply: int, tt_move: chess.Move | None,
        snapshot: _OrderingSnapshot | None = None,
    ) -> tuple[bool, list[tuple[int, int, bool, chess.Move]]]:
        """Exhaust the native iterator before returning moves for recursion."""
        moves_generator: Iterator[chess.Move] | None = None
        killers: Sequence[chess.Move | None] = ()
        history: Sequence[Sequence[int]] = ()
        decorated_moves: list[tuple[int, int, bool, chess.Move]] = []
        try:
            is_in_check, moves_generator = cython_chess.generate_search_moves(
                board, chess.BB_ALL, chess.BB_ALL
            )
            killers = snapshot.killers if snapshot is not None else self.killer_moves[ply]
            history = (
                snapshot.history if snapshot is not None
                else self.history_table._plane(board.turn)
            )
            for move in moves_generator:
                if snapshot is not None and move == tt_move:
                    continue  # The validated TT move has already been searched.
                score = 0
                is_capture = board.is_capture(move)
                if move == tt_move:
                    score = 100000
                elif is_capture:
                    attacker_piece_type = board.piece_type_at(move.from_square)
                    captured_piece_type = board.piece_type_at(move.to_square)
                    if captured_piece_type is None:  # En-passant
                        captured_piece_type = chess.PAWN
                    score = 10000 + (self.PIECE_VALUES.get(captured_piece_type, 0) * 10 - self.PIECE_VALUES.get(attacker_piece_type, 0))
                elif move in killers:
                    score = 6000 if move == killers[0] else 5000
                hist = history[move.from_square][move.to_square]
                decorated_moves.append((-score, -hist, is_capture, move))
            decorated_moves.sort(key=lambda item: (item[0], item[1]))
            return is_in_check, decorated_moves
        finally:
            # A worker may retain the exception and this helper traceback.
            moves_generator = None
            decorated_moves = []
            killers = (None, None)
            snapshot = None
            history = ()

    def _ordering_snapshot(
        self,
        board: chess.Board,
        killers: tuple[chess.Move | None, chess.Move | None],
    ) -> _OrderingSnapshot:
        """Freeze node-entry ordering values before the first child search."""

        turn = board.turn
        return _OrderingSnapshot(
            self.history_table.snapshot(turn, board.occupied_co[turn]),
            killers,
        )

    def _generate_pvs_captures(
        self,
        board: chess.Board,
        tt_move: chess.Move | None,
    ) -> tuple[bool, list[chess.Move]]:
        """Materialize the exact legal-capture subsequence for one node."""

        moves_generator: Iterator[chess.Move] | None = None
        ep_generator: Iterator[chess.Move] | None = None
        moves: list[chess.Move] = []
        captures: list[chess.Move] = []
        seen: set[chess.Move] = set()
        try:
            is_in_check, moves_generator = cython_chess.generate_search_moves(
                board, chess.BB_ALL, board.occupied_co[not board.turn]
            )
            moves = list(moves_generator)
            moves_generator = None
            ep_generator = cython_chess.generate_legal_ep(
                board, chess.BB_ALL, chess.BB_ALL
            )
            moves.extend(ep_generator)
            ep_generator = None

            for move in moves:
                if move == tt_move:
                    continue
                if move in seen:
                    raise RuntimeError("capture stage produced a duplicate move")
                seen.add(move)
                if not board.is_capture(move):
                    # Treat the mask as a filter boundary, not as permission
                    # to admit a non-capture into this ordering stage.
                    continue
                captures.append(move)
            return is_in_check, captures
        finally:
            moves_generator = None
            ep_generator = None
            moves = []
            captures = []
            seen = set()

    def _prepare_pvs_captures(
        self,
        board: chess.Board,
        captures: Sequence[chess.Move],
        snapshot: _OrderingSnapshot | None,
    ) -> list[tuple[int, int, bool, chess.Move]]:
        """Decorate and stably order an already materialized capture stage."""

        history: Sequence[Sequence[int]] = ()
        decorated_moves: list[tuple[int, int, bool, chess.Move]] = []
        try:
            if snapshot is None:
                raise ValueError("capture decoration requires node-entry ordering")
            history = snapshot.history
            for move in captures:
                attacker_piece_type = board.piece_type_at(move.from_square)
                captured_piece_type = board.piece_type_at(move.to_square)
                if captured_piece_type is None:  # En-passant
                    captured_piece_type = chess.PAWN
                score = 10000 + (
                    self.PIECE_VALUES.get(captured_piece_type, 0) * 10
                    - self.PIECE_VALUES.get(attacker_piece_type, 0)
                )
                hist = history[move.from_square][move.to_square]
                decorated_moves.append((-score, -hist, True, move))
            decorated_moves.sort(key=lambda item: (item[0], item[1]))
            return decorated_moves
        finally:
            captures = ()
            decorated_moves = []
            snapshot = None
            history = ()

    def _generate_pvs_killers(
        self,
        board: chess.Board,
        killers: tuple[chess.Move | None, chess.Move | None],
        seen: set[chess.Move],
    ) -> list[tuple[int, chess.Move]]:
        """Return legal quiet node-entry killers not searched by earlier stages."""

        candidates: list[tuple[int, chess.Move]] = []
        emitted: set[chess.Move] = set()
        try:
            emitted = set(seen)
            for slot, move in enumerate(killers):
                if move is None or move in emitted or not board.is_legal(move):
                    continue
                if board.is_capture(move):
                    raise RuntimeError("capture stage omitted a legal killer capture")
                candidates.append((6000 if slot == 0 else 5000, move))
                emitted.add(move)
            return candidates
        finally:
            candidates = []
            emitted = set()
            killers = (None, None)
            seen = set()

    def _prepare_pvs_killers(
        self,
        candidates: Sequence[tuple[int, chess.Move]],
        snapshot: _OrderingSnapshot | None,
    ) -> list[tuple[int, int, bool, chess.Move]]:
        """Decorate frozen legal quiet killers with node-entry history."""

        history: Sequence[Sequence[int]] = ()
        decorated_moves: list[tuple[int, int, bool, chess.Move]] = []
        try:
            if snapshot is None:
                raise ValueError("killer decoration requires node-entry ordering")
            history = snapshot.history
            for score, move in candidates:
                hist = history[move.from_square][move.to_square]
                decorated_moves.append((-score, -hist, False, move))
            return decorated_moves
        finally:
            candidates = ()
            decorated_moves = []
            snapshot = None
            history = ()

    def _prepare_pvs_other(
        self,
        board: chess.Board,
        killers: tuple[chess.Move | None, chess.Move | None],
        snapshot: _OrderingSnapshot | None,
        seen: set[chess.Move],
    ) -> tuple[bool, list[tuple[int, int, bool, chess.Move]]]:
        """Prepare remaining legal non-captures from the unchanged full generator."""

        moves_generator: Iterator[chess.Move] | None = None
        decorated_moves: list[tuple[int, int, bool, chess.Move]] = []
        history: Sequence[Sequence[int]] = (
            snapshot.history
            if snapshot is not None
            else self.history_table._plane(board.turn)
        )
        try:
            is_in_check, moves_generator = cython_chess.generate_search_moves(
                board, chess.BB_ALL, chess.BB_ALL
            )
            for move in moves_generator:
                if move in seen:
                    continue
                if board.is_capture(move):
                    raise RuntimeError("capture stage omitted a legal capture")
                if move in killers:
                    raise RuntimeError("killer stage omitted a legal quiet killer")
                hist = history[move.from_square][move.to_square]
                decorated_moves.append((0, -hist, False, move))
            moves_generator = None
            decorated_moves.sort(key=lambda item: item[1])
            return is_in_check, decorated_moves
        finally:
            moves_generator = None
            decorated_moves = []
            killers = (None, None)
            snapshot = None
            history = ()
            seen = set()

    def pvs_search(self, board: chess.Board, depth: int, alpha: int, beta: int,
                   lmr_depth: int = 3, ply: int = 0, *,
                   tt_context: tuple[int, int] | None = None
                   ) -> tuple[int | None, chess.Move | None]:
        """
        Principal Variation Search (PVS) with time and stop flag checks and
        LMR (Late Move Reductions).

        Args:
            board: The current chess.Board object (should be a copy for recursive calls).
            depth: The remaining search depth.
            alpha: The alpha bound.
            beta: The beta bound.
            lmr_depth: The depth below which LMR can be applied (e.g., don't reduce moves at very low depths).
        Returns:
            A tuple (value, best_move_at_this_node).
            Returns (None, None) if the search was stopped by time or stop flag.
        """
        original_alpha = alpha
        tt_entry = None
        if self.stop_flag_check():
            return None, None # Signal to stop

        # Base case 
        if depth <= 0:
            # Time check before quiescence search (or evaluation)
            value = self.quiescence_search(board, alpha, beta)
            if value is None: # Propagate stop signal from quiescence search
                 return None, None
            return value, None

        if board.is_repetition(3) or board.is_fifty_moves() or board.is_insufficient_material(): # Draw condition
            return 0, None

        if tt_context is None:
            tt_context = tt_context_from_board(board)
        
        # Check transposition table
        if not board.is_repetition(2): # next node may be 3-fold repetition so we do not return on tt_entry
            tt_entry = probe_tt_entry(board, depth, tt_context=tt_context)

            if tt_entry:
                if tt_entry.node_type == 'EXACT':
                    return tt_entry.value, tt_entry.best_move
                elif tt_entry.node_type == 'LOWERBOUND':
                    alpha = max(alpha, tt_entry.value)
                elif tt_entry.node_type == 'UPPERBOUND':
                    beta = min(beta, tt_entry.value)
                if alpha >= beta:
                    return tt_entry.value, tt_entry.best_move

        tt_move, _ = probe_tt_move(board, board_hash=tt_context[0])
        snapshot = None
        decorated_moves: list[tuple[int, int, bool, chess.Move]] = []
        captures: list[chess.Move] = []
        killer_candidates: list[tuple[int, chess.Move]] = []
        seen: set[chess.Move] = set()
        entry_killers = (
            self.killer_moves[ply][0],
            self.killer_moves[ply][1],
        )
        try:
            if tt_move is not None and board.is_legal(tt_move):
                snapshot = self._ordering_snapshot(board, entry_killers)
                decorated_moves = [(-100000, -snapshot.history[tt_move.from_square][tt_move.to_square],
                                    board.is_capture(tt_move), tt_move)]
                seen.add(tt_move)
                is_in_check = False
            else:
                tt_move = None
                is_in_check = False

            history_turn = board.turn
            best_value = -MATE_SCORE
            best_move_for_node = None
            first_move = True
            next_stage: Literal['full', 'captures', 'killers', 'other', 'done'] = (
                'captures' if board.knights | board.bishops | board.rooks | board.queens
                else 'full'
            )

            # recursive search
            i = -1
            stage_index = 0
            while True:
                if stage_index == len(decorated_moves):
                    decorated_moves = []
                    stage_index = 0
                    if next_stage == 'done':
                        break
                    if self.stop_flag_check():
                        return None, None
                    if next_stage == 'full':
                        is_in_check, decorated_moves = self._prepare_pvs_moves(
                            board, ply, tt_move, snapshot
                        )
                        snapshot = None
                        next_stage = 'done'
                    elif next_stage == 'captures':
                        capture_check, captures = self._generate_pvs_captures(
                            board, tt_move
                        )
                        is_in_check = capture_check
                        if captures:
                            if snapshot is None:
                                snapshot = self._ordering_snapshot(
                                    board, entry_killers
                                )
                            decorated_moves = self._prepare_pvs_captures(
                                board, captures, snapshot
                            )
                        seen.update(item[3] for item in decorated_moves)
                        next_stage = 'killers'
                    elif next_stage == 'killers':
                        killer_candidates = self._generate_pvs_killers(
                            board, entry_killers, seen
                        )
                        if killer_candidates:
                            if snapshot is None:
                                snapshot = self._ordering_snapshot(
                                    board, entry_killers
                                )
                            decorated_moves = self._prepare_pvs_killers(
                                killer_candidates, snapshot
                            )
                        seen.update(item[3] for item in decorated_moves)
                        next_stage = 'other'
                    else:
                        other_check, decorated_moves = self._prepare_pvs_other(
                            board, entry_killers, snapshot, seen
                        )
                        if other_check != is_in_check:
                            raise RuntimeError("staged generators disagree on check status")
                        snapshot = None
                        next_stage = 'done'
                    if self.stop_flag_check():
                        return None, None
                    if not decorated_moves:
                        continue
                i += 1
                _, _, is_capture, move = decorated_moves[stage_index]
                stage_index += 1
                # Check time/stop flag before pushing the move
                if self.stop_flag_check():
                     return None, None

                reduction = 0
                zeroing = depth > 1 and board.is_zeroing(move)

                if first_move:
                    # Principal Variation Search - first move is searched with a full window
                    board.push(move)
                    try:
                        child_context = _child_tt_context(
                            board, tt_context, zeroing, depth - 1)
                        child_value, _ = self.pvs_search(
                            board, depth - 1, -beta, -alpha, lmr_depth, ply + 1,
                            tt_context=child_context)
                    finally:
                        board.pop()

                    if child_value is None:
                       return None, None

                    value = -child_value
                    first_move = False

                else:
                    # Zero Window Search (Null Window Search) for subsequent moves, potentially with reduction

                    # Late Move Reduction (LMR)
                    # Do not reduce moves if we are in check, if the move is a capture,
                    # or if the move gives check.
                    if depth > lmr_depth and not is_in_check and not is_capture and not move.promotion:
                        reduction = self.lmr_reduction_table[depth][i]
                    reduced_depth = max(0, (depth - 1) - reduction)
                    board.push(move)
                    try:
                        child_context = _child_tt_context(
                            board, tt_context, zeroing, reduced_depth)
                        child_value, _ = self.pvs_search(
                            board, reduced_depth, -alpha - 1, -alpha, lmr_depth,
                            ply + 1, tt_context=child_context)
                    finally:
                        board.pop()

                    if child_value is None:
                        return None, None

                    value = -child_value

                    if alpha < value and (reduction or beta > alpha + 1):
                        board.push(move)
                        try:
                            if child_context is None:
                                child_context = _child_tt_context(
                                    board, tt_context, zeroing, depth - 1)
                            child_value, _ = self.pvs_search(
                                board, depth - 1, -beta, -alpha, lmr_depth,
                                ply + 1, tt_context=child_context)
                        finally:
                            board.pop()

                        if child_value is None:
                            return None, None

                        value = -child_value

                # Alpha-Beta Pruning
                if value >= beta: # Beta cutoff

                    if not is_capture:
                        # Set killer move for quiet moves
                        if self.killer_moves[ply][0] != move:
                            self.killer_moves[ply][0], self.killer_moves[ply][1] = move, self.killer_moves[ply][0]
                        # Update history heuristic table
                        self.history_table.add(
                            history_turn, move.from_square, move.to_square,
                            self.history_factors['beta_cutoff'][depth])

                    # Beta cutoff (fail high) - indicates value is a lower bound
                    node_type_str: Literal['LOWERBOUND', 'UPPERBOUND', 'EXACT'] = "LOWERBOUND"

                    # Store the entry using the Python wrapper
                    store_tt_entry(
                        board, depth, value, node_type_str, move,
                        tt_context=tt_context)

                    return value, move # Return beta for cutoff

                # Update best value and best move for this node
                if value > best_value:
                    best_value = value
                    best_move_for_node = move
                    best_value_update = True # Control history score for best value update

                    # Update alpha. No need to check alpha if best_value failed already.
                    if value > alpha:
                        alpha = value
                        best_move_for_node = move # Update best move if alpha improved
                        if not is_capture:
                            self.history_table.add(
                                history_turn, best_move_for_node.from_square,
                                best_move_for_node.to_square,
                                self.history_factors['alpha_improve'][depth])
                            best_value_update = False

                    # Update history if value is the new best value and not new alpha
                    if best_value_update: # Implicit history update for best_value < value < alpha
                        if not is_capture:
                            self.history_table.add(
                                history_turn, move.from_square, move.to_square,
                                self.history_factors['regular_update'][depth])

            if first_move:
                if is_in_check:
                    return -MATE_SCORE + board.ply(), None
                return 0, None

            node_type_str = 'UPPERBOUND'
            if best_value > original_alpha:
                node_type_str = 'EXACT'

            store_tt_entry(
                board, depth, best_value, node_type_str, best_move_for_node,
                tt_context=tt_context)

            return best_value, best_move_for_node
        finally:
            # Retained worker exceptions must not keep node snapshots alive.
            snapshot = None
            decorated_moves = []
            captures = []
            killer_candidates = []
            seen.clear()
            entry_killers = (None, None)

    def quiescence_search(self, board: chess.Board, alpha: int, beta: int,
                          max_q_depth: int = 17) -> int | None:
        """
        Performs a quiescence search to evaluate positions after all tactical
        moves (captures, promotions) have been resolved, preventing the horizon effect.
        """
        if self.stop_flag_check():
            #if DEBUG:
            #    self._uci_print(f"info quiescence_search stopped by stop_flag_check at {time.time()}")
            return None # Signal stop

        rule_draw = (
            board.halfmove_clock >= 100
            or board.is_insufficient_material()
            or board.is_repetition(3)
        )
        if rule_draw:
            in_check, evasions = cython_chess.generate_qsearch_moves(
                board,
                chess.BB_ALL,
                chess.BB_ALL,
                search_captures=False,
            )
            if in_check and next(evasions or iter(()), None) is None:
                return -MATE_SCORE + board.ply()
            return 0

        stand_pat = evaluate_board_static(board, board.turn)

        search_captures = max_q_depth > 0 and stand_pat < beta
        in_check, has_legal_move, noisy_moves = cython_chess.generate_qsearch_moves_with_status(
            board,
            chess.BB_ALL,
            chess.BB_ALL,
            search_captures=search_captures,
        )

        # Decorate-Sort-Undecorate for efficient MVV-LVA sorting
        scored_noisy_moves = []
        score: int | None
        for move in noisy_moves:
            # For en passant, the captured piece is a pawn
            captured_piece_type = board.piece_type_at(move.to_square)
            if captured_piece_type is None:
                # En passant is a capture on an empty target square. Other
                # empty targets are quiet evasions and have no victim value.
                captured_piece_type = chess.PAWN if board.is_en_passant(move) else None
            
            attacker_piece_type = board.piece_type_at(move.from_square)

            # MVV-LVA score: (Victim * 10) - Attacker
            victim_value = self.PIECE_VALUES.get(captured_piece_type, 0)
            score = victim_value * 10 - self.PIECE_VALUES[attacker_piece_type]
            scored_noisy_moves.append((score, move))

        if in_check:
            if not has_legal_move:
                return -MATE_SCORE + board.ply()
        else:
            if not has_legal_move:
                return 0

            # If the current position is better than beta, return stand_pat
            if stand_pat >= beta:
                return stand_pat

            if max_q_depth <= 0:
                return stand_pat

            # If the current position is better than alpha, update alpha
            if stand_pat > alpha:
                alpha = stand_pat

        # In check, the QSearch generator supplies every legal evasion.
        # Otherwise it supplies captures and quiet promotions.

        # Sort moves by their pre-calculated score, highest first
        scored_noisy_moves.sort(key=lambda item: item[0], reverse=True)

        # Process sorted moves
        for _, move in scored_noisy_moves:
            board.push(move)
            try:
                # Recursively call quiescence search
                score = self.quiescence_search(board, -beta, -alpha, max_q_depth - 1)
            finally:
                board.pop()
            if score is None:
                return None
            score = -score

            # Alpha-Beta Pruning
            if score >= beta:
                return score # Beta cutoff

            if score > alpha:
                alpha = score # Update alpha
        
        return alpha

    def clear_search_tables(self) -> None:
        """Clear all search-related tables between searches."""
        self.history_table.clear()
        if self.use_transposition_table:
            try:
                init_cython_tt(self.tt_size_bytes)
            except MemoryError as e:
                self._uci_print(f"Error initializing Cython TT: {e}", flush=True)
        
        self.killer_moves = [[None, None] for _ in range(self.MAX_LMR_DEPTH)] #reset killers             
        
        # Add other table clears here if needed (like killer moves, etc.)
    
    def stop_flag_check(self) -> bool:
        """
        Sophisticated time check function.
        Checks time only every N calls, where N depends on remaining time.
        Updates self.stop_flag if time is exceeded.
        """
        # Task cancellation is checked even between scheduled clock checks.
        if self._cancel_requested():
            return True

        task = getattr(self, '_active_task', None)
        if task is not None and task.ponder and not self._ponder_clock_armed:
            hit_at = task.hit_at
            if hit_at is not None:
                self._search_start_time = hit_at
                self._search_time_limit = task.time_limit
                self._last_time_check_time = hit_at
                self._stop_check_counter = 0
                self._stop_check_threshold = 1
                self._ponder_clock_armed = True

        self._stop_check_counter += 1

        if self._stop_check_counter < self._stop_check_threshold:
            return False

        # Perform the actual time check only when the counter reaches the threshold
        if self._stop_check_counter >= self._stop_check_threshold:
            self._stop_check_counter = 0 # Reset counter

            if self._search_time_limit is not None:
                elapsed_time = time.monotonic() - self._search_start_time
                remaining_time = self._search_time_limit - elapsed_time

                # Adjust threshold based on remaining time
                if remaining_time < 0.016: # Less than 16ms remaining
                      self._stop_check_threshold = 1 # Check every call
                elif remaining_time < 0.033: # Less than 33ms remaining
                      self._stop_check_threshold = 8
                elif remaining_time < 0.066: # Less than 66ms remaining
                      self._stop_check_threshold = 16
                elif remaining_time < 0.125: # Less than 125ms remaining
                      self._stop_check_threshold = 32
                elif remaining_time < 0.250: # Less than 250ms remaining
                      self._stop_check_threshold = 64
                elif remaining_time < 0.500: # Less than 500ms remaining
                      self._stop_check_threshold = 128
                elif remaining_time < 1.0: # Less than 1 second remaining
                      self._stop_check_threshold = 256
                elif remaining_time < 2: # Less than 2 seconds remaining
                      self._stop_check_threshold = 512 
                else:
                      self._stop_check_threshold = 1014 # more than 2 seconds remaining

                # Only perform the time check if a time limit is set
                if self._search_time_limit is not None:
                    elapsed_time = time.monotonic() - self._search_start_time
                    if elapsed_time >= self._search_time_limit:
                        self.stop_flag = True # Set the stop flag

        return self.stop_flag # Return the current state of the flag


    def _stop_search(self) -> None:
        self._search_worker.cancel_and_wait()

    def handle_stop(self) -> None:
        self._stop_search()
        self._uci_print("info string search stopped", flush=True)

    def handle_ponderhit(self) -> None:
        self._search_worker.ponderhit()

    def _shutdown(self) -> None:
        """Join the only worker before freeing native resources, exactly once."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._search_worker.shutdown()
        if self.use_transposition_table and not self._tt_freed:
            free_cython_tt()
            self._tt_freed = True
        atexit.unregister(self._shutdown)

    def handle_quit(self) -> None:
        self._shutdown()
        self._uci_print("info string search stopped", flush=True)
        # Perform any necessary cleanup
        self._uci_print("bye", flush=True)

if __name__ == "__main__":
    engine = UCIEngine()
    engine.run()
