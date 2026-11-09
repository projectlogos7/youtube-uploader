"""Resumable upload state (RS-1). Owner-only file permissions.

The state file holds the session URI, which is a bearer-like secret for the
upload, so it is written 0600 inside a 0700 directory and never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .audit import utcnow
from .fsutil import atomic_write_json

# initiated  -> session created, no bytes confirmed
# uploading  -> some bytes sent
# finalizing -> the final chunk is being (or was) sent; outcome may be uncertain
# uncertain  -> outcome could not be determined; reviewer must resolve
# complete   -> video resource received
STATES = {"initiated", "uploading", "finalizing", "uncertain", "complete"}


@dataclass
class UploadState:
    approval_id: str
    file_sha256: str
    channel_id: str
    title: str
    total_bytes: int
    session_uri: str = ""
    offset: int = 0
    status: str = "initiated"
    video_id: str | None = None
    started_at: str = ""
    updated_at: str = ""


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)

    def _path(self, approval_id: str) -> Path:
        return self.dir / f"{approval_id}.state.json"

    def load(self, approval_id: str) -> UploadState | None:
        p = self._path(approval_id)
        if not p.exists():
            return None
        return UploadState(**json.loads(p.read_text()))

    def save(self, st: UploadState) -> None:
        if st.status not in STATES:
            raise ValueError(f"bad state {st.status}")
        st.updated_at = utcnow()
        if not st.started_at:
            st.started_at = st.updated_at
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        atomic_write_json(self._path(st.approval_id), asdict(st), mode=0o600)

    def delete(self, approval_id: str) -> None:
        p = self._path(approval_id)
        if p.exists():
            p.unlink()

    def all(self) -> list[UploadState]:
        if not self.dir.exists():
            return []
        return [UploadState(**json.loads(p.read_text()))
                for p in sorted(self.dir.glob("*.state.json"))]
