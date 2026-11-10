"""The client's background threads: the sync worker and the presence heartbeat.

The GUI sends commands to the worker, and the worker sends back (event, value)
pairs on a queue that the GUI reads.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import httpx

from .constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_JOIN_SECONDS,
    RETRY_INTERVAL_SECONDS,
    STATE_ERROR,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_SYNCING,
    SYNC_INTERVAL_SECONDS,
)
from .sync import Deferred, SyncEngine
from .transport import ApiClient

COMMAND_POLL_SECONDS = 0.1


def run_heartbeat(config: dict, stop: threading.Event, state: Callable[[], str]) -> None:
    """
    Sends a presence message every few seconds until stop is set.

    @param config: Client settings.
    @param stop: Set to end the heartbeat.
    @param state: Returns the current synchronization state.
    """
    api = ApiClient(config["server_url"], config["client_id"])
    try:
        while not stop.is_set():
            try:
                api.presence(config["display_name"], state())
            except Exception:
                pass  # Try again on the next heartbeat
            # sleep until the next heartbeat, but allow stopping the thread
            stop.wait(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        api.close()


def is_retryable(error: Exception) -> bool:
    """
    Returns whether an error is a network failure that may pass by itself.

    @param error: The error that stopped the cycle.
    """
    return isinstance(error, httpx.TransportError)


class SyncWorker(threading.Thread):
    """Runs the synchronization cycles and the GUI's requests in the background."""

    def __init__(self, directory, config: dict, output: queue.Queue):
        """
        Creates the worker.

        @param directory: Client's profile folder.
        @param config: Client settings.
        @param output: Queue for events to the GUI.
        """
        super().__init__(name="sync-worker", daemon=True)
        self.directory = directory
        self.config = config
        self.output = output
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.reported_state = STATE_SYNCING

    def command(self, name: str, **arguments) -> None:
        """
        Adds a command from the GUI to the queue.

        @param name: Command name, such as "sync" or "history".
        @param arguments: Command arguments.
        """
        self.commands.put((name, arguments))

    def emit(self, event: str, value=None) -> None:
        """
        Sends an event to the GUI.

        @param event: Event name.
        @param value: Event data.
        """
        self.output.put((event, value))

    def report_progress(self, operation: str, path: str, current: int, total: int):
        """
        Sends the progress of an upload or download to the GUI.

        @param operation: "Uploading" or "Downloading".
        @param path: Relative path of the file.
        @param current: Bytes sent so far.
        @param total: File size in bytes.
        """
        self.emit("progress", (operation, path, current, total))

    def run(self) -> None:
        """Starts the engine and the heartbeat, and runs until stopped."""
        engine = None
        heartbeat = None
        try:
            engine = SyncEngine(self.directory, self.config, progress=self.report_progress)
            # create a heartbeat thread that sends presence messages
            heartbeat = threading.Thread(
                target=run_heartbeat,
                args=(self.config, self.stop, lambda: self.reported_state),
                daemon=True,
            )
            heartbeat.start()
            self._run_loop(engine)
        except Exception as exc:
            self.emit("error", str(exc))
        finally:
            # disconnect gracefully
            self.stop.set()
            if heartbeat:
                heartbeat.join(timeout=HEARTBEAT_JOIN_SECONDS)
            if engine:
                engine.close()
            self.emit("disconnected")

    def _run_loop(self, engine: SyncEngine) -> None:
        """
        Handles GUI commands and runs a sync cycle every few seconds.

        @param engine: The sync engine.
        """
        next_sync = 0.0
        connected = False
        while not self.stop.is_set():
            # handle GUI commands before starting the next sync
            if self._process_next_command(engine):
                # expecting true when the user asked to sync now
                next_sync = 0.0
            # when the sync is paused, commands are still handled but the sync is skipped
            if self.paused.is_set():
                self.reported_state = STATE_PAUSED
                continue
            # wait until the next scheduled synchronization time
            if time.monotonic() < next_sync:
                continue
            # run the next synchronization cycle
            try:
                self.reported_state = STATE_SYNCING
                entries = engine.sync_once()
                if not connected:
                    self.emit("connected", self.config)
                    connected = True
                self.emit("files", entries)
                self.emit("synced", engine.last_sync)
                self.reported_state = STATE_IDLE
                # schedule the next regular sync
                next_sync = time.monotonic() + SYNC_INTERVAL_SECONDS
            except Deferred:
                # a local file changed during the cycle, try again later
                next_sync = time.monotonic() + SYNC_INTERVAL_SECONDS
            except Exception as exc:
                self.reported_state = STATE_ERROR
                self.emit("error", str(exc))
                self.emit("stale")
                if is_retryable(exc):
                    # network errors are retried automatically
                    next_sync = time.monotonic() + RETRY_INTERVAL_SECONDS
                else:
                    # server error, wait until the user fixes it and presses Resume
                    self.paused.set()
                    self.emit("paused_error")

    def _process_next_command(self, engine: SyncEngine) -> bool:
        """
        Handles one command from the queue. Returns True if it asks for a sync now.

        @param engine: The sync engine.
        """
        try:
            # get and handle one pending command from the GUI
            name, arguments = self.commands.get(timeout=COMMAND_POLL_SECONDS)
        except queue.Empty:
            return False
        try:
            return self._handle(engine, name, arguments)
        except Exception as exc:
            self.emit("error", str(exc))
            if name == "clients":
                # if the "clients" request fails, mark the clients list page as stale
                self.emit("stale")
            return False

    def _handle(self, engine: SyncEngine, name: str, arguments: dict) -> bool:
        """
        Runs one command and sends its result to the GUI.

        @param engine: The sync engine.
        @param name: Command name.
        @param arguments: Command arguments.
        """
        api = engine.api
        # handle the requested GUI command
        if name == "history":
            self.emit("history", api.items("history", {"path": arguments["path"]}))
        elif name == "clients":
            self.emit("clients", api.items("clients"))
        elif name == "actors":
            self.emit("actors", api.items("clients", {"include_offline": "true"}))
        elif name == "events":
            self.emit("events", api.items("events", arguments["params"]))
        elif name == "restore":
            engine.restore(arguments["path"], arguments["revision"])
            self.emit("restored", arguments["path"])
            return True
        elif name == "sync":
            # return true to mark that a sync request was made
            return True
        return False
