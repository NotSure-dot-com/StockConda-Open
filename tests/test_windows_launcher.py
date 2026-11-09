"""The frozen entry point must keep the same stdin/stdout UCI lifecycle."""

from pathlib import Path
import sys

from uci_harness import UciSession


def test_windows_launcher_handshake_and_quit():
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "stockconda_frozen.py"
    session = UciSession([sys.executable, str(launcher)])
    try:
        session.fence()
        session.ready()
        session.finish()
    finally:
        session.close()
