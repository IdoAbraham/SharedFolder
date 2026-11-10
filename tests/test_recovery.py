from uuid import uuid4

from sharedfolder.constants import OP_CREATE_DIRECTORY
from sharedfolder.storage import Store


def test_server_restart(tmp_path):
    """After a server restart the versions are kept and no client is online."""
    client_id = str(uuid4())
    store = Store(tmp_path)
    store.presence(client_id, "A", "idle", "127.0.0.1")
    store.mutate(client_id, OP_CREATE_DIRECTORY, "docs")
    store.close()

    store = Store(tmp_path)  # The server starts again on the same folder
    changes = store.changes()["items"]
    assert len(changes) == 1
    assert changes[0]["path"] == "docs"
    assert store.clients(str(uuid4()))["items"] == []
    store.close()
