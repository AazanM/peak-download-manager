# Peak Download Manager

A fast, free, open-source download manager for macOS and Windows, in the spirit of IDM and FDM.

- **Videos and music** from YouTube, TikTok, Instagram, X, Facebook, Reddit, Pinterest, Dailymotion, SoundCloud, Twitch, Bilibili and [1,800+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md). Choose the quality before you download and see the exact file size for each one.
- **Any file**: split across up to 16 connections with aria2, so large downloads finish sooner.
- **Torrents and magnet links** use libtorrent, the engine behind qBittorrent. Before a torrent starts you see its file list, can pick which files to download, and can set a priority for each (High / Normal / Low / Skip). You can change them while it runs.
- **One click from the browser.** A Peak button sits on every video. Magnet and `.torrent` links open in Peak.
- **A card that stays out of the way.** The small window shows what's downloading right now. Expand it for the full library.
- Everything runs on your own machine. There are no accounts, no ads and no tracking.

## How it's built

| Part | What it is |
|---|---|
| `backend/server.py` | Local API on `127.0.0.1:7878`. It queues jobs and drives yt-dlp, ffmpeg and aria2. |
| `backend/ytworker.py` | A yt-dlp process that stays loaded, so looking up a link is instant. |
| `backend/ltworker.py` | The libtorrent engine for torrents: DHT, PEX, LSD, UPnP and extra public trackers. |
| `backend/ui/` | The app's interface (HTML/JS), shared by macOS, Windows and the browser. |
| `mac/` | Native macOS shell (Swift): window, menu bar item, notifications, magnet handler. |
| `windows/` | Windows shell (pywebview and a tray icon) plus the installer. |
| `extension/` | Chrome extension (Manifest V3): the button on videos, the popup, magnet capture. |

Only the extension and the app's own page can call the API. Requests from any other website get a 403.

## Install on macOS

```bash
brew install yt-dlp ffmpeg aria2
./scripts/setup-torrents.sh      # libtorrent for torrents (needs Python 3.10–3.13)
./scripts/build-app.sh           # builds "Peak Download Manager.app" into /Applications
```

On first launch Peak offers to become the default app for magnet links and `.torrent` files. You can change this later from the menu bar icon (right-click → **Use Peak for Magnet Links & Torrents**).

## Install on Windows

Build on a Windows PC with Python 3.12, and optionally Inno Setup 6 for the installer:

```powershell
powershell -ExecutionPolicy Bypass -File windows\build.ps1
```

This gives you `windows\Output\PeakSetup.exe`, which bundles yt-dlp, ffmpeg, aria2 and libtorrent, so users don't install anything else. You can also run the **Windows build** GitHub Action and download `PeakSetup` from it.

## Browser extension

In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose the `extension/` folder. Then pin Peak to the toolbar.

- Hover a video and click the Peak button in its top-right corner to pick a quality (sizes are shown).
- Right-click a page or link and choose **Peak: download as MP3 / video / torrent**.
- Shortcuts: ⌥⇧M for MP3, ⌥⇧V for video.
- Magnet and `.torrent` links open Peak's New download box.

## Using it

- Paste a link anywhere in the window (⌘V / Ctrl+V). The **New download** box shows the size, the folder and the quality, or a torrent's files, before anything starts.
- Click a download to see its details: speed, peers, connections and, for torrents, every file with its own progress and priority.
- Files go to `~/Downloads/Peak Downloads`. You can change this, set a folder per type, add speed limits, a proxy and more in **Settings**.

If a site stops working, update yt-dlp (`./scripts/update.sh` or `brew upgrade yt-dlp`). Sites change often.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | |
| GET | `/api/formats?url=` | qualities with exact sizes, or a file's size |
| POST | `/api/torrent/meta` | `{url}` or `{name, data}` (base64 `.torrent`) → name and file list |
| POST | `/api/download` | `{url, mode: "mp3"\|"video"\|"file"\|"torrent", quality?, folder?, torrent?, prio?}` |
| GET | `/api/jobs` | all downloads |
| POST | `/api/jobs/:id/pause \| resume \| retry \| reveal \| open` | |
| POST | `/api/jobs/:id/prio` | `{prio: [0\|1\|4\|7, …]}` per file |
| DELETE | `/api/jobs/:id` | cancel if active, otherwise remove from the list |
| GET | `/api/stats` | speed, counts, disk space |
| GET/POST | `/api/settings` | |

## Please use it responsibly

Peak is a general-purpose tool. Only download content you have the right to download. Respect copyright and each site's terms of service.

## Credits

Peak stands on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org), [aria2](https://aria2.github.io) and [libtorrent](https://libtorrent.org). Each keeps its own license. The Windows installer bundles their unmodified binaries.

## License

[MIT](LICENSE)
