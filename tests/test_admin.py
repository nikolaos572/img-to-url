"""Tests for the admin page: listing, pagination, stats, delete, clear-all."""

import threading
import time
from pathlib import Path

from app import db, main

from .conftest import make_png, upload

LATE_FILENAME = "image-0123456789abcdef-0123456789abcdef.png"


def _upload_many(auth_client, count):
    """Upload N images, then give each row a distinct created_at so the
    newest-first ordering is fully deterministic (uploads can land within
    the same wall-clock second)."""
    settings = auth_client.app.state.settings
    for i in range(count):
        response = upload(auth_client, f"img-{i}.png", make_png(10 + i))
        assert response.status_code == 200
    # Force distinct created_at values by rewriting rows after upload.
    filenames = [
        row["filename"]
        for row in db.list_images(settings.db_path, limit=10**9, offset=0)
    ]
    base = 1_700_000_000
    for offset, filename in enumerate(sorted(filenames)):
        with db.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE images SET created_at = ? WHERE filename = ?",
                (base + offset, filename),
            )
    return list(reversed(sorted(filenames)))  # newest upload first


class TestAdminListing:
    def test_requires_login(self, client):
        assert client.get("/admin", follow_redirects=False).status_code == 303

    def test_empty_state(self, auth_client):
        response = auth_client.get("/admin")
        assert response.status_code == 200
        assert "No images yet" in response.text
        assert "0 B" in response.text  # total space field

    def test_shows_uploaded_images(self, auth_client):
        data = upload(auth_client, "photo.png", make_png(2048)).json()
        response = auth_client.get("/admin")
        assert response.status_code == 200
        assert data["url"] in response.text
        assert "2.0 KB" in response.text
        assert "1" in response.text  # image count
        assert data["filename"] in response.text

    def test_newest_first_pagination_by_10(self, auth_client):
        filenames = _upload_many(auth_client, 15)  # 15 uploads -> 2 pages

        page1 = auth_client.get("/admin")
        rows1 = page1.text.count('class="img-row"')
        assert rows1 == 10
        # First page contains the 10 newest uploads.
        for filename in filenames[:10]:
            assert filename in page1.text
        assert filenames[10] not in page1.text

        page2 = auth_client.get("/admin?page=2")
        assert page2.text.count('class="img-row"') == 5
        for filename in filenames[10:]:
            assert filename in page2.text
        assert filenames[0] not in page2.text
        assert "Page 2 of 2" in page2.text

    def test_page_validation(self, auth_client):
        _upload_many(auth_client, 12)  # 2 pages
        # Out-of-range page clamps to the last page.
        response = auth_client.get("/admin?page=99")
        assert response.text.count('class="img-row"') == 2
        assert "Page 2 of 2" in response.text
        # Invalid / non-positive pages fall back to page 1.
        for query in ["", "?page=0", "?page=-3", "?page=abc"]:
            response = auth_client.get("/admin" + query)
            assert response.text.count('class="img-row"') == 10
            assert "Page 1 of 2" in response.text

    def test_rows_have_copy_buttons(self, auth_client):
        first = upload(auth_client, "a.png", make_png(5)).json()
        second = upload(auth_client, "b.png", make_png(6)).json()
        html = auth_client.get("/admin").text
        # Each row's button carries its own direct link for admin.js to copy.
        assert html.count("data-copy=") == 2
        assert f'data-copy="{first["url"]}"' in html
        assert f'data-copy="{second["url"]}"' in html
        assert "onclick" not in html  # wired up by admin.js, as the CSP requires

    def test_total_space_and_count(self, auth_client):
        upload(auth_client, "a.png", make_png(1024))  # magic + 1024 bytes
        upload(auth_client, "b.png", make_png(2048))
        response = auth_client.get("/admin")
        assert response.text.count('class="img-row"') == 2
        assert "3.0 KB" in response.text


class TestAdminDelete:
    def test_delete_removes_file_row_and_link(self, client, auth_client):
        settings = auth_client.app.state.settings
        data = upload(auth_client, "gone.png", make_png(5)).json()
        filename = data["filename"]
        assert client.get("/" + filename).status_code == 200

        response = auth_client.post(
            "/admin/delete", data={"filename": filename}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin?page=1"

        assert client.get("/" + filename).status_code == 404
        assert not (settings.images_dir / filename).exists()
        assert db.get_image(settings.db_path, filename) is None

    def test_delete_keeps_other_images(self, auth_client):
        keep = upload(auth_client, "keep.png", make_png(5)).json()["filename"]
        drop = upload(auth_client, "drop.png", make_png(5)).json()["filename"]
        auth_client.post("/admin/delete", data={"filename": drop}, follow_redirects=False)
        assert db.get_image(auth_client.app.state.settings.db_path, keep) is not None

    def test_delete_invalid_filename_404(self, auth_client):
        for bad in ["../../etc/passwd", "nonsense.png", "", "image-x.svg"]:
            response = auth_client.post(
                "/admin/delete", data={"filename": bad}, follow_redirects=False
            )
            assert response.status_code == 404, bad

    def test_delete_unknown_valid_filename_redirects(self, auth_client):
        valid_shape = "image-" + "a" * 16 + "-" + "b" * 16 + ".png"
        response = auth_client.post(
            "/admin/delete", data={"filename": valid_shape}, follow_redirects=False
        )
        assert response.status_code == 303

    def test_delete_preserves_pagination(self, auth_client):
        _upload_many(auth_client, 12)
        rows = db.list_images(auth_client.app.state.settings.db_path, 1, 0)
        response = auth_client.post(
            "/admin/delete",
            data={"filename": rows[0]["filename"], "page": "2"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/admin?page=2"


class TestAdminClearAll:
    def test_clear_all_removes_everything(self, client, auth_client):
        settings = auth_client.app.state.settings
        data = upload(auth_client, "a.png", make_png(10)).json()
        upload(auth_client, "b.png", make_png(10))

        response = auth_client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin"

        assert db.count_images(settings.db_path) == 0
        assert list(settings.images_dir.iterdir()) == []
        assert client.get("/" + data["filename"]).status_code == 404

        page = auth_client.get("/admin")
        assert "No images yet" in page.text
        assert "0 B" in page.text

    def test_clear_all_when_empty_is_harmless(self, auth_client):
        response = auth_client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303

    # Clear all racing an upload. A file left without its row would stay
    # public while being invisible in the admin panel and never expiring,
    # so each test checks that files on disk and rows match exactly.

    def test_upload_finishing_during_clear_all_leaves_no_orphan(
        self, auth_client, monkeypatch
    ):
        settings = auth_client.app.state.settings
        for i in range(3):
            upload(auth_client, f"{i}.png", make_png(10))

        late = threading.Thread(
            target=main._save_image, args=(settings, LATE_FILENAME, make_png(20))
        )
        real_unlink = Path.unlink

        def unlink_while_an_upload_arrives(path, *args, **kwargs):
            if late.ident is None:  # first file only: start the upload now
                late.start()
                late.join(timeout=0.5)  # let it finish unless Clear all blocks it
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink_while_an_upload_arrives)
        response = auth_client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303
        late.join(timeout=10)
        assert not late.is_alive()
        _assert_files_match_rows(settings)

    def test_clear_all_during_an_upload_leaves_no_orphan(
        self, auth_client, monkeypatch
    ):
        settings = auth_client.app.state.settings
        upload(auth_client, "a.png", make_png(10))

        writing = threading.Event()
        real_write_bytes = Path.write_bytes

        def slow_write_bytes(path, data):
            writing.set()
            time.sleep(0.5)  # Clear all is requested while the file is written
            return real_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", slow_write_bytes)
        late = threading.Thread(
            target=main._save_image, args=(settings, LATE_FILENAME, make_png(20))
        )
        late.start()
        assert writing.wait(timeout=5)
        response = auth_client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303
        late.join(timeout=10)
        assert not late.is_alive()
        _assert_files_match_rows(settings)

    def test_file_that_cannot_be_deleted_keeps_its_row(self, auth_client, monkeypatch):
        settings = auth_client.app.state.settings
        stuck = upload(auth_client, "stuck.png", make_png(10)).json()["filename"]
        upload(auth_client, "other.png", make_png(11))
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == stuck:
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink)
        response = auth_client.post("/admin/clear-all", follow_redirects=False)
        assert response.status_code == 303
        # Still listed (so it can be retried), and everything else is gone.
        rows = db.list_images(settings.db_path, limit=10, offset=0)
        assert [row["filename"] for row in rows] == [stuck]
        _assert_files_match_rows(settings)


def _assert_files_match_rows(settings):
    rows = {row["filename"] for row in db.list_images(settings.db_path, limit=10**9, offset=0)}
    files = {path.name for path in settings.images_dir.iterdir()}
    assert files == rows, f"orphan files: {files - rows}, dead rows: {rows - files}"
