"""An in-memory fake of the parts of the YouTube Data API and Google OAuth
token endpoint that the uploader uses. No test ever touches the network."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from studio_uploader.errors import TransportError
from studio_uploader.transport import Response

ACCESS_TOKEN = "ya29.FAKE-ACCESS-TOKEN-do-not-log-0123456789"
REFRESH_TOKEN = "1//0gFAKE-REFRESH-TOKEN-do-not-log-abcdef"
CLIENT_SECRET = "GOCSPX-FAKE-client-secret-do-not-log"
CHANNEL_ID = "UCabcdefghijklmnopqrstuv"
OTHER_CHANNEL_ID = "UCzzzzzzzzzzzzzzzzzzzzzz"
UPLOADS_PLAYLIST = "UUabcdefghijklmnopqrstuv"


class SimulatedCrash(BaseException):
    """Stands in for the process being killed. BaseException so that no
    ``except Exception`` in the code under test can swallow it."""


@dataclass
class Session:
    upload_id: str
    total: int
    params: dict
    body: dict
    received: int = 0
    video_id: str | None = None
    expired: bool = False


@dataclass
class Call:
    method: str
    url: str
    headers: dict
    body_len: int
    kind: str


class FakeYouTube:
    def __init__(self, channels: list[dict] | None = None):
        self.channels = channels if channels is not None else [self.channel(CHANNEL_ID)]
        self.videos: dict[str, dict] = {}
        self.uploads: list[dict] = []  # playlistItems for the uploads playlist
        self.sessions: dict[str, Session] = {}
        self.calls: list[Call] = []
        self.inserts: list[dict] = []
        self.token_status = 200
        self.token_error = None
        # Injection hooks
        self.before: Callable[[Call], None] | None = None     # may raise
        self.after_put: Callable[[Session, bool], None] | None = None  # (session, finished) may raise
        self.status_queue: dict[str, list[int]] = {}        # endpoint kind -> statuses to return first
        self.error_reason: str = "backendError"

    @staticmethod
    def channel(cid: str, title: str = "The Network") -> dict:
        return {"id": cid, "snippet": {"title": title, "customUrl": "@thenetwork"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}}}

    # ---- helpers for assertions ---------------------------------------------
    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c.kind == kind)

    @property
    def api_calls(self) -> list[Call]:
        return [c for c in self.calls if c.kind != "token"]

    # ---- transport -----------------------------------------------------------
    def send(self, method: str, url: str, headers: dict, body: bytes | None) -> Response:
        p = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(p.query).items()}
        kind = self._kind(method, p.hostname, p.path, q)
        call = Call(method, url, dict(headers), len(body or b""), kind)
        self.calls.append(call)
        if self.before:
            self.before(call)
        queued = self.status_queue.get(kind)
        if queued:
            st = queued.pop(0)
            if st == -1:
                raise TransportError("simulated connection reset")
            reason = self.error_reason
            return Response(st, {}, json.dumps({"error": {"code": st, "errors": [{"reason": reason}]}}).encode())

        if kind == "token":
            return self._token(body)
        if kind != "token":
            assert headers.get("Authorization") == f"Bearer {ACCESS_TOKEN}", "missing/invalid auth header"
        if kind == "channels":
            return self._json({"items": self.channels})
        if kind == "videos":
            v = self.videos.get(q.get("id", ""))
            return self._json({"items": [v] if v else []})
        if kind == "playlistItems":
            return self._json({"items": list(self.uploads)})
        if kind == "insert":
            return self._insert(q, headers, body)
        if kind == "put":
            return self._put(q, headers, body)
        return Response(404, {}, b'{"error":{"code":404,"errors":[{"reason":"notFound"}]}}')

    def _kind(self, method, host, path, q) -> str:
        if host == "oauth2.googleapis.com":
            return "token"
        if method == "GET" and path.endswith("/channels"):
            return "channels"
        if method == "GET" and path.endswith("/videos"):
            return "videos"
        if method == "GET" and path.endswith("/playlistItems"):
            return "playlistItems"
        if method == "POST" and path == "/upload/youtube/v3/videos":
            return "insert"
        if method == "PUT" and "upload_id" in q:
            return "put"
        return "other"

    def _json(self, obj, status=200, headers=None) -> Response:
        return Response(status, headers or {}, json.dumps(obj).encode())

    def _token(self, body: bytes | None) -> Response:
        form = {k: v[0] for k, v in parse_qs((body or b"").decode()).items()}
        if self.token_error:
            return self._json({"error": self.token_error}, 400)
        assert form.get("refresh_token") == REFRESH_TOKEN
        return self._json({"access_token": ACCESS_TOKEN, "expires_in": 3599,
                           "scope": "https://www.googleapis.com/auth/youtube.upload "
                                    "https://www.googleapis.com/auth/youtube.readonly",
                           "token_type": "Bearer"})

    def _insert(self, q, headers, body) -> Response:
        meta = json.loads(body)
        self.inserts.append({"params": q, "body": meta})
        total = int(headers["X-Upload-Content-Length"])
        uid = "SECRETUPLOADID" + secrets.token_hex(8)
        self.sessions[uid] = Session(uid, total, q, meta)
        loc = f"https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id={uid}"
        return Response(200, {"Location": loc}, b"")

    def _range(self, s: Session) -> dict:
        return {"Range": f"bytes=0-{s.received - 1}"} if s.received else {}

    def _put(self, q, headers, body) -> Response:
        s = self.sessions.get(q["upload_id"])
        if s is None or s.expired:
            return Response(404, {}, b'{"error":{"code":404,"errors":[{"reason":"notFound"}]}}')
        cr = headers["Content-Range"]
        if cr.startswith("bytes */"):
            if s.video_id:
                return self._json(self.videos[s.video_id], 200)
            return Response(308, self._range(s), b"")
        rng, total = cr.split(" ", 1)[1].split("/")
        start, end = (int(x) for x in rng.split("-"))
        assert int(total) == s.total
        if start != s.received:
            return Response(308, self._range(s), b"")
        assert len(body) == end - start + 1
        s.received = end + 1
        finished = s.received == s.total
        if finished:
            self._create_video(s)
        if self.after_put:
            self.after_put(s, finished)
        if finished:
            return self._json(self.videos[s.video_id], 200)
        return Response(308, self._range(s), b"")

    def _create_video(self, s: Session) -> None:
        vid = "vid" + secrets.token_hex(4)
        s.video_id = vid
        status = dict(s.body["status"])
        status["uploadStatus"] = "uploaded"
        self.videos[vid] = {
            "id": vid,
            "snippet": dict(s.body["snippet"], channelId=CHANNEL_ID),
            "status": status,
            "processingDetails": {"processingStatus": "processing"},
            "fileDetails": {"fileSize": str(s.total)},
        }
        self.uploads.insert(0, {"snippet": {"title": s.body["snippet"]["title"],
                                            "publishedAt": "2099-01-01T00:00:00Z",
                                            "resourceId": {"videoId": vid}},
                                "contentDetails": {"videoId": vid}})
