from __future__ import annotations

from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASTAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: AnyHttpUrl = "https://api.ainm.no"
    access_token: str | None = None
    data_dir: Path = Path("data")
    documentation_path: Path = Path("documentation.md")
    request_timeout_seconds: float = Field(default=30.0, gt=0.0)
    user_agent: str = "astarisland-baseline/0.1.0"

    @property
    def has_token(self) -> bool:
        return bool(self.access_token)

