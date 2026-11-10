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
    m.UPLOAD_TO_FRAMEIO = False
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


# ---- Frame.io ------------------------------------------------------------------

class FakeFrameio:
    def __init__(self, share_ok=True, fail_parts=0):
        self.share_ok, self.fail_parts = share_ok, fail_parts
        self.files, self.parts, self.shares = [], [], []

    def __call__(self, method, url, headers=None, body=None):
        if "adobelogin" in url:
            return 200, {}, b'{"access_token": "adobe-at", "refresh_token": "r"}'
        if "s3.example" in url:
            if self.fail_parts:
                self.fail_parts -= 1
                raise OSError("reset")
            assert headers["x-amz-acl"] == "private" and "Authorization" not in headers
            self.parts.append(len(body))
            return 200, {}, b""
        assert headers["Authorization"] == "Bearer adobe-at"
        if url.endswith("/files/local_upload"):
            d = json.loads(body)["data"]
            self.files.append(d)
            n = 3
            size = d["file_size"]
            per = -(-size // n)
            urls = [{"size": min(per, size - i * per), "url": f"https://s3.example/p{i}?sig=x"} for i in range(n)]
            return 201, {}, json.dumps({"data": {"id": "file1", "media_type": "video/mp4", "upload_urls": urls,
                                                 "view_url": "https://next.frame.io/project/p/view/file1"}}).encode()
        if url.endswith("/shares"):
            if not self.share_ok:
                return 422, {}, b'{"errors": "nope"}'
            self.shares.append(json.loads(body)["data"])
            return 201, {}, b'{"data": {"id": "sh1", "short_url": "https://f.io/AbCd"}}'
        return 404, {}, b"{}"


@pytest.fixture
def fio(mod, monkeypatch):
    fake = FakeFrameio()
    monkeypatch.setattr(mod.frameio, "request", fake)
    monkeypatch.setattr(mod.frameio, "FRAMEIO_CLIENT_ID", "adobe-client")
    monkeypatch.setattr(mod.frameio, "_load", lambda: {"refresh_token": "r", "client_secret": "s",
                                                        "account_id": "a", "project_id": "p", "folder_id": "f"})
    monkeypatch.setattr(mod.frameio, "_save", lambda d: None)
    monkeypatch.setattr(mod.frameio.time, "sleep", lambda s: None)
    mod.UPLOAD_TO_FRAMEIO = True
    return fake


def test_uploads_to_youtube_and_frameio_with_links(mod, fio, monkeypatch):
    yt = Fake()
    monkeypatch.setattr(mod, "request", yt)
    p = _video(mod)
    mod.handle(p)
    assert yt.videos == 1 and sum(fio.parts) == 700_000
    assert fio.files[0] == {"name": "Episode 1.mp4", "file_size": 700_000}
    log = mod.LOG_FILE.read_text()
    assert "youtu.be/vid1" in log and "https://f.io/AbCd" in log
    links = (mod.DONE_FOLDER / "Episode 1 links.txt").read_text()
    assert "YouTube: https://youtu.be/vid1" in links and "Frame.io: https://f.io/AbCd" in links


def test_frameio_share_fallback_to_team_link(mod, fio, monkeypatch):
    fio.share_ok = False
    monkeypatch.setattr(mod, "request", Fake())
    mod.handle(_video(mod))
    assert "https://next.frame.io/project/p/view/file1" in mod.LOG_FILE.read_text()


def test_frameio_part_retry(mod, fio, monkeypatch):
    fio.fail_parts = 2
    monkeypatch.setattr(mod, "request", Fake())
    mod.handle(_video(mod))
    assert sum(fio.parts) == 700_000


def test_frameio_failure_does_not_repeat_youtube(mod, fio, monkeypatch):
    yt = Fake()
    monkeypatch.setattr(mod, "request", yt)
    p = _video(mod)
    real = mod.frameio.upload
    monkeypatch.setattr(mod.frameio, "upload", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        mod.handle(p)
    assert p.exists() and yt.videos == 1          # file waits, YouTube done
    monkeypatch.setattr(mod.frameio, "upload", real)
    mod.handle(p)
    assert yt.videos == 1 and len(fio.files) == 1  # only Frame.io retried
    assert not p.exists()


def test_frameio_only(mod, fio, monkeypatch):
    mod.UPLOAD_TO_YOUTUBE = False
    yt = Fake()
    monkeypatch.setattr(mod, "request", yt)
    mod.handle(_video(mod))
    assert yt.videos == 0 and len(fio.files) == 1


def test_nothing_signed_in_leaves_file(mod, monkeypatch):
    mod.UPLOAD_TO_YOUTUBE = False
    p = _video(mod)
    mod.handle(p)
    assert p.exists()


def test_frameio_expired_sign_in(mod, fio, monkeypatch):
    def expired(method, url, headers=None, body=None):
        return 400, {}, b'{"error": "invalid_grant"}'
    monkeypatch.setattr(mod.frameio, "request", expired)
    with pytest.raises(mod.frameio.SignInExpired):
        mod.frameio.access_token()
