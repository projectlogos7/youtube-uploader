"""YouTube client guards, OAuth/PKCE, redaction, config."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from studio_uploader import auth
from studio_uploader.audit import AuditLog
from studio_uploader.config import load_config
from studio_uploader.errors import (AuthError, DryRunViolation, EndpointNotAllowed)
from studio_uploader.keychain import MemoryStore
from studio_uploader.redact import redact, redact_text
from studio_uploader.transport import Response

from conftest import client_for, good_manifest_fields
from fake_youtube import ACCESS_TOKEN, CLIENT_SECRET, REFRESH_TOKEN, FakeYouTube

ROOT = Path(__file__).resolve().parents[1]


def _client(cfg, fake, live=True):
    return client_for(cfg, fake, AuditLog(cfg.log_dir, echo=io.StringIO()), live=live)


@pytest.mark.parametrize("method,url", [
    ("PUT", "https://www.googleapis.com/youtube/v3/videos?part=status"),        # videos.update
    ("POST", "https://www.googleapis.com/youtube/v3/thumbnails/set"),
    ("POST", "https://www.googleapis.com/youtube/v3/playlistItems"),
    ("DELETE", "https://www.googleapis.com/youtube/v3/videos?id=x"),
    ("GET", "http://www.googleapis.com/youtube/v3/channels"),
    ("PUT", "https://evil.example.com/upload/youtube/v3/videos?upload_id=x"),
])
def test_endpoint_allowlist(cfg, fake, method, url):
    c = _client(cfg, fake)
    with pytest.raises(EndpointNotAllowed):
        c.request(method, url)
    assert fake.calls == []


def test_dry_run_client_refuses_writes(cfg, fake):
    c = _client(cfg, fake, live=False)
    with pytest.raises(DryRunViolation):
        c.init_resumable(dict(good_manifest_fields(), file_sha256="0" * 64), 10, "video/mp4")
    assert fake.calls == []


def test_401_refreshes_once_then_hard_stops(cfg, fake):
    c = _client(cfg, fake)
    fake.status_queue["channels"] = [401]
    assert c.channels_mine()
    assert fake.count("token") == 2
    fake.status_queue["channels"] = [401, 401]
    with pytest.raises(AuthError):
        c.channels_mine()


def test_pkce_pair():
    v, ch = auth.pkce_pair()
    assert 43 <= len(v) <= 128
    assert ch == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_auth_url_scopes_and_pkce(cfg):
    url = auth.build_auth_url(cfg.oauth, "http://127.0.0.1:5555", "CHAL", "STATE")
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert set(q["scope"].split()) == {"https://www.googleapis.com/auth/youtube.upload",
                                       "https://www.googleapis.com/auth/youtube.readonly"}
    assert q["code_challenge_method"] == "S256" and q["code_challenge"] == "CHAL"
    assert q["redirect_uri"].startswith("http://127.0.0.1:")


class TokenEndpoint:
    def __init__(self):
        self.form = None

    def send(self, method, url, headers, body):
        self.form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        return Response(200, {}, json.dumps({"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                                             "expires_in": 3599, "scope": " ".join(auth.SCOPES)}).encode())


def _browser(state_override=None, error=None):
    def open_browser(url):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        params = {"state": state_override or q["state"], "scope": q["scope"]}
        if error:
            params["error"] = error
        else:
            params["code"] = "4/0AFAKEAUTHCODE1234567890"
        target = q["redirect_uri"] + "/?" + "&".join(f"{k}={urllib.request.quote(v)}" for k, v in params.items())
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
        return True
    return open_browser


def test_consent_flow_loopback_pkce(cfg, capsys):
    store = MemoryStore()
    tok = TokenEndpoint()
    auth.run_consent(cfg.oauth, tok, store, client_secret=CLIENT_SECRET, open_browser=_browser(), timeout_s=10)
    assert store.secret["refresh_token"] == REFRESH_TOKEN
    assert tok.form["grant_type"] == "authorization_code" and "code_verifier" in tok.form
    assert tok.form["redirect_uri"].startswith("http://127.0.0.1:")
    out = capsys.readouterr().out
    assert REFRESH_TOKEN not in out and ACCESS_TOKEN not in out


def test_consent_state_mismatch_rejected(cfg):
    store = MemoryStore()
    with pytest.raises(AuthError, match="state mismatch"):
        auth.run_consent(cfg.oauth, TokenEndpoint(), store, client_secret="x",
                         open_browser=_browser(state_override="forged"), timeout_s=10)
    assert store.secret is None


def test_consent_denied(cfg):
    with pytest.raises(AuthError, match="not granted"):
        auth.run_consent(cfg.oauth, TokenEndpoint(), MemoryStore(), client_secret="x",
                         open_browser=_browser(error="access_denied"), timeout_s=10)


def test_refresh_invalid_grant(cfg):
    fake = FakeYouTube()
    fake.token_error = "invalid_grant"
    p = auth.RefreshTokenProvider(cfg.oauth, MemoryStore({"refresh_token": REFRESH_TOKEN, "client_id": "c",
                                                           "client_secret": "s"}), fake)
    with pytest.raises(AuthError, match="invalid_grant"):
        p.access_token()


def test_refresh_without_stored_credential(cfg):
    p = auth.RefreshTokenProvider(cfg.oauth, MemoryStore(), FakeYouTube())
    with pytest.raises(AuthError, match="authorize"):
        p.access_token()


@pytest.mark.parametrize("text", [
    f"Authorization: Bearer {ACCESS_TOKEN}",
    f'{{"refresh_token": "{REFRESH_TOKEN}"}}',
    f"client_secret={CLIENT_SECRET}&x=1",
    "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=ABCsecret",
    "code=4/0AFAKEAUTHCODE1234567890",
])
def test_redact_text(text):
    out = redact_text(text)
    for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CLIENT_SECRET, "ABCsecret", "4/0AFAKEAUTHCODE1234567890"):
        assert secret not in out


def test_redact_keeps_error_codes():
    assert redact({"code": "kill_switch"}) == {"code": "kill_switch"}
    assert "XYZsecret" not in redact_text("GET /?state=a&code=XYZsecret&scope=b")


def test_redact_structure():
    out = redact({"session_uri": "x", "nested": [{"access_token": "y"}], "ok": "fine"})
    assert out == {"session_uri": "[REDACTED]", "nested": [{"access_token": "[REDACTED]"}], "ok": "fine"}


def test_example_config_loads(tmp_path):
    text = (ROOT / "config" / "config.example.toml").read_text()
    text = text.replace("~/StudioUploader", str(tmp_path / "base")).replace(
        "~/Movies/Approved Staging", str(tmp_path / "staging"))
    p = tmp_path / "c.toml"
    p.write_text(text)
    cfg = load_config(p)
    assert cfg.readiness.require_marker is True
    assert cfg.chunk_size % (256 * 1024) == 0


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('base_dir = "/tmp/a"\nstaging_dir = "/tmp/b"\nprivacy = "public"\n')
    with pytest.raises(ValueError, match="unknown"):
        load_config(p)


def test_repo_contains_no_secrets():
    pat = __import__("re").compile(r"ya29\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{10,}|\b1//0[A-Za-z0-9_-]{20,}")
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT).parts
        if not p.is_file() or rel[0] == "tests" or any(x.startswith(".") or x == "__pycache__" for x in rel[:-1]):
            continue
        assert not pat.search(p.read_text(errors="ignore")), p
