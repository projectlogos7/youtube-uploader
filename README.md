# Media Uploader

Watches a folder. When it's switched on, every finished media file that lands there is uploaded
to **YouTube** (always Private), **Frame.io** (with a share link), or both. Then the file is moved
into `Uploaded/` with a `.links.txt` next to it. A web page lets you connect accounts, switch
things on and off, and see what's happening.

Other computers on your Wi‑Fi can use it too: open the page and drag files in, or add a computer
so a folder on it is watched as well.

## Start

You need **Docker Desktop**.

```bash
git clone https://github.com/projectlogos7/youtube-uploader.git
cd youtube-uploader
cp .env.example .env        # then set WATCH_FOLDER to your folder
./start.sh                  # this computer only
./start.sh --wifi           # or: also other computers on your Wi-Fi
```

Open **http://localhost:8765**. It keeps running in the background and starts again with Docker.
Stop it with `./start.sh stop`; see what it's doing with `./start.sh logs`.

## Connect YouTube (once)

1. At https://console.cloud.google.com, signed in as the channel's Google account: create a project,
   enable **YouTube Data API v3**, set up the **OAuth consent screen** (External) and add the
   channel's Gmail as a **test user**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app.**
3. In the page, paste the client ID and secret under **YouTube** and click **Connect YouTube**.
   Sign in and allow. On the computer running the app it finishes by itself. On another computer
   you end on a `localhost` error page: copy that address and paste it into the box.

While the Google app is in "Testing", Google ends the sign-in after 7 days. Click **Reconnect**
when the page says so, or **Publish app** on the consent screen to stop that.

## Connect Frame.io (once)

1. At https://developer.adobe.com/console, signed in with the Adobe ID you use for Frame.io:
   **Create new project → Add API → Frame.io API → OAuth Web App**.
2. Set **Default redirect URI** and **Redirect URI pattern** to `https://localhost`.
3. In the page, paste the client ID and secret under **Frame.io**, click **Connect Frame.io**, sign in.
   You end on an error page at `localhost`: that's expected. Copy the whole address, paste it, click
   **Finish**, then pick the project uploads should go into.

## Use it

- Switch **on** the watcher (top right) and choose **YouTube**, **Frame.io** or both.
- Put finished files in the folder. It waits until a file stops changing, then uploads it.
- Links appear under **History** and in `Uploaded/<file>.links.txt`.
- YouTube takes video files only; Frame.io takes video, audio, images and PDFs.
- The same file is never uploaded twice to the same place.

## Other computers on your Wi‑Fi

Start with `./start.sh --wifi`. It prints the address other computers should open
(e.g. `http://studio-mac.local:8765`).

- **Drop files:** open that address on any computer and drag files onto the page.
- **Watch a folder on another computer:** on the page click **Add a computer**, copy the two lines,
  and paste them into Terminal on that computer. That folder is now watched too; sent files
  are moved to a `Sent` folder there. Click **Remove** in the page to stop it.
  (The `--install` part keeps it running on a Mac; on other systems leave the window open.)

Anyone on your network can open the page with `--wifi`, so only use it on a network you trust.
Run `./start.sh` again without `--wifi` to make it private to this computer.

## Where things are kept

Settings, sign-ins and history are in the Docker volume `uploader-data` (sign-ins in a file only
the app can read). Nothing is sent anywhere except Google/YouTube and Adobe/Frame.io.

## Tests

```bash
pip install pytest && pytest
```
All tests use fake YouTube and Frame.io servers.
