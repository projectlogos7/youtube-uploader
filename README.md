# Media Uploader

Watch folders, upload finished media to **YouTube** (always Private), **Frame.io** (with a share
link), or both. Everything is set from a web page: switch the watcher on, pick the folder,
connect the accounts, see what's uploading.

With `--wifi`, other computers on your network can add their own watch folder. They only
**send** finished files to the main computer; all uploading happens on the main computer.

```
 Laptop A: ~/Footage ──send──┐
 Laptop B: ~/Exports ──send──┼──> Main computer ──upload──> YouTube / Frame.io
 Main:     ~/Movies/Upload ──┘     (web app at :8765)
```

## Start (main computer)

Needs **Docker Desktop**.

```bash
git clone https://github.com/projectlogos7/youtube-uploader.git
cd youtube-uploader
./start.sh --wifi        # or just ./start.sh to keep it on this computer only
```

Open **http://localhost:8765**:

1. **Watch folders → This computer → Choose folder.** Pick any folder in your home folder.
2. **Upload to:** switch on YouTube and/or Frame.io.
3. Connect the accounts (below), then switch the watcher **on** (top right).

Finished files are uploaded, then moved into `Uploaded/` in that folder with a `.links.txt`.
Links also appear under **History**. `./start.sh stop` stops it; `./start.sh logs` shows the log.

## Add another computer (needs `--wifi`)

1. On the main computer's page click **+ Add another computer** and copy the line.
2. On the other computer (same Wi‑Fi) open **Terminal**, paste it, press Return.
   It installs a small helper that keeps running in the background (Macs; on other
   systems leave the window open).
3. The computer appears in the page within seconds. Click **Choose folder** next to it and pick
   a folder *on that computer*. You can do this from any device that has the page open.

Finished files in that folder are sent to the main computer and moved into `Sent/` on the laptop.
Click **Remove** in the page to stop a computer; its helper uninstalls itself.

Why a helper? A web page on its own isn't allowed to watch folders on a laptop (and would stop
when the tab closes), so each extra computer needs this one-time install.

## Connect YouTube (once)

1. https://console.cloud.google.com, signed in as the channel's account: create a project, enable
   **YouTube Data API v3**, set up the **OAuth consent screen** (External), add the channel's Gmail
   as a **test user**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app.**
3. Paste the ID and secret under **YouTube account**, click **Connect YouTube**, sign in. On the main
   computer it finishes by itself; on another computer you land on a `localhost` error page, so copy
   that address into the box and click **Finish**.

In "Testing" mode Google ends the sign-in after 7 days; reconnect when the page says so, or click
**Publish app** on the consent screen.

## Connect Frame.io (once)

1. https://developer.adobe.com/console, signed in with your Frame.io Adobe ID: **Create project →
   Add API → Frame.io API → OAuth Web App**. Set both redirect fields to `https://localhost`.
2. Paste the ID and secret under **Frame.io account**, click **Connect Frame.io**, sign in. You land on
   an error page at `localhost`: that's expected. Copy its address into the box, click **Finish**,
   then pick the project.

## Good to know

- YouTube takes video only; Frame.io takes video, audio, images and PDFs.
- A file is uploaded once it has stopped changing for 20 seconds, so half-exported files are left alone.
- The same file is never uploaded twice to the same place.
- With `--wifi`, anyone on your network can open the page (no password), so use it on a network you
  trust. `./start.sh` without `--wifi` makes it private to the main computer again.
- The main computer's watch folder must be inside your home folder (that's what is shared with Docker).
- Settings, sign-ins and history live in the Docker volume `uploader-data`.

## Tests

```bash
pip install pytest && pytest
```
They use fake YouTube / Frame.io servers and a simulated laptop helper.
