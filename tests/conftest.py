import socket
import threading
import time

import pytest
import uvicorn

from sharedfolder.config import configure
from sharedfolder.sync import SyncEngine
from sharedfolder.server import create_app


@pytest.fixture
def server(tmp_path):
    """Runs a real server on a free localhost port and returns its URL."""
    app = create_app(tmp_path / "server")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    instance = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not instance.started:
        assert thread.is_alive(), "Test server failed to start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(10)
    sock.close()
    app.state.store.close()


def make_client(tmp_path, server, name):
    """
    Creates a client with its own folders.

    @param tmp_path: Temporary folder of the test.
    @param server: Server URL.
    @param name: Client name.
    """
    profile = tmp_path / (name + "-profile")
    root = tmp_path / name
    return SyncEngine(profile, configure(profile, server, root, name))


@pytest.fixture
def clients(tmp_path, server):
    """Two clients, A and B, connected to the test server."""
    a = make_client(tmp_path, server, "A")
    b = make_client(tmp_path, server, "B")
    yield a, b
    a.close()
    b.close()
