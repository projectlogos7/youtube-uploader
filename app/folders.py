"""Listing folders for the web app's folder picker (used by the server and the laptop helper)."""

from pathlib import Path


def list_dirs(path, root=None):
    """Sub-folders of `path`. With `root`, browsing never goes above it."""
    root = Path(root).resolve() if root else None
    home = root or Path.home()
    try:
        p = Path(path).expanduser().resolve() if path else home
    except (OSError, RuntimeError):
        p = home
    if root and p != root and root not in p.parents:
        p = root
    if not p.is_dir():
        p = home
    try:
        dirs = sorted((c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
                      key=str.lower)
    except OSError:
        dirs = []
    at_top = (root is not None and p == root) or p == p.parent
    shortcuts = [str(home)]
    volumes = Path("/Volumes")
    if root is None and volumes.is_dir():          # external drives on a Mac
        shortcuts += [str(v) for v in sorted(volumes.iterdir()) if v.is_dir()]
    return {"path": str(p), "parent": None if at_top else str(p.parent), "dirs": dirs, "shortcuts": shortcuts}
