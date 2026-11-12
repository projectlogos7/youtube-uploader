"""Web UI + API, and the watcher, in one process.

Environment:
  WATCH_DIR      folder to watch                       (default /watch)
  DATA_DIR       settings, sign-ins, history           (default /data)
  PORT           port the browser uses                 (default 8765)
  LAN_ACCESS     1 = other computers on the network may use it (set by ./start.sh --wifi)
  SERVER_URL     address other computers should use    (shown in the page)
"""

import ipaddress
import json
import os
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import frameio, youtube
from .store import Store
from .watcher import MEDIA_TYPES, Watcher

UI = (Path(__file__).parent / "ui.html").read_bytes()
AGENT = (Path(__file__).parent / "agent.py").read_bytes()
ONLINE_SECS = 60


def host_allowed(host, lan, extra=()):
    """localhost always; on the network also IP addresses and *.local names.
    Other names are refused so a web page elsewhere can't trick a browser into calling us."""
    host = host.strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host in extra:
        return True
    if not lan:
        return False
    if host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def safe_name(name):
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f/:]", "_", name)
    if not name or name.startswith(".") or Path(name).suffix.lower() not in MEDIA_TYPES:
        raise ValueError("Only media files can be sent")
    return name[:200]


def make_handler(store, watcher, port=8765, lan=False, extra_hosts=(), server_url=""):
    redirect_uri = f"http://localhost:{port}/"

    class Handler(BaseHTTPRequestHandler):
        server_version = "MediaUploader"

        def log_message(self, fmt, *args):
            pass  # URLs can contain sign-in codes; don't print them

        # ---- safety: only this machine, only this page -----------------------
        def _host_ok(self):
            raw = self.headers.get("Host") or ""
            host = raw[1:raw.index("]")] if raw.startswith("[") else raw.rsplit(":", 1)[0]
            if not host_allowed(host, lan, extra_hosts):
                return False
            origin = self.headers.get("Origin")
            return not origin or urlsplit(origin).netloc == raw

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where):
            self.send_response(303)
            self.send_header("Location", where)
            self.end_headers()

        # ---- GET -------------------------------------------------------------
        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                if "code" in q or "error" in q:      # Google sends the user back here
                    try:
                        if "error" in q:
                            raise ValueError(f"Google sign-in was cancelled ({q['error']})")
                        youtube.finish_auth(store, q["code"], q.get("state", ""))
                        watcher.clear_reconnect_waits()
                        return self._redirect("/?ok=youtube")
                    except Exception as e:  # noqa: BLE001
                        return self._redirect("/?err=" + _q(str(e)))
                return self._send(200, UI, "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, status(store, watcher, lan, server_url))
            if u.path == "/agent.py":
                return self._send(200, AGENT, "text/x-python; charset=utf-8")
            if u.path == "/api/frameio/projects":
                return self._call(lambda: {"projects": frameio.list_projects(store)})
            return self._send(404, {"error": "not found"})

        # ---- POST ------------------------------------------------------------
        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            path = urlsplit(self.path).path
            if path == "/api/incoming":
                return self._incoming()
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(415, {"error": "JSON only"})
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad JSON"})
            route = ROUTES.get(urlsplit(self.path).path)
            if not route:
                return self._send(404, {"error": "not found"})
            return self._call(lambda: route(data))

        def _incoming(self):
            """A file sent from another computer (or dropped on the page). Saved into the
            watch folder, where the watcher picks it up like any other file."""
            if self.headers.get("X-Media-Uploader") != "1":   # custom header = no cross-site posts
                return self._send(403, {"error": "missing header"})
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                name = safe_name(q.get("name", ""))
                size = int(self.headers.get("Content-Length") or -1)
                if size <= 0:
                    raise ValueError("Empty file")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            cid = q.get("computer", "")
            source = (store.computers().get(cid) or {}).get("name") or q.get("from") or "Web page"
            tmp_dir = watcher.folder / ".incoming"
            tmp_dir.mkdir(exist_ok=True)
            tmp = tmp_dir / f"{secrets.token_hex(8)}.part"
            h = __import__("hashlib").sha256()
            try:
                with open(tmp, "wb") as f:
                    left = size
                    while left:
                        block = self.rfile.read(min(left, 1 << 20))
                        if not block:
                            raise ValueError("Transfer was cut off")
                        f.write(block)
                        h.update(block)
                        left -= len(block)
                want = self.headers.get("X-Sha256")
                if want and want != h.hexdigest():
                    raise ValueError("File was damaged in transfer")
                target = watcher.folder / name
                n = 1
                while target.exists():
                    target = watcher.folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
                    n += 1
                os.replace(tmp, target)
                past = time.time() - 3600          # finished file: no need to wait for it to settle
                os.utime(target, (past, past))
            except (ValueError, OSError) as e:
                tmp.unlink(missing_ok=True)
                return self._send(400, {"error": str(e)})
            watcher.sources[target.name] = source
            if cid:
                store.computer_sent(cid)
            watcher.poke()
            return self._send(200, {"ok": True, "saved_as": target.name})

        def _call(self, fn):
            try:
                return self._send(200, fn() or {"ok": True})
            except (youtube.NeedsReconnect, frameio.NeedsReconnect, ValueError, RuntimeError) as e:
                return self._send(400, {"error": str(e)})
            except OSError as e:
                return self._send(502, {"error": f"Network problem: {e}"})

    # ---- actions ---------------------------------------------------------------
    def settings(d):
        patch = {k: bool(d[k]) for k in ("enabled", "to_youtube", "to_frameio") if k in d}
        store.update(patch)
        watcher.poke()

    def scan(d):
        watcher.retry_at.clear()
        watcher.poke()

    def yt_connect(d):
        cid, secret = (d.get("client_id") or "").strip(), (d.get("client_secret") or "").strip()
        if not cid or not secret:
            raise ValueError("Enter the Google client ID and client secret")
        return {"url": youtube.start_auth(store, cid, secret, redirect_uri)}

    def yt_finish(d):
        youtube.finish_from_address(store, d.get("address") or "")
        watcher.clear_reconnect_waits()

    def yt_disconnect(d):
        youtube.disconnect(store)

    def heartbeat(d):
        cid = str(d.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", cid):
            raise ValueError("bad computer id")
        if d.get("register") or cid in store.computers():
            store.computer_seen(cid, name=str(d.get("name") or "Computer")[:80],
                                folder=str(d.get("folder") or "")[:300],
                                waiting=[str(x)[:200] for x in (d.get("waiting") or [])][:50])
            return {"ok": True}
        return {"removed": True}

    def remove_computer(d):
        store.remove_computer(str(d.get("id") or ""))

    def fio_connect(d):
        cid, secret = (d.get("client_id") or "").strip(), (d.get("client_secret") or "").strip()
        if not cid or not secret:
            raise ValueError("Enter the Adobe client ID and client secret")
        return {"url": frameio.start_auth(store, cid, secret)}

    def fio_finish(d):
        projects = frameio.finish_auth(store, d.get("address") or "")
        watcher.clear_reconnect_waits()
        return {"projects": projects}

    def fio_project(d):
        for p in frameio.list_projects(store):
            if p["project_id"] == d.get("project_id"):
                frameio.choose_project(store, p)
                watcher.clear_reconnect_waits()
                return
        raise ValueError("Project not found")

    def fio_disconnect(d):
        frameio.disconnect(store)

    ROUTES = {
        "/api/settings": settings, "/api/scan": scan,
        "/api/youtube/connect": yt_connect, "/api/youtube/finish": yt_finish,
        "/api/youtube/disconnect": yt_disconnect,
        "/api/computers/heartbeat": heartbeat, "/api/computers/remove": remove_computer,
        "/api/frameio/connect": fio_connect, "/api/frameio/finish": fio_finish,
        "/api/frameio/project": fio_project, "/api/frameio/disconnect": fio_disconnect,
    }
    return Handler


def _q(s):
    from urllib.parse import quote
    return quote(s[:300])


def status(store, watcher, lan=False, server_url=""):
    s = store.settings()
    w = watcher.snapshot()
    now = time.time()
    computers = [{"id": cid, "name": c.get("name"), "folder": c.get("folder"), "sent": c.get("sent", 0),
                  "waiting": c.get("waiting", []), "online": now - c.get("last_seen", 0) < ONLINE_SECS,
                  "last_seen": time.strftime("%Y-%m-%d %H:%M", time.localtime(c.get("last_seen", 0)))}
                 for cid, c in store.computers().items()]
    return {
        "lan": lan, "server_url": server_url, "computers": computers,
        "enabled": s["enabled"], "to_youtube": s["to_youtube"], "to_frameio": s["to_frameio"],
        "watch_dir": os.environ.get("FOLDER_LABEL") or str(watcher.folder), "watch_dir_ok": watcher.folder.is_dir(),
        "youtube": {"connected": youtube.connected(store), "channel": s["youtube"]["channel"],
                    "client_id": s["youtube"]["client_id"]},
        "frameio": {"connected": frameio.connected(store), "project": s["frameio"]["project_name"],
                    "ready": frameio.ready(store), "client_id": s["frameio"]["client_id"]},
        "current": w["current"], "last_scan": w["last_scan"], "notice": w["notice"],
        "files": w["files"],
        "history": list(reversed(store.history(40))),
    }


def main():
    watch_dir = os.environ.get("WATCH_DIR", "/watch")
    data_dir = os.environ.get("DATA_DIR", "/data")
    port = int(os.environ.get("PORT", "8765"))
    lan = os.environ.get("LAN_ACCESS", "0").lower() in ("1", "true", "yes", "on")
    server_url = os.environ.get("SERVER_URL", "")
    extra = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
    if server_url:
        extra.add(urlsplit(server_url).hostname or "")

    store = Store(data_dir)
    watcher = Watcher(store, watch_dir)
    watcher.start()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, watcher, port, lan, extra, server_url))
    where = server_url if lan and server_url else f"http://localhost:{port}"
    print(f"Media Uploader running. Open {where}  (watching {watch_dir}, "
          f"{'open to the local network' if lan else 'this computer only'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
