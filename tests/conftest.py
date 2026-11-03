"""Shared pytest fixtures.

Database tests need a dedicated, disposable PostgreSQL database whose URL is given in
``PIPELINE_TEST_DATABASE_URL``; they are skipped when it is not set. The fixtures drop and
recreate the pipeline schemas, so never point this at a database you care about.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from example_1.config import Settings

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SOURCE_FILES = ("products.csv", "orders_2024-01-01.csv", "orders_2024-01-02.csv")


def reset_database(database_url: str) -> None:
    """Drop all pipeline schemas so a test starts from an empty warehouse.

    Args:
        database_url: URL of the disposable test database.
    """
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS ops, bronze, silver, gold CASCADE")


@pytest.fixture
def database_url() -> str:
    """Return the test database URL after resetting it, or skip if none is configured."""
    url = os.environ.get("PIPELINE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PIPELINE_TEST_DATABASE_URL is not set")
    reset_database(url)
    return url


@pytest.fixture
def source_dir() -> Path:
    """Return the read-only folder holding the exercise's source files."""
    return DATA_DIR


@pytest.fixture
def reset_db(database_url: str) -> Callable[[], None]:
    """Return a function that empties the test database again in the middle of a test."""
    return lambda: reset_database(database_url)


@pytest.fixture
def landing_dir(tmp_path: Path) -> Path:
    """Return an empty landing folder that tests fill with source files."""
    path = tmp_path / "landing"
    path.mkdir()
    return path


@pytest.fixture
def full_landing_dir(landing_dir: Path) -> Path:
    """Return a landing folder holding the products file and both daily order drops."""
    for name in SOURCE_FILES:
        shutil.copy(DATA_DIR / name, landing_dir / name)
    return landing_dir


@pytest.fixture
def settings(database_url: str, full_landing_dir: Path, tmp_path: Path) -> Settings:
    """Return settings pointing at the test database and the full landing folder."""
    return Settings(database_url=database_url, data_dir=full_landing_dir, out_dir=tmp_path / "out")
