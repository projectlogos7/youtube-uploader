"""Web app + API + watcher, in one process (the "main" computer).

Environment:
  HOST_HOME   folder the web app may browse to pick this computer's watch folder (default: $HOME)
  DATA_DIR    settings, sign-ins, history, inbox      (default /data)
  PORT        port                                     (default 8765)
  LAN_ACCESS  1 = other computers on the network may use it (./start.sh --wifi)
  SERVER_URL  address other computers should use       (shown in the page)
"""

import ipaddress
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import frameio, youtube
from .folders import list_dirs
from .store import Store
from .watcher import MEDIA_TYPES, THIS_COMPUTER, Watcher

UI = (Path(__file__).parent / "ui.html").read_bytes()
AGENT = (Path(__file__).parent / "agent.py").read_bytes()
ONLINE_SECS = 45
POLL_SECS = 20


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


class Relay:
    """Passes requests (e.g. "list the folders in ~/Movies") to a laptop's helper, which
    picks them up by long-polling, and hands back its answer."""

    def __init__(self):
        self.cv = threading.Condition()
        self.jobs, self.answers = {}, {}

    def ask(self, cid, job, timeout=12):
        jid = secrets.token_hex(6)
        with self.cv:
            self.jobs.setdefault(cid, []).append({**job, "job": jid})
            self.cv.notify_all()
            if not self.cv.wait_for(lambda: jid in self.answers, timeout):
                self.jobs[cid] = [j for j in self.jobs.get(cid, []) if j["job"] != jid]
                raise RuntimeError("That computer isn't answering. Is it switched on and on the same Wi-Fi?")
            return self.answers.pop(jid)

    def wake(self, cid):
        with self.cv:
            self.jobs.setdefault(cid, []).append({"type": "update", "job": ""})
            self.cv.notify_all()

    def wait(self, cid, timeout=POLL_SECS):
        with self.cv:
            self.cv.wait_for(lambda: self.jobs.get(cid), timeout)
            return self.jobs.pop(cid, [])

    def answer(self, jid, result):
        with self.cv:
            self.answers[jid] = result
            self.cv.notify_all()


def make_handler(store, watcher, home, port=8765, lan=False, extra_hosts=(), server_url=""):
    redirect_uri = f"http://localhost:{port}/"
    relay = Relay()

    class Handler(BaseHTTPRequestHandler):
        server_version = "MediaUploader"
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass  # URLs can contain sign-in codes; don't print them

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
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                if "code" in q or "error" in q:      # Google sends the browser back here
                    try:
                        if "error" in q:
                            raise ValueError(f"Google sign-in was cancelled ({q['error']})")
                        youtube.finish_auth(store, q["code"], q.get("state", ""))
                        watcher.clear_reconnect_waits()
                        return self._redirect("/?ok=youtube")
                    except Exception as e:  # noqa: BLE001
                        return self._redirect("/?err=" + quote(str(e)[:300]))
                return self._send(200, UI, "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, status(store, watcher, lan, server_url))
            if u.path == "/api/browse":
                return self._call(lambda: browse(q.get("computer", "main"), q.get("path", "")))
            if u.path == "/api/frameio/projects":
                return self._call(lambda: {"projects": frameio.list_projects(store)})
            if u.path == "/agent.py":
                return self._send(200, AGENT, "text/x-python; charset=utf-8")
            return self._send(404, {"error": "not found"})

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
            route = ROUTES.get(path)
            if not route:
                return self._send(404, {"error": "not found"})
            return self._call(lambda: route(data))

        def _call(self, fn):
            try:
                return self._send(200, fn() or {"ok": True})
            except (youtube.NeedsReconnect, frameio.NeedsReconnect, ValueError, RuntimeError) as e:
                return self._send(400, {"error": str(e)})
            except OSError as e:
                return self._send(502, {"error": f"Network problem: {e}"})

        def _incoming(self):
            """A finished file sent by a laptop's helper. Saved into that laptop's inbox folder."""
            if self.headers.get("X-Media-Uploader") != "1":   # custom header = no cross-site posts
                return self._send(403, {"error": "missing header"})
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            cid = q.get("computer", "")
            if cid not in store.computers():
                return self._send(403, {"error": "unknown computer; add it in the web app first"})
            try:
                name = safe_name(q.get("name", ""))
                size = int(self.headers.get("Content-Length") or -1)
                if size <= 0:
                    raise ValueError("Empty file")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            box = watcher.inbox / cid
            tmp_dir = watcher.inbox / ".incoming"
            box.mkdir(parents=True, exist_ok=True)
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
                if self.headers.get("X-Sha256") not in (None, h.hexdigest()):
                    raise ValueError("File was damaged in transfer")
                target = box / name
                n = 1
                while target.exists():
                    target = box / f"{Path(name).stem} ({n}){Path(name).suffix}"
                    n += 1
                os.replace(tmp, target)
            except (ValueError, OSError) as e:
                tmp.unlink(missing_ok=True)
                return self._send(400, {"error": str(e)})
            store.computer_sent(cid)
            watcher.poke()
            return self._send(200, {"ok": True, "saved_as": target.name})

    # ---- actions -----------------------------------------------------------------
    def browse(computer, path):
        if computer == "main":
            return list_dirs(path, root=home)
        if computer not in store.computers():
            raise ValueError("Unknown computer")
        return relay.ask(computer, {"type": "browse", "path": path})

    def set_folder(d):
        computer, folder = d.get("computer", "main"), str(d.get("path") or "")
        if computer == "main":
            p = Path(folder).resolve()
            if not p.is_dir() or (p != home and home not in p.parents):
                raise ValueError("Pick a folder inside your home folder")
            store.update({"folder": str(p)})
            watcher.poke()
        elif computer in store.computers():
            store.set_computer_folder(computer, folder)
            relay.wake(computer)
        else:
            raise ValueError("Unknown computer")

    def poll(d):
        """Laptop helper checks in; held open until there's something for it (or 20 s)."""
        cid = str(d.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", cid):
            raise ValueError("bad computer id")
        known = cid in store.computers()
        if not known and not d.get("register"):
            return {"removed": True}
        store.computer_seen(cid, name=str(d.get("name") or "Computer")[:80],
                            waiting=[str(x)[:200] for x in (d.get("waiting") or [])][:50],
                            problem=str(d.get("problem") or "")[:200])
        jobs = [] if d.get("no_wait") else relay.wait(cid)
        if cid not in store.computers():
            return {"removed": True}
        store.computer_seen(cid)
        return {"folder": store.computers()[cid].get("folder", ""),
                "jobs": [j for j in jobs if j["type"] != "update"]}

    def reply(d):
        relay.answer(str(d.get("job") or ""), d.get("result"))

    def remove_computer(d):
        cid = str(d.get("id") or "")
        store.remove_computer(cid)
        relay.wake(cid)

    def settings(d):
        store.update({k: bool(d[k]) for k in ("enabled", "to_youtube", "to_frameio") if k in d})
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
                return None
        raise ValueError("Project not found")

    ROUTES = {
        "/api/settings": settings, "/api/scan": scan, "/api/folder": set_folder,
        "/api/computers/poll": poll, "/api/computers/reply": reply, "/api/computers/remove": remove_computer,
        "/api/youtube/connect": yt_connect, "/api/youtube/finish": yt_finish,
        "/api/youtube/disconnect": lambda d: youtube.disconnect(store),
        "/api/frameio/connect": fio_connect, "/api/frameio/finish": fio_finish,
        "/api/frameio/project": fio_project, "/api/frameio/disconnect": lambda d: frameio.disconnect(store),
    }
    Handler.relay = relay
    return Handler


def status(store, watcher, lan=False, server_url=""):
    s = store.settings()
    w = watcher.snapshot()
    now = time.time()
    computers = [{"id": cid, "name": c.get("name"), "folder": c.get("folder", ""), "sent": c.get("sent", 0),
                  "waiting": c.get("waiting", []), "problem": c.get("problem", ""),
                  "online": now - c.get("last_seen", 0) < ONLINE_SECS,
                  "last_seen": time.strftime("%d %b %H:%M", time.localtime(c.get("last_seen", 0)))}
                 for cid, c in store.computers().items()]
    return {
        "lan": lan, "server_url": server_url, "enabled": s["enabled"],
        "to_youtube": s["to_youtube"], "to_frameio": s["to_frameio"],
        "main": {"name": THIS_COMPUTER, "folder": s["folder"],
                 "folder_ok": bool(s["folder"]) and Path(s["folder"]).is_dir()},
        "computers": computers,
        "youtube": {"connected": youtube.connected(store), "channel": s["youtube"]["channel"],
                    "client_id": s["youtube"]["client_id"]},
        "frameio": {"connected": frameio.connected(store), "project": s["frameio"]["project_name"],
                    "ready": frameio.ready(store), "client_id": s["frameio"]["client_id"]},
        "current": w["current"], "last_scan": w["last_scan"], "notice": w["notice"], "files": w["files"],
        "history": list(reversed(store.history(40))),
    }


def main():
    home = Path(os.environ.get("HOST_HOME") or Path.home()).resolve()
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    port = int(os.environ.get("PORT", "8765"))
    lan = os.environ.get("LAN_ACCESS", "0").lower() in ("1", "true", "yes", "on")
    server_url = os.environ.get("SERVER_URL", "")
    extra = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
    if server_url:
        extra.add(urlsplit(server_url).hostname or "")

    store = Store(data_dir)
    watcher = Watcher(store, data_dir / "inbox")
    watcher.start()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store, watcher, home, port, lan, extra, server_url))
    server.daemon_threads = True
    where = server_url if lan and server_url else f"http://localhost:{port}"
    print(f"Media Uploader running. Open {where}  "
          f"({'open to your network' if lan else 'this computer only'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
