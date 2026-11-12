"""Settings, secrets and upload history, kept as small files in the data folder.

settings.json  - folder options and which destinations are on (no secrets)
secrets.json   - OAuth client secrets and refresh tokens (file mode 0600)
history.jsonl  - one line per finished upload or error
"""

import copy
import json
import os
import threading
import time
from pathlib import Path

DEFAULTS = {
    "enabled": False,
    "to_youtube": True,
    "to_frameio": False,
    "youtube": {"client_id": "", "channel": ""},
    "frameio": {"client_id": "", "account_id": "", "project_id": "", "folder_id": "", "project_name": ""},
}


def _merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.dir / "settings.json"
        self.secrets_path = self.dir / "secrets.json"
        self.history_path = self.dir / "history.jsonl"
        self.computers_path = self.dir / "computers.json"
        self.lock = threading.RLock()

    # ---- settings ----------------------------------------------------------
    def settings(self):
        with self.lock:
            saved = json.loads(self.settings_path.read_text()) if self.settings_path.exists() else {}
            return _merge(DEFAULTS, saved)

    def update(self, patch):
        with self.lock:
            s = _merge(self.settings(), patch)
            self._write(self.settings_path, s, 0o644)
            return s

    # ---- secrets -----------------------------------------------------------
    def secret(self, name):
        with self.lock:
            if not self.secrets_path.exists():
                return None
            return json.loads(self.secrets_path.read_text()).get(name)

    def set_secret(self, name, value):
        with self.lock:
            all_ = json.loads(self.secrets_path.read_text()) if self.secrets_path.exists() else {}
            if value is None:
                all_.pop(name, None)
            else:
                all_[name] = value
            self._write(self.secrets_path, all_, 0o600)

    # ---- history -----------------------------------------------------------
    def add_history(self, **entry):
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with self.lock, open(self.history_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def history(self, limit=None):
        with self.lock:
            if not self.history_path.exists():
                return []
            rows = [json.loads(line) for line in self.history_path.read_text().splitlines() if line.strip()]
        return rows[-limit:] if limit else rows

    def link_for(self, digest, dest):
        for row in reversed(self.history()):
            if row.get("sha256") == digest and row.get("dest") == dest and row.get("link"):
                return row["link"]
        return None

    # ---- computers on the network that send files in -----------------------
    def computers(self):
        with self.lock:
            return json.loads(self.computers_path.read_text()) if self.computers_path.exists() else {}

    def computer_seen(self, cid, **info):
        with self.lock:
            all_ = self.computers()
            all_[cid] = {**all_.get(cid, {"sent": 0}), **info, "last_seen": time.time()}
            self._write(self.computers_path, all_, 0o644)

    def computer_sent(self, cid):
        with self.lock:
            all_ = self.computers()
            if cid in all_:
                all_[cid]["sent"] = all_[cid].get("sent", 0) + 1
                self._write(self.computers_path, all_, 0o644)

    def remove_computer(self, cid):
        with self.lock:
            all_ = self.computers()
            all_.pop(cid, None)
            self._write(self.computers_path, all_, 0o644)

    # ---- helpers -----------------------------------------------------------
    def _write(self, path, obj, mode):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
