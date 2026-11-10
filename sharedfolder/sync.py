"""The synchronization engine: keeps the local shared folder equal to the server.

A signature is {"kind": ..., "sha256": ...} and describes what a path holds.
None means the path does not exist. The scan also keeps each file's size and
modification time, to notice a file that changes while it is read or uploaded.

The client's state.json holds:
    entries   the last synchronized server entry of each path.
    cursor    the last server event ID applied locally.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

import httpx

from .utils import (
    Problem,
    read_json,
    sha256_file,
    valid_path,
    write_json,
)
from .constants import (
    DIRECTORY_KIND,
    FILE_KIND,
    STAGING_DIRECTORY_NAME,
    STATE_SYNCING,
)
from .transport import ApiClient


class Deferred(Exception):
    """A local file changed during the cycle; the next cycle handles it."""


def local_path(root: Path, relative: str) -> Path:
    """
    Returns the full local path of a shared path.

    @param root: Path of the shared folder.
    @param relative: Shared path inside the folder.
    """
    valid_path(relative)
    if not root.is_dir():
        raise Problem("Shared folder is missing or inaccessible.")
    return root / relative


def local_signature(path: Path) -> dict | None:
    """
    Returns the signature of what is on disk at a path.

    @param path: Local path.
    """
    if not path.exists():
        return None
    if path.is_dir():
        return {"kind": DIRECTORY_KIND, "sha256": None}
    if not path.is_file():
        raise Problem(f"Unsupported item in the folder: {path}")
    return {"kind": FILE_KIND, "sha256": sha256_file(path)}


def expected_signature(entry: dict | None) -> dict | None:
    """
    Returns the signature a server entry should have.

    @param entry: Server entry, or None.
    """
    if not entry or entry.get("deleted"):
        return None
    return {"kind": entry["kind"], "sha256": entry.get("sha256")}


def depth(path: str) -> int:
    """
    Returns how many folders deep a path is.

    @param path: Relative path.
    """
    return path.count("/")


def latest_per_path(events: list[dict]) -> list[dict]:
    """
    Keeps the newest event of each path and every deletion, in the same order.
    Deletions are kept so a file replaced by a folder (or the reverse) is removed first.

    @param events: Server events in order.
    """
    newest = {}
    for event in events:
        # find the newest event for each path
        newest[event["path"]] = event["event_id"]
    result = []
    for event in events:
        # keep the newest event for each path, and all deletions
        if event["event_id"] == newest[event["path"]] or event["deleted"]:
            result.append(event)
    return result


class LocalScanner:
    """Reads the shared folder and calculates the hash of every file."""

    def __init__(self, root: Path):
        """
        Creates the scanner.

        @param root: Path of the shared folder.
        """
        self.root = root

    def scan(self) -> dict[str, dict]:
        """Returns the signature of every file and folder in the shared folder."""
        if not self.root.is_dir():
            raise Problem("Shared folder is missing. Restore it before resuming.")
        result = {}
        try:
            self._scan_directory(self.root, result)
        except FileNotFoundError:
            # A file was deleted during the scan: try again in the next cycle
            if self.root.is_dir():
                raise Deferred() from None
            raise Problem("Shared folder disappeared during scanning.") from None
        return result

    def _scan_directory(self, directory: Path, result: dict) -> None:
        """
        Adds the signatures of one folder and its subfolders to the result.

        @param directory: Folder to read.
        @param result: Dict of relative path -> signature to fill.
        """
        with os.scandir(directory) as items:
            for item in items:
                path = Path(item.path)
                relative = path.relative_to(self.root).as_posix()
                # skip the staging folder, which is not synced
                if relative == STAGING_DIRECTORY_NAME:
                    continue
                valid_path(relative)
                if item.is_dir():
                    result[relative] = {"kind": DIRECTORY_KIND, "sha256": None}
                    # go into the folder
                    self._scan_directory(path, result)
                elif item.is_file():
                    before = item.stat()
                    content_hash = sha256_file(path)
                    after = path.stat()
                    # The file changed while it was hashed, defer and scan it again next cycle
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        raise Deferred()
                    result[relative] = {
                        "kind": FILE_KIND,
                        "sha256": content_hash,
                        "size": after.st_size,
                        "mtime_ns": after.st_mtime_ns,
                    }
                else:
                    raise Problem(f"Unsupported item in the folder: {relative}")


class SyncEngine:
    """Synchronizes one shared folder with the server."""

    def __init__(self, profile_dir: Path, config: dict, progress: Callable | None = None):
        """
        Loads the saved state and connects to the server.

        @param profile_dir: Client's profile folder.
        @param config: Client settings.
        @param progress: Called during uploads and downloads.
        """
        self.directory = profile_dir.resolve()
        self.config = config
        self.root = Path(self.config["sync_root"])
        self.state_path = self.directory / "state.json"
        self.state = read_json(self.state_path, {"entries": {}, "cursor": 0})
        self.api = ApiClient(self.config["server_url"], self.config["client_id"], progress)
        self.scanner = LocalScanner(self.root)
        self.last_sync: float | None = None
        self._save()

    def close(self) -> None:
        """Closes the connection to the server."""
        self.api.close()

    def _save(self) -> None:
        """Saves the state to state.json."""
        write_json(self.state_path, self.state)

    def sync_once(self) -> list[dict]:
        """Runs one synchronization cycle and returns the synchronized entries."""
        self.api.presence(self.config["display_name"], STATE_SYNCING)
        # Send local changes first, so they are not overwritten by remote ones
        self._publish_local_changes(self.scanner.scan())
        self._consume_changes()
        self.last_sync = time.time()
        return self.entries()

    def restore(self, path: str, revision: int) -> dict:
        """
        Makes an earlier revision current again, as a new revision.

        @param path: Relative path to restore.
        @param revision: Revision to restore.
        """
        self.sync_once()
        result = self.api.request("POST", "restore", params={"path": path, "revision": revision})
        self._apply(result["entry"])
        return result

    def entries(self) -> list[dict]:
        """Returns the last synchronized entry of every path."""
        return list(self.state["entries"].values())

    def _publish_local_changes(self, local: dict) -> None:
        """
        Sends local deletions (children first), then creations and updates (parents first).

        @param local: Result of the last folder scan.
        """
        for path in sorted(self._removed_paths(local), key=depth, reverse=True):
            kind = self.state["entries"][path]["kind"]
            endpoint = "directories" if kind == DIRECTORY_KIND else "files"
            answer = self.api.request("DELETE", endpoint, params={"path": path})
            self._save_answer(path, answer)
        for path in sorted(local, key=depth):
            item = local[path]
            current = expected_signature(self.state["entries"].get(path))
            signature = {
                "kind": item["kind"],
                "sha256": item.get("sha256")
            }
            # skip unchanged items
            if signature == current:
                continue
            if item["kind"] == DIRECTORY_KIND:
                answer = self.api.request("POST", "directories", params={"path": path})
                self._save_answer(path, answer)
            else:
                self._upload(path, item)

    def _removed_paths(self, local: dict) -> list[str]:
        """
        Returns synchronized paths that were deleted locally or changed type.

        @param local: Result of the last folder scan.
        """
        removed = []
        for path, entry in self.state["entries"].items():
            if entry["deleted"]:
                continue
            current = local.get(path)
            if current is None or current["kind"] != entry["kind"]:
                removed.append(path)
        return removed

    def _upload(self, path: str, item: dict) -> None:
        """
        Uploads a local file to the server.

        @param path: Relative path of the file.
        @param item: File information from the last scan.
        """
        source = local_path(self.root, path)
        try:
            current = source.stat()
            # The file changed since the scan, send it in the next cycle
            if current.st_size != item["size"] or current.st_mtime_ns != item["mtime_ns"]:
                raise Deferred()
            answer = self.api.upload(path, source, item["sha256"], item["size"])
        except FileNotFoundError:
            # the file was deleted after the scan, the next scan sends the deletion
            raise Deferred() from None
        except httpx.LocalProtocolError:
            # the file changed size while it was being uploaded
            raise Deferred() from None
        except Problem as error:
            if error.status != 422:
                raise
            # the file changed during upload, so the hash did not match, send it again next cycle
            raise Deferred() from None
        self._save_answer(path, answer)

    def _save_answer(self, path: str, answer: dict) -> None:
        """
        Updates the local sync state with the server's response to a local change.

        @param path: Relative path of the change.
        @param answer: Server answer with the new entry and any new parent folders.
        """
        for entry in answer["entries"]:
            self.state["entries"][entry["path"]] = entry
        # update the path of the modified item in case it exists
        if answer["entry"]:
            self.state["entries"][path] = answer["entry"]
        else:
            # the server doesn't contain the path we requested to modify
            self.state["entries"].pop(path, None)
        self._save()

    def _consume_changes(self) -> None:
        """Downloads new server events and applies them in order."""
        # get all events since the last sync
        events = self.api.items("changes", {"after_event_id": self.state["cursor"]})
        for entry in latest_per_path(events):
            self._apply(entry)
            self.state["cursor"] = entry["event_id"]
            self._save()

    def _apply(self, entry: dict) -> None:
        """
        Applies a server entry locally without overwriting unsent local changes.

        @param entry: Server entry to apply.
        """
        path = entry["path"]
        previous_entry = self.state["entries"].get(path)
        # ignore events that were already applied
        if previous_entry and previous_entry["revision"] >= entry["revision"]:
            return
        # don't overwrite a local change that was not sent yet
        target = local_path(self.root, path)
        if local_signature(target) != expected_signature(previous_entry):
            raise Deferred()
        staged_file = None
        if not entry["deleted"] and entry["kind"] == FILE_KIND:
            staged_file = self._stage_download(entry)
        self._perform(entry, staged_file)

    def _stage_download(self, entry: dict) -> Path:
        """
        Downloads a file into the staging folder and returns its path.

        @param entry: Server entry of the file.
        """
        staging = self.root / STAGING_DIRECTORY_NAME
        staging.mkdir(exist_ok=True)
        staged_file = staging / uuid4().hex
        try:
            self.api.download(entry, staged_file)
        except BaseException:
            staged_file.unlink(missing_ok=True)
            raise
        return staged_file

    def _perform(self, entry: dict, staged_file: Path | None) -> None:
        """
        Changes the local path and saves the entry as synchronized.

        @param entry: Server entry to apply.
        @param staged_file: Downloaded file, for file entries.
        """
        target = local_path(self.root, entry["path"])
        if entry["deleted"]:
            if target.is_dir():
                target.rmdir()
            elif target.exists():
                target.unlink()
        elif entry["kind"] == DIRECTORY_KIND:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Replace the file in one step
            os.replace(staged_file, target)
        self.state["entries"][entry["path"]] = entry
        self._save()
