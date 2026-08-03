"""Shared fixtures for tests."""

import os
import sys
from pathlib import Path

import pytest
from backend.app.config import settings
from backend.app.session_manager import SessionManager

# V2: make src/browsepilot importable (src layout) + repo root on sys.path
ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture
def session_manager(tmp_path):
    """Create a SessionManager pointed at a temp directory."""
    original = settings.data_dir
    settings.data_dir = str(tmp_path)
    os.makedirs(f"{tmp_path}/sessions", exist_ok=True)
    sm = SessionManager(max_active_sessions=10)
    yield sm
    settings.data_dir = original
