"""Server storage: an SQLite database plus the content files (blobs)."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

from .utils import Problem, valid_path
from .constants import (
    ACTION_CREATED,
    ACTION_DELETED,
    ACTION_RESTORED,
    ACTION_UPDATED,
    DIRECTORY_KIND,
    FILE_KIND,
    OP_CREATE_DIRECTORY,
    OP_DELETE_DIRECTORY,
    OP_DELETE_FILE,
    OP_RESTORE,
    OP_UPLOAD,
    PRESENCE_EXPIRY_SECONDS,
)

SCHEMA = """
    CREATE TABLE IF NOT EXISTS clients(
        client_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
        last_ip TEXT, last_seen REAL, reported_sync_state TEXT);
    CREATE TABLE IF NOT EXISTS versions(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL, revision INTEGER NOT NULL,
        kind TEXT NOT NULL, deleted INTEGER NOT NULL,
        blob_id TEXT, sha256 TEXT, size INTEGER,
        action TEXT NOT NULL, client_id TEXT NOT NULL,
        client_name TEXT NOT NULL, server_time REAL NOT NULL,
        UNIQUE(path, revision));
    CREATE INDEX IF NOT EXISTS versions_path ON versions(path, event_id);
"""


class Store:
    """Holds the server's storage folder: the content files and the database."""

    def __init__(self, directory: Path):
        """
        Opens the storage folder and the database.

        @param directory: Server storage folder.
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs = self.directory / "blobs"
        self.temporary = self.directory / "temporary"
        self.blobs.mkdir(exist_ok=True)
        self.temporary.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / "metadata.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # No client is online when the server starts
        self.db.execute("UPDATE clients SET last_seen=0")
        self.db.commit()
        self._database_lock = threading.Lock()

    def call(self, method, *args, **kwargs):
        """
        Runs one storage operation on this store, one at a time. A failed change is rolled back.

        @param method: Store method to run.
        @param args: Arguments for the method.
        """
        with self._database_lock:
            try:
                return method(self, *args, **kwargs)
            except Exception:
                self.db.rollback()
                raise

    def close(self) -> None:
        """Closes the database."""
        with self._database_lock:
            self.db.close()

    def presence(self, client_id: str, name: str, state: str, ip: str) -> dict:
        """
        Saves that a client is online.

        @param client_id: Client UUID.
        @param name: Display name.
        @param state: Reported synchronization state.
        @param ip: Client IP address.
        """
        self.db.execute(
            "INSERT OR REPLACE INTO clients VALUES(?,?,?,?,?)",
            (client_id, name, ip, time.time(), state),
        )
        self.db.commit()
        return {"client_id": client_id}

    def client(self, client_id: str) -> dict:
        """
        Returns a registered client. Fails if it never sent presence.

        @param client_id: Client UUID.
        """
        row = self.db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            raise Problem("Send a presence request first.", 404)
        return dict(row)

    def clients(self, caller: str, include_offline: bool = False) -> dict:
        """
        Returns the other online clients, or every known client.

        @param caller: UUID of the client asking (left out of the online list).
        @param include_offline: Return every client that was ever seen.
        """
        if include_offline:
            rows = self.db.execute("SELECT * FROM clients ORDER BY client_id")
        else:
            rows = self.db.execute(
                "SELECT * FROM clients WHERE client_id!=? AND last_seen>? ORDER BY client_id",
                (caller, time.time() - PRESENCE_EXPIRY_SECONDS),
            )
        return {"items": [dict(row) for row in rows]}

    def current(self, path: str) -> dict | None:
        """
        Returns the latest version of a path, including a deletion.

        @param path: Relative path.
        """
        row = self.db.execute(
            "SELECT * FROM versions WHERE path=? ORDER BY revision DESC LIMIT 1", (path,)
        ).fetchone()
        return dict(row) if row else None

    def version(self, path: str, revision: int) -> dict:
        """
        Returns one revision of a path.

        @param path: Relative path.
        @param revision: Revision number.
        """
        row = self.db.execute(
            "SELECT * FROM versions WHERE path=? AND revision=?", (valid_path(path), revision)
        ).fetchone()
        if row is None:
            raise Problem("Version not found.", 404)
        return dict(row)

    def changes(self, after_event_id: int = 0) -> dict:
        """
        Returns the events after an event ID, oldest first.

        @param after_event_id: Last event the client already has.
        """
        rows = self.db.execute(
            "SELECT * FROM versions WHERE event_id>? ORDER BY event_id", (after_event_id,)
        )
        return {"items": [dict(row) for row in rows]}

    def history(self, path: str) -> dict:
        """
        Returns all versions of a path, newest first.

        @param path: Relative path.
        """
        rows = self.db.execute(
            "SELECT * FROM versions WHERE path=? ORDER BY revision DESC", (valid_path(path),)
        )
        return {"items": [dict(row) for row in rows]}

    def events(self, path: str = "", clients: list[str] | None = None,
               actions: list[str] | None = None) -> dict:
        """
        Returns the activity log, newest first. A row must match every filter that is given.

        @param path: Text the path must contain.
        @param clients: Client UUIDs to include (any of them).
        @param actions: Actions to include (any of them).
        """
        rows = self.db.execute("SELECT * FROM versions ORDER BY event_id DESC")
        items = []
        for row in rows:
            if path and path not in row["path"]:
                continue
            if clients and row["client_id"] not in clients:
                continue
            if actions and row["action"] not in actions:
                continue
            items.append(dict(row))
        return {"items": items}

    def mutate(self, client_id: str, operation: str, path: str, blob: str | None = None,
               sha: str | None = None, size: int | None = None, source: int | None = None) -> dict:
        """
        Applies one change in a single transaction and returns the new state.

        @param client_id: UUID of the client making the change.
        @param operation: One of the OP_* names.
        @param path: Relative path.
        @param blob: Uploaded temporary file, for uploads.
        @param sha: SHA-256 hash of the upload.
        @param size: Size of the upload in bytes.
        @param source: Revision to restore, for restores.
        """
        path = valid_path(path)
        client = self.client(client_id)
        entries = self._apply(operation, path, client, blob, sha, size, source)
        result = {"entry": self.current(path), "entries": entries}
        self.db.commit()
        return result

    def _apply(self, operation, path, client, blob, sha, size, source) -> list[dict]:
        """
        Records a change without committing it, and returns the new entries (empty if nothing changed).

        @param operation: One of the OP_* names.
        @param path: Relative path.
        @param client: Client row.
        @param blob: Uploaded temporary file, for uploads.
        @param sha: SHA-256 hash of the content.
        @param size: Size of the content in bytes.
        @param source: Revision to restore, for restores.
        """
        current = self.current(path)
        is_live = bool(current and not current["deleted"])
        is_directory = operation in (OP_CREATE_DIRECTORY, OP_DELETE_DIRECTORY)
        kind = DIRECTORY_KIND if is_directory else FILE_KIND
        restored = None
        if operation == OP_RESTORE:
            restored = self.version(path, source)
            if restored["deleted"]:
                raise Problem("Choose a version containing a file or directory.")
            kind = restored["kind"]
            blob, sha, size = restored["blob_id"], restored["sha256"], restored["size"]
        if is_live and current["kind"] != kind:
            raise Problem("Existing path has a different type.")

        if operation in (OP_DELETE_FILE, OP_DELETE_DIRECTORY):
            if not is_live:
                return []
            if kind == DIRECTORY_KIND:
                self._require_empty_directory(path)
            return [self._record(path, kind, True, ACTION_DELETED, client)]
        if operation == OP_CREATE_DIRECTORY and is_live:
            return []
        if operation == OP_UPLOAD and is_live and current["sha256"] == sha:
            return []  # Same content as now: no new version

        entries = self._create_missing_parents(path, client)
        if operation == OP_UPLOAD:
            blob = self._publish_blob(blob)
        if restored:
            action = ACTION_RESTORED
        elif is_live:
            action = ACTION_UPDATED
        else:
            action = ACTION_CREATED
        entries.append(self._record(path, kind, False, action, client, blob, sha, size))
        return entries

    def _require_empty_directory(self, path: str) -> None:
        """
        Fails if a folder still has files or folders in it.

        @param path: Relative path of the folder.
        """
        # The latest version of every path inside the folder decides if it still exists
        latest = {}
        for row in self.db.execute("SELECT path, deleted FROM versions ORDER BY event_id"):
            if row["path"].startswith(path + "/"):
                latest[row["path"]] = row["deleted"]
        for deleted in latest.values():
            if not deleted:
                raise Problem("Delete children before their directory.")

    def _create_missing_parents(self, path: str, client: dict) -> list[dict]:
        """
        Creates the parent folders of a path that do not exist yet.

        @param path: Relative path.
        @param client: Client row.
        """
        entries = []
        components = path.split("/")
        for count in range(1, len(components)):
            parent = "/".join(components[:count])
            current = self.current(parent)
            if current and not current["deleted"]:
                if current["kind"] != DIRECTORY_KIND:
                    raise Problem(f"Parent is a file: {parent}")
            else:
                entries.append(self._record(parent, DIRECTORY_KIND, False, ACTION_CREATED, client))
        return entries

    def _publish_blob(self, uploaded_file: str) -> str:
        """
        Moves a checked upload into the content folder and returns its blob ID.

        @param uploaded_file: Temporary file of the upload.
        """
        blob_id = uuid4().hex
        os.replace(uploaded_file, self.blobs / blob_id)
        return blob_id

    def _record(self, path, kind, deleted, action, client, blob=None, sha=None, size=None) -> dict:
        """
        Adds a new revision of a path, which becomes its current version.

        @param path: Relative path.
        @param kind: FILE_KIND or DIRECTORY_KIND.
        @param deleted: Whether this revision is a deletion.
        @param action: One of the ACTION_* names.
        @param client: Client row.
        @param blob: Blob ID of the content.
        @param sha: SHA-256 hash of the content.
        @param size: Size of the content in bytes.
        """
        current = self.current(path)
        revision = current["revision"] + 1 if current else 1
        self.db.execute(
            "INSERT INTO versions(path,revision,kind,deleted,blob_id,sha256,size,"
            "action,client_id,client_name,server_time) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (path, revision, kind, int(deleted), blob, sha, size, action,
             client["client_id"], client["display_name"], time.time()),
        )
        return self.current(path)
