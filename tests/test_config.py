"""Tests for Settings parsing from environment variables."""

from pathlib import Path

import pytest

from app.config import ConfigError, Settings, url_origin


def test_defaults():
    settings = Settings.from_env(
        {"APP_PASSWORD": "pw", "BASE_URL": "https://images.example.com"}
    )
    assert settings.password == "pw"
    assert settings.base_url == "https://images.example.com"
    assert settings.site_name == "img-to-url"
    assert settings.brand_mark == "img"
    assert settings.max_size_mb == 10
    assert settings.max_size_bytes == 10 * 1024 * 1024
    assert settings.expiration_hours == 0
    assert settings.port == 8000
    assert settings.workers == 1
    assert settings.data_dir.as_posix() == "data"
    assert settings.cookie_secure is True
    assert settings.db_path.name == "img-to-url.db"
    assert settings.images_dir.name == "images"
    # No SESSION_SECRET: left empty so create_app() uses a random per-install
    # key instead of one derived from the password alone.
    assert settings.session_secret == ""


def test_base_url_default_and_trailing_slash_stripped():
    settings = Settings.from_env({"APP_PASSWORD": "pw", "BASE_URL": "https://x.test/"})
    assert settings.base_url == "https://x.test"

    default = Settings.from_env({"APP_PASSWORD": "pw"})
    assert default.base_url == "http://localhost:8000"
    assert default.cookie_secure is False


def test_missing_password_raises():
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        Settings.from_env({})


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://nope",
        "images.example.com",  # no scheme
        "https://",  # no host
        "https://:443",
        "https://images.example.com:99999",  # port out of range
        "https://images.example.com:abc",
    ],
)
def test_invalid_base_url_raises(raw):
    with pytest.raises(ConfigError, match="BASE_URL must be an http:// or https:// address"):
        Settings.from_env({"APP_PASSWORD": "pw", "BASE_URL": raw})


@pytest.mark.parametrize(
    "base_url,origin",
    [
        ("https://images.example.com", "https://images.example.com"),
        ("https://Images.Example.COM/", "https://images.example.com"),
        ("https://images.example.com:443", "https://images.example.com"),
        ("http://images.example.com:80/sub/path", "http://images.example.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://images.example.com:8443", "https://images.example.com:8443"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_origin_is_base_url_scheme_host_and_port(base_url, origin):
    settings = Settings.from_env({"APP_PASSWORD": "pw", "BASE_URL": base_url})
    assert settings.origin == origin


@pytest.mark.parametrize("value", ["null", "", "file:///etc", "https://", "http://x:abc"])
def test_url_origin_of_non_origins_is_none(value):
    assert url_origin(value) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("10", 10.0), ("0.5", 0.5), ("2.5", 2.5)],
)
def test_max_size_parsing(raw, expected):
    settings = Settings.from_env({"APP_PASSWORD": "pw", "IMG_MAX_SIZE": raw})
    assert settings.max_size_mb == expected


@pytest.mark.parametrize("raw", ["abc", "-3", "0", "nan", "inf"])
def test_invalid_max_size_raises(raw):
    with pytest.raises(ConfigError, match="IMG_MAX_SIZE"):
        Settings.from_env({"APP_PASSWORD": "pw", "IMG_MAX_SIZE": raw})


@pytest.mark.parametrize("raw", ["72", "0"])
def test_expiration_parsing(raw):
    settings = Settings.from_env(
        {"APP_PASSWORD": "pw", "IMG_EXPIRATION_HOURS": raw}
    )
    assert settings.expiration_hours == int(raw)


def test_invalid_expiration_raises():
    with pytest.raises(ConfigError, match="IMG_EXPIRATION_HOURS"):
        Settings.from_env({"APP_PASSWORD": "pw", "IMG_EXPIRATION_HOURS": "-1"})


def test_image_cache_defaults_to_no_caching():
    settings = Settings.from_env({"APP_PASSWORD": "pw"})
    assert settings.image_cache_minutes == 0
    assert settings.image_cache_control == "no-store"


@pytest.mark.parametrize(
    "raw,header",
    [
        ("5", "public, max-age=300"),
        ("30", "public, max-age=1800"),
        ("0", "no-store"),
        ("", "no-store"),  # set but empty: same as unset
    ],
)
def test_image_cache_minutes(raw, header):
    settings = Settings.from_env({"APP_PASSWORD": "pw", "IMG_CACHE_MINUTES": raw})
    assert settings.image_cache_control == header


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5", "5m"])
def test_invalid_image_cache_minutes_raises(raw):
    with pytest.raises(ConfigError, match="IMG_CACHE_MINUTES"):
        Settings.from_env({"APP_PASSWORD": "pw", "IMG_CACHE_MINUTES": raw})


def test_port_and_workers_validation():
    assert Settings.from_env({"APP_PASSWORD": "pw", "PORT": "9000"}).port == 9000
    assert Settings.from_env({"APP_PASSWORD": "pw", "WORKERS": "4"}).workers == 4
    with pytest.raises(ConfigError, match="PORT"):
        Settings.from_env({"APP_PASSWORD": "pw", "PORT": "99999"})
    with pytest.raises(ConfigError, match="WORKERS"):
        Settings.from_env({"APP_PASSWORD": "pw", "WORKERS": "x"})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("172.17.0.1", "172.17.0.1"),
        (" 172.18.0.0/16 , 10.0.0.5 ", "172.18.0.0/16,10.0.0.5"),
        ("::1,fd00::/8", "::1,fd00::/8"),
        ("*", "*"),
    ],
)
def test_forwarded_allow_ips(raw, expected):
    settings = Settings.from_env({"APP_PASSWORD": "pw", "FORWARDED_ALLOW_IPS": raw})
    assert settings.forwarded_allow_ips == expected


@pytest.mark.parametrize(
    "raw,problem",
    [
        ("172.17.0.1/16", "'172.17.0.1/16' is not one (172.17.0.1/16 has host bits set)"),
        ("proxy.example.com", "'proxy.example.com' is not one"),
        ("10.0.0.1,", "'' is not one"),
        ("*,10.0.0.1", "'*' is not one"),
        ("300.1.1.1", "'300.1.1.1' is not one"),
    ],
)
def test_invalid_forwarded_allow_ips_raises(raw, problem):
    with pytest.raises(ConfigError, match="FORWARDED_ALLOW_IPS must be IP addresses") as error:
        Settings.from_env({"APP_PASSWORD": "pw", "FORWARDED_ALLOW_IPS": raw})
    assert problem in str(error.value)


def test_custom_session_secret():
    settings = Settings.from_env(
        {"APP_PASSWORD": "pw", "SESSION_SECRET": "my-secret"}
    )
    assert settings.session_secret == "my-secret"


def test_site_name():
    custom = Settings.from_env({"APP_PASSWORD": "pw", "SITE_NAME": "My Pics"})
    assert custom.site_name == "My Pics"
    assert custom.brand_mark == "myp"

    # Whitespace-only SITE_NAME falls back to the default instead of
    # rendering an empty title.
    blank = Settings.from_env({"APP_PASSWORD": "pw", "SITE_NAME": "   "})
    assert blank.site_name == "img-to-url"


def test_brand_mark_strips_non_alphanumerics():
    from app.config import DEFAULT_SITE_NAME, Settings

    assert Settings.from_env(
        {"APP_PASSWORD": "pw", "SITE_NAME": "My-Pics Page!"}
    ).brand_mark == "myp"
    # Short names use whatever alphanumeric characters exist.
    short = Settings(password="pw", base_url="https://x.test", max_size_mb=1,
                     expiration_hours=0, data_dir=Path("."), port=8000,
                     workers=1, session_secret="s", site_name="P")
    assert short.brand_mark == "p"
    # Degenerate empty name (only possible via direct construction).
    empty = Settings(password="pw", base_url="https://x.test", max_size_mb=1,
                     expiration_hours=0, data_dir=Path("."), port=8000,
                     workers=1, session_secret="s", site_name="")
    assert empty.brand_mark == "app"
    assert DEFAULT_SITE_NAME == "img-to-url"
