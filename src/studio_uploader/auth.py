"""Owner authorization (FR-1): installed-app flow with PKCE and loopback redirect.

* Desktop OAuth client in an owner-controlled Google Cloud project.
* Scopes: youtube.upload and youtube.readonly only.
* Owner consents in their own browser; redirect to 127.0.0.1 on a random port.
* invalid_grant (expired/revoked) is a hard stop asking for re-authorization.
  There is no silent retry.

Consent is not approval of any video.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import webbrowser
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import OAuthConfig
from .errors import AuthError, TransportError
from .keychain import SecretStore
from .transport import Transport
from .youtube import TokenProvider

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(oauth: OAuthConfig, redirect_uri: str, challenge: str, state: str,
                   login_hint: str = "") -> str:
    params = {
        "client_id": oauth.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{oauth.auth_uri}?{urlencode(params)}"


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlsplit(self.path).query)
        type(self).result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authorization received. You can close this tab and return to the terminal.")

    def log_message(self, *args):  # silence: request line contains the auth code
        pass


def run_consent(oauth: OAuthConfig, transport: Transport, store: SecretStore, *,
                client_secret: str, login_hint: str = "",
                open_browser: Callable[[str], bool] = webbrowser.open,
                timeout_s: float = 300.0) -> None:
    """Run once, by the channel owner, in their own browser."""
    if not oauth.client_id:
        raise AuthError("oauth.client_id is not configured")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)

    _CodeCatcher.result = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    server.timeout = 1.0
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"
    url = build_auth_url(oauth, redirect_uri, challenge, state, login_hint)
    print("Opening the Google consent page in the owner's browser.\n"
          "If it does not open, paste this URL into the owner's browser:\n" + url)
    open_browser(url)

    deadline = time.monotonic() + timeout_s
    try:
        while not _CodeCatcher.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    res = _CodeCatcher.result
    _CodeCatcher.result = {}
    if not res:
        raise AuthError("timed out waiting for owner consent")
    if res.get("error"):
        raise AuthError(f"consent was not granted: {res['error']}")
    if not secrets.compare_digest(res.get("state", ""), state):
        raise AuthError("state mismatch on OAuth redirect; aborting")
    granted = set((res.get("scope") or "").split())
    if granted and not set(SCOPES) <= granted:
        raise AuthError("required scopes were not granted")

    body = urlencode({"code": res["code"], "client_id": oauth.client_id, "client_secret": client_secret,
                      "code_verifier": verifier, "grant_type": "authorization_code",
                      "redirect_uri": redirect_uri}).encode()
    resp = transport.send("POST", oauth.token_uri,
                          {"Content-Type": "application/x-www-form-urlencoded"}, body)
    data = _json(resp.body)
    if resp.status != 200 or "refresh_token" not in data:
        raise AuthError(f"token exchange failed: {data.get('error', resp.status)}")
    store.write({"refresh_token": data["refresh_token"], "client_id": oauth.client_id,
                 "client_secret": client_secret, "scope": data.get("scope", ""),
                 "obtained_at": int(time.time())})


def _json(b: bytes) -> dict:
    try:
        d = json.loads(b or b"{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


class RefreshTokenProvider(TokenProvider):
    """Exchanges the stored refresh token for short-lived access tokens.

    The access token lives in memory only.
    """

    def __init__(self, oauth: OAuthConfig, store: SecretStore, transport: Transport,
                 now: Callable[[], float] = time.time):
        self.oauth = oauth
        self.store = store
        self.transport = transport
        self.now = now
        self._token: str | None = None
        self._expires = 0.0

    def invalidate(self) -> None:
        self._token = None
        self._expires = 0.0

    def access_token(self) -> str:
        if self._token and self.now() < self._expires - 60:
            return self._token
        secret = self.store.read()
        if not secret or not secret.get("refresh_token"):
            raise AuthError("no stored credential; owner must run `studio-uploader authorize`")
        body = urlencode({"client_id": secret["client_id"], "client_secret": secret["client_secret"],
                          "refresh_token": secret["refresh_token"],
                          "grant_type": "refresh_token"}).encode()
        try:
            resp = self.transport.send("POST", self.oauth.token_uri,
                                       {"Content-Type": "application/x-www-form-urlencoded"}, body)
        except TransportError as exc:
            raise AuthError(f"token endpoint unreachable: {exc}") from None
        data = _json(resp.body)
        if resp.status != 200:
            err = data.get("error", f"HTTP {resp.status}")
            if err == "invalid_grant":
                raise AuthError("invalid_grant: consent expired or revoked; owner re-authorization required")
            raise AuthError(f"token refresh failed: {err}")
        granted = set(data.get("scope", "").split())
        if granted - set(SCOPES):
            raise AuthError("token carries unexpected scopes; refusing to use it")
        self._token = data["access_token"]
        self._expires = self.now() + float(data.get("expires_in", 3600))
        return self._token


def revoke(oauth: OAuthConfig, store: SecretStore, transport: Transport) -> bool:
    secret = store.read()
    if not secret:
        return False
    body = urlencode({"token": secret["refresh_token"]}).encode()
    resp = transport.send("POST", oauth.revoke_uri, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    return resp.status == 200


class OfflineTokenProvider(TokenProvider):
    """Used for --offline dry runs: any attempt to get a token is an error."""

    def access_token(self) -> str:
        raise AuthError("offline dry run: no API calls permitted")

    def invalidate(self) -> None:
        pass


def token_age_days(store: SecretStore, now: Callable[[], float] = time.time) -> float | None:
    """Age of the stored refresh token in days, from the consent timestamp."""
    secret = store.read()
    if not secret or not secret.get("obtained_at"):
        return None
    return (now() - float(secret["obtained_at"])) / 86400.0


def check_token_age(age_days: float | None, oauth: OAuthConfig) -> str | None:
    """Return a warning, or raise AuthError if a Testing-mode token has expired."""
    if age_days is None or not oauth.testing_mode:
        return None
    if age_days >= oauth.token_max_age_days:
        raise AuthError(f"refresh token is {age_days:.1f} days old; Testing-mode tokens expire after "
                        f"{oauth.token_max_age_days:.0f} days. Owner must run `authorize` again")
    if age_days >= oauth.token_max_age_days - 1:
        return (f"refresh token expires in under a day ({age_days:.1f} of "
                f"{oauth.token_max_age_days:.0f} days); schedule owner re-consent")
    return None
