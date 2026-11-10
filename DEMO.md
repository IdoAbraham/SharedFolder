# Three-client demonstration

Use three Ubuntu computers or VMs for the networking demonstration. For a quick local rehearsal, launch three GUI profiles with different profile folders and shared roots against a server at `http://127.0.0.1:8000`.

## Start

On the server, run:

```bash
python -m sharedfolder.server
```

On each client, run `python -m sharedfolder.gui`. Choose a different local folder and names A, B, and C, then connect to the server's LAN address. Open the Clients tab to verify the other two appear.

For a local rehearsal, use:

```bash
python -m sharedfolder.gui --profile "$HOME/.local/share/sharedfolder-A"
python -m sharedfolder.gui --profile "$HOME/.local/share/sharedfolder-B"
python -m sharedfolder.gui --profile "$HOME/.local/share/sharedfolder-C"
```

Run each command in a separate terminal. Configure three separate shared folders.

## Demonstrate synchronization

1. On A, create `notes.txt` containing “first version”.
2. Verify B and C receive it.
3. On B, change it to “second version”.
4. Verify A and C receive the new content.
5. Create an empty folder named `Empty example` on C; verify it appears elsewhere.
6. Delete that empty folder and verify removal.
7. Create a file with a Hebrew name and a binary file; compare contents after propagation.

## Demonstrate ordering and history

1. Pause B.
2. Edit and save `notes.txt` on B.
3. Edit and save the same file on A; allow A and C to synchronize.
4. Resume B. B's later submitted update becomes current.
5. Open History for `notes.txt`. Both accepted edits are present with the source names.
6. Select the first revision and Restore. Confirm that this creates a new version.
7. Verify every client receives the restored content.

The winner is determined by server commit order, regardless of client clocks.

## Demonstrate deletion and restoration

1. Delete `notes.txt` on A.
2. Verify it disappears from the local folders on B and C.
3. In History, select the deleted path and an earlier non-deleted revision.
4. Restore it and verify propagation.

## Demonstrate activity and presence

1. In Activity, select clients A and B with actions Created and Updated.
2. Verify events match one selected client and one selected action.
3. Clear all actions: no server activity rows should appear.
4. Restore all selections.
5. Pause one client: it remains online with a paused reported state.
6. Close one client: it disappears from Clients after approximately fifteen seconds.

## Demonstrate an outage

1. Stop the server.
2. Save a local edit on A.
3. Restart the server from the same directory, so it uses the same `server-data`.
4. Verify the client reconnects and the edit propagates.
5. Confirm earlier history is still present.

## Evidence to collect for the portfolio

Record the Ubuntu/Python versions, LAN addresses, server/client commands, screenshots of each tab, actual propagation times, history rows, relevant activity logs, and test outputs. Accepted change history is in the server database and the Activity tab.

For a meaningful LAN test, run at least two distinct computers/VMs rather than only localhost profiles.
