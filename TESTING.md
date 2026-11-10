# Testing

The project targets Ubuntu with Python 3.10 or newer. Each test that needs a server starts its own real localhost HTTP server with its own storage directory, so tests are independent of each other and exercise the network, storage, and synchronization layers end to end.

## Running the tests

From the project directory with the virtual environment active:

```bash
python -m pytest -v --junitxml=test-results/pytest.xml
```

## Results

Record the Ubuntu run here: Ubuntu version, Python version, date, and the final pytest summary line.

| Field | Value |
|---|---|
| Ubuntu | |
| Python | |
| Date | |
| Result | |

## Coverage

| Area | Tests |
|---|---|
| Paths and identity | Unsafe paths are rejected; a request without a client ID is rejected |
| Uploads and downloads | Every revision downloads with its own content; a wrong hash is rejected; an identical upload adds no revision |
| Server rules | A file and a folder cannot share a path; a folder with content cannot be deleted |
| History and change feed | History lists revisions newest first; the change feed returns only later events |
| Activity and presence | Activity filter by action and file name; online clients and their reported state |
| Synchronization | Create, update and delete reach the other client; a received file is not sent back |
| Directories | Empty nested folders are created and deleted on the other client |
| File types | Empty file, Hebrew file name, binary file |
| Restore | An old version comes back on every client as a new revision |
| Conflicts | When two clients edit the same file, the last edit the server accepted wins; both stay in history |
| Offline client | Gets the latest version on return; its offline edit is sent on reconnect |
| Network failure | A change that failed to upload is sent in the next cycle |
| Missing shared folder | Synchronization stops and nothing is deleted on the server |
| Server restart | Versions survive; presence resets |

Multi-machine propagation times and the desktop interface are checked during the LAN demonstration in DEMO.md, on at least two computers or VMs.

## Implementation details relative to the design

- Versions and events share one append-only SQLite table; its global event ID and per-path revision provide both views.
- The current version of a path is its newest row in that table. Directory records contain no blob.
- Client bookkeeping uses a JSON state file.
- The project targets a small number of files and clients, so list endpoints return all rows in one response.
- Client scan/poll and heartbeat intervals use the design defaults.
- No authentication, certificate setup, editing-base preconditions, alternate edit copies, or automatic merging were implemented.
