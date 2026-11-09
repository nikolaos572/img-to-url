"""Application configuration, loaded entirely from environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

#: Fallback when no BASE_URL is provided (local development / docker run without a proxy).
DEFAULT_BASE_URL = "http://localhost:8000"

#: Site name shown in the tab title, top bar and footer (SITE_NAME env var).
DEFAULT_SITE_NAME = "img-to-url"

#: Session cookies stay valid for this many days (requirement: ~30 days).
SESSION_DAYS = 30


class ConfigError(RuntimeError):
    """Raised when an environment variable is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    password: str
    base_url: str
    max_size_mb: float
    expiration_hours: int
    data_dir: Path
    port: int
    workers: int
    #: SESSION_SECRET, or "" to let create_app() use a random per-install key.
    session_secret: str
    site_name: str = DEFAULT_SITE_NAME
    #: IMG_CACHE_MINUTES: how long browsers may cache a served image (0 = never).
    image_cache_minutes: int = 0

    @property
    def brand_mark(self) -> str:
        """Short badge text for the logo: first 3 alphanumeric chars, lowercase."""
        slug = "".join(ch for ch in self.site_name.lower() if ch.isalnum())
        return slug[:3] or "app"

    @property
    def max_size_bytes(self) -> int:
        return int(self.max_size_mb * 1024 * 1024)

    @property
    def image_cache_control(self) -> str:
        """Cache-Control for served images: no caching unless a time is set."""
        if self.image_cache_minutes <= 0:
            # no-store: no browser or proxy keeps a copy, so a deleted image
            # is gone on the very next load.
            return "no-store"
        return f"public, max-age={self.image_cache_minutes * 60}"

    @property
    def cookie_secure(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "img-to-url.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)

        password = env.get("APP_PASSWORD", "").strip()
        if not password:
            raise ConfigError(
                "APP_PASSWORD is required: pass the password for the single "
                "'user' account, e.g. -e APP_PASSWORD='change-me' on docker run."
            )

        base_url = env.get("BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ConfigError(
                f"BASE_URL must start with http:// or https:// (got {base_url!r})."
            )

        max_size_mb = _env_float(env, "IMG_MAX_SIZE", default=10.0, minimum=0.001)
        expiration_hours = _env_int(env, "IMG_EXPIRATION_HOURS", default=0, minimum=0)
        image_cache_minutes = _env_int(env, "IMG_CACHE_MINUTES", default=0, minimum=0)
        port = _env_int(env, "PORT", default=8000, minimum=1, maximum=65535)
        workers = _env_int(env, "WORKERS", default=1, minimum=1, maximum=64)
        data_dir = Path(env.get("DATA_DIR", "data")).expanduser()

        # Left empty when unset: create_app() then combines a random key kept
        # in the database with the password (see auth.derive_session_secret).
        session_secret = env.get("SESSION_SECRET", "").strip()

        site_name = env.get("SITE_NAME", "").strip() or DEFAULT_SITE_NAME

        return cls(
            password=password,
            base_url=base_url,
            max_size_mb=max_size_mb,
            expiration_hours=expiration_hours,
            data_dir=data_dir,
            port=port,
            workers=workers,
            session_secret=session_secret,
            site_name=site_name,
            image_cache_minutes=image_cache_minutes,
        )


def _env_int(
    env: dict[str, str],
    name: str,
    *,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from None
    if value < minimum or (maximum is not None and value > maximum):
        top = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be at least {minimum}{top} (got {value}).")
    return value


def _env_float(
    env: dict[str, str],
    name: str,
    *,
    default: float,
    minimum: float,
) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if not math.isfinite(value):
        # float() accepts "inf" and "nan"; both would crash later when
        # converted to a byte count.
        raise ConfigError(f"{name} must be a finite number, got {raw!r}.")
    if value <= 0 or value < minimum:
        raise ConfigError(f"{name} must be greater than 0 (got {value}).")
    return value
