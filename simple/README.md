# Simple YouTube folder uploader

Drop a finished video into a folder. It gets uploaded to YouTube as **Private** and
(optionally) to **Frame.io** with a share link, then moved into `Uploaded/`. That's it.

- Title = the file name (e.g. `Episode 7.mp4` → "Episode 7").
- Always **Private**, subscribers are not notified. Publish by hand in YouTube Studio.
- Waits until the export has finished before uploading.
- The same file is never uploaded twice. Every upload is listed in `uploaded.log` with its link,
  and each moved video gets a `<name> links.txt` next to it with its YouTube and Frame.io links.
- No installs needed. Uses the Mac's built-in Python.

## Setup (once)

**1. Get a Google client ID** (signed in as the YouTube channel's Google account)
1. Go to https://console.cloud.google.com and create a project.
2. **APIs & Services → Library**: search **YouTube Data API v3**, then **Enable**.
3. **APIs & Services → OAuth consent screen**: pick **External**, fill in the app name and your
   email, and add your channel's Gmail under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app**.
   Keep the window open: you need the **Client ID** and **Client secret**.

**2. Download this folder** to the Mac, e.g. into your home folder as `~/youtube-uploader`.

**3. Edit the settings** at the top of `simple/watch_and_upload.py`:
- `WATCH_FOLDER`: the folder to watch (default `~/Movies/YouTube Upload`)
- `CLIENT_ID`: paste the Client ID

**4. Sign in** (in Terminal):
```bash
cd ~/youtube-uploader/simple
python3 watch_and_upload.py auth
```
Paste the client secret. The browser opens, so sign in with the channel's account and click
**Allow**. If Google says the app isn't verified, click **Continue**: it's your own app.

**5. Start it:**
```bash
./keep_running.sh start
```
It now runs in the background all the time, and starts again after a restart.

## Add Frame.io (optional)

You need a Frame.io account that signs in with an **Adobe ID** (current Frame.io).

**1. Make an Adobe credential**
1. Go to https://developer.adobe.com/console and sign in with the same Adobe ID you use for Frame.io.
2. **Create new project → Add API → Frame.io API** (the V4 one).
3. Choose **OAuth User Authentication → OAuth Web App**.
4. Set **Default redirect URI** to `https://localhost` and **Redirect URI pattern** to `https://localhost`.
5. Save. Copy the **Client ID** and **Client secret**.

**2. Put the Client ID** in `simple/frameio.py` at `FRAMEIO_CLIENT_ID`.

**3. Sign in and pick the project** the videos should go into:
```bash
cd ~/youtube-uploader/simple
python3 watch_and_upload.py frameio-auth
```
Paste the client secret. The browser opens; sign in. You'll land on an error page at
`localhost`: **that's expected**. Copy the whole address from the address bar, paste it into
Terminal, then pick your account / workspace / project by number.

**4. Restart the watcher:** `./keep_running.sh start`

From now on each video goes to YouTube (Private) **and** Frame.io. The Frame.io link is a public
share link (`https://f.io/...`). If Frame.io won't make one, you get the normal Frame.io link,
which works for people in your Frame.io team.

- Frame.io only, no YouTube: set `UPLOAD_TO_YOUTUBE = False` in `watch_and_upload.py`.
- No public share link, just the team link: set `MAKE_SHARE_LINK = False` in `frameio.py`.

## Day to day

- Export or drop a video into the watch folder. A minute or so after the export finishes, it
  uploads. It then shows up in `Uploaded/` and in `uploaded.log` with its YouTube link.
- Watch it work: `./keep_running.sh log`
- Stop it: `./keep_running.sh stop`

## If something goes wrong

- **"FRAME.IO SIGN-IN EXPIRED"**: run `python3 watch_and_upload.py frameio-auth` again (Adobe
  ends the sign-in if it isn't used for about two weeks).
- **Frame.io says 401 / not authorized right after signing in**: the Adobe ID must be the one
  your Frame.io login uses. Otherwise remove and re-add the Frame.io API in the Adobe console.
- **"YOUTUBE SIGN-IN EXPIRED"** in the log: run `python3 watch_and_upload.py auth` again. While your
  Google app is in "Testing" mode, Google ends the sign-in every 7 days. To stop that, in the
  consent screen click **Publish app**.
- **A file stays in the folder**: check `./keep_running.sh log` for the error. It retries every
  30 seconds by itself.
- **Daily limit**: YouTube allows about 100 uploads per day per project.
