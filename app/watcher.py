"""Folder watcher. When enabled, uploads each finished media file in the folder to the
chosen destinations, then moves it into an `Uploaded` subfolder."""

import hashlib
import shutil
import threading
import time
from pathlib import Path

from . import frameio, youtube

MEDIA_TYPES = youtube.VIDEO_TYPES | {".wav", ".mp3", ".aac", ".m4a", ".aif", ".aiff", ".flac",
                                     ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".psd", ".pdf"}
DESTINATIONS = {"youtube": ("YouTube", youtube), "frameio": ("Frame.io", frameio)}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Watcher(threading.Thread):
    def __init__(self, store, folder, interval=10, settle=20, clock=time.time):
        super().__init__(daemon=True)
        self.store = store
        self.folder = Path(folder)
        self.done_folder = self.folder / "Uploaded"
        self.interval = interval      # seconds between scans
        self.settle = settle          # file must be unchanged this long before upload
        self.clock = clock
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.seen = {}                # name -> (size, mtime, first time seen unchanged)
        self.retry_at = {}            # name -> (time, attempts)
        self.sources = {}             # file name -> computer it came from
        self.state = {"current": None, "last_scan": None, "files": {}, "notice": ""}

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
            return {**self.state, "files": dict(self.state["files"])}

    # ---- scanning -------------------------------------------------------------
    def destinations(self):
        s = self.store.settings()
        out = []
        if s["to_youtube"] and youtube.connected(self.store):
            out.append("youtube")
        if s["to_frameio"] and frameio.ready(self.store):
            out.append("frameio")
        return out

    def candidates(self):
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.iterdir()
                      if p.is_file() and not p.name.startswith((".", "~"))
                      and p.suffix.lower() in MEDIA_TYPES)

    def is_settled(self, path):
        if path.name in self.sources:   # received over the network: already complete
            return True
        st = path.stat()
        now = self.clock()
        key = (st.st_size, st.st_mtime)
        prev = self.seen.get(path.name)
        if st.st_size == 0 or not prev or prev[:2] != key:
            self.seen[path.name] = (*key, now)
            return False
        return now - prev[2] >= self.settle and now - st.st_mtime >= self.settle

    def scan(self):
        self._set(last_scan=time.strftime("%H:%M:%S"), notice="")
        if not self.folder.is_dir():
            self._set(notice=f"Watch folder not found: {self.folder}")
            return
        files = self.candidates()
        names = {p.name for p in files}
        with self.lock:
            for gone in set(self.state["files"]) - names:
                del self.state["files"][gone]
        dests = self.destinations()
        if not dests:
            self._set(notice="No destination is turned on and connected.")
        for path in files:
            if not self.store.settings()["enabled"]:
                return
            if not self.is_settled(path):
                self._file(path.name, "waiting for the file to finish copying/exporting")
                continue
            when, _ = self.retry_at.get(path.name, (0, 0))
            if self.clock() < when:
                continue
            if dests:
                self.process(path, dests)

    def _file(self, name, status):
        with self.lock:
            self.state["files"][name] = status

    # ---- one file -------------------------------------------------------------
    def process(self, path, dests):
        digest = sha256(path)
        links = {}
        usable = [d for d in dests if DESTINATIONS[d][1].accepts(path)]
        if not usable:
            self._file(path.name, "skipped: YouTube only takes video files")
            return
        for dest in usable:
            label, mod = DESTINATIONS[dest]
            done = self.store.link_for(digest, dest)
            if done:
                links[label] = done
                continue
            self._file(path.name, f"uploading to {label}")
            self._set(current={"file": path.name, "dest": label, "pct": 0})
            try:
                link = mod.upload(self.store, path,
                                  lambda pct, l=label: self._set(current={"file": path.name, "dest": l, "pct": pct}))
            except Exception as e:  # noqa: BLE001
                self._failed(path, label, e)
                return
            finally:
                self._set(current=None)
            links[label] = link
            self.store.add_history(file=path.name, dest=dest, sha256=digest, link=link, status="ok",
                                   source=self.sources.get(path.name, ""))
        self.retry_at.pop(path.name, None)
        self._move(path, links)

    def _failed(self, path, label, err):
        when, attempts = self.retry_at.get(path.name, (0, 0))
        attempts += 1
        wait = 10**9 if isinstance(err, (youtube.NeedsReconnect, frameio.NeedsReconnect)) else min(30 * 2 ** attempts, 1800)
        self.retry_at[path.name] = (self.clock() + wait, attempts)
        msg = f"{label}: {err}"
        self._file(path.name, "error, " + ("waiting for reconnect" if wait == 10**9 else f"retrying in {wait // 60 or 1} min"))
        self.store.add_history(file=path.name, dest=label.lower().replace(".", ""), status="error", error=msg,
                               source=self.sources.get(path.name, ""))

    def _move(self, path, links):
        self.done_folder.mkdir(exist_ok=True)
        target = self.done_folder / path.name
        if target.exists():
            target = self.done_folder / f"{path.stem} ({int(time.time())}){path.suffix}"
        shutil.move(str(path), target)
        (self.done_folder / f"{target.name}.links.txt").write_text(
            "".join(f"{k}: {v}\n" for k, v in links.items()))
        with self.lock:
            self.state["files"].pop(path.name, None)
        self.seen.pop(path.name, None)
        self.sources.pop(path.name, None)

    def clear_reconnect_waits(self):
        """Called after a connector is (re)connected so waiting files are retried."""
        self.retry_at.clear()
        self.poke()
