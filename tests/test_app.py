"""All tests use fake YouTube / Frame.io servers. Nothing touches the network."""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import frameio, net, server, youtube
from app.store import Store
from app.watcher import Watcher
from fakes import FakeCloud


@pytest.fixture
def env(tmp_path, monkeypatch):
    cloud = FakeCloud()
    monkeypatch.setattr(net, "request", cloud)
    monkeypatch.setattr(youtube, "_sleep", lambda s: None)
    monkeypatch.setattr(frameio, "_sleep", lambda s: None)
    store = Store(tmp_path / "data")
    home = tmp_path / "home"
    folder = home / "Movies" / "Upload"
    folder.mkdir(parents=True)
    clock = {"t": time.time()}
    w = Watcher(store, tmp_path / "data" / "inbox", settle=20, clock=lambda: clock["t"])

    class E:
        pass
    e = E()
    e.cloud, e.store, e.home, e.folder, e.w, e.clock, e.tmp = cloud, store, home, folder, w, clock, tmp_path
    return e


def connect_youtube(e):
    url = youtube.start_auth(e.store, "gid", "gsecret", "http://localhost:8765/")
    youtube.finish_auth(e.store, "code1", parse_qs(urlsplit(url).query)["state"][0])


def connect_frameio(e, pick=True):
    url = frameio.start_auth(e.store, "aid", "asecret")
    state = parse_qs(urlsplit(url).query)["state"][0]
    projects = frameio.finish_auth(e.store, f"https://localhost/?code=c1&state={state}")
    if pick:
        frameio.choose_project(e.store, projects[0])
    return projects


def add_file(e, name="Episode 1.mp4", size=700_000, age=100, folder=None):
    p = (folder or e.folder) / name
    p.write_bytes(os.urandom(size))
    t = e.clock["t"] - age
    os.utime(p, (t, t))
    return p


def watch_main(e, **extra):
    e.store.update({"enabled": True, "folder": str(e.folder), **extra})


def settle_and_scan(e):
    e.w.scan()
    e.clock["t"] += 30
    e.w.scan()


# ---- accounts ------------------------------------------------------------------

def test_youtube_connect_stores_secret_privately(env):
    connect_youtube(env)
    assert youtube.connected(env.store) and env.store.settings()["youtube"]["channel"] == "The Network"
    assert (env.store.secrets_path.stat().st_mode & 0o777) == 0o600
    assert "g-rt" not in env.store.settings_path.read_text()


def test_youtube_bad_state_rejected(env):
    youtube.start_auth(env.store, "gid", "s", "http://localhost:8765/")
    with pytest.raises(ValueError):
        youtube.finish_auth(env.store, "code", "forged")


def test_frameio_connect_and_pick_project(env):
    projects = connect_frameio(env, pick=False)
    assert [p["name"] for p in projects] == ["Episodes", "Reels"] and not frameio.ready(env.store)
    frameio.choose_project(env.store, projects[1])
    assert frameio.ready(env.store) and env.store.settings()["frameio"]["folder_id"] == "f2"


# ---- watcher on the main computer --------------------------------------------------

def test_off_by_default_and_no_folder(env):
    connect_youtube(env)
    p = add_file(env)
    env.w.interval, env.w.settle = 0.02, 0
    env.w.start()
    time.sleep(0.3)
    assert env.store.settings()["enabled"] is False and env.cloud.yt_inserts == [] and p.exists()
    env.store.update({"enabled": True})          # on, but no folder chosen yet
    env.w.scan()
    assert "Choose a watch folder" in env.w.snapshot()["notice"] and p.exists()


def test_youtube_only(env):
    connect_youtube(env)
    watch_main(env, to_youtube=True, to_frameio=False)
    p = add_file(env)
    settle_and_scan(env)
    q, meta = env.cloud.yt_inserts[0]
    assert meta["status"]["privacyStatus"] == "private" and q["notifySubscribers"] == ["false"]
    assert meta["snippet"]["title"] == "Episode 1"
    assert not p.exists() and (env.folder / "Uploaded" / "Episode 1.mp4").exists()
    assert "YouTube: https://youtu.be/vid1" in (env.folder / "Uploaded" / "Episode 1.mp4.links.txt").read_text()
    h = env.store.history()[-1]
    assert h["link"] == "https://youtu.be/vid1" and h["source"] == "This computer"


def test_both_destinations(env):
    connect_youtube(env)
    connect_frameio(env)
    watch_main(env, to_youtube=True, to_frameio=True)
    add_file(env)
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"] and sum(env.cloud.fio_parts) == 700_000
    links = (env.folder / "Uploaded" / "Episode 1.mp4.links.txt").read_text()
    assert "Frame.io: https://f.io/AbCd" in links


def test_frameio_takes_non_video_youtube_does_not(env):
    connect_youtube(env)
    connect_frameio(env)
    watch_main(env, to_youtube=True, to_frameio=False)
    p = add_file(env, "song.wav", 5000)
    settle_and_scan(env)
    assert p.exists() and env.cloud.yt_inserts == []
    assert "only takes video" in env.w.snapshot()["files"][0]["status"]
    env.store.update({"to_frameio": True})
    env.w.scan()
    assert not p.exists() and len(env.cloud.fio_files) == 1


def test_waits_while_file_is_changing(env):
    connect_youtube(env)
    watch_main(env)
    p = add_file(env, age=0)
    env.w.scan()
    env.clock["t"] += 30
    with open(p, "ab") as f:
        f.write(b"more")
    env.w.scan()
    assert env.cloud.yt_inserts == [] and "waiting" in env.w.snapshot()["files"][0]["status"]


def test_ignores_hidden_and_other_files(env):
    connect_youtube(env)
    watch_main(env)
    add_file(env, ".partial.mp4")
    add_file(env, "notes.txt")
    settle_and_scan(env)
    assert env.cloud.yt_inserts == []


def test_frameio_failure_does_not_repeat_youtube(env):
    connect_youtube(env)
    connect_frameio(env)
    watch_main(env, to_youtube=True, to_frameio=True)
    p = add_file(env)
    env.cloud.fail["s3"] = 99
    settle_and_scan(env)
    assert p.exists() and env.cloud.yt_videos == ["vid1"]
    assert "trying again" in env.w.snapshot()["files"][0]["status"]
    env.cloud.fail["s3"] = 0
    env.clock["t"] += 3600
    env.w.scan()
    assert env.cloud.yt_videos == ["vid1"] and not p.exists()


def test_same_file_never_uploaded_twice(env):
    connect_youtube(env)
    watch_main(env)
    data = add_file(env).read_bytes()
    settle_and_scan(env)
    copy = env.folder / "Copy.mp4"
    copy.write_bytes(data)
    os.utime(copy, (env.clock["t"] - 100,) * 2)
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"] and not copy.exists()


def test_network_drop_resumes(env):
    connect_youtube(env)
    watch_main(env)
    env.cloud.fail["yt_put"] = 1
    add_file(env)
    settle_and_scan(env)
    assert env.cloud.yt_videos == ["vid1"]


def test_share_link_falls_back_to_team_link(env):
    connect_frameio(env)
    env.cloud.share_ok = False
    watch_main(env, to_youtube=False, to_frameio=True)
    add_file(env)
    settle_and_scan(env)
    assert env.store.history()[-1]["link"] == "https://next.frame.io/project/pr1/view/file1"


def test_expired_sign_in_waits_for_reconnect(env):
    connect_youtube(env)
    watch_main(env)
    env.cloud.token_error = "invalid_grant"
    p = add_file(env)
    settle_and_scan(env)
    assert p.exists() and "reconnect" in env.w.snapshot()["files"][0]["status"]
    env.cloud.token_error = None
    env.w.clear_reconnect_waits()
    env.w.scan()
    assert not p.exists()


# ---- web app -------------------------------------------------------------------------

@pytest.fixture(params=[True])
def web(env, request):
    handler = server.make_handler(env.store, env.w, env.home.resolve(), 8765, lan=request.param)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env.relay = handler.relay
    yield env, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(base, path, body=None, headers=None, timeout=10):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=h, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}") if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_page_and_status_hide_secrets(web):
    env, base = web
    code, html = call(base, "/")
    assert code == 200 and b"Watch folders" in html
    connect_youtube(env)
    code, s = call(base, "/api/status")
    assert s["youtube"]["connected"] and "g-rt" not in json.dumps(s) and "gsecret" not in json.dumps(s)


def test_pick_main_folder_in_web_app(web):
    env, base = web
    code, r = call(base, "/api/browse?computer=main&path=")
    assert code == 200 and r["path"] == str(env.home.resolve()) and r["dirs"] == ["Movies"] and r["parent"] is None
    code, r = call(base, "/api/browse?computer=main&path=" + urllib.parse.quote(str(env.home / "Movies")))
    assert r["dirs"] == ["Upload"]
    code, r = call(base, "/api/browse?computer=main&path=/etc")          # can't leave home
    assert r["path"] == str(env.home.resolve())
    code, _ = call(base, "/api/folder", {"computer": "main", "path": str(env.folder)})
    assert code == 200 and env.store.settings()["folder"] == str(env.folder.resolve())
    code, r = call(base, "/api/folder", {"computer": "main", "path": "/etc"})
    assert code == 400


def test_google_redirect_finishes_sign_in(web):
    env, base = web
    _, r = call(base, "/api/youtube/connect", {"client_id": "gid", "client_secret": "gsecret"})
    state = parse_qs(urlsplit(r["url"]).query)["state"][0]
    opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                                              {"redirect_request": lambda *a, **k: None}))
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(f"{base}/?code=abc&state={state}", timeout=5)
    assert e.value.code == 303 and e.value.headers["Location"] == "/?ok=youtube"
    assert youtube.connected(env.store)


def test_youtube_paste_from_other_computer(web):
    env, base = web
    _, r = call(base, "/api/youtube/connect", {"client_id": "gid", "client_secret": "gsecret"})
    state = parse_qs(urlsplit(r["url"]).query)["state"][0]
    code, _ = call(base, "/api/youtube/finish", {"address": f"http://localhost:8765/?state={state}&code=c"})
    assert code == 200 and youtube.connected(env.store)


def test_frameio_flow_over_http(web):
    env, base = web
    _, r = call(base, "/api/frameio/connect", {"client_id": "aid", "client_secret": "s"})
    state = parse_qs(urlsplit(r["url"]).query)["state"][0]
    code, r = call(base, "/api/frameio/finish", {"address": f"https://localhost/?code=x&state={state}"})
    assert code == 200 and len(r["projects"]) == 2
    code, _ = call(base, "/api/frameio/project", {"project_id": "pr2"})
    assert code == 200 and env.store.settings()["frameio"]["project_name"] == "Reels"


def test_rejects_other_sites(web):
    env, base = web
    assert call(base, "/api/settings", {"enabled": True}, {"Origin": "https://evil.example"})[0] == 403
    assert call(base, "/api/settings", {"enabled": True}, {"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(base + "/api/settings", data=b"enabled=1", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 415 and env.store.settings()["enabled"] is False


def test_host_rules():
    assert server.host_allowed("localhost", lan=False)
    assert not server.host_allowed("192.168.1.20", lan=False)
    assert server.host_allowed("192.168.1.20", lan=True)
    assert server.host_allowed("studio-mac.local", lan=True)
    assert not server.host_allowed("evil.example", lan=True)


@pytest.mark.parametrize("web", [False], indirect=True)
def test_local_only_refuses_network_address(web):
    env, base = web
    assert call(base, "/api/status", headers={"Host": "192.168.1.20:8765"})[0] == 403
    assert call(base, "/api/status")[0] == 200


# ---- other computers ---------------------------------------------------------------

def test_unknown_computer_cannot_send(web):
    env, base = web
    req = urllib.request.Request(f"{base}/api/incoming?name=a.mp4&computer=nobody123", data=b"x", method="POST",
                                 headers={"X-Media-Uploader": "1", "Content-Type": "application/octet-stream"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403


def test_offline_computer_browse_times_out(web, monkeypatch):
    env, base = web
    env.store.computer_seen("laptop0001", name="Laptop")
    monkeypatch.setattr(server.Relay.ask, "__defaults__", (0.3,))
    code, r = call(base, "/api/browse?computer=laptop0001&path=")
    assert code == 400 and "isn't answering" in r["error"]


def test_laptop_end_to_end(web, monkeypatch):
    """A laptop helper connects, the web app browses its folders and picks one,
    the helper sends a finished file, the main computer uploads it."""
    import importlib
    env, base = web
    agent = importlib.import_module("app.agent")
    laptop_home = env.tmp / "laptop"
    upload_dir = laptop_home / "Footage"
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(agent, "SETTLE", 0)
    monkeypatch.setattr(agent, "EVERY", 0.05)
    monkeypatch.setattr(agent, "save", lambda cfg: None)
    removed = threading.Event()
    monkeypatch.setattr(agent, "uninstall", lambda quiet=False: None)
    monkeypatch.setattr(agent, "quit_helper", removed.set)
    monkeypatch.setattr(agent.Path, "home", classmethod(lambda cls: laptop_home))
    agent.state.update(folder="", waiting=[], problem="")
    cfg = {"server": base, "id": "laptop0001", "name": "Arham MBP"}
    threading.Thread(target=agent.poll_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=agent.watch_loop, args=(cfg,), daemon=True).start()

    for _ in range(50):
        _, s = call(base, "/api/status")
        if s["computers"]:
            break
        time.sleep(0.05)
    assert s["computers"][0]["name"] == "Arham MBP" and s["computers"][0]["online"]

    code, r = call(base, "/api/browse?computer=laptop0001&path=")             # relayed to the laptop
    assert code == 200 and r["path"] == str(laptop_home) and r["dirs"] == ["Footage"]
    assert call(base, "/api/folder", {"computer": "laptop0001", "path": str(upload_dir)})[0] == 200
    for _ in range(50):
        if agent.state["folder"] == str(upload_dir):
            break
        time.sleep(0.05)
    assert agent.state["folder"] == str(upload_dir)

    connect_youtube(env)
    env.store.update({"enabled": True})
    f = upload_dir / "Interview.mp4"
    f.write_bytes(os.urandom(300_000))
    os.utime(f, (time.time() - 100,) * 2)
    for _ in range(100):
        if (upload_dir / "Sent" / "Interview.mp4").exists():
            break
        time.sleep(0.05)
    assert (upload_dir / "Sent" / "Interview.mp4").exists()            # laptop: sent and moved
    env.w.scan()                                                        # main computer uploads
    assert env.cloud.yt_videos == ["vid1"]
    assert env.store.history()[-1]["source"] == "Arham MBP"
    assert not any((env.w.inbox / "laptop0001").iterdir())              # inbox copy cleaned up
    assert call(base, "/api/status")[1]["computers"][0]["sent"] == 1

    call(base, "/api/computers/remove", {"id": "laptop0001"})
    assert call(base, "/api/status")[1]["computers"] == []
    assert removed.wait(5)                                              # helper stopped itself


def test_agent_served_by_app(web):
    env, base = web
    code, body = call(base, "/agent.py")
    assert code == 200 and b"def poll_loop(" in body
