"""Loads and saves the client's settings (config.json)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .utils import Problem, read_json, valid_display_name, write_json

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"


def default_profile_directory() -> Path:
    """Returns the default profile folder of the client."""
    return Path.home() / ".local" / "share" / "sharedfolder"


def load_config(profile_dir: Path) -> dict:
    """
    Returns the saved settings, or an empty dict if there are none yet.

    @param profile_dir: Client's profile folder.
    """
    return read_json(Path(profile_dir) / CONFIG_FILE, {})


def configure(profile_dir: Path, server_url: str, sync_root: Path, display_name: str) -> dict:
    """
    Checks and saves the client's settings. The client ID is created on the first save.

    @param profile_dir: Client's profile folder.
    @param server_url: Server address, such as http://192.168.1.10:8000.
    @param sync_root: Shared folder on this computer.
    @param display_name: Name shown to other users.
    """
    profile_dir = Path(profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    config = read_json(profile_dir / CONFIG_FILE, {})
    config.setdefault("client_id", str(uuid4()))
    _validate_server_url(server_url)
    config["server_url"] = server_url.rstrip("/")
    config["sync_root"] = str(Path(sync_root).expanduser().resolve())
    config["display_name"] = valid_display_name(display_name)
    root = Path(config["sync_root"])
    if profile_dir.is_relative_to(root):
        raise Problem("The profile folder must be outside the shared folder.")
    # Create the shared folder only for a new profile, so a missing folder is not
    # seen as "every file was deleted".
    if not (profile_dir / STATE_FILE).exists():
        root.mkdir(parents=True, exist_ok=True)
    write_json(profile_dir / CONFIG_FILE, config)
    return config


def _validate_server_url(server_url: str) -> None:
    """
    Checks that the server address starts with http:// and has a host.

    @param server_url: Server address entered by the user.
    """
    address = urlparse(server_url)
    if address.scheme != "http" or not address.hostname:
        raise Problem("Use an HTTP server URL, such as http://192.168.1.10:8000.")
