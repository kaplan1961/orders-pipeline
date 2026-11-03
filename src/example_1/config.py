"""Runtime configuration loaded from ``PIPELINE_*`` environment variables or a ``.env`` file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pipeline settings.

    Attributes:
        database_url: PostgreSQL connection URL (``PIPELINE_DATABASE_URL``).
        data_dir: Folder where the daily ``orders_*.csv`` files and ``products.csv`` land.
        out_dir: Folder for the exported gold CSV.
    """

    model_config = SettingsConfigDict(env_prefix="PIPELINE_", env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql://pipeline:pipeline@localhost:5432/orders",
        description="PostgreSQL URL; the default matches docker-compose.yml (local dev only)",
    )
    data_dir: Path = Field(default=Path("data"), description="Landing folder for source files")
    out_dir: Path = Field(default=Path("out"), description="Folder for exported output")
