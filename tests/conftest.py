"""Shared fixtures. Every test gets its own SQLite file under pytest's tmp_path, so the suite never
touches the demo database and never needs the network.

Windows note: Git Bash's /tmp is invisible to the Windows interpreter. Always use tmp_path or a
repo-relative path, never a hardcoded POSIX temp directory.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from recall_relay.core import seed as seed_module
from recall_relay.core.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_FIXTURES = REPO_ROOT / "data" / "fixtures" / "ledger"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A fresh, empty Store on its own database file."""
    return Store(tmp_path / "recall_relay_test.db")


@pytest.fixture(scope="session")
def _seed_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Seed once per session into a template file; each test copies it.

    `load_seed` commits per row, which is correct but slow to repeat ~20 times. `load_seed` itself is
    still exercised directly (test_store.test_seeding_is_idempotent) so this shortcut cannot hide a bug
    in it.
    """
    path = tmp_path_factory.mktemp("seed-template") / "seeded.db"
    counts = seed_module.load_seed(Store(path))
    assert counts == {"agencies": 12, "receipts": 60, "distributions": 140}
    return path


@pytest.fixture
def seeded_store(tmp_path: Path, _seed_template: Path) -> Store:
    """A Store carrying the full demo ledger: 12 agencies, 60 receipts, 140 distributions."""
    db = tmp_path / "recall_relay_seeded.db"
    shutil.copyfile(_seed_template, db)
    return Store(db)


@pytest.fixture
def seed():
    """The seed module itself, so tests can reach the pinned ids without importing it everywhere."""
    return seed_module


@pytest.fixture
def ledger_fixture_dir() -> Path:
    """The committed CSVs under data/fixtures/ledger."""
    return LEDGER_FIXTURES
