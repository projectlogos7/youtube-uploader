"""Fake Google / YouTube / Adobe / Frame.io / S3 servers for tests."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import frameio, net, server, youtube
from app.store import Store
from app.watcher import Watcher


class FakeCloud:
    """Answers like Google, YouTube, Adobe IMS, Frame.io and S3."""

    def __init__(self):
        self.yt_videos, self.yt_inserts, self.fio_files, self.fio_parts, self.shares = [], [], [], [], []
        self.yt_received = self.yt_total = 0
        self.share_ok = True
        self.token_error = None
        self.fail = {}  # kind -> count of OSErrors to raise

    def _maybe_fail(self, kind):
        if self.fail.get(kind):
            self.fail[kind] -= 1
            raise OSError("connection reset")

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = headers or {}
        u = urlsplit(url)
        if u.hostname == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if self.token_error:
                return 400, {}, json.dumps({"error": self.token_error}).encode()
            out = {"access_token": "g-at"}
            if form["grant_type"] == "authorization_code":
                assert form["code_verifier"] and form["redirect_uri"] == "http://localhost:8765/"
                out["refresh_token"] = "g-rt"
            return 200, {}, json.dumps(out).encode()
        if u.hostname == "ims-na1.adobelogin.com":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if self.token_error:
                return 400, {}, json.dumps({"error": self.token_error}).encode()
            return 200, {}, json.dumps({"access_token": "a-at", "refresh_token": "a-rt"}).encode()
        if u.hostname == "www.googleapis.com":
            assert headers["Authorization"] == "Bearer g-at"
            if u.path.endswith("/channels"):
                return 200, {}, b'{"items": [{"snippet": {"title": "The Network"}}]}'
            if method == "POST":
                meta = json.loads(body)
                self.yt_inserts.append((parse_qs(u.query), meta))
                self.yt_total, self.yt_received = int(headers["X-Upload-Content-Length"]), 0
                return 200, {"location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S"}, b""
            cr = headers["Content-Range"]
            if cr.startswith("bytes */"):
                return 308, ({"range": f"bytes=0-{self.yt_received - 1}"} if self.yt_received else {}), b""
            self._maybe_fail("yt_put")
            start, end = map(int, cr.split(" ")[1].split("/")[0].split("-"))
            assert start == self.yt_received
            self.yt_received = end + 1
            if self.yt_received == self.yt_total:
                vid = f"vid{len(self.yt_videos) + 1}"
                self.yt_videos.append(vid)
                return 200, {}, json.dumps({"id": vid}).encode()
            return 308, {"range": f"bytes=0-{end}"}, b""
        if u.hostname == "s3.example":
            self._maybe_fail("s3")
            assert headers["x-amz-acl"] == "private" and "Authorization" not in headers
            self.fio_parts.append(len(body))
            return 200, {}, b""
        if u.hostname == "api.frame.io":
            assert headers["Authorization"] == "Bearer a-at"
            p = u.path
            if p == "/v4/accounts":
                return 200, {}, b'{"data": [{"id": "acc1", "display_name": "Studio"}]}'
            if p.endswith("/workspaces"):
                return 200, {}, b'{"data": [{"id": "ws1", "name": "Main"}]}'
            if p.endswith("/projects"):
                return 200, {}, json.dumps({"data": [
                    {"id": "pr1", "name": "Episodes", "root_folder_id": "f1"},
                    {"id": "pr2", "name": "Reels", "root_folder_id": "f2"}]}).encode()
            if p.endswith("/files/local_upload"):
                d = json.loads(body)["data"]
                self.fio_files.append((p, d))
                size, n = d["file_size"], 3
                per = -(-size // n)
                urls = [{"size": min(per, size - i * per), "url": f"https://s3.example/p{i}?sig=1"} for i in range(n)]
                return 201, {}, json.dumps({"data": {"id": "file1", "media_type": "video/mp4", "upload_urls": urls,
                                                     "view_url": "https://next.frame.io/project/pr1/view/file1"}}).encode()
            if p.endswith("/shares"):
                if not self.share_ok:
                    return 422, {}, b'{"errors": "no"}'
                self.shares.append(json.loads(body)["data"])
                return 201, {}, b'{"data": {"id": "sh1", "short_url": "https://f.io/AbCd"}}'
        return 404, {}, b"{}"


