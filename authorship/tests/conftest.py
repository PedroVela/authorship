import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return str(p)


@pytest.fixture
def qr(project):
    """The golden QR session, replayed into a fresh project."""
    return harness.replay(project)


@pytest.fixture
def qr_confirmed(qr):
    """The QR session plus the human-confirmed edges from edges.json."""
    harness.confirm_edges(qr, harness.load_edges())
    return qr
