"""Tests for simple/watch_and_upload.py against a fake YouTube. No network."""

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_FOLDER", str(tmp_path / "watch"))
    monkeypatch.setenv("YT_CLIENT_ID", "x.apps.googleusercontent.com")
    spec = importlib.util.spec_from_file_location("wau", ROOT / "simple" / "watch_and_upload.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.CHUNK = 256 * 1024
    m.WATCH_FOLDER.mkdir(parents=True)
    monkeypatch.setattr(m, "keychain_load", lambda: {"refresh_token": "r", "client_secret": "s"})
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return m


class Fake:
    def __init__(self):
        self.inserts, self.received, self.total, self.videos = [], 0, 0, 0
        self.fail_next_put = False
        self.token_error = None

    def __call__(self, method, url, headers=None, body=None):
        if "oauth2" in url:
            if self.token_error:
                return 400, {}, json.dumps({"error": self.token_error}).encode()
            return 200, {}, b'{"access_token": "ya29.t"}'
        if method == "POST":
            self.inserts.append((url, json.loads(body)))
            self.total, self.received = int(headers["X-Upload-Content-Length"]), 0
            return 200, {"location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S"}, b""
        cr = headers["Content-Range"]
        if cr.startswith("bytes */"):
            return 308, {"range": f"bytes=0-{self.received - 1}"} if self.received else {}, b""
        if self.fail_next_put:
            self.fail_next_put = False
            raise OSError("connection reset")
        start, end = map(int, cr.split(" ")[1].split("/")[0].split("-"))
        assert start == self.received
        self.received = end + 1
        if self.received == self.total:
            self.videos += 1
            return 200, {}, json.dumps({"id": f"vid{self.videos}"}).encode()
        return 308, {"range": f"bytes=0-{end}"}, b""


def _video(m, name="Episode 1.mp4", size=700_000, age=60):
    p = m.WATCH_FOLDER / name
    p.write_bytes(os.urandom(size))
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_uploads_private_and_moves(mod, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(mod, "request", fake)
    p = _video(mod)
    assert mod.is_finished(p)
    mod.handle(p)
    url, meta = fake.inserts[0]
    assert meta["status"]["privacyStatus"] == "private"
    assert "notifySubscribers=false" in url
    assert meta["snippet"]["title"] == "Episode 1"
    assert not p.exists() and (mod.DONE_FOLDER / "Episode 1.mp4").exists()
    assert "youtu.be/vid1" in mod.LOG_FILE.read_text()


def test_recovers_from_network_drop(mod, monkeypatch):
    fake = Fake()
    fake.fail_next_put = True
    monkeypatch.setattr(mod, "request", fake)
    mod.handle(_video(mod))
    assert fake.videos == 1


def test_same_file_never_uploaded_twice(mod, monkeypatch):
    fake = Fake()
    monkeypatch.setattr(mod, "request", fake)
    p = _video(mod)
    data = p.read_bytes()
    mod.handle(p)
    p2 = mod.WATCH_FOLDER / "Copy.mp4"
    p2.write_bytes(data)
    mod.handle(p2)
    assert len(fake.inserts) == 1


def test_waits_for_export_to_finish(mod):
    assert not mod.is_finished(_video(mod, age=0))            # still being written
    assert not mod.is_finished(_video(mod, "x.txt"))          # not a video
    assert not mod.is_finished(_video(mod, ".hidden.mp4"))    # temp/hidden
    p = _video(mod, "open.mp4")
    with open(p, "ab"):
        assert not mod.is_finished(p)                          # still open in an app


def test_expired_sign_in(mod, monkeypatch):
    fake = Fake()
    fake.token_error = "invalid_grant"
    monkeypatch.setattr(mod, "request", fake)
    p = _video(mod)
    with pytest.raises(mod.SignInExpired):
        mod.handle(p)
    assert p.exists()  # left in place, retried later
