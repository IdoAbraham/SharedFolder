"""HTTP API server. Run with: python -m sharedfolder.server"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .utils import Problem, valid_display_name, valid_path
from .constants import (
    CLIENT_ID_HEADER,
    CONTENT_HASH_HEADER,
    FILE_KIND,
    OP_CREATE_DIRECTORY,
    OP_DELETE_DIRECTORY,
    OP_DELETE_FILE,
    OP_RESTORE,
    OP_UPLOAD,
    SYNC_STATES,
)
from .storage import Store

LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


def create_app(storage: Path) -> FastAPI:
    """
    Builds the API and opens the server storage.

    @param storage: Server storage folder.
    """
    app = FastAPI(title="SharedFolder")
    app.state.store = Store(storage)
    _register_error_handlers(app)
    app.include_router(router)
    return app


async def run_in_store(request: Request, method, *args, **kwargs):
    """
    Runs a storage operation on a worker thread, so other requests keep going.

    @param request: Current request.
    @param method: Store method to run.
    @param args: Arguments for the method.
    """
    return await asyncio.to_thread(request.app.state.store.call, method, *args, **kwargs)


def get_client_id(request: Request) -> str:
    """
    Returns the caller's client ID from the X-Client-ID header, in standard UUID form.
    A request without a valid client ID is rejected.

    @param request: Current request.
    """
    try:
        return str(UUID(request.headers.get(CLIENT_ID_HEADER, "")))
    except ValueError:
        raise Problem("A UUID X-Client-ID header is required.") from None


async def read_metadata(request: Request) -> dict:
    """
    Reads a JSON object from the request body.

    @param request: Current request.
    """
    try:
        value = await request.json()
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise Problem("Expected a JSON object.", 422)
    return value


def upload_headers(request: Request) -> tuple[int, str]:
    """
    Returns the size and SHA-256 hash the client declared for an upload.

    @param request: Current request.
    """
    length = request.headers.get("Content-Length", "")
    if not length.isdigit():
        raise Problem("A valid Content-Length is required.")
    return int(length), request.headers.get(CONTENT_HASH_HEADER, "")


async def receive_upload(request: Request, destination: Path, length: int, content_hash: str) -> None:
    """
    Saves the uploaded body to a file and checks its size and hash.

    @param request: Current request.
    @param destination: Temporary file to write.
    @param length: Declared size in bytes.
    @param content_hash: Declared SHA-256 hash.
    """
    received_bytes, hasher = 0, hashlib.sha256()
    with destination.open("wb") as output:
        async for chunk in request.stream():
            received_bytes += len(chunk)
            if received_bytes > length:
                break
            output.write(chunk)
            hasher.update(chunk)
    if received_bytes != length or hasher.hexdigest() != content_hash:
        raise Problem("Size or hash verification failed.", 422)


@router.post("/presence")
async def presence(request: Request):
    """Registers the client as online, with its name and state."""
    client_id = get_client_id(request)
    data = await read_metadata(request)
    name = valid_display_name(data.get("display_name"))
    state = data.get("sync_state")
    if state not in SYNC_STATES:
        raise Problem("Unknown synchronization state.")
    address = request.client.host
    return await run_in_store(request, Store.presence, client_id, name, state, address)


@router.get("/clients")
async def clients(request: Request, include_offline: bool = False):
    """Returns the other online clients, or every known client."""
    caller = get_client_id(request)
    return await run_in_store(request, Store.clients, caller, include_offline)


@router.get("/changes")
async def changes(request: Request, after_event_id: int = 0):
    """Returns the events after a given event ID."""
    get_client_id(request)
    if after_event_id < 0:
        raise Problem("Event ID must be nonnegative.")
    return await run_in_store(request, Store.changes, after_event_id)


@router.put("/content")
async def upload(request: Request, path: str):
    """Receives a new version of a file."""
    client_id = get_client_id(request)
    valid_path(path)
    length, content_hash = upload_headers(request)
    temporary_path = request.app.state.store.temporary / uuid4().hex
    try:
        await receive_upload(request, temporary_path, length, content_hash)
        return await run_in_store(
            request, Store.mutate, client_id, OP_UPLOAD, path, str(temporary_path), content_hash, length
        )
    finally:
        temporary_path.unlink(missing_ok=True)


@router.get("/content")
async def download(request: Request, path: str, revision: int):
    """Sends the content of one revision of a file."""
    get_client_id(request)
    entry = await run_in_store(request, Store.version, path, revision)
    if entry["deleted"] or entry["kind"] != FILE_KIND:
        raise Problem("This revision has no file content.", 404)
    return FileResponse(
        request.app.state.store.blobs / entry["blob_id"], media_type="application/octet-stream"
    )


@router.delete("/files")
async def delete_file(request: Request, path: str):
    """Deletes a file."""
    client_id = get_client_id(request)
    return await run_in_store(request, Store.mutate, client_id, OP_DELETE_FILE, path)


@router.post("/directories")
async def create_directory(request: Request, path: str):
    """Creates a folder."""
    client_id = get_client_id(request)
    return await run_in_store(request, Store.mutate, client_id, OP_CREATE_DIRECTORY, path)


@router.delete("/directories")
async def delete_directory(request: Request, path: str):
    """Deletes an empty folder."""
    client_id = get_client_id(request)
    return await run_in_store(request, Store.mutate, client_id, OP_DELETE_DIRECTORY, path)


@router.post("/restore")
async def restore(request: Request, path: str, revision: int):
    """Makes an earlier revision current again, as a new revision."""
    client_id = get_client_id(request)
    if revision < 1:
        raise Problem("Choose a positive source revision.", 422)
    return await run_in_store(request, Store.mutate, client_id, OP_RESTORE, path, source=revision)


@router.get("/history")
async def history(request: Request, path: str):
    """Returns all versions of a path."""
    get_client_id(request)
    return await run_in_store(request, Store.history, path)


@router.get("/events")
async def events(request: Request, path: str = ""):
    """Returns the activity log, filtered by path text, clients and actions."""
    get_client_id(request)
    return await run_in_store(
        request,
        Store.events,
        path,
        request.query_params.getlist("client_id"),
        request.query_params.getlist("action"),
    )


def _register_error_handlers(app: FastAPI) -> None:
    """
    Turns errors into JSON answers of the form {"message": ...}.

    @param app: The FastAPI application.
    """

    @app.exception_handler(Problem)
    async def problem(request, exc: Problem):
        """Returns an expected error with its status."""
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        """Returns a disk error as 507."""
        LOG.exception("Storage operation failed", exc_info=exc)
        return JSONResponse({"message": "Storage operation failed."}, status_code=507)


def main() -> None:
    """Reads the command line and runs the server."""
    parser = argparse.ArgumentParser(description="Run the SharedFolder HTTP server.")
    parser.add_argument("--storage", type=Path, default=Path("server-data"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(arguments.storage)
    try:
        uvicorn.run(app, host=arguments.host, port=arguments.port)
    finally:
        # Close the database when the server stops, even after an error
        app.state.store.close()


if __name__ == "__main__":
    main()
