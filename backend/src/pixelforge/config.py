"""Runtime configuration.

Every value is overridable through ``PIXELFORGE_*`` environment variables or a
``.env`` file, so the same build runs on a dev laptop and a device host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_PACKAGE_DIR = Path(__file__).resolve().parent


def _repo_root() -> Path:
    """Locate the checkout, or fall back to the working directory.

    ``parents[2]`` is the backend directory in a src-layout checkout, but in a
    non-editable wheel install it lands in ``site-packages`` -- where writing a
    data directory or looking for ``vendor/`` is wrong. Probing for
    ``pyproject.toml`` distinguishes the two, and the cwd fallback keeps an
    installed copy working with data under wherever it was started.
    """
    candidate = _PACKAGE_DIR.parents[1]
    return candidate if (candidate / "pyproject.toml").is_file() else Path.cwd()


_REPO_ROOT = _repo_root()


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
    adb_server_host: str = Field(
        default="127.0.0.1",
        description=(
            "Host running the adb server. Set this when PixelForge runs in a "
            "container and adb runs on the host -- the only workable shape on "
            "macOS, where Docker cannot access USB. Forwarded ports (scrcpy, "
            "uiautomator2) are dialled here too, because adb binds them on the "
            "server's machine."
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

    # --- device source -------------------------------------------------------
    device_provider: Literal["local", "devicefarmer"] = "local"
    devicefarmer_url: str | None = None
    devicefarmer_access_token: SecretStr | None = None
    devicefarmer_poll_interval_s: float = Field(default=3.0, gt=0, le=300)
    devicefarmer_verify_ssl: bool = True

    # --- storage -------------------------------------------------------------
    data_dir: Path = Field(default=_REPO_ROOT / ".pixelforge")
    asset_dir: Path | None = Field(
        default=None,
        description=(
            "Directory for standalone cropped PNG assets. Defaults to "
            "<data_dir>/assets; set PIXELFORGE_ASSET_DIR to choose another root."
        ),
    )

    # --- leases --------------------------------------------------------------
    lease_ttl_s: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        description="Device lease TTL; the frontend heartbeats at roughly TTL/3.",
    )

    # --- plugins -------------------------------------------------------------
    exporter_plugins: list[Path] = Field(
        default_factory=list,
        description=(
            "Directories of exporter plugin modules. Business-specific export "
            "formats live here rather than in core -- see examples/exporters/. "
            "These are imported as Python, so treat them as source."
        ),
    )

    # --- server --------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8420, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    log_level: str = "INFO"

    @field_validator("data_dir", "asset_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator("exporter_plugins")
    @classmethod
    def _expand_all(cls, value: list[Path]) -> list[Path]:
        return [path.expanduser() for path in value]

    @model_validator(mode="after")
    def _device_provider_configured(self) -> Settings:
        if self.device_provider == "devicefarmer":
            if not self.devicefarmer_url:
                raise ValueError(
                    "PIXELFORGE_DEVICEFARMER_URL is required for the devicefarmer provider"
                )
            if self.devicefarmer_access_token is None:
                raise ValueError(
                    "PIXELFORGE_DEVICEFARMER_ACCESS_TOKEN is required for the "
                    "devicefarmer provider"
                )
        return self

    @property
    def frontend_dir(self) -> Path | None:
        """Where the build-free frontend lives, if it is present.

        Checked rather than computed: a wheel install has no sibling ``frontend``
        directory, and the API must still come up in that case -- just without the
        UI mounted.
        """
        override = os.environ.get("PIXELFORGE_FRONTEND_DIR", "").strip()
        candidates = (
            [Path(override).expanduser()]
            if override
            else [_REPO_ROOT.parent / "frontend", Path.cwd() / "frontend"]
        )
        return next(
            (path for path in candidates if (path / "index.html").is_file()), None
        )

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def assets_dir(self) -> Path:
        return self.asset_dir or self.data_dir / "assets"

    @property
    def vendor_dir(self) -> Path:
        return _REPO_ROOT / "vendor"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.templates_dir, self.captures_dir, self.assets_dir):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
