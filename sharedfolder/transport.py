"""HTTP client for the server API. Used only from background threads."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Iterator

import httpx

from .utils import Problem, read_chunks
from .constants import (
    CLIENT_ID_HEADER,
    CONNECT_TIMEOUT_SECONDS,
    CONTENT_HASH_HEADER,
    STATE_IDLE,
    TRANSFER_CHUNK_BYTES,
    TRANSFER_TIMEOUT_SECONDS,
)


class ApiClient:
    """Sends requests to the server in the name of one client."""

    def __init__(self, url: str, client_id: str, progress: Callable | None = None):
        """
        Opens the HTTP connection pool.

        @param url: Server address.
        @param client_id: This client's UUID, sent with every request.
        @param progress: Called as progress (operation, path, bytes_done, total) during transfers.
        """
        self.http = httpx.Client(
            base_url=url.rstrip("/") + "/api/v1/",
            headers={CLIENT_ID_HEADER: client_id},
            timeout=httpx.Timeout(TRANSFER_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
            trust_env=False,
        )
        self.progress = progress

    def close(self) -> None:
        """Closes the HTTP connections."""
        self.http.close()

    def request(self, method: str, endpoint: str, **kwargs):
        """
        Sends a request and returns the JSON answer.

        @param method: HTTP method, such as "GET".
        @param endpoint: Endpoint name, such as "changes".
        @param kwargs: Extra arguments for httpx (params, json, ...).
        """
        response = self.http.request(method, endpoint, **kwargs)
        return self._check(response).json()

    def presence(self, name: str, state: str = STATE_IDLE) -> dict:
        """
        Tells the server that the current client is online.

        @param name: Display name.
        @param state: Current synchronization state.
        """
        return self.request("POST", "presence", json={"display_name": name, "sync_state": state})

    def items(self, endpoint: str, params=None) -> list:
        """
        Returns the "items" list of a GET endpoint.

        @param endpoint: Endpoint name, such as "history".
        @param params: Query parameters.
        """
        response = self.request("GET", endpoint, params=params)
        return response["items"]

    def upload(self, path: str, source: Path, content_hash: str, size: int) -> dict:
        """
        Uploads a file to the server.

        @param path: Relative path of the file.
        @param source: Local file to send.
        @param content_hash: SHA-256 hash of the file.
        @param size: File size in bytes.
        """
        with source.open("rb") as stream:
            return self.request(
                "PUT",
                "content",
                params={"path": path},
                # use a generator to send the file in chunks instead of loading it all into memory
                content=self._upload_chunks(path, stream, size),
                headers={"Content-Length": str(size), CONTENT_HASH_HEADER: content_hash},
            )

    def download(self, entry: dict, destination) -> None:
        """
        Downloads one revision of a file and checks its size and hash.

        @param entry: Server entry with path, revision, size and sha256.
        @param destination: Local file to write.
        """
        received_bytes = 0
        hasher = hashlib.sha256()
        # stream the file instead of loading the whole response into memory
        with self.http.stream(
            "GET", "content", params={"path": entry["path"], "revision": entry["revision"]}
        ) as response:
            if response.is_error:
                # read the error body so _check can show its message
                response.read()
            self._check(response)
            with destination.open("wb") as output:
                for chunk in response.iter_bytes(TRANSFER_CHUNK_BYTES):
                    received_bytes += len(chunk)
                    # calculate the hash while downloading
                    hasher.update(chunk)
                    output.write(chunk)
                    if self.progress:
                        self.progress("Downloading", entry["path"], received_bytes, entry["size"])
        # make sure the downloaded file matches the version described by the server
        if received_bytes != entry["size"] or hasher.hexdigest() != entry["sha256"]:
            raise Problem("Downloaded revision failed verification.", 422)

    def _upload_chunks(self, path: str, stream, size: int) -> Iterator[bytes]:
        """
        Yields the file in chunks and reports the upload progress.

        @param path: Relative path of the file.
        @param stream: Open file to send.
        @param size: File size in bytes.
        """
        sent_bytes = 0
        for chunk in read_chunks(stream):
            sent_bytes += len(chunk)
            if self.progress:
                self.progress("Uploading", path, sent_bytes, size)
            yield chunk

    @staticmethod
    def _check(response: httpx.Response) -> httpx.Response:
        """
        Raises the server's error as a Problem in case of HTTP error,
        otherwise returns the response.

        @param response: HTTP response to check.
        """
        if response.is_error:
            try:
                data = response.json()
            except ValueError:
                data = {}
            # return the error message from the server, if any. Otherwise, use the HTTP status code.
            error_msg = data.get("message", f"HTTP {response.status_code}")
            raise Problem(error_msg, response.status_code)
        return response
