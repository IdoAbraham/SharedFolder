"""Tkinter desktop client: one window with five tabs and a status bar.

The network work runs on SyncWorker; its results come back on a queue that
App.drain reads. Run with: python -m sharedfolder.gui [--profile DIR]
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import configure, default_profile_directory, load_config
from .constants import (
    ACTIONS,
    DEFAULT_DISPLAY_NAME,
    DEFAULT_SERVER_URL,
    DIRECTORY_KIND,
)
from .worker import SyncWorker

POLL_MILLISECONDS = 100
TABS = ("Connection", "Files", "History", "Activity", "Clients")
FONT_TITLE = ("Ubuntu", 16, "bold")
FONT_BOLD = ("Ubuntu", 10, "bold")
ACCENT = "#2F5D8A"  # The color of the window, header and progress bar

# Color of each connection state in the header badge
STATE_COLORS = {"Offline": "gray50", "Connecting": "#1565C0", "In sync": "#2E7D32",
                "Paused": "#E08E00", "Error": "#C62828"}


def format_time(value: float | None) -> str:
    """
    Formats a time as date and clock.

    @param value: Seconds since the epoch, or None.
    """
    if value:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
    return "—"


def format_clock(value: float | None) -> str:
    """
    Formats time to a clock format.

    @param value: Seconds since the epoch, or None.
    """
    if value:
        return time.strftime("%H:%M:%S", time.localtime(value))
    return "—"


def format_size(size: int | None) -> str:
    """
    Formats a file size in B or KB.

    @param size: Size in bytes, or None for folders.
    """
    if size is None:
        return "—"
    if size < 1024:
        return f"{size:,} B"
    # convert bytes to KB and show one decimal place
    return f"{size / 1024:,.1f} KB"


def apply_style(root: tk.Tk) -> None:
    """
    Chooses the ttk theme and the few styles the window uses.

    @param root: The main window.
    """
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=style.lookup("TFrame", "background"))
    style.configure("Treeview", rowheight=26)
    style.configure("Horizontal.TProgressbar", background=ACCENT)


def make_table(parent, columns, height: int = 12) -> ttk.Treeview:
    """
    Creates a table with a scrollbar.

    @param parent: Widget that holds the table.
    @param columns: List of (key, heading, width).
    @param height: Number of visible rows.
    """
    holder = ttk.Frame(parent)
    holder.pack(fill="both", expand=True, pady=(12, 0))
    column_keys = []
    for key, heading, width in columns:
        column_keys.append(key)

    tree = ttk.Treeview(holder, columns=column_keys, show="headings",
                        height=height, selectmode="browse")
    for key, heading, width in columns:
        tree.heading(key, text=heading, anchor="w")
        tree.column(key, width=width, minwidth=60, anchor="w")
    scrollbar = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    return tree


def fill_table(tree: ttk.Treeview, rows) -> None:
    """
    Replaces all rows of a table.

    @param tree: The table.
    @param rows: List of rows; each row is a list of column values.
    """
    children = tree.get_children()
    for child in children:
        tree.delete(child)

    for index, values in enumerate(rows):
        tree.insert("", "end", iid=str(index), values=list(values))


class ListFilter(ttk.LabelFrame):
    """A list where several lines can be selected, with an All button."""

    def __init__(self, parent, title: str):
        """
        Creates the list.

        @param parent: Widget that holds the list.
        @param title: Title shown above the list.
        """
        super().__init__(parent, text=title, padding=8)
        self.keys: list[str] = []  # Line number -> key
        self.listbox = tk.Listbox(self, selectmode="multiple", exportselection=False, height=4)
        self.listbox.pack(side="left", fill="both", expand=True)
        ttk.Button(self, text="All", command=self.select_all).pack(side="left", anchor="n", padx=(8, 0))

    def set_items(self, items) -> None:
        """
        Shows new lines, all selected.

        @param items: List of (key, label).
        """
        self.listbox.delete(0, "end")
        self.keys = []
        for key, label in items:
            self.listbox.insert("end", label)
            self.keys.append(key)
        self.select_all()

    def select_all(self) -> None:
        """Selects all lines."""
        self.listbox.selection_set(0, "end")

    def selected(self) -> list[str]:
        """Returns the keys of the selected lines."""
        selected_keys = []
        for index in self.listbox.curselection():
            selected_keys.append(self.keys[index])
        return selected_keys


class App:
    """The main window and its actions."""

    def __init__(self, root: tk.Tk, profile_dir: Path):
        """
        Builds the window.

        @param root: The main Tk window.
        @param profile_dir: Client's profile folder.
        """
        # configure the application state and communication objects
        self.root = root
        self.directory = profile_dir.expanduser().resolve()
        self.worker = None
        self.output = queue.Queue()
        self.closing = False
        self.versions: list[dict] = []  # History table row -> version

        # configure the main window
        root.title("SharedFolder")
        root.geometry("1100x720")
        root.minsize(900, 600)
        apply_style(root)

        # build the main window sections
        self._build_header()
        self._build_status_bar()
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=20)
        self.tabs = {}

        # create the five tabs
        for name in TABS:
            self.tabs[name] = ttk.Frame(self.notebook, padding=20)
            self.notebook.add(self.tabs[name], text=name)
        # build the content of each tab
        self._build_connection_tab(load_config(self.directory))
        self._build_files_tab()
        self._build_history_tab()
        self._build_activity_tab()
        self._build_clients_tab()

        # connect GUI events and start checking the worker output queue
        self.notebook.bind("<<NotebookTabChanged>>", lambda _event: self._tab_changed())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(POLL_MILLISECONDS, self.drain)

    def _build_header(self) -> None:
        """Builds the colored title bar with the connection state badge."""
        header = tk.Frame(self.root, background=ACCENT, padx=20, pady=12)
        header.pack(fill="x", pady=(0, 12))
        tk.Label(header, text="SharedFolder", background=ACCENT, foreground="white",
                 font=FONT_TITLE).pack(side="left")
        self.badge = tk.Label(header, background="white", font=FONT_BOLD, padx=10, pady=2)
        self.badge.pack(side="right")

    def _build_status_bar(self) -> None:
        """Builds the status bar: progress bar, message and last sync time."""
        bar = ttk.Frame(self.root, padding=(20, 8))
        bar.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(bar)
        self.progress.pack(fill="x", pady=(0, 6))
        self.message = tk.StringVar(value="Choose your shared folder and connect.")
        ttk.Label(bar, textvariable=self.message).pack(side="left")
        self.last_sync = tk.StringVar(value="Last sync —")
        ttk.Label(bar, textvariable=self.last_sync).pack(side="right")
        self.set_state("Offline", self.message.get())

    def _build_connection_tab(self, config: dict) -> None:
        """
        Builds the Connection tab: settings and connect, pause and disconnect buttons.

        @param config: Saved settings shown in the fields.
        """
        tab = self.tabs["Connection"]
        tab.columnconfigure(1, weight=1)
        self.folder = tk.StringVar(value=config.get("sync_root", ""))
        self.server = tk.StringVar(value=config.get("server_url", DEFAULT_SERVER_URL))
        self.name = tk.StringVar(value=config.get("display_name", DEFAULT_DISPLAY_NAME))
        self.client_id = tk.StringVar(value=config.get("client_id", "Generated when you save"))
        self.settings_widgets: list[ttk.Widget] = []
        fields = (("Shared folder", self.folder), ("Server URL", self.server),
                  ("Display name", self.name), ("Client ID", self.client_id))
        for row, (label, variable) in enumerate(fields):
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=6)
            # only the client id field is read-only, the others are editable
            readonly = variable is self.client_id
            state = "normal"
            if readonly:
                state = "readonly"
            entry = ttk.Entry(tab, textvariable=variable, state=state)
            entry.grid(row=row, column=1, sticky="ew", pady=6)
            if not readonly:
                self.settings_widgets.append(entry)
        browse = ttk.Button(tab, text="Browse…", command=self.browse)
        browse.grid(row=0, column=2, padx=(8, 0))
        buttons = ttk.Frame(tab)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(20, 0))
        connect = ttk.Button(buttons, text="Save & connect", command=self.connect)
        self.pause_button = ttk.Button(buttons, text="Pause", command=self.toggle_pause)
        disconnect = ttk.Button(buttons, text="Disconnect", command=self.disconnect)
        for button in (connect, self.pause_button, disconnect):
            button.pack(side="left", padx=(0, 8))
        self.settings_widgets += [browse, connect]
        ttk.Label(tab, wraplength=640, justify="left",
                  text="Your client ID is generated once and kept on this computer. "
                       "Files already in the folder are uploaded on first connect. "
                       "The last change the server accepts becomes the shared version; "
                       "earlier versions stay in History.").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(24, 0))

    def _build_files_tab(self) -> None:
        """Builds the Files tab: the list of synchronized files and folders."""
        tab = self.tabs["Files"]
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Sync now", command=self.sync_now).pack(side="left")
        ttk.Button(toolbar, text="Open folder", command=self.open_folder).pack(side="left", padx=8)
        ttk.Label(toolbar, text="Deleted entries stay listed so they can be restored.").pack(side="right")
        self.file_table = make_table(tab, [("path", "Path", 360), ("kind", "Type", 90),
                                           ("revision", "Revision", 80), ("state", "State", 100),
                                           ("client", "Last changed by", 160)])

    def _build_history_tab(self) -> None:
        """Builds the History tab: path choice, versions list and restore button."""
        tab = self.tabs["History"]
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x")
        self.history_choice = ttk.Combobox(toolbar)
        self.history_choice.pack(side="left", fill="x", expand=True)
        self.history_choice.bind("<<ComboboxSelected>>", lambda _event: self.load_history())
        self.history_choice.bind("<Return>", lambda _event: self.load_history())
        ttk.Button(toolbar, text="Restore selected version", command=self.restore).pack(side="left", padx=(8, 0))
        ttk.Label(tab, text="Restoring adds the chosen version as a new version for everyone. "
                            "Restoring a folder recreates only that folder.").pack(anchor="w", pady=(10, 0))
        self.history_table = make_table(tab, [("rev", "Revision", 80), ("action", "Action", 110),
                                              ("date", "Server time", 170),
                                              ("client", "Client", 170), ("size", "Size", 100)])

    def _build_activity_tab(self) -> None:
        """Builds the Activity tab: filters and the server's activity log."""
        tab = self.tabs["Activity"]
        filters = ttk.Frame(tab)
        filters.pack(fill="x")
        filters.columnconfigure((0, 1), weight=1, uniform="filters")
        self.client_filter = ListFilter(filters, "Clients")
        self.client_filter.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.action_filter = ListFilter(filters, "Actions")
        self.action_filter.grid(row=0, column=1, sticky="nsew")
        action_items = []
        for action in ACTIONS:
            action_items.append((action, action))
        self.action_filter.set_items(action_items)
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(12, 0))
        ttk.Label(row, text="Filename contains").pack(side="left")
        self.filename_filter = ttk.Entry(row)
        self.filename_filter.pack(side="left", fill="x", expand=True, padx=8)
        self.filename_filter.bind("<Return>", lambda _event: self.load_events())
        ttk.Button(row, text="Apply filters", command=self.load_events).pack(side="left")
        ttk.Button(row, text="Refresh clients",
                   command=lambda: self.command("actors")).pack(side="left", padx=(8, 0))
        self.event_table = make_table(tab, [("time", "Server time", 170),
                                            ("action", "Action", 100), ("path", "Path", 280),
                                            ("client", "Client", 150), ("rev", "Revision", 80)],
                                      height=6)

    def _build_clients_tab(self) -> None:
        """Builds the Clients tab: shows the other online computers."""
        tab = self.tabs["Clients"]
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Refresh", command=lambda: self.command("clients")).pack(side="left")
        self.clients_summary = tk.StringVar(value="Connect to see other computers.")
        ttk.Label(toolbar, textvariable=self.clients_summary).pack(side="left", padx=12)
        self.client_table = make_table(tab, [("name", "Name", 160), ("state", "State", 100),
                                             ("ip", "IP address", 130),
                                             ("seen", "Last seen", 100),
                                             ("id", "Client ID", 290)])

    def set_state(self, state: str, message: str) -> None:
        """
        Shows the connection state in the header badge and a message in the status bar.

        @param state: One of the STATE_COLORS names.
        @param message: Text for the status bar.
        """
        self.badge.configure(text="●  " + state, foreground=STATE_COLORS[state])
        self.message.set(message)

    def set_progress(self, fraction: float) -> None:
        """
        Fills the progress bar.

        @param fraction: Part done, from 0 to 1.
        """
        self.progress["value"] = fraction * 100

    def current_tab(self) -> str:
        """Returns the name of the open tab."""
        return self.notebook.tab(self.notebook.select(), "text")

    def _tab_changed(self) -> None:
        """Refreshes the client lists when their tab opens."""
        if self.current_tab() == "Clients":
            self.command("clients")
        elif self.current_tab() == "Activity":
            self.command("actors")

    def browse(self) -> None:
        """Lets the user choose the shared folder."""
        folder = filedialog.askdirectory(parent=self.root)
        if folder:
            self.folder.set(folder)

    def save_settings(self) -> dict | None:
        """Saves the settings from the fields. Returns None and shows an error if they are wrong."""
        try:
            if not self.folder.get().strip():
                raise ValueError("Choose a shared folder.")
            config = configure(self.directory, self.server.get(), Path(self.folder.get()),
                               self.name.get())
        except Exception as exc:
            messagebox.showerror("Settings", str(exc), parent=self.root)
            return None
        self.client_id.set(config["client_id"])
        return config

    def connect(self) -> None:
        """Saves the settings and starts the sync worker."""
        if self.worker and self.worker.is_alive():
            return
        config = self.save_settings()
        if config is None:
            return
        for widget in self.settings_widgets:
            widget.configure(state="disabled")
        self.set_state("Connecting", f"Connecting to {config['server_url']}…")
        self.worker = SyncWorker(self.directory, config, self.output)
        self.worker.start()

    def disconnect(self) -> None:
        """Stops the sync worker after its current operation."""
        if self.worker:
            self.worker.stop.set()
            self.message.set("Disconnecting after the active operation…")

    def toggle_pause(self) -> None:
        """Pauses or resumes synchronization."""
        if not self.worker or not self.worker.is_alive():
            return
        if self.worker.paused.is_set():
            self.worker.paused.clear()
            self.pause_button.configure(text="Pause")
            self.set_state("Connecting", "Resuming synchronization…")
            self.command("sync")
        else:
            self.worker.paused.set()
            self.pause_button.configure(text="Resume")
            self.set_state("Paused", "Paused. Presence continues; changes wait until you resume.")

    def command(self, name: str, **arguments) -> None:
        """
        Sends a command to the worker if it is running.

        @param name: Command name.
        @param arguments: Command arguments.
        """
        if self.worker and self.worker.is_alive() and not self.worker.stop.is_set():
            self.worker.command(name, **arguments)

    def sync_now(self) -> None:
        """Asks for a sync cycle right away."""
        self.command("sync")

    def open_folder(self) -> None:
        """Opens the shared folder in the file manager."""
        if Path(self.folder.get()).is_dir():
            subprocess.Popen(["xdg-open", self.folder.get()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def load_history(self) -> None:
        """Asks for the versions of the chosen path."""
        path = self.history_choice.get()
        if path:
            self.command("history", path=path)

    def restore(self) -> None:
        """Restores the selected version after the user confirms the action."""
        selection = self.history_table.selection()
        if not selection:
            return
        version = self.versions[int(selection[0])]
        if version["deleted"]:
            messagebox.showinfo("Restore", "This revision marks a deletion. "
                                           "Choose an earlier one.", parent=self.root)
            return
        if messagebox.askyesno("Restore shared version",
                               f"Restore {version['path']} from revision {version['revision']}?"
                               "\n\nThis creates a new version on every connected computer.",
                               parent=self.root):
            self.command("restore", path=version["path"], revision=version["revision"])

    def load_events(self) -> None:
        """Asks for the activity log with the chosen filters."""
        clients = self.client_filter.selected()
        actions = self.action_filter.selected()
        if not clients or not actions:
            fill_table(self.event_table, [])
            return
        params = []
        for client in clients:
            params.append(("client_id", client))
        for action in actions:
            params.append(("action", action))
        if self.filename_filter.get():
            params.append(("path", self.filename_filter.get()))
        self.command("events", params=params)

    def drain(self) -> None:
        """Handles the worker's events. Runs every POLL_MILLISECONDS."""
        while not self.output.empty():
            event, value = self.output.get()
            self._handle_event(event, value)
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(POLL_MILLISECONDS, self.drain)

    def _handle_event(self, event: str, value) -> None:
        """
        Updates the window for one event from the worker.

        @param event: Event name.
        @param value: Event data.
        """
        if event == "connected":
            self.command("actors")
            self.command("clients")
        elif event == "disconnected":
            self.pause_button.configure(text="Pause")
            for widget in self.settings_widgets:
                widget.configure(state="normal")
            self.clients_summary.set("Stale · disconnected")
            self.set_progress(0)
            self.set_state("Offline", "Disconnected. Your files stay usable; "
                                      "changes sync on reconnect.")
        elif event == "error":
            self.set_state("Error", value[:160])
        elif event == "paused_error":
            self.pause_button.configure(text="Resume")
        elif event == "stale":
            self.clients_summary.set("Stale · server unavailable")
        elif event == "progress":
            operation, path, current, total = value
            self.message.set(f"{operation} {path} · {current:,} / {total:,} bytes")
            self.set_progress(current / max(1, total))
        elif event == "synced":
            self._on_synced(value)
        elif event == "files":
            self._show_files(value)
        elif event == "history":
            self._show_history(value)
        elif event == "actors":
            clients = []
            for client in value:
                clients.append((client["client_id"], client["display_name"]))
            self.client_filter.set_items(clients)
            self.load_events()
        elif event == "events":
            rows = []
            for item in value:
                row = (
                    format_time(item["server_time"]),
                    item["action"],
                    item["path"],
                    item["client_name"],
                    item["revision"],
                )
                rows.append(row)
            fill_table(self.event_table, rows)
        elif event == "clients":
            self._show_clients(value)
        elif event == "restored":
            self.message.set(f"Restored {value} as a new shared version.")
            self.load_history()

    def _on_synced(self, last_sync: float) -> None:
        """
        Shows that a sync cycle finished.

        @param last_sync: Time the cycle finished.
        """
        self.last_sync.set("Last sync " + format_clock(last_sync))
        self.set_progress(1)
        if self.worker and self.worker.paused.is_set():
            self.set_state("Paused", "Paused. Presence continues; changes wait until you resume.")
        else:
            self.set_state("In sync", "All accepted changes are synchronized.")

    def _show_files(self, entries: list[dict]) -> None:
        """
        Fills the Files table and the path list of the History tab.

        @param entries: Synchronized entries.
        """
        ordered = sorted(entries, key=lambda entry: entry["path"])
        rows = []
        history_paths = []

        for entry in ordered:
            kind = "File"
            if entry["kind"] == DIRECTORY_KIND:
                kind = "Folder"

            state = "Synced"
            if entry["deleted"]:
                state = "Deleted"

            row = (
                entry["path"],
                kind,
                entry["revision"],
                state,
                entry.get("client_name", ""),
            )
            rows.append(row)
            history_paths.append(entry["path"])

        fill_table(self.file_table, rows)
        self.history_choice["values"] = history_paths

    def _show_history(self, versions: list[dict]) -> None:
        """
        Fills the History table.

        @param versions: Versions of the chosen path, newest first.
        """
        self.versions = versions
        rows = []
        for version in versions:
            row = (
                version["revision"],
                version["action"],
                format_time(version["server_time"]),
                version["client_name"],
                format_size(version["size"]),
            )
            rows.append(row)
        fill_table(self.history_table, rows)

    def _show_clients(self, clients: list[dict]) -> None:
        """
        Fills the Clients table.

        @param clients: Other online clients.
        """
        self.clients_summary.set("")  # Fresh data: clear the "Stale" notice
        rows = []
        for client in clients:
            row = (
                client["display_name"],
                client["reported_sync_state"],
                client["last_ip"],
                format_clock(client["last_seen"]),
                client["client_id"],
            )
            rows.append(row)
        fill_table(self.client_table, rows)

    def close(self) -> None:
        """Disconnects, drain() closes the window when the worker stops."""
        self.closing = True
        self.disconnect()


def main() -> None:
    """Reads the command line and opens the window."""
    parser = argparse.ArgumentParser(description="Run the SharedFolder desktop client.")
    parser.add_argument("--profile", type=Path, default=default_profile_directory())
    arguments = parser.parse_args()
    root = tk.Tk()
    App(root, arguments.profile)
    root.mainloop()


if __name__ == "__main__":
    main()
