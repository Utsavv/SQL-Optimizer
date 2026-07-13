"""Make the ``scripts`` package importable when pytest runs from the repo root.

The tests exercise the deterministic engine with mock cursors and sample plan
XML — no SQL Server, no pyodbc, no network — so they run anywhere pytest does.
"""
import sys
from pathlib import Path

# sp-optimizer/ holds the scripts/ package; put it on the path.
SP_OPTIMIZER = Path(__file__).resolve().parent.parent
if str(SP_OPTIMIZER) not in sys.path:
    sys.path.insert(0, str(SP_OPTIMIZER))
