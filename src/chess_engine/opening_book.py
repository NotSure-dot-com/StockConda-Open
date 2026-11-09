"""
Opening Book Module

This module provides functionality for managing and using chess opening books.
"""

import logging
import operator
import os
import pickle
import random
import threading
import time
from collections import defaultdict
from typing import (
    Any, DefaultDict, Dict, List, Optional, Set, 
    TextIO, Tuple, Union, cast, Sequence, Type, TypeVar
)

import chess
import chess.pgn
import numpy as np
import numpy.typing as npt

# Type aliases
MoveSequence = Tuple[chess.Move, ...]
MoveCounts = Dict[chess.Move, int]
MoveSequenceCounts = Dict[MoveSequence, int]
T = TypeVar('T', bound='OpeningBook')

# Use standard chess module exceptions
BadMoveError = chess.IllegalMoveError  # Alias for compatibility
HeadersError = ValueError  # Use ValueError for header-related issues

# Set up logging
logger = logging.getLogger(__name__)

# Custom exceptions for opening book operations
class OpeningBookError(Exception):
    """Base class for opening book related exceptions."""
    pass

class OpeningBookFileError(OpeningBookError):
    """Exception raised for file-related errors in opening book operations."""
    pass

class OpeningBookDataError(OpeningBookError):
    """Exception raised for data-related errors in opening book operations."""
    pass

class OpeningBookMoveError(OpeningBookError):
    """Exception raised for move-related errors in opening book operations."""
    pass

class OpeningBook:
    """
    A class to represent and manage a chess opening book.
    
    The opening book stores move sequences and their frequencies from PGN games,
    allowing for quick lookup of book moves during gameplay.
    """
    
    def __init__(self, move_sequences: Optional[Union[Dict[Tuple[chess.Move, ...], int], Dict[str, Any]]] = None, 
                 book_data: Optional[Dict[str, Any]] = None) -> None:
        """
        Initialize the opening book.
        
        Args:
            move_sequences: Optional dictionary mapping move sequences to their frequencies.
            book_data: Optional dictionary containing book data (legacy format support).
        """
        self.move_sequences: Dict[Tuple[chess.Move, ...], int] = {}
        self.first_moves: Dict[chess.Move, int] = {}
        self.exhausted = False
        self.stats: Dict[str, Any] = {}  # For storing statistics about the book
        
        # Handle different initialization patterns
        if book_data is not None and 'move_sequences' in book_data:
            # Handle the case where book_data is passed with move_sequences inside
            self._initialize_from_dict(book_data)
        elif isinstance(move_sequences, dict):
            if all(isinstance(k, tuple) for k in move_sequences.keys()):
                # Standard case: move_sequences is a dict of sequence->count
                self.move_sequences = cast(Dict[Tuple[chess.Move, ...], int], move_sequences)
                self._initialize_first_moves()
            elif 'move_sequences' in move_sequences:
                # Handle case where move_sequences is actually the book_data dict
                self._initialize_from_dict(cast(Dict[str, Any], move_sequences))
        
    def _initialize_from_dict(self, data: Dict[str, Any]) -> None:
        """
        Initialize the opening book from a dictionary.
        
        Args:
            data: Dictionary containing move sequences and their counts.
        
        Raises:
            OpeningBookDataError: If the data format is invalid.
        """
        if not isinstance(data, dict):
            raise OpeningBookDataError("Expected dictionary for opening book data")
            
        self.move_sequences = {}
        
        # Extract move sequences from the data
        move_sequences: Dict[Any, Any] = {}
        if 'move_sequences' in data and isinstance(data['move_sequences'], dict):
            move_sequences = data['move_sequences']
        elif any(isinstance(k, (tuple, list, str, chess.Move)) for k in data.keys() if k != 'move_sequences'):
            move_sequences = data
        else:
            raise OpeningBookDataError("Invalid opening book format: expected 'move_sequences' or move sequences directly")
            
        # Convert move sequences to the correct format
        self.move_sequences = {}
        for key, count in move_sequences.items():
            # Skip non-move sequence entries
            if key == 'move_sequences':
                continue
                
            # Validate count
            if not isinstance(count, (int, float)) or count <= 0:
                logger.warning(f"Skipping invalid count for move sequence: {key}")
                continue

            try:
                if isinstance(key, tuple):
                    # Already in the correct format
                    self.move_sequences[key] = int(count)
                    
                    # Update first moves for quick lookup
                    if key:  # If the tuple is not empty
                        first_move = key[0]
                        self.first_moves[first_move] = self.first_moves.get(first_move, 0) + int(count)
                        
                elif isinstance(key, str):
                    # Convert string representation to tuple of moves
                    move_strs = key.strip("()").split(",")
                    moves = tuple(chess.Move.from_uci(move_str.strip()) for move_str in move_strs if move_str.strip())
                    self.move_sequences[moves] = int(count)
                    
                    # Update first moves for quick lookup
                    if moves:  # If the tuple is not empty
                        first_move = moves[0]
                        self.first_moves[first_move] = self.first_moves.get(first_move, 0) + int(count)
                        
                elif isinstance(key, (list, tuple)):
                    # Convert list of moves to tuple
                    moves_list = []
                    for move in key:
                        if isinstance(move, chess.Move):
                            moves_list.append(move)
                        elif isinstance(move, str):
                            moves_list.append(chess.Move.from_uci(move))
                        elif hasattr(move, 'uci'):
                            moves_list.append(chess.Move.from_uci(move.uci()))
                        else:
                            raise ValueError(f"Invalid move in sequence: {move}")
                    
                    moves_tuple = tuple(moves_list)
                    self.move_sequences[moves_tuple] = int(count)
                    
                    # Update first moves for quick lookup
                    if moves_tuple:  # If the tuple is not empty
                        first_move = moves_tuple[0]
                        self.first_moves[first_move] = self.first_moves.get(first_move, 0) + int(count)
                        
                else:
                    raise ValueError(f"Invalid move sequence format: {key}")
                    
            except (ValueError, AttributeError) as e:
                logger.warning(f"Skipping invalid move sequence: {key} - {str(e)}")
                continue

        # Log summary
        total_sequences = len(self.move_sequences)
        total_first_moves = len(self.first_moves)
        logger.info(f"Loaded {total_sequences} move sequences with {total_first_moves} first moves")
    
    def _initialize_first_moves(self) -> None:
        """
        Initialize the first_moves dictionary from move_sequences.
        This is an optimization to quickly look up first moves.
        
        Raises:
            OpeningBookDataError: If there's an error processing the move sequences.
        """
        self.first_moves = {}
        
        # If no move sequences, nothing to do
        if not hasattr(self, 'move_sequences') or not self.move_sequences:
            logger.debug("No move sequences found when initializing first moves")
            return
            
        # Create a temporary board to check move legality
        temp_board = chess.Board()
        processed_moves = 0
        skipped_moves = 0
        
        for sequence, count in self.move_sequences.items():
            # Skip empty sequences
            if not sequence:
                skipped_moves += 1
                continue
                
            try:
                # Get the first move
                first_move = sequence[0]
                
                # Ensure it's a chess.Move object
                if not isinstance(first_move, chess.Move):
                    if isinstance(first_move, str):
                        first_move = chess.Move.from_uci(first_move)
                    elif hasattr(first_move, 'uci'):
                        first_move = chess.Move.from_uci(first_move.uci())
                    else:
                        raise ValueError(f"Invalid move type: {type(first_move)}")
                
                # Only add legal moves
                if first_move in temp_board.legal_moves:
                    # Convert count to int and ensure it's positive
                    move_count = int(count)
                    if move_count <= 0:
                        logger.warning(f"Skipping non-positive count {move_count} for move {first_move}")
                        skipped_moves += 1
                        continue
                        
                    self.first_moves[first_move] = self.first_moves.get(first_move, 0) + move_count
                    processed_moves += 1
                else:
                    logger.debug(f"Skipping illegal first move: {first_move}")
                    skipped_moves += 1
                    
            except (ValueError, AttributeError, IndexError) as e:
                logger.warning(f"Skipping invalid first move in sequence {sequence}: {e}")
                skipped_moves += 1
                continue
        
        # Log summary
        if processed_moves > 0 or skipped_moves > 0:
            logger.debug(f"Processed {processed_moves} first moves, skipped {skipped_moves} moves")
    
    def __repr__(self) -> str:
        """Return a string representation of the opening book."""
        return (f"OpeningBook(sequences={len(self.move_sequences)}, "
                f"first_moves={len(self.first_moves)}, exhausted={self.exhausted})")
    
    def add_sequence(self, moves: Sequence[chess.Move], weight: int = 1) -> None:
        """
        Add a move sequence to the opening book.
        
        Args:
            moves: Sequence of moves to add.
            weight: Weight of the sequence (default: 1).
        """
        if not moves:
            return
            
        # Update first moves
        self.first_moves[moves[0]] = self.first_moves.get(moves[0], 0) + weight
        
        # Update all prefixes of the move sequence
        for i in range(1, len(moves) + 1):
            seq = tuple(moves[:i])
            self.move_sequences[seq] = self.move_sequences.get(seq, 0) + weight
    
    def get_move(self, board: chess.Board, temperature: float = 10) -> Optional[chess.Move]:
        """
        Get a move from the opening book based on the current board position.
        
        This method first checks if the book is exhausted or empty. If not, it gets the legal moves
        for the current position and handles the first move separately for efficiency. For other positions,
        it finds sequences that match the current move history and selects the next move.
        
        Args:
            board: Current chess board position.
            temperature: Controls randomness in move selection (0.0 = always choose best, higher = more random).
            
        Returns:
            A move from the opening book, or None if no move is found.
            
        Raises:
            TypeError: If the board is not a chess.Board instance.
            ValueError: If temperature is negative.
        """
        # Validate input parameters
        if not isinstance(board, chess.Board):
            raise TypeError(f"Expected chess.Board, got {type(board).__name__}")
            
        if temperature < 0:
            raise ValueError(f"Temperature must be non-negative, got {temperature}")

        # The bundled book contains lines from the standard initial position.
        # A bare FEN's empty move stack is not evidence of that history.
        if board.root().fen(en_passant='fen') != chess.STARTING_FEN:
            return None
        
        # Check if the book is exhausted or empty
        if not hasattr(self, 'move_sequences') or not self.move_sequences:
            self.exhausted = True
            logger.debug("No move sequences available in opening book")
            return None
            
        if self.exhausted:
            logger.debug("Opening book is exhausted")
            return None
            
        # Get legal moves for the current position
        try:
            legal_moves = set(board.legal_moves)
            if not legal_moves:  # No legal moves means game over
                logger.debug("No legal moves available in current position")
                return None
        except Exception as e:
            logger.error(f"Error getting legal moves: {e}")
            return None
            
        try:
            # Handle first move separately for efficiency
            if board.fullmove_number == 1 and board.turn == chess.WHITE:
                logger.debug("Getting first move from opening book")
                return self._get_first_move(legal_moves, temperature)
            
            # For other positions, find sequences that match the current move history
            move_history = list(board.move_stack)
            matching_sequences = self._find_matching_sequences(move_history)
            
            if not matching_sequences:
                logger.debug(f"No matching sequences found in opening book for move history of length {len(move_history)}")
                self.exhausted = True
                return None
            
            # Get next moves from matching sequences
            next_moves = self._get_next_moves(matching_sequences, move_history, legal_moves)
            
            if not next_moves:
                logger.debug("No valid next moves found in matching sequences")
                self.exhausted = True
                return None
                
            # Select and return a move based on the temperature
            selected_move = self._select_move(next_moves, temperature)
            if selected_move is not None:
                logger.debug(f"Selected move from opening book: {selected_move.uci()}")
            else:
                logger.debug("No move selected from opening book")
                
            return selected_move
            
        except Exception as e:
            logger.error(f"Error in get_move: {e}")
            self.exhausted = True
            return None
        
    def _get_first_move(self, legal_moves: Set[chess.Move], temperature: float) -> Optional[chess.Move]:
        """
        Get a first move from the opening book.
        
        Args:
            legal_moves: Set of legal moves in the current position.
            temperature: Controls randomness in move selection.
            
        Returns:
            A selected move, or None if no valid first moves are found.
            
        Raises:
            TypeError: If legal_moves is not a set of chess.Move objects.
            ValueError: If temperature is negative.
        """
        # Validate input parameters
        if not isinstance(legal_moves, set):
            raise TypeError(f"Expected set of chess.Move, got {type(legal_moves).__name__}")
            
        if temperature < 0:
            raise ValueError(f"Temperature must be non-negative, got {temperature}")
            
        if not hasattr(self, 'first_moves') or not self.first_moves:
            logger.debug("No first moves available in opening book")
            return None
            
        legal_first_moves: Dict[chess.Move, int] = {}
        processed_moves = 0
        skipped_moves = 0
        
        for move, count in self.first_moves.items():
            try:
                # Ensure count is valid
                move_count = int(count)
                if move_count <= 0:
                    logger.debug(f"Skipping non-positive count {move_count} for move {move}")
                    skipped_moves += 1
                    continue
                
                # Ensure move is a chess.Move object
                if not isinstance(move, chess.Move):
                    try:
                        move = chess.Move.from_uci(str(move))
                    except (ValueError, AttributeError) as e:
                        logger.debug(f"Skipping invalid move format: {move}")
                        skipped_moves += 1
                        continue
                
                # Only include legal moves
                if move in legal_moves:
                    legal_first_moves[move] = legal_first_moves.get(move, 0) + move_count
                    processed_moves += 1
                else:
                    logger.debug(f"Skipping illegal first move: {move}")
                    skipped_moves += 1
                    
            except (ValueError, AttributeError, TypeError) as e:
                logger.warning(f"Skipping invalid first move {move} (count={count}): {e}")
                skipped_moves += 1
                continue
        
        if processed_moves > 0 or skipped_moves > 0:
            logger.debug(f"Processed {processed_moves} first moves, skipped {skipped_moves} moves")
                
        return self._select_move(legal_first_moves, temperature) if legal_first_moves else None
        
    def _find_matching_sequences(self, move_history: List[chess.Move]) -> Dict[MoveSequence, int]:
        """
        Find all sequences that match the current move history.
        
        Args:
            move_history: List of moves played so far.
            
        Returns:
            Dictionary of matching move sequences and their counts.
            
        Raises:
            TypeError: If move_history is not a list of chess.Move objects.
        """
        if not isinstance(move_history, list):
            raise TypeError(f"Expected list of chess.Move, got {type(move_history).__name__}")
            
        matching_sequences: Dict[MoveSequence, int] = {}
        history_len = len(move_history)
        
        if not hasattr(self, 'move_sequences') or not self.move_sequences:
            logger.debug("No move sequences available for matching")
            return matching_sequences
        
        processed = 0
        skipped = 0
        
        for seq, count in self.move_sequences.items():
            # Skip if sequence is too short or invalid
            if not seq or not isinstance(seq, (tuple, list)) or len(seq) <= history_len:
                skipped += 1
                continue
                
            try:
                # Convert sequence moves to chess.Move objects if needed
                seq_moves: List[chess.Move] = []
                for i, move in enumerate(seq[:history_len]):  # Only check up to move_history length
                    if not isinstance(move, chess.Move):
                        try:
                            move = chess.Move.from_uci(str(move))
                        except (ValueError, AttributeError) as e:
                            logger.debug(f"Skipping invalid move {move} at position {i}: {e}")
                            raise
                    seq_moves.append(move)
                
                # Check if the sequence matches the move history
                if seq_moves == move_history:
                    # Ensure the sequence is a proper tuple of chess.Move objects
                    matching_sequences[tuple(seq_moves) + seq[history_len:]] = int(count)
                    processed += 1
                    
            except (ValueError, AttributeError, IndexError) as e:
                logger.debug(f"Skipping sequence due to error: {e}")
                skipped += 1
                continue
        
        if processed > 0 or skipped > 0:
            logger.debug(f"Found {processed} matching sequences, skipped {skipped} sequences")
                
        return matching_sequences
        
    def _get_next_moves(self, 
                      matching_sequences: Dict[MoveSequence, int], 
                      move_history: List[chess.Move], 
                      legal_moves: Set[chess.Move]) -> Dict[chess.Move, int]:
        """
        Get the next possible moves from matching sequences.
        
        Args:
            matching_sequences: Dictionary of matching move sequences and their counts.
            move_history: List of moves played so far.
            legal_moves: Set of legal moves in the current position.
            
        Returns:
            Dictionary mapping next moves to their total counts.
            
        Raises:
            TypeError: If any input parameter has incorrect type.
        """
        if not isinstance(matching_sequences, dict):
            raise TypeError(f"Expected dict for matching_sequences, got {type(matching_sequences).__name__}")
        if not isinstance(move_history, list):
            raise TypeError(f"Expected list for move_history, got {type(move_history).__name__}")
        if not isinstance(legal_moves, set):
            raise TypeError(f"Expected set for legal_moves, got {type(legal_moves).__name__}")
            
        next_moves: Dict[chess.Move, int] = {}
        history_len = len(move_history)
        processed = 0
        skipped = 0
        
        for seq, count in matching_sequences.items():
            try:
                # Validate count
                move_count = int(count)
                if move_count <= 0:
                    logger.debug(f"Skipping non-positive count {move_count} for sequence")
                    skipped += 1
                    continue
                
                # Get the next move in the sequence
                if len(seq) <= history_len:
                    logger.debug(f"Sequence too short: {seq} (history length: {history_len})")
                    skipped += 1
                    continue
                    
                next_move = seq[history_len]
                
                # Ensure it's a chess.Move object
                if not isinstance(next_move, chess.Move):
                    try:
                        next_move = chess.Move.from_uci(str(next_move))
                    except (ValueError, AttributeError) as e:
                        logger.debug(f"Skipping invalid move format: {next_move}")
                        skipped += 1
                        continue
                
                # Only include legal moves
                if next_move in legal_moves:
                    next_moves[next_move] = next_moves.get(next_move, 0) + move_count
                    processed += 1
                else:
                    logger.debug(f"Skipping illegal next move: {next_move}")
                    skipped += 1
                    
            except (IndexError, ValueError, AttributeError, TypeError) as e:
                logger.warning(f"Error processing sequence {seq}: {e}")
                skipped += 1
                continue
        
        if processed > 0 or skipped > 0:
            logger.debug(f"Processed {processed} next moves, skipped {skipped} moves")
                
        return next_moves
    
    def _select_move(self, move_counts: Dict[chess.Move, int], temperature: float) -> Optional[chess.Move]:
        """
        Select a move based on move counts and temperature.
        
        Args:
            move_counts: Dictionary mapping moves to their counts.
            temperature: Controls randomness (0.0 = best move, higher = more random).
            
        Returns:
            A selected move, or None if no moves are available.
        """
        if not move_counts:
            return None
            
        moves = list(move_counts.keys())
        counts = np.array([move_counts[move] for move in moves], dtype=np.float64)
        
        # If temperature is very small or zero, return the move with highest count
        if temperature < 1e-6:
            return moves[np.argmax(counts)]
            
        # Apply temperature scaling
        if temperature != 1.0:
            # Scale the log probabilities by temperature
            log_probs = np.log(counts + 1e-10)  # Add small epsilon to avoid log(0)
            scaled_logits = log_probs / temperature
            
            # Calculate softmax
            exp_logits = np.exp(scaled_logits - np.max(scaled_logits))  # Shift for numerical stability
            probs = exp_logits / np.sum(exp_logits)  # This already normalizes to sum=1
        else:
            # At temperature 1.0, use the original normalized counts
            probs = counts / np.sum(counts)  # Normalize here instead of earlier
        
        # Log the probabilities for debugging
        logger.debug(f"Move probabilities (temp={temperature}): {dict(zip([m.uci() for m in moves], np.round(probs, 4)))}")
        
        # Select a move based on the probabilities
        try:
            selected_idx = np.random.choice(len(moves), p=probs)
            return moves[selected_idx]
        except (ValueError, IndexError) as e:
            logger.warning(f"Error in probabilistic move selection: {e}")
            # Fall back to selecting the most frequent move if there's any error
            return moves[np.argmax(counts)]
            
    @classmethod
    def load(cls, filename: str) -> 'OpeningBook':
        """
        Load an opening book from a file.
        
        Args:
            filename: Path to the opening book file.
            
        Returns:
            A new OpeningBook instance with the loaded data.
            
        Raises:
            OpeningBookFileError: If there's an error reading the file.
            OpeningBookDataError: If the file contains invalid data.
        """
        try:
            with open(filename, 'rb') as f:
                data = pickle.load(f)
                
            # Create a new OpeningBook instance
            book = cls()
            
            # Handle different data formats
            if not isinstance(data, dict):
                raise OpeningBookDataError("Invalid opening book format: expected a dictionary")
                
            # Load move sequences
            if 'move_sequences' in data and isinstance(data['move_sequences'], dict):
                for move_str, count in data['move_sequences'].items():
                    try:
                        if isinstance(move_str, str):
                            # Handle space-separated UCI string format
                            moves = tuple(chess.Move.from_uci(move) for move in move_str.split())
                            book.move_sequences[moves] = count
                        elif isinstance(move_str, tuple):
                            # Handle tuple of Move objects
                            book.move_sequences[move_str] = count
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"Skipping invalid move sequence: {move_str}")
                        continue
            
            # Load first moves
            if 'first_moves' in data and isinstance(data['first_moves'], dict):
                for move_str, count in data['first_moves'].items():
                    try:
                        if isinstance(move_str, str):
                            move = chess.Move.from_uci(move_str)
                            book.first_moves[move] = count
                        elif isinstance(move_str, chess.Move):
                            book.first_moves[move_str] = count
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"Skipping invalid first move: {move_str}")
                        continue
            
            # Load stats if available
            if 'stats' in data and isinstance(data['stats'], dict):
                book.stats = data['stats']
            
            logger.info(f"Successfully loaded opening book from {filename}")
            logger.info(f"Move sequences: {len(book.move_sequences)}, First moves: {len(book.first_moves) if hasattr(book, 'first_moves') else 'N/A'}")
            
            # Initialize first moves for quick lookup
            book._initialize_first_moves()
            
            return book
            
        except (FileNotFoundError, PermissionError) as e:
            raise OpeningBookFileError(f"Could not open opening book file {filename}: {e}") from e
        except (pickle.PickleError, EOFError) as e:
            raise OpeningBookDataError(f"Error reading opening book file {filename}: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error loading opening book: {e}", exc_info=True)
            raise OpeningBookError(f"Unexpected error loading opening book: {e}") from e

    def save(self, filename: str) -> None:
        """
        Save the opening book to a file.
        
        Args:
            filename: Path to save the opening book to.
            
        Raises:
            OpeningBookFileError: If the file cannot be written.
            OpeningBookError: If an unexpected error occurs during saving.
        """
        try:
            # Convert move sequences to a serializable format
            data = {
                'move_sequences': {
                    ' '.join(move.uci() for move in moves): count 
                    for moves, count in self.move_sequences.items()
                },
                'first_moves': {
                    move.uci(): count 
                    for move, count in self.first_moves.items()
                },
                'stats': getattr(self, 'stats', {})
            }
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(os.path.abspath(filename)), exist_ok=True)
            
            # Save to file
            with open(filename, 'wb') as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
                
                logger.info(f"Successfully saved opening book to {filename}")
            
        except (IOError, OSError) as e:
            raise OpeningBookFileError(f"Could not write to opening book file {filename}: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error saving opening book: {e}", exc_info=True)
            raise OpeningBookError(f"Unexpected error saving opening book: {e}") from e
    
    def _find_matching_sequence(self, board: chess.Board) -> Dict[chess.Move, int]:
        """
        Find all possible next moves and their frequencies in the opening book
        that match the current board position.
        
        Args:
            board: Current chess board position.
            
        Returns:
            Dictionary mapping moves to their frequencies.
        """
        move_history = list(board.move_stack)
        next_moves: Dict[chess.Move, int] = {}
        
        for seq, count in self.move_sequences.items():
            if len(seq) > len(move_history) and seq[:len(move_history)] == tuple(move_history):
                move = seq[len(move_history)]
                next_moves[move] = next_moves.get(move, 0) + count
                
        return next_moves
    
    def choose_move(self, board: chess.Board, temperature: float = 0.0) -> Optional[chess.Move]:
        """
        Choose a move from the opening book based on the current board position.
        
        This is an alias for get_move() for backward compatibility.
        
        Args:
            board: Current chess board position.
            temperature: Controls randomness in move selection (0.0 = always choose best, higher = more random).
            
        Returns:
            A move from the opening book, or None if no move is found.
        """
        return self.get_move(board, temperature)


# Module-level cache for the opening book
_OPENING_BOOK_CACHE = None
_OPENING_BOOK_LOCK = threading.Lock()

def load_opening_book() -> Optional[OpeningBook]:
    """
    Try to load the bundled opening book from the source-tree root.
    
    This function caches the loaded book in memory to avoid reloading it
    on subsequent calls.
    
    Returns:
        An OpeningBook instance if loaded successfully, None otherwise.
    """
    global _OPENING_BOOK_CACHE
    
    # Check cache first
    if _OPENING_BOOK_CACHE is not None:
        return _OPENING_BOOK_CACHE
        
    # If we get here, we need to load the book
    with _OPENING_BOOK_LOCK:
        # Double-check cache in case another thread loaded it
        if _OPENING_BOOK_CACHE is not None:
            return _OPENING_BOOK_CACHE
            
        # Do not unpickle an unrelated file from the caller's working directory.
        bundled_book = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'opening_book.pkl',
        )
        possible_paths = [bundled_book]
        
        # Print search paths for debugging - using ASCII characters only for Windows compatibility
        logger.debug("\nSearching for opening book in the following locations:")
        for i, path in enumerate(possible_paths, 1):
            exists = "found" if os.path.exists(path) else "not found"
            logger.debug(f"  {i}. {path} ({exists})")
        
        # Try each path
        for path in possible_paths:
            try:
                if os.path.exists(path):
                    logger.debug(f"Attempting to load opening book from: {path}")
                    opening_book = OpeningBook.load(path)
                    if hasattr(opening_book, 'move_sequences'):
                        logger.info(f"Successfully loaded opening book from {path}")
                        logger.info(f"Move sequences: {len(opening_book.move_sequences)}, First moves: {len(opening_book.first_moves) if hasattr(opening_book, 'first_moves') else 'N/A'}")
                    
                    # Cache the loaded book
                    _OPENING_BOOK_CACHE = opening_book
                    return _OPENING_BOOK_CACHE
            except Exception as e:
                logger.warning(f"Failed to load opening book from {path}: {e}")
        
        # If we get here, no book was loaded
        logger.warning("No opening book found. The engine will play without an opening book.")
        logger.info(f"To use an opening book, place it at {bundled_book}")
        
        # Cache None to avoid repeated failed lookups
        _OPENING_BOOK_CACHE = None
        return None
    
    return None

# The opening book is loaded on demand by the UCI engine
