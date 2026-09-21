"""Runtime configuration.

Every value is overridable through ``PIXELFORGE_*`` environment variables or a
``.env`` file, so the same build runs on a dev laptop and a device host.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIXELFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # --- adb -----------------------------------------------------------------
    adb_executable: str = Field(
        default="adb",
        description=(
            "Path to adb. Prefer the vendored binary: two adb versions on one "
            "machine kill each other's servers."
        ),
    )
    adb_server_port: int = Field(
        default=5038,
        ge=1,
        le=65535,
        description=(
            "Private adb server port. Deliberately not 5037 -- that one is a "
            "machine-wide singleton any other tool can restart."
        ),
    )
    adb_timeout_s: float = Field(default=15.0, gt=0, le=300)

    # --- storage -------------------------------------------------------------
    data_dir: Path = Field(default=_REPO_ROOT / ".pixelforge")

    # --- leases --------------------------------------------------------------
    lease_ttl_s: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        description="Device lease TTL; the frontend heartbeats at roughly TTL/3.",
    )

    # --- server --------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8420, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    log_level: str = "INFO"

    @field_validator("data_dir")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def vendor_dir(self) -> Path:
        return _REPO_ROOT / "vendor"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.templates_dir, self.captures_dir):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
