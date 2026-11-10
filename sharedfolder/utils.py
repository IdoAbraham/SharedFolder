"""Helpers used by more than one module: the error type, validation,
JSON files and hashing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .constants import (
    MAX_DISPLAY_NAME_LENGTH,
    STAGING_DIRECTORY_NAME,
    TRANSFER_CHUNK_BYTES,
)


class Problem(Exception):
    """An expected error with a message for the user and an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        """
        Creates the error.

        @param message: Text shown to the user.
        @param status: HTTP status code the server returns for it.
        """
        super().__init__(message)
        self.message = message
        self.status = status


def valid_path(value: str) -> str:
    """
    Checks that a path is a relative path inside the shared folder and returns it.
    Rejects empty parts, "." and "..", and the client's staging folder.

    @param value: Path that uses "/" between its parts.
    """
    if not isinstance(value, str):
        raise Problem("Expected a relative path inside the shared folder.")
    for part in value.split("/"):
        if part in ("", ".", "..", STAGING_DIRECTORY_NAME):
            raise Problem("Expected a relative path inside the shared folder.")
    return value


def valid_display_name(name) -> str:
    """
    Checks that a display name has 1-80 characters and returns it trimmed.

    @param name: Display name entered by the user.
    """
    if name is None:
        name = ""
    name = str(name).strip()
    if not name or len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise Problem("Display name must contain 1–80 characters.")
    return name


def write_json(path: Path, value) -> None:
    """
    Saves a value as a JSON file.

    @param path: Path of the JSON file.
    @param value: Value to save.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read_json(path: Path, default=None):
    """
    Reads a JSON file.

    @param path: Path of the JSON file.
    @param default: Value returned when the file does not exist.
    """
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def read_chunks(stream):
    """
    Reads an open file in fixed-size chunks.

    @param stream: File opened in binary mode.
    """
    while True:
        chunk = stream.read(TRANSFER_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def sha256_file(path: Path) -> str:
    """
    Calculates the SHA-256 hash of a file.

    @param path: Path of the file.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in read_chunks(stream):
            hasher.update(chunk)
    return hasher.hexdigest()
