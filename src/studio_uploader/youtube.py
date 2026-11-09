"""YouTube Data API v3 client restricted to what the uploader needs.

Guards enforced here, in one place, before any byte leaves the machine:

* kill switch checked before every request (RS-8)
* endpoint allowlist: channels.list, videos.list, playlistItems.list,
  videos.insert (resumable init) and PUTs to the resumable session; nothing
  else, in particular no videos.update, so privacy can never be changed later
* write requests refused unless the client was built in live mode (FR-6)
* every insert request asserted Private + notifySubscribers=false + no
  publishAt (FR-6, privacy guard)
* bounded retries with jittered exponential backoff on 5xx/network (RS-6)
* hard stops on auth, quota and other permanent 4xx errors (RS-7)
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from .audit import AuditLog
from .config import RetryConfig
from .errors import (AuthError, DryRunViolation, EndpointNotAllowed,
                     PermanentApiError, PrivacyViolation, QuotaExceeded,
                     RetriesExhausted, TransportError)
from .killswitch import KillSwitch
from .transport import Response, Transport

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"

READ_ENDPOINTS = {
    ("GET", "www.googleapis.com", "/youtube/v3/channels"),
    ("GET", "www.googleapis.com", "/youtube/v3/videos"),
    ("GET", "www.googleapis.com", "/youtube/v3/playlistItems"),
}
INSERT_ENDPOINT = ("POST", "www.googleapis.com", "/upload/youtube/v3/videos")

QUOTA_REASONS = {"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded",
                 "rateLimitExceeded", "userRateLimitExceeded"}


class TokenProvider:
    """Interface: returns a short-lived access token. See auth.py."""

    def access_token(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def invalidate(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


def build_insert_request(manifest: dict) -> tuple[dict[str, str], dict[str, Any]]:
    """Map an approved manifest to videos.insert params and body.

    Privacy is not read from the manifest; it is fixed here.
    """
    params = {"part": "snippet,status", "notifySubscribers": "false", "uploadType": "resumable"}
    body = {
        "snippet": {
            "title": manifest["title"],
            "description": manifest["description"],
            "tags": list(manifest["tags"]),
            "categoryId": manifest["categoryId"],
            "defaultLanguage": manifest["defaultLanguage"],
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": bool(manifest["madeForKids"]),
            "license": manifest["license"],
            "containsSyntheticMedia": bool(manifest["containsSyntheticMedia"]),
        },
    }
    assert_private(params, body)
    return params, body


def assert_private(params: dict[str, str], body: dict[str, Any]) -> None:
    status = body.get("status")
    if not isinstance(status, dict) or status.get("privacyStatus") != "private":
        raise PrivacyViolation("insert request is not private")
    if "publishAt" in status:
        raise PrivacyViolation("publishAt must never be sent")
    if params.get("notifySubscribers") != "false":
        raise PrivacyViolation("notifySubscribers must be false")


@dataclass
class RequestStats:
    total: int = 0
    reads: int = 0
    insert_inits: int = 0
    upload_puts: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)

    @property
    def upload_requests(self) -> int:
        return self.insert_inits + self.upload_puts


class YouTubeClient:
    def __init__(self, transport: Transport, tokens: TokenProvider, killswitch: KillSwitch,
                 audit: AuditLog, *, live: bool, retry: RetryConfig,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: random.Random | None = None):
        self.transport = transport
        self.tokens = tokens
        self.killswitch = killswitch
        self.audit = audit
        self.live = live
        self.retry = retry
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.stats = RequestStats()
        self._session_hosts: set[str] = set()

    # ---- core request path -------------------------------------------------

    def _classify(self, method: str, url: str) -> str:
        p = urlsplit(url)
        if p.scheme != "https":
            raise EndpointNotAllowed("non-https request refused")
        key = (method, p.hostname, p.path)
        if key in READ_ENDPOINTS:
            return "read"
        if key == INSERT_ENDPOINT:
            return "insert"
        if method == "PUT" and p.path == INSERT_ENDPOINT[2] and "upload_id=" in p.query \
                and p.hostname in self._session_hosts:
            return "upload_put"
        raise EndpointNotAllowed(f"{method} {p.hostname}{p.path} is not on the allowlist")

    def _backoff(self, attempt: int) -> float:
        base = min(self.retry.base_delay_s * (2 ** attempt), self.retry.max_delay_s)
        return base / 2 + self.rng.uniform(0, base / 2)

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                body: bytes | None = None, ok: tuple[int, ...] = (200,),
                retry: bool = True) -> Response:
        kind = self._classify(method, url)
        if kind != "read" and not self.live:
            raise DryRunViolation(f"{kind} request refused: not in live mode")

        endpoint = urlsplit(url).path
        refreshed = False
        attempt = 0
        while True:
            # RS-8: kill switch before *every* request, including retries.
            self.killswitch.check()
            hdrs = dict(headers or {})
            hdrs["Authorization"] = f"Bearer {self.tokens.access_token()}"
            self.stats.total += 1
            self.stats.by_endpoint[endpoint] = self.stats.by_endpoint.get(endpoint, 0) + 1
            if kind == "read":
                self.stats.reads += 1
            elif kind == "insert":
                self.stats.insert_inits += 1
            else:
                self.stats.upload_puts += 1
            try:
                resp = self.transport.send(method, url, hdrs, body)
            except TransportError as exc:
                if not retry:
                    raise  # caller (chunk upload) handles via a status query
                if attempt + 1 >= self.retry.max_attempts:
                    raise RetriesExhausted(f"network error after {attempt + 1} attempts: {exc}") from None
                delay = self._backoff(attempt)
                self.audit.warn("api.retry", endpoint=endpoint, attempt=attempt + 1, reason=str(exc),
                                delay_s=round(delay, 2))
                self.sleep(delay)
                attempt += 1
                continue

            if resp.status in ok:
                return resp
            if resp.status == 401 and not refreshed:
                self.audit.warn("api.unauthorized_refreshing", endpoint=endpoint)
                self.tokens.invalidate()
                refreshed = True
                continue
            if resp.status == 401:
                raise AuthError("API rejected credentials after refresh; owner re-authorization required")
            reasons = _error_reasons(resp)
            if resp.status in (403, 429) and reasons & QUOTA_REASONS:
                raise QuotaExceeded(f"quota or rate limit: {sorted(reasons)}")
            if resp.status >= 500:
                if not retry:
                    raise TransportError(f"HTTP {resp.status}")
                if attempt + 1 >= self.retry.max_attempts:
                    raise RetriesExhausted(f"HTTP {resp.status} after {attempt + 1} attempts")
                delay = self._backoff(attempt)
                self.audit.warn("api.retry", endpoint=endpoint, attempt=attempt + 1,
                                status=resp.status, delay_s=round(delay, 2))
                self.sleep(delay)
                attempt += 1
                continue
            raise PermanentApiError(f"HTTP {resp.status} from {endpoint}: {sorted(reasons) or 'no reason'}")

    def _get_json(self, path: str, params: dict[str, str]) -> dict:
        resp = self.request("GET", f"{API}/{path}?{urlencode(params)}")
        return json.loads(resp.body or b"{}")

    # ---- read-only calls ---------------------------------------------------

    def channels_mine(self) -> list[dict]:
        data = self._get_json("channels", {"mine": "true", "part": "id,snippet,contentDetails"})
        return data.get("items", [])

    def video(self, video_id: str) -> dict | None:
        data = self._get_json("videos", {"id": video_id,
                                         "part": "snippet,status,processingDetails,fileDetails"})
        items = data.get("items", [])
        return items[0] if items else None

    def recent_uploads(self, uploads_playlist_id: str, max_results: int = 50) -> list[dict]:
        data = self._get_json("playlistItems", {"playlistId": uploads_playlist_id,
                                                "part": "snippet,contentDetails",
                                                "maxResults": str(max_results)})
        return data.get("items", [])

    # ---- resumable upload (live only) --------------------------------------

    def init_resumable(self, manifest: dict, total_bytes: int, content_type: str) -> str:
        params, body = build_insert_request(manifest)
        assert_private(params, body)  # belt and braces at the send site
        raw = json.dumps(body).encode()
        resp = self.request(
            "POST", f"{UPLOAD_API}?{urlencode(params)}",
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Length": str(total_bytes),
                     "X-Upload-Content-Type": content_type},
            body=raw, ok=(200, 201))
        loc = resp.header("Location")
        if not loc:
            raise PermanentApiError("resumable init returned no session URI")
        host = urlsplit(loc).hostname
        if host != "www.googleapis.com":
            raise EndpointNotAllowed("session URI points at an unexpected host")
        self._session_hosts.add(host)
        return loc

    def trust_session(self, session_uri: str) -> None:
        """Allow PUTs to a session URI loaded from protected local state."""
        host = urlsplit(session_uri).hostname
        if host != "www.googleapis.com":
            raise EndpointNotAllowed("stored session URI points at an unexpected host")
        self._session_hosts.add(host)

    def put_chunk(self, session_uri: str, data: bytes, start: int, total: int) -> Response:
        end = start + len(data) - 1
        return self.request("PUT", session_uri,
                            headers={"Content-Length": str(len(data)),
                                     "Content-Range": f"bytes {start}-{end}/{total}"},
                            body=data, ok=(200, 201, 308), retry=False)

    def query_status(self, session_uri: str, total: int) -> Response:
        """Ask the server how many bytes it has. 308=incomplete, 200/201=done, 404/410=gone."""
        return self.request("PUT", session_uri,
                            headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
                            body=b"", ok=(200, 201, 308, 404, 410))


def _error_reasons(resp: Response) -> set[str]:
    try:
        data = json.loads(resp.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return set()
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return set()
    return {e.get("reason") for e in err.get("errors", []) if isinstance(e, dict) and e.get("reason")}


def next_offset(resp: Response) -> int:
    """Parse the Range header of a 308: 'bytes=0-1234' means next byte is 1235."""
    rng = resp.header("Range")
    if not rng:
        return 0
    try:
        return int(rng.split("-")[-1]) + 1
    except ValueError:
        raise PermanentApiError("unparseable Range header on 308") from None
