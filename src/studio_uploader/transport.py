"""Minimal HTTP transport. Tests replace it with a mocked YouTube API.

Uses http.client directly so a 308 (resumable "Resume Incomplete") is never
treated as a redirect.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from .errors import TransportError


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.headers.items():
            if k.lower() == name:
                return v
        return None


class Transport(Protocol):
    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response: ...


class HttpTransport:
    def __init__(self, timeout_s: float = 120.0):
        self.timeout_s = timeout_s
        self._ctx = ssl.create_default_context()

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise TransportError("only https is allowed")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn = http.client.HTTPSConnection(parts.hostname, parts.port or 443,
                                           timeout=self.timeout_s, context=self._ctx)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return Response(resp.status, {k: v for k, v in resp.getheaders()}, data)
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            # Message deliberately excludes the URL (could be a session URI).
            raise TransportError(f"{type(exc).__name__} talking to {parts.hostname}") from None
        finally:
            conn.close()
