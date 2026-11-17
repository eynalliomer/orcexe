"""Test suite for orcexe."""

from pathlib import Path
import sys

# Every module must be importable in its own worker (and by unittest by name).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/orcexe/src'))
