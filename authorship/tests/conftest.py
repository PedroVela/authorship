import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import harness  # noqa: E402


@pytest.fixture(autouse=True)
def _no_live_classifier(monkeypatch, tmp_path):
    """Tests that want the classifier pass a FakeClassifier explicitly."""
    monkeypatch.setenv("AUTHORSHIP_AUTO", "0")
    monkeypatch.setenv("AUTHORSHIP_HINT", "0")
    monkeypatch.setenv("AUTHORSHIP_ANCHOR", "0")  # sealing at session end is on by default; tests opt in
    monkeypatch.setenv("AUTHORSHIP_BIN_DIR", str(tmp_path / "bin"))  # never write to the real ~/.local/bin
    monkeypatch.setenv("AUTHORSHIP_CONFIG", str(tmp_path / "classifier.json"))  # never read the real saved choice
    monkeypatch.setenv("AUTHORSHIP_PYTHON", sys.executable)  # init never runs setx on the real machine
    for k in ("AUTHORSHIP_AUTO_BACKEND", "AUTHORSHIP_AUTO_MODEL", "AUTHORSHIP_JEV_PROVIDER", "AUTHORSHIP_JEV_MODEL"):
        monkeypatch.delenv(k, raising=False)


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
