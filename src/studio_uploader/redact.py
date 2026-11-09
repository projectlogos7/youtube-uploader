"""Redaction for anything that reaches a log, report or terminal (RS-9)."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = {
    "access_token", "refresh_token", "id_token", "client_secret",
    "code_verifier", "authorization", "session_uri", "location", "upload_id",
    "password", "token", "secret",
}

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Resumable session URIs carry upload_id; treat the whole URI as secret.
    (re.compile(r"https?://[^\s\"']*upload_id=[^\s\"']*"), "[REDACTED_SESSION_URI]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/\-]+=*"), r"\1 [REDACTED]"),
    (re.compile(r"ya29\.[A-Za-z0-9._\-]+"), "[REDACTED_ACCESS_TOKEN]"),
    (re.compile(r"\b1//[A-Za-z0-9._\-]+"), "[REDACTED_REFRESH_TOKEN]"),
    (re.compile(r"GOCSPX-[A-Za-z0-9._\-]+"), "[REDACTED_CLIENT_SECRET]"),
    (re.compile(r"\b4/[0-9A-Za-z_\-]{10,}"), "[REDACTED_AUTH_CODE]"),
    # OAuth authorization code in a query string or form body.
    (re.compile(r"(?<![A-Za-z0-9_])(code=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(
        r"(?i)(?<![A-Za-z0-9_])(\"?(?:access_token|refresh_token|id_token|client_secret|"
        r"code_verifier|upload_id|session_uri|authorization)\"?\s*[:=]\s*\"?)[^\"&\s,}]+"),
     r"\1[REDACTED]"),
]


def redact_text(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def redact(value: Any) -> Any:
    """Recursively redact a JSON-like structure."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
