"""Docker smoke tests (marked ``docker``; skipped when Docker is unavailable).

Builds the production image, runs it exactly like a real deployment
(BASE_URL + APP_PASSWORD + multiple workers), and drives the full flow over
HTTP: login, upload, public direct link, admin listing, delete. Also checks
that the app runs as UID/GID 61001 and works with a host directory prepared
as docs/configuration.md describes.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.docker

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_TAG = "img-to-url:test"
CONTAINER_NAME = "img-to-url-smoke"
CONTAINER_PASSWORD = "docker-test-pw"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"smoke" * 40
APP_UID_GID = "61001:61001"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=300
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _wait_until_healthy(url: str, container: str) -> None:
    deadline = time.time() + 120
    last_error = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{url}/healthz", timeout=2)
            if response.status_code == 200:
                return
            last_error = f"status {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        time.sleep(1)
    logs = _run_docker("logs", container, check=False)
    raise RuntimeError(
        f"Container never became healthy: {last_error}\n{logs.stdout}{logs.stderr}"
    )


def _login(url: str) -> requests.Session:
    http = requests.Session()
    response = http.post(
        f"{url}/login",
        data={"username": "user", "password": CONTAINER_PASSWORD},
        allow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return http


def _process_owners(container: str) -> set[str]:
    """uid:gid of every process in the container (the image has no ps)."""
    script = (
        "import os\n"
        "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
        "    try:\n"
        "        st = os.stat(f'/proc/{pid}')\n"
        "    except FileNotFoundError:\n"
        "        continue\n"
        "    print(f'{st.st_uid}:{st.st_gid}')\n"
    )
    return set(_run_docker("exec", container, "python", "-c", script).stdout.split())


@pytest.fixture(scope="module")
def docker_image() -> str:
    if not _docker_available():
        pytest.skip("Docker is not available on this machine")
    _run_docker("build", "-t", IMAGE_TAG, str(REPO_ROOT))
    return IMAGE_TAG


@pytest.fixture(scope="module")
def base_url(docker_image) -> str:
    if not _docker_available():
        pytest.skip("Docker is not available on this machine")

    _run_docker("rm", "-f", CONTAINER_NAME, check=False)  # stale leftovers
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    _run_docker(
        "run", "-d", "--name", CONTAINER_NAME,
        "-p", f"127.0.0.1:{port}:8000",
        "-e", f"APP_PASSWORD={CONTAINER_PASSWORD}",
        "-e", f"BASE_URL={url}",
        "-e", "IMG_MAX_SIZE=5",
        "-e", "IMG_EXPIRATION_HOURS=0",
        "-e", "WORKERS=4",
        docker_image,
    )

    try:
        _wait_until_healthy(url, CONTAINER_NAME)
        yield url
    finally:
        _run_docker("rm", "-f", CONTAINER_NAME, check=False)


@pytest.fixture(scope="module")
def session(base_url) -> requests.Session:
    """A logged-in requests session (cookie jar keeps the session cookie)."""
    return _login(base_url)


class TestDockerSmoke:
    def test_healthz(self, base_url):
        response = requests.get(f"{base_url}/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_runs_as_uid_61001(self, base_url):
        # Every process (uvicorn's supervisor and all 4 workers) runs as
        # 61001, never root or a typical host login such as 1000.
        assert _process_owners(CONTAINER_NAME) == {APP_UID_GID}
        # The container's /data volume gets the image's ownership on first use.
        owner = _run_docker("exec", CONTAINER_NAME, "stat", "-c", "%u:%g", "/data")
        assert owner.stdout.strip() == APP_UID_GID

    def test_anonymous_root_is_plain_404(self, base_url):
        response = requests.get(base_url + "/", allow_redirects=False)
        assert response.status_code == 404
        assert "Location" not in response.headers
        assert response.headers["Content-Type"].startswith("text/html")
        assert "<h1>404 Not Found</h1>" in response.text

    def test_anonymous_admin_redirected_to_login(self, base_url):
        response = requests.get(base_url + "/admin", allow_redirects=False)
        assert response.status_code == 303
        assert response.headers["Location"] == "/login"

    def test_wrong_password_rejected(self, base_url):
        response = requests.post(
            f"{base_url}/login",
            data={"username": "user", "password": "wrong"},
            allow_redirects=False,
        )
        assert response.status_code == 401

    def test_upload_returns_public_direct_url(self, base_url, session):
        response = session.post(
            f"{base_url}/upload", files={"file": ("smoke.png", PNG_BYTES)}
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["size"] == len(PNG_BYTES)
        assert re.fullmatch(
            r"image-[0-9a-f]{16}-[0-9a-f]{16}\.png", data["filename"]
        )
        assert data["url"] == f"{base_url}/{data['filename']}"

        # The direct link is public: no session cookie required.
        public = requests.get(data["url"])
        assert public.status_code == 200
        assert public.content == PNG_BYTES
        assert public.headers["Content-Type"] == "image/png"
        # IMG_CACHE_MINUTES is not set on this container: no caching.
        assert public.headers["Cache-Control"] == "no-store"

        # HEAD works too (link-preview bots and uptime monitors use it).
        head = requests.head(data["url"])
        assert head.status_code == 200
        assert head.headers["Content-Length"] == str(len(PNG_BYTES))

    def test_anonymous_upload_rejected(self, base_url):
        response = requests.post(
            f"{base_url}/upload", files={"file": ("anon.png", PNG_BYTES)}
        )
        assert response.status_code == 401

    def test_upload_rejects_non_images(self, base_url, session):
        response = session.post(
            f"{base_url}/upload", files={"file": ("evil.sh", b"#!/bin/sh\n")}
        )
        assert response.status_code == 415

    def test_admin_shows_images_and_delete_works(self, base_url, session):
        upload = session.post(
            f"{base_url}/upload", files={"file": ("admin.png", PNG_BYTES)}
        ).json()

        response = session.get(f"{base_url}/admin")
        assert response.status_code == 200
        assert upload["url"] in response.text

        response = session.post(
            f"{base_url}/admin/delete",
            data={"filename": upload["filename"]},
            allow_redirects=False,
        )
        assert response.status_code == 303
        gone = requests.get(upload["url"])
        assert gone.status_code == 404
        assert gone.headers["Content-Type"].startswith("text/html")
        assert "<h1>404 Not Found</h1>" in gone.text

    def test_post_from_another_origin_is_refused(self, base_url, session):
        upload = session.post(
            f"{base_url}/upload", files={"file": ("keep.png", PNG_BYTES)}
        ).json()
        response = session.post(
            f"{base_url}/admin/clear-all",
            headers={"Origin": "https://evil.example"},
            allow_redirects=False,
        )
        assert response.status_code == 403
        assert requests.get(upload["url"]).status_code == 200  # nothing deleted

    def test_clear_all_leaves_empty_state(self, base_url, session):
        session.post(f"{base_url}/upload", files={"file": ("x.png", PNG_BYTES)})
        response = session.post(f"{base_url}/admin/clear-all", allow_redirects=False)
        assert response.status_code == 303
        page = session.get(f"{base_url}/admin")
        assert "No images yet" in page.text

    def test_multiple_workers_serve_parallel_requests(self, base_url, session):
        """The container runs with WORKERS=4; hammer it with parallel uploads.

        Each thread uses its own session (fresh login) to avoid any shared
        cookie-jar state, while all requests hit the same multi-worker app.
        """
        import concurrent.futures

        def one(i: int) -> int:
            http = requests.Session()
            response = http.post(
                f"{base_url}/login",
                data={"username": "user", "password": CONTAINER_PASSWORD},
                allow_redirects=False,
            )
            assert response.status_code == 303
            response = http.post(
                f"{base_url}/upload",
                files={"file": (f"par{i}.png", PNG_BYTES + str(i).encode())},
            )
            return response.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(one, range(8)))
        assert statuses == [200] * 8


class TestHostDirectory:
    def test_host_directory_given_to_61001_works(self, docker_image, tmp_path):
        """The setup docs/configuration.md describes for a host directory."""
        host_dir = tmp_path / "data"
        host_dir.mkdir()
        mount = f"{host_dir}:/data"
        name = f"{CONTAINER_NAME}-hostdir"
        port = _free_port()
        url = f"http://127.0.0.1:{port}"

        def as_root(*cmd: str, check: bool = True) -> None:
            _run_docker("run", "--rm", "--user", "0", "-v", mount, docker_image,
                        *cmd, check=check)

        _run_docker("rm", "-f", name, check=False)
        try:
            # The documented chown (the form that also works with rootless Docker).
            as_root("chown", APP_UID_GID, "/data")
            _run_docker(
                "run", "-d", "--name", name,
                "-p", f"127.0.0.1:{port}:8000",
                "-e", f"APP_PASSWORD={CONTAINER_PASSWORD}",
                "-e", f"BASE_URL={url}",
                "-v", mount,
                docker_image,
            )
            _wait_until_healthy(url, name)

            response = _login(url).post(
                f"{url}/upload", files={"file": ("host.png", PNG_BYTES)}
            )
            assert response.status_code == 200, response.text
            assert requests.get(response.json()["url"]).content == PNG_BYTES

            stored = f"/data/images/{response.json()['filename']}"
            owners = _run_docker(
                "exec", name, "stat", "-c", "%u:%g", "/data/img-to-url.db", stored
            ).stdout.split()
            assert owners == [APP_UID_GID, APP_UID_GID]
        finally:
            _run_docker("rm", "-f", name, check=False)
            # Hand the files back to this user so pytest can delete tmp_path.
            as_root("chown", "-R", f"{os.getuid()}:{os.getgid()}", "/data", check=False)
