import os

import httpx
import pytest

from sharedfolder.utils import Problem


def sync_clients(first, second):
    """
    Runs two sync cycles on both clients, first before second.

    @param first: Client that syncs first.
    @param second: Client that syncs second.
    """
    for _ in range(2):
        first.sync_once()
        second.sync_once()


def unreachable_upload(path, source, content_hash, size):
    """
    Fails like an unreachable server.

    @param path: Relative path of the file.
    @param source: Local file to send.
    @param content_hash: SHA-256 hash of the file.
    @param size: File size in bytes.
    """
    raise httpx.ConnectError("server unreachable")


def test_file_sync_between_two_clients(clients):
    """A file created on one client reaches the other client."""
    a, b = clients
    (a.root / "notes.txt").write_text("hello")
    sync_clients(a, b)
    assert (b.root / "notes.txt").read_text() == "hello"
    # B does not send the downloaded file back as a new version
    assert len(a.api.items("history", {"path": "notes.txt"})) == 1


def test_file_update(clients):
    """A change to a file reaches the other client."""
    a, b = clients
    (a.root / "notes.txt").write_text("first")
    sync_clients(a, b)
    (b.root / "notes.txt").write_text("second")
    sync_clients(b, a)
    assert (a.root / "notes.txt").read_text() == "second"


def test_file_delete(clients):
    """Deleting a file is synchronized to the other client."""
    a, b = clients
    (a.root / "notes.txt").write_text("hello")
    sync_clients(a, b)
    (a.root / "notes.txt").unlink()
    sync_clients(a, b)
    assert not (b.root / "notes.txt").exists()


def test_directory_create_and_delete(clients):
    """Empty nested folders are created and deleted on the other client."""
    a, b = clients
    (a.root / "docs" / "empty").mkdir(parents=True)
    sync_clients(a, b)
    assert (b.root / "docs" / "empty").is_dir()
    (a.root / "docs" / "empty").rmdir()
    (a.root / "docs").rmdir()
    sync_clients(a, b)
    assert not (b.root / "docs").exists()


def test_empty_file(clients):
    """An empty file is synchronized."""
    a, b = clients
    (a.root / "empty.txt").touch()
    sync_clients(a, b)
    assert (b.root / "empty.txt").read_bytes() == b""


def test_unicode_filename(clients):
    """A file with a Hebrew name is synchronized."""
    a, b = clients
    (a.root / "שלום.txt").write_text("hello")
    sync_clients(a, b)
    assert (b.root / "שלום.txt").read_text() == "hello"


def test_binary_file(clients):
    """A binary file arrives with exactly the same bytes."""
    a, b = clients
    data = os.urandom(25600)
    (a.root / "image.bin").write_bytes(data)
    sync_clients(a, b)
    assert (b.root / "image.bin").read_bytes() == data


def test_restore_old_version(clients):
    """Restoring an old version brings its content back on every client."""
    a, b = clients
    (a.root / "r").write_text("one")
    sync_clients(a, b)
    (a.root / "r").write_text("two")
    sync_clients(a, b)
    a.restore("r", 1)
    sync_clients(a, b)
    assert (b.root / "r").read_text() == "one"
    history = b.api.items("history", {"path": "r"})
    assert history[0]["action"] == "Restored"
    assert history[1]["action"] == "Updated"
    assert history[2]["action"] == "Created"


def test_last_update_wins(clients):
    """When two clients edit the same file, the edit the server accepts last wins."""
    a, b = clients
    (a.root / "report").write_text("start")
    sync_clients(a, b)
    (a.root / "report").write_text("A edit")
    (b.root / "report").write_text("B edit")
    a.sync_once()  # A's edit reaches the server first
    b.sync_once()  # B's edit reaches the server last
    a.sync_once()
    assert (a.root / "report").read_text() == "B edit"
    # Both edits are kept in the history
    assert len(a.api.items("history", {"path": "report"})) == 3


def test_offline_client_gets_latest_version(clients):
    """A client that did not sync for a while gets the latest version."""
    a, b = clients
    (a.root / "r").write_text("one")
    sync_clients(a, b)
    for text in ("two", "three", "four"):
        (a.root / "r").write_text(text)
        a.sync_once()
    b.sync_once()
    assert (b.root / "r").read_text() == "four"


def test_offline_edit_sent_on_reconnect(clients):
    """An edit made while a client was offline is sent when it syncs again."""
    a, b = clients
    (a.root / "r").write_text("start")
    sync_clients(a, b)
    (b.root / "r").write_text("offline edit")
    (a.root / "other").write_text("new on A")
    a.sync_once()
    sync_clients(b, a)
    assert (a.root / "r").read_text() == "offline edit"
    assert (b.root / "other").read_text() == "new on A"


def test_network_error_retries(clients):
    """A change that failed to upload because of a network error is sent in the next cycle."""
    a, b = clients
    (a.root / "r").write_text("saved while the server was unreachable")
    original_upload = a.api.upload
    a.api.upload = unreachable_upload
    with pytest.raises(httpx.ConnectError):
        a.sync_once()
    a.api.upload = original_upload
    sync_clients(a, b)
    assert (b.root / "r").read_text() == "saved while the server was unreachable"


def test_missing_shared_folder(clients):
    """A missing shared folder stops the sync and deletes nothing on the server."""
    a, b = clients
    (a.root / "r").write_text("keep")
    a.sync_once()
    a.root.rename(a.root.parent / "moved")
    with pytest.raises(Problem, match="missing"):
        a.sync_once()
    for event in a.api.items("changes"):
        assert not event["deleted"]
