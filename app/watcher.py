"""Watcher. When switched on, it looks at:
  * the folder chosen for this computer, and
  * the inbox, where files sent by other computers' helpers arrive (one sub-folder per computer).
Each finished media file is uploaded to the chosen destinations. Files from this computer's
folder are then moved into `Uploaded/` there; files from the inbox are deleted (the original
stays on the laptop that sent it)."""

import hashlib
import shutil
import threading
import time
from pathlib import Path

from . import frameio, youtube

MEDIA_TYPES = youtube.VIDEO_TYPES | {".wav", ".mp3", ".aac", ".m4a", ".aif", ".aiff", ".flac",
                                     ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".pdf"}
DESTINATIONS = {"youtube": ("YouTube", youtube), "frameio": ("Frame.io", frameio)}
THIS_COMPUTER = "This computer"


def is_media(path):
    return (path.is_file() and not path.name.startswith((".", "~"))
            and path.suffix.lower() in MEDIA_TYPES)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Watcher(threading.Thread):
    def __init__(self, store, inbox, interval=10, settle=20, clock=time.time):
        super().__init__(daemon=True)
        self.store = store
        self.inbox = Path(inbox)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.interval = interval      # seconds between scans
        self.settle = settle          # a local file must be unchanged this long before upload
        self.clock = clock
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.seen = {}                # path -> (size, mtime, first time seen unchanged)
        self.retry_at = {}            # path -> (time, attempts)
        self.state = {"current": None, "last_scan": None, "files": [], "notice": ""}

    # ---- thread ---------------------------------------------------------------
    def run(self):
        while True:
            try:
                if self.store.settings()["enabled"]:
                    self.scan()
            except Exception as e:  # noqa: BLE001 - the watcher must never die
                self._set(notice=f"Watcher error: {e}")
            self.wake.wait(self.interval)
            self.wake.clear()

    def poke(self):
        self.wake.set()

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def snapshot(self):
        with self.lock:
            return {**self.state, "files": list(self.state["files"])}

    def clear_reconnect_waits(self):
        self.retry_at.clear()
        self.poke()

    # ---- what to look at ------------------------------------------------------
    def destinations(self):
        s = self.store.settings()
        out = []
        if s["to_youtube"] and youtube.connected(self.store):
            out.append("youtube")
        if s["to_frameio"] and frameio.ready(self.store):
            out.append("frameio")
        return out

    def folders(self):
        """[(folder, label, is_inbox)]"""
        out = []
        local = self.store.settings()["folder"]
        if local:
            out.append((Path(local), THIS_COMPUTER, False))
        computers = self.store.computers()
        for d in sorted(p for p in self.inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
            out.append((d, (computers.get(d.name) or {}).get("name") or "Another computer", True))
        return out

    def is_settled(self, path, from_inbox):
        if from_inbox:                 # arrived complete over the network
            return True
        st = path.stat()
        now = self.clock()
        key = (st.st_size, st.st_mtime)
        prev = self.seen.get(str(path))
        if st.st_size == 0 or not prev or prev[:2] != key:
            self.seen[str(path)] = (*key, now)
            return False
        return now - prev[2] >= self.settle and now - st.st_mtime >= self.settle

    # ---- scanning -------------------------------------------------------------
    def scan(self):
        self._set(last_scan=time.strftime("%H:%M:%S"), notice="")
        dests = self.destinations()
        notices, listing = [], []
        local = self.store.settings()["folder"]
        if not local:
            notices.append("Choose a watch folder for this computer.")
        elif not Path(local).is_dir():
            notices.append(f"This computer's folder isn't there any more: {local}")
        if not dests:
            notices.append("No destination is turned on and connected.")
        for folder, label, from_inbox in self.folders():
            if not folder.is_dir():
                continue
            for path in sorted(p for p in folder.iterdir() if is_media(p)):
                if not self.store.settings()["enabled"]:
                    return
                status = self.handle(path, label, from_inbox, dests)
                if status:
                    listing.append({"file": path.name, "from": label, "status": status})
                    self._set(files=list(listing))
        self._set(files=listing, notice=" ".join(notices))

    def handle(self, path, label, from_inbox, dests):
        """Upload if ready. Returns a status line if the file is still in the folder."""
        if not self.is_settled(path, from_inbox):
            return "waiting for the file to finish copying or exporting"
        when, _ = self.retry_at.get(str(path), (0, 0))
        if self.clock() < when:
            return self._retry_text(path)
        if not dests:
            return "waiting for a destination"
        usable = [d for d in dests if DESTINATIONS[d][1].accepts(path)]
        if not usable:
            return "skipped: YouTube only takes video files"
        return self.process(path, label, from_inbox, usable)

    def _retry_text(self, path):
        when, _ = self.retry_at[str(path)]
        if when - self.clock() > 10**8:
            return "error, waiting for you to reconnect"
        return f"error, trying again in {max(1, int((when - self.clock()) // 60))} min"

    # ---- one file -------------------------------------------------------------
    def process(self, path, label, from_inbox, usable):
        digest = sha256(path)
        links = {}
        for dest in usable:
            name, mod = DESTINATIONS[dest]
            done = self.store.link_for(digest, dest)
            if done:
                links[name] = done
                continue
            self._set(current={"file": path.name, "from": label, "dest": name, "pct": 0})
            try:
                link = mod.upload(self.store, path, lambda pct, n=name: self._set(
                    current={"file": path.name, "from": label, "dest": n, "pct": pct}))
            except Exception as e:  # noqa: BLE001
                self._failed(path, label, name, e)
                return self._retry_text(path)
            finally:
                self._set(current=None)
            links[name] = link
            self.store.add_history(file=path.name, source=label, dest=dest, sha256=digest, link=link, status="ok")
        self.retry_at.pop(str(path), None)
        self.seen.pop(str(path), None)
        if from_inbox:
            path.unlink()
        else:
            self._move(path, links)
        return None

    def _failed(self, path, label, name, err):
        _, attempts = self.retry_at.get(str(path), (0, 0))
        attempts += 1
        reconnect = isinstance(err, (youtube.NeedsReconnect, frameio.NeedsReconnect))
        wait = 10**9 if reconnect else min(30 * 2 ** attempts, 1800)
        self.retry_at[str(path)] = (self.clock() + wait, attempts)
        self.store.add_history(file=path.name, source=label, dest=name.lower().replace(".", ""),
                               status="error", error=f"{name}: {err}")

    def _move(self, path, links):
        done = path.parent / "Uploaded"
        done.mkdir(exist_ok=True)
        target = done / path.name
        if target.exists():
            target = done / f"{path.stem} ({int(time.time())}){path.suffix}"
        shutil.move(str(path), target)
        (done / f"{target.name}.links.txt").write_text("".join(f"{k}: {v}\n" for k, v in links.items()))
