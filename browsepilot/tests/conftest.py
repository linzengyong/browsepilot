"""Shared test bootstrap."""

import sys
from pathlib import Path

# V2: make src/browsepilot importable (src layout) + repo root on sys.path
ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
