import hashlib
from uuid import uuid4

import httpx
import pytest

from sharedfolder.utils import Problem, valid_path
from sharedfolder.transport import ApiClient


@pytest.fixture
def api(server):
    """A registered API client for the test server."""
    client = ApiClient(server, str(uuid4()))
    client.presence("Test PC")
    yield client
    client.close()


def put(api, path, data=b"x"):
    """
    Uploads bytes as a file.

    @param api: API client.
    @param path: Relative path.
    @param data: File content.
    """
    headers = {"Content-Length": str(len(data)), "X-Content-SHA256": hashlib.sha256(data).hexdigest()}
    return api.request("PUT", "content", params={"path": path}, content=data, headers=headers)


def download(api, path, revision):
    """
    Downloads one revision of a file and returns its content.

    @param api: API client.
    @param path: Relative path.
    @param revision: Revision number.
    """
    return api.http.get("content", params={"path": path, "revision": revision}).content


def test_bad_path():
    """Paths that leave the shared folder, absolute paths and empty paths are rejected."""
    for path in ("../x", "a/../x", "/x", ""):
        with pytest.raises(Problem):
            valid_path(path)


def test_client_id_required(server):
    """A request without a client ID is rejected."""
    response = httpx.get(server + "/api/v1/clients", trust_env=False)  # Ignore the computer's proxy settings
    assert response.status_code == 400


def test_upload_with_wrong_hash(api):
    """An upload whose hash does not match is rejected and not saved."""
    with pytest.raises(Problem) as error:
        api.request("PUT", "content", params={"path": "bad"}, content=b"x",
                    headers={"X-Content-SHA256": "wrong"})
    assert error.value.status == 422
    assert api.items("changes") == []


def test_upload_and_download(api):
    """Uploaded files are saved, and every revision can be downloaded."""
    put(api, "r", b"first")
    put(api, "r", b"second")
    assert download(api, "r", 1) == b"first"
    assert download(api, "r", 2) == b"second"


def test_same_upload_no_new_revision(api):
    """Uploading the same content again does not add a revision."""
    put(api, "r", b"same")
    put(api, "r", b"same")
    assert len(api.items("history", {"path": "r"})) == 1


def test_history(api):
    """The history lists every revision of a file, newest first."""
    put(api, "r", b"one")
    put(api, "r", b"two")
    history = api.items("history", {"path": "r"})
    assert history[0]["action"] == "Updated"
    assert history[1]["action"] == "Created"


def test_change_feed(api):
    """The change feed returns only the events after a given event ID."""
    put(api, "a")
    events = api.items("changes", {"after_event_id": 0})
    assert len(events) == 1
    assert events[0]["path"] == "a"
    put(api, "b")
    events = api.items("changes", {"after_event_id": events[0]["event_id"]})
    assert len(events) == 1
    assert events[0]["path"] == "b"


def test_file_and_directory_same_path(api):
    """A file cannot be uploaded where a folder already exists."""
    put(api, "d/r")  # Also creates the folder d
    with pytest.raises(Problem):
        put(api, "d")


def test_delete_non_empty_directory(api):
    """A folder that still has a file in it cannot be deleted."""
    put(api, "d/r")
    with pytest.raises(Problem):
        api.request("DELETE", "directories", params={"path": "d"})


def test_activity_filter(api):
    """The activity log can be filtered by action and file name."""
    put(api, "a", b"one")
    put(api, "a", b"two")
    put(api, "b")
    events = api.items("events", [("action", "Created"), ("path", "a")])
    assert len(events) == 1
    assert events[0]["path"] == "a"


def test_clients_list(api, server):
    """A client that sent presence appears in the list of online clients."""
    other = ApiClient(server, str(uuid4()))
    other.presence("Other", "paused")
    rows = api.items("clients")
    assert len(rows) == 1
    assert rows[0]["reported_sync_state"] == "paused"
    other.close()
