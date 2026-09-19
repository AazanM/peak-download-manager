"""Peak Download Manager backend: a tiny local server that wraps yt-dlp + ffmpeg.

Runs on http://127.0.0.1:7878. Only the Peak extension and the app UI
served from this server are allowed to call the API.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HOST, PORT = "127.0.0.1", 7878
WIN = os.name == "nt"
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))     # PyInstaller unpacks next to _MEIPASS
UI_DIR = ROOT / "ui"
DATA_DIR = (Path(os.environ.get("APPDATA", Path.home())) / "Peak Download Manager" if WIN
            else Path.home() / "Library" / "Application Support" / "Peak Download Manager")
# Windows build ships yt-dlp / ffmpeg / aria2c in a bin folder beside the app.
BIN_DIR = Path(os.environ.get("PEAK_BIN") or (Path(sys.executable).parent / "bin" if getattr(sys, "frozen", False) else ROOT / "bin"))
NO_WINDOW = 0x08000000 if WIN else 0    # CREATE_NO_WINDOW: no console flashing up per download
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.peakdm.backend.plist"
SESSION_START = time.time()
HISTORY_FILE = DATA_DIR / "history.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
DEFAULT_SETTINGS = {
    # Save to
    "download_dir": str(Path.home() / "Downloads" / "Peak Downloads"),
    "video_dir": "",            # "" = use download_dir
    "audio_dir": "",
    "image_dir": "",
    "subfolder_by_site": False,
    "filename": "title",        # title | title_id | site_title | date_title
    "if_exists": "rename",      # rename | overwrite | skip
    # Quality
    "audio_quality": "320",     # kbps for mp3
    "video_quality": "best",    # best | 2160 | 1440 | 1080 | 720 | 480
    "container": "mp4",         # mp4 | mkv
    "compatible": False,        # H.264 only, plays in QuickTime everywhere (caps at 1080p)
    "embed_thumbnail": True,
    "subtitles": False,         # download + embed English subtitles
    # Connection
    "max_downloads": 3,         # simultaneous downloads, the rest wait in the queue
    "connections": 16,          # parallel connections per download (aria2)
    "speed_limit": 0,           # MB/s, 0 = unlimited
    "retries": 10,
    "proxy": "",
    "cookies_browser": "",      # "" | chrome | safari | firefox | brave | edge | arc
    # When done
    "notify": True,
    "sound": True,
    "reveal_when_done": False,
    "open_app_on_start": True,  # read by the extension
}
NUMERIC = {"max_downloads": (1, 10), "connections": (1, 16), "speed_limit": (0, 1000), "retries": (0, 50)}

def find_tool(name):
    exe = name + (".exe" if WIN else "")
    for p in (BIN_DIR / exe, shutil.which(name), f"/opt/homebrew/bin/{name}"):
        if p and os.path.exists(p):
            return str(p)
    return None


YTDLP = find_tool("yt-dlp") or "yt-dlp"
FFMPEG_DIR = str(Path(find_tool("ffmpeg") or "/opt/homebrew/bin/ffmpeg").parent)
ARIA2 = find_tool("aria2c")
CACHE_DIR = DATA_DIR / "cache"
# Plain files (not media pages) go straight to aria2, IDM-style.
FILE_EXT = re.compile(r"\.(zip|rar|7z|tar|gz|tgz|bz2|xz|dmg|pkg|iso|img|exe|msi|apk|ipa|deb|rpm|appimage|"
                      r"pdf|epub|docx?|xlsx?|pptx?|csv|bin|dat|jar|whl|psd|ai|sketch|fig)$", re.I)

PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%.*?(?:at\s+(\S+))?\s+ETA\s+(\S+)")
# aria2:  [#0812af 3.9MiB/21MiB(18%) CN:16 DL:5.0MiB ETA:3s]
# torrents add SD:<seeds>:  [#2089b0 1.2MiB/100MiB(1%) CN:44 SD:12 DL:3.4MiB ETA:30s]
ARIA_RE = re.compile(r"\[#\w+ ([\d.]+\w+)/([\d.]+\w+)\((\d+)%\) CN:(\d+)(?: SD:(\d+))? DL:([\d.]+\w+)(?: ETA:(\w+))?")
TORRENT_DIR = DATA_DIR / "torrents"
# Extra public trackers: more peers than the torrent's own list alone (FDM/qBittorrent users add these too).
TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce", "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce", "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce", "udp://tracker.openbittorrent.com:6969/announce",
    "udp://explodie.org:6969/announce", "https://tracker.gbitt.info:443/announce",
])
DEST_RE = re.compile(r'(?:Destination: |Merging formats into "|\[ExtractAudio\] Destination: )(.+?)"?$')

jobs = {}           # id -> job dict
procs = {}          # id -> Popen
lock = threading.Lock()


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_history():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with lock:
        done = [j for j in jobs.values() if j["status"] in ("done", "error", "cancelled", "paused")]
    HISTORY_FILE.write_text(json.dumps(done[-500:], indent=2))


def settings():
    return {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}


FILENAMES = {
    "title": "%(title).150B",
    "title_id": "%(title).150B [%(id)s]",
    "site_title": "%(extractor_key)s - %(title).150B",
    "date_title": "%(upload_date>%Y-%m-%d|)s %(title).150B",
}


def out_dir(s, kind, site=None):
    """Folder for a download: per-type folder (like IDM categories) + optional site subfolder."""
    d = s.get(f"{kind}_dir") or s["download_dir"]
    if s.get("subfolder_by_site") and site:
        d = os.path.join(d, re.sub(r"[^\w .-]+", "", site) or "Other")
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def unique_path(path):
    """IDM-style rename: "name.mp4" -> "name (2).mp4" if taken."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def build_cmd(job, s):
    folder = job.get("folder") or out_dir(s, "audio" if job["mode"] == "mp3" else "video", job.get("site"))
    name = job.get("filename") or FILENAMES.get(s["filename"], FILENAMES["title"])
    cmd = [YTDLP, "--newline", "--no-playlist", "--ffmpeg-location", FFMPEG_DIR,
           "--embed-metadata", "--continue",
           "-N", str(s["connections"]), "--retries", str(s["retries"]),
           "-o", os.path.join(folder, name + ".%(ext)s")]
    if ARIA2 and job["mode"] == "video" and (job.get("est_bytes") or 0) >= ARIA_MIN:
        # Big files: one file split over many connections (~1.8x faster on YouTube).
        # Small ones stay on yt-dlp's own downloader: YouTube throttles each plain
        # connection to about playback speed, so aria2 is slower below ~40 MB.
        c = job.get("conns") or s["connections"]
        cmd += ["--downloader", ARIA2, "--downloader-args",
                f"aria2c:-x{c} -s{c} -k1M --file-allocation=none --summary-interval=1 --console-log-level=warn"]
    if s["speed_limit"]:
        cmd += ["--limit-rate", f"{s['speed_limit']}M"]
    if s["proxy"]:
        cmd += ["--proxy", s["proxy"]]
    if s["cookies_browser"]:
        cmd += ["--cookies-from-browser", s["cookies_browser"]]
    if s["if_exists"] == "overwrite":
        cmd += ["--force-overwrites"]
    if job["mode"] == "mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", f"{job['quality'] or s['audio_quality']}K"]
        if s["embed_thumbnail"]:
            cmd += ["--embed-thumbnail"]
    else:
        q = job["quality"] or s["video_quality"]
        cmd += ["-f", video_format(q, s["compatible"]), "-S", ",".join(FORMAT_SORT), "--merge-output-format", s["container"]]
        if s["embed_thumbnail"] and s["container"] == "mp4":
            cmd += ["--embed-thumbnail"]
        if s["subtitles"]:
            cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "en.*", "--embed-subs"]
    info = job.get("info_file")
    if info and os.path.exists(info) and time.time() - os.path.getmtime(info) < 1800:
        return cmd + ["--load-info-json", info]    # skip a second page extraction
    return cmd + [job["url"]]


def remove_partials(job):
    """Delete leftover .part / fragment files of a cancelled download (like IDM)."""
    import glob
    f = job.get("file")
    if not f:
        return
    if job.get("mode") == "torrent":             # the torrent's own file/folder + aria2's control file
        shutil.rmtree(f, ignore_errors=True) if os.path.isdir(f) else None
    if job.get("mode") in ("file", "torrent"):
        for p in (f, f + ".aria2"):
            try:
                os.remove(p)
            except OSError:
                pass
        return
    stem = os.path.splitext(f)[0]
    for p in glob.glob(glob.escape(stem) + "*"):
        if p.endswith((".part", ".ytdl", ".aria2")) or ".part-Frag" in p or re.search(r"\.f\d+\.\w+(\.part)?$", p):
            try:
                os.remove(p)
            except OSError:
                pass


def running_count():
    return sum(1 for j in jobs.values() if j["status"] in ("downloading", "converting"))


IN_APP = os.environ.get("PEAK_APP") == "1"
hooks = {"notify": None, "show": None}    # set by the Windows shell (runs this module in-process)


# ---- OS helpers ----
def reveal(path):
    if WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)] if os.path.isfile(path) else ["explorer", os.path.normpath(path)])
    else:
        subprocess.Popen(["open", "-R", path])


def open_path(path):
    os.startfile(path) if WIN else subprocess.Popen(["open", path])


def pick_folder():
    """Native folder chooser; None if cancelled."""
    if WIN:
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='Choose where Peak saves downloads';"
              "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True, creationflags=NO_WINDOW)
    else:
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose where Peak saves downloads")'],
                           capture_output=True, text=True)
    out = r.stdout.strip()
    return out.rstrip("/\\") if r.returncode == 0 and out else None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def start_at_login():
    if WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, "Peak")
            return True
        except OSError:
            return False
    r = subprocess.run(["plutil", "-extract", "RunAtLoad", "raw", str(LAUNCH_AGENT)], capture_output=True, text=True)
    return r.stdout.strip() != "false"


def set_start_at_login(on):
    if WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "Peak", 0, winreg.REG_SZ, f'"{sys.executable}" --hidden')
            else:
                try:
                    winreg.DeleteValue(k, "Peak")
                except OSError:
                    pass
    elif LAUNCH_AGENT.exists():
        subprocess.run(["plutil", "-replace", "RunAtLoad", "-bool", "true" if on else "false", str(LAUNCH_AGENT)])


def popen_group(cmd, **kw):
    """Start a download in its own process group so pause/cancel can stop its children too."""
    if WIN:
        return subprocess.Popen(cmd, creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP, **kw)
    return subprocess.Popen(cmd, start_new_session=True, **kw)


def notify_done(job, s):
    if job["status"] not in ("done", "error"):
        return
    ok = job["status"] == "done"
    title = "Download complete" if ok else "Download failed"
    body = (job["title"] if ok else job.get("error") or job["title"])[:180]
    if s["notify"] and hooks["notify"]:
        hooks["notify"](title, body)
    elif s["notify"] and not IN_APP and not WIN:     # the Mac app posts its own notifications
        subprocess.Popen(["osascript", "-e", f"display notification {json.dumps(body)} with title \"Peak\" subtitle {json.dumps(title)}"])
    if s["sound"]:
        if WIN:
            import winsound
            winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        else:
            subprocess.Popen(["afplay", f"/System/Library/Sounds/{'Glass' if ok else 'Basso'}.aiff"])
    if ok and s["reveal_when_done"] and job["file"] and os.path.exists(job["file"]):
        reveal(job["file"])



# ---------- Pinterest fallback ----------
# yt-dlp only reads a pin's main video. Carousel pins keep their videos in
# carousel_data, and image pins have none, so we read the pin data ourselves.
PIN_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140 Safari/537.36"}


def is_pinterest(url):
    return re.search(r"(^|[/.])(pinterest\.[a-z.]+|pin\.it)/", url) is not None


def pinterest_media(url):
    """Returns {"title", "thumbnail", "items": [{"url", "kind": video|image, "width"}]} or None."""
    import urllib.request
    from urllib.parse import quote
    try:
        if "pin.it/" in url:   # short link -> real pin URL
            url = urllib.request.urlopen(urllib.request.Request(url, headers=PIN_UA), timeout=15).geturl()
        m = re.search(r"/pin/(?:[^/]*--)?(\d+)", url)
        if not m:
            return None
        data = quote(json.dumps({"options": {"field_set_key": "unauth_react_main_pin", "id": m.group(1)}}))
        req = urllib.request.Request(f"https://www.pinterest.com/resource/PinResource/get/?data={data}",
                                     headers={**PIN_UA, "X-Pinterest-PWS-Handler": "www/pin/[id].js"})
        d = json.load(urllib.request.urlopen(req, timeout=20))["resource_response"]["data"]
    except Exception:
        return None

    def best_video(vl):
        mp4s = [v for v in (vl or {}).values() if isinstance(v, dict) and str(v.get("url", "")).endswith(".mp4")]
        return max(mp4s, key=lambda v: v.get("width") or 0, default=None)

    items = []
    for slot in (d.get("carousel_data") or {}).get("carousel_slots") or []:
        v = best_video((slot.get("videos") or {}).get("video_list"))
        if v:
            items.append({"url": v["url"], "kind": "video", "width": v.get("width"), "height": v.get("height")})
        elif (slot.get("images") or {}).get("orig"):
            items.append({"url": slot["images"]["orig"]["url"], "kind": "image"})
    if not items:
        v = best_video((d.get("videos") or {}).get("video_list"))
        if v:
            items.append({"url": v["url"], "kind": "video", "width": v.get("width"), "height": v.get("height")})
    if not items and (d.get("images") or {}).get("orig"):
        items.append({"url": d["images"]["orig"]["url"], "kind": "image"})
    title = (d.get("title") or d.get("grid_title") or "").strip() or f"Pinterest pin {m.group(1)}"
    thumb = (d.get("images") or {}).get("orig", {}).get("url")
    return {"title": title, "thumbnail": thumb, "items": items} if items else None


def run_pinterest_fallback(job, s):
    """Download every video (or the image) in a pin. Returns True on success."""
    import urllib.request
    media = pinterest_media(job["url"])
    if not media:
        return False
    items = [i for i in media["items"] if i["kind"] == "video"] or media["items"]
    if job["mode"] == "mp3":
        items = [i for i in items if i["kind"] == "video"]
        if not items:
            job["error"] = "This pin has no video, so there's no audio to extract"
            return False
    job.update(title=media["title"], thumbnail=media["thumbnail"], site="Pinterest", status="downloading")
    safe = re.sub(r'[\\/:*?"<>|]+', " ", media["title"])[:120].strip()
    files, total = [], len(items)
    for n, it in enumerate(items, 1):
        if job["status"] == "cancelled":
            return True
        ext = ".mp4" if it["kind"] == "video" else os.path.splitext(it["url"])[1] or ".jpg"
        suffix = f" ({n})" if total > 1 else ""
        dest = os.path.join(out_dir(s, "video" if it["kind"] == "video" else "image", "Pinterest"), f"{safe}{suffix}{ext}")
        if s["if_exists"] == "skip" and os.path.exists(dest):
            files.append(dest)
            continue
        if s["if_exists"] == "rename":
            dest = unique_path(dest)
        with urllib.request.urlopen(urllib.request.Request(it["url"], headers=PIN_UA), timeout=30) as r, open(dest, "wb") as out:
            size, got, t0 = int(r.headers.get("Content-Length") or 0), 0, time.time()
            while chunk := r.read(1 << 16):
                out.write(chunk)
                got += len(chunk)
                el = max(time.time() - t0, 1e-3)
                job["speed"] = f"{got / el / 1048576:.2f}MiB/s"
                job["progress"] = ((n - 1) + (got / size if size else 0)) / total * 100
        if job["mode"] == "mp3":
            job["status"] = "converting"
            mp3 = dest[:-4] + ".mp3"
            subprocess.run([os.path.join(FFMPEG_DIR, "ffmpeg"), "-y", "-loglevel", "error", "-i", dest,
                            "-vn", "-b:a", f"{job['quality']}k", mp3])
            os.remove(dest)
            dest = mp3
            job["status"] = "downloading"
        files.append(dest)
    job["file"] = files[0]
    if total > 1:
        job["title"] = f"{media['title']} · {total} {'videos' if items[0]['kind'] == 'video' else 'items'}"
    return True


def kill_tree(proc):
    """Stop yt-dlp *and* the aria2c/ffmpeg it started (they share a process group)."""
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()


def shutdown(*_):
    """App quit: stop every running download so nothing keeps going in the background."""
    for p in list(procs.values()):
        kill_tree(p)
    if worker.proc:
        worker.proc.kill()
    lt_engine.stop()
    for j in jobs.values():
        if j["status"] in ("queued", "downloading", "converting"):
            j.update(status="paused", speed=None, eta=None)
    save_history()
    if __name__ == "__main__":
        os._exit(0)


def stopped(job):
    return job["status"] in ("cancelled", "paused")


def run_job(job_id):
    job = jobs[job_id]
    s = settings()

    # Fetch title first so the UI has something nice to show (warm worker, cached).
    if job["title"] == job["url"] and job["mode"] not in ("file", "torrent"):
        try:
            ext = "mp3" if job["mode"] == "mp3" else s["container"]
            tmpl = FILENAMES.get(s["filename"], FILENAMES["title"]) + ".%(ext)s"
            res = extract(job["url"], s, outtmpl=tmpl)
            if "error" in res:
                if is_pinterest(job["url"]) and (media := pinterest_media(job["url"])):
                    job.update(title=media["title"], thumbnail=media["thumbnail"], site="Pinterest")
                raise ValueError(res["error"])
            info = res["info"]
            job.update(title=info.get("title") or job["url"],
                       thumbnail=info.get("thumbnail"),
                       site=info.get("extractor_key"),
                       duration=info.get("duration"))
            job["est_bytes"] = estimate_size(info, job)
            if info.get("_type", "video") == "video" and info.get("formats"):
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                job["info_file"] = str(CACHE_DIR / f"{job_id}.info.json")
                Path(job["info_file"]).write_text(json.dumps(info))
            # IDM-style "rename if exists": pick a free name before downloading.
            if s["if_exists"] == "rename" and not job.get("filename") and res.get("filename"):
                folder = job.get("folder") or out_dir(s, "audio" if job["mode"] == "mp3" else "video", job.get("site"))
                base = os.path.splitext(res["filename"])[0]
                if os.path.exists(os.path.join(folder, f"{base}.{ext}")):
                    free = unique_path(os.path.join(folder, f"{base}.{ext}"))
                    job["filename"] = os.path.splitext(os.path.basename(free))[0].replace("%", "%%")
        except Exception:
            pass

    if job["mode"] == "torrent" and not job.get("engine") and lt_engine.available():
        meta = torrent_meta(job["url"])
        if meta.get("files"):
            set_torrent_meta(job, meta)

    # Queue: wait for a free slot (Settings → Simultaneous downloads), first come first served.
    job["ready"] = True
    while running_count() >= int(settings()["max_downloads"]) or next(
            (j for j in sorted(jobs.values(), key=lambda j: j["created_at"])
             if j["status"] == "queued" and j.get("ready")), job) is not job:
        if stopped(job):
            return
        time.sleep(0.4)
    if stopped(job):
        return
    s = settings()
    job["status"] = "downloading"
    if job.get("engine") == "lt":
        return run_lt(job)
    cmd = {"file": build_file_cmd, "torrent": build_torrent_cmd}.get(job["mode"], build_cmd)(job, s)
    proc = popen_group(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1, encoding="utf-8", errors="replace")
    procs[job_id] = proc
    last_lines = []
    for line in proc.stdout:
        line = line.strip()
        last_lines = (last_lines + [line])[-5:]
        if m := PROGRESS_RE.search(line):
            if t := re.search(r"of\s+~?\s*([\d.]+\w+)", line):
                job["total"] = t.group(1)
            job["progress"] = float(m.group(1))
            job["speed"] = m.group(2)
            job["eta"] = m.group(3)
        elif m := ARIA_RE.search(line):
            job["progress"] = float(m.group(3))
            job["speed"] = m.group(6) + "/s"
            job["eta"] = m.group(7)
            job["connections"] = int(m.group(4))
            job["total"] = m.group(2)
            if job["mode"] == "torrent":
                job["peers"], job["seeds"] = int(m.group(4)), int(m.group(5) or 0)
        if job["mode"] == "torrent" and (m := re.search(r"^(?:FILE: |.*Download complete: )(.+)$", line)):
            torrent_found(job, m.group(1))
        elif m := DEST_RE.search(line):
            job["file"] = m.group(1)
        elif m := re.search(r"\[download\] (.+) has already been downloaded", line):
            job["file"] = m.group(1)
        if "[ExtractAudio]" in line or "[Merger]" in line:
            job["status"] = "converting"
    proc.wait()
    procs.pop(job_id, None)
    if job["status"] == "paused":
        job.update(speed=None, eta=None)
        return

    if proc.returncode != 0 and job["status"] != "cancelled" and is_pinterest(job["url"]):
        try:
            if run_pinterest_fallback(job, s) and job["status"] != "cancelled":
                job.update(status="done", progress=100, speed=None, eta=None,
                           bytes=os.path.getsize(job["file"]) if job["file"] else None)
                job["finished_at"] = time.time()
                save_history()
                notify_done(job, s)
                return
        except Exception as e:
            last_lines.append(f"ERROR: Pinterest: {e}")
        if job.get("error"):
            last_lines.append("ERROR: " + job["error"])

    if job["status"] == "cancelled":
        pass
    elif proc.returncode == 0:
        if job.get("info_file"):
            try:
                os.remove(job["info_file"])
            except OSError:
                pass
        size = path_size(job["file"]) if job["file"] and os.path.exists(job["file"]) else None
        job.update(status="done", progress=100, speed=None, eta=None, bytes=size)
    else:
        err = next((l for l in reversed(last_lines) if "ERROR" in l), last_lines[-1] if last_lines else "")
        job.update(status="error", error=err.replace("ERROR: ", ""))
    job["finished_at"] = time.time()
    save_history()
    notify_done(job, s)


def start(job_id):
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()


def set_torrent_meta(job, meta, prio=None):
    files = meta["files"]
    prio = prio if prio and len(prio) == len(files) else [4] * len(files)
    job.update(engine="lt", torrent=meta["torrent"], title=meta["name"] or job["title"],
               files=[{"path": f["path"], "size": f["size"], "prio": int(p), "done": 0} for f, p in zip(files, prio)],
               est_bytes=sum(f["size"] for f, p in zip(files, prio) if int(p)))
    job["file"] = os.path.join(torrent_folder(job, settings()), files[0]["path"].replace("\\", "/").split("/")[0])


def new_job(url, mode, quality=None, folder=None, filename=None, conns=None, meta=None, prio=None):
    s = settings()
    if is_torrent(url):
        mode = "torrent"
    elif is_file_url(url):
        mode = "file"
    quality = str(quality or (s["audio_quality"] if mode == "mp3" else s["video_quality"]))
    job_id = uuid.uuid4().hex[:10]
    job = {"id": job_id, "url": url, "mode": mode, "quality": quality,
           "status": "queued", "progress": 0,
           "title": torrent_name(url) if mode == "torrent" else unquote(os.path.basename(urlparse(url).path)) if mode == "file" else url,
           "thumbnail": None,
           "site": None, "speed": None, "eta": None, "file": None,
           "error": None, "bytes": None, "created_at": time.time(), "finished_at": None}
    if folder:
        job["folder"] = os.path.expanduser(folder)
        os.makedirs(job["folder"], exist_ok=True)
    if filename:
        job["filename"] = re.sub(r'[/\\:]', "-", filename).replace("%", "%%")
        if mode != "file":
            job["filename"] = os.path.splitext(job["filename"])[0] if job["filename"].lower().endswith((".mp3", ".mp4", ".mkv", ".webm")) else job["filename"]
    if conns:
        job["conns"] = max(1, min(16, int(conns)))
    if mode == "torrent" and meta and meta.get("files"):
        set_torrent_meta(job, meta, prio)
    with lock:
        jobs[job_id] = job
    start(job_id)
    return job


ARIA_MIN = 40_000_000


FORMAT_SORT = ["res", "fps", "vcodec:avc1", "acodec:m4a"]


def video_format(q, compatible=False):
    """The -f selector for a quality choice. Shared by downloads and the size preview so they agree."""
    q = str(q or "best")
    if compatible:
        cap = min(int(q), 1080) if q.isdigit() else 1080
        return f"bv*[vcodec^=avc1][height<={cap}]+ba[ext=m4a]/b[vcodec^=avc1][height<={cap}]/bv*[height<={cap}]+ba/b"
    # Nothing at or under the chosen height (e.g. Dailymotion's lowest is 288p)? Take the
    # smallest there is rather than jumping to the biggest.
    return "bv*+ba/b" if q in ("best", "0") else f"bv*[height<={q}]+ba/b[height<={q}]/wv*+ba/w"


def res_label(w, h):
    """Standard name for a frame size. The bigger of the long-side and height classes wins, so
    3840x2026 (cinema 4K) is "4K" not "2026p", and 320x240 (4:3) is "240p" not "144p"."""
    long = max(w or 0, h or 0)
    by_long = next((n for px, n in ((7680, "8K"), (5120, "5K"), (3840, "4K"), (2560, "1440p"), (1920, "1080p"),
                                    (1280, "720p"), (854, "480p"), (640, "360p"), (426, "240p"), (256, "144p"))
                    if long >= px * 0.97), None)
    short = min(w or h or 0, h or w or 0)
    by_short = next((n for px, n in ((4320, "8K"), (2160, "4K"), (1440, "1440p"), (1080, "1080p"), (720, "720p"),
                                     (480, "480p"), (360, "360p"), (240, "240p"), (144, "144p"))
                     if short >= px * 0.97), None)
    order = ["144p", "240p", "360p", "480p", "720p", "1080p", "1440p", "4K", "5K", "8K"]
    best = max([x for x in (by_long, by_short) if x], key=order.index, default=None)
    return best or f"{short}p"


def estimate_size(info, job):
    """Rough size of the video stream we'll pick (bytes), from the already-fetched formats."""
    q = job["quality"]
    cap = int(q) if q.isdigit() and int(q) > 0 else 10**6
    sizes = [f.get("filesize") or f.get("filesize_approx") or 0 for f in info.get("formats") or []
             if f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= cap]
    return max(sizes, default=0) or (info.get("filesize") or info.get("filesize_approx") or 0)


def build_file_cmd(job, s):
    """Plain file (zip/dmg/pdf…): aria2 with many connections, like IDM/FDM."""
    folder = job.get("folder") or s.get("file_dir") or s["download_dir"]
    os.makedirs(folder, exist_ok=True)
    name = job.get("filename") or unquote(os.path.basename(urlparse(job["url"]).path)) or "download"
    if s["if_exists"] == "rename" and not job.get("filename"):
        name = os.path.basename(unique_path(os.path.join(folder, name)))
        job["filename"] = name
    job["file"] = os.path.join(folder, name)
    c = job.get("conns") or s["connections"]
    cmd = [ARIA2, "-x", str(c), "-s", str(c), "-k", "1M", "--file-allocation=none", "--continue=true",
           "--summary-interval=1", "--console-log-level=warn", "--show-console-readout=true",
           "--max-tries", str(max(1, s["retries"])), "--retry-wait=1", "--auto-file-renaming=false",
           "--allow-overwrite=true", "-d", folder, "-o", name]
    if s["speed_limit"]:
        cmd += [f"--max-download-limit={s['speed_limit']}M"]
    if s["proxy"]:
        cmd += [f"--all-proxy={s['proxy']}"]
    return cmd + [job["url"]]


# ---------- torrents (aria2's BitTorrent engine: DHT, PEX, LSD, encryption, trackers) ----------
def is_torrent(url):
    return url.startswith("magnet:") or urlparse(url).path.lower().endswith(".torrent") or \
        (os.path.isabs(url) and url.lower().endswith(".torrent"))


def torrent_name(url):
    if url.startswith("magnet:"):
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(url).query)
        return (q.get("dn") or [None])[0] or "Magnet " + (re.search(r"btih:(\w{8})", url, re.I) or [None, "link"])[1]
    return re.sub(r"\.torrent$", "", unquote(os.path.basename(urlparse(url).path)), flags=re.I)


def torrent_folder(job, s):
    return job.get("folder") or s.get("torrent_dir") or s["download_dir"]


def build_torrent_cmd(job, s):
    folder = torrent_folder(job, s)
    os.makedirs(folder, exist_ok=True)
    cmd = [ARIA2, "--dir", folder, "--seed-time=0", "--follow-torrent=mem", "--bt-save-metadata=false",
           "--enable-dht=true", "--enable-dht6=true", "--bt-enable-lpd=true", "--enable-peer-exchange=true",
           "--bt-max-peers=0", "--bt-request-peer-speed-limit=20M", "--bt-tracker=" + TRACKERS,
           "--listen-port=6881-6999", "--dht-listen-port=6881-6999", "--file-allocation=none",
           "--continue=true", "--summary-interval=1", "--console-log-level=warn", "--show-console-readout=true",
           "--max-connection-per-server=16", "--split=16", "--min-split-size=1M"]
    if s["speed_limit"]:
        cmd += [f"--max-download-limit={s['speed_limit']}M"]
    if s["proxy"]:
        cmd += [f"--all-proxy={s['proxy']}"]
    return cmd + [job["url"]]


def torrent_found(job, path):
    """aria2 names each finished file; the torrent's item is its first folder under the download dir."""
    folder = os.path.abspath(torrent_folder(job, settings()))
    rel = os.path.relpath(os.path.abspath(path), folder)
    if rel.startswith("..") or path.endswith(".torrent") or "[METADATA]" in path:
        return
    job["file"] = os.path.join(folder, rel.split(os.sep)[0])
    if job["title"].startswith("Magnet "):
        job["title"] = rel.split(os.sep)[0]


def path_size(p):
    if os.path.isdir(p):
        return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
    return os.path.getsize(p)


def is_file_url(url):
    return bool(ARIA2) and bool(FILE_EXT.search(urlparse(url).path))


# ---- warm extractor worker (see ytworker.py) ----
class Worker:
    def __init__(self):
        self.proc = None
        self.pending = {}           # id -> [Event, result]
        self.lock = threading.Lock()
        self.local = None

    def inproc(self):
        """yt-dlp.exe has no Python to borrow, so on Windows yt_dlp is bundled and runs in this process."""
        if self.local is None:
            self.local = False
            if WIN or getattr(sys, "frozen", False):
                try:
                    import ytworker
                    from concurrent.futures import ThreadPoolExecutor
                    self.local = ytworker
                    self.pool = ThreadPoolExecutor(4)
                except ImportError:
                    pass
        return self.local

    def python(self):
        try:
            with open(YTDLP) as f:
                line = f.readline()
            if line.startswith("#!") and os.path.exists(line[2:].strip()):
                return line[2:].strip()
        except (OSError, UnicodeDecodeError):
            pass
        return None

    def ensure(self):
        if self.inproc():
            return True
        if self.proc and self.proc.poll() is None:
            return True
        py = self.python()
        if not py:
            return False
        self.proc = subprocess.Popen([py, "-u", str(ROOT / "ytworker.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        return True

    def read(self, proc):
        for line in proc.stdout:
            try:
                res = json.loads(line)
            except ValueError:
                continue
            if slot := self.pending.pop(res.get("id"), None):
                slot[1] = res
                slot[0].set()
        for slot in list(self.pending.values()):     # worker died: fail everything waiting
            slot[0].set()

    def call(self, req, timeout=60):
        if self.inproc():                      # fixed pool: each thread keeps its warm YoutubeDL
            try:
                return self.pool.submit(self.local.handle, dict(req)).result(timeout)
            except Exception:  # noqa: BLE001 - timeout / crash -> caller falls back to the CLI
                return None
        with self.lock:
            if not self.ensure():
                return None
            req["id"] = uuid.uuid4().hex
            slot = self.pending[req["id"]] = [threading.Event(), None]
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except OSError:
                self.pending.pop(req["id"], None)
                return None
        slot[0].wait(timeout)
        return slot[1]


worker = Worker()


# ---- libtorrent engine (see ltworker.py); aria2 stays the engine for plain files ----
LT_PYTHON = [os.environ.get("PEAK_LT_PYTHON") or "", str(DATA_DIR / "lt-venv" / ("Scripts/python.exe" if WIN else "bin/python"))]


class LT:
    """Talks to ltworker (own process on the Mac, in-process where libtorrent is bundled)."""

    def __init__(self):
        self.proc, self.eng, self.pending, self.lock = None, None, {}, threading.Lock()
        self.ok = None

    def available(self):
        if self.ok is None:
            try:
                import libtorrent  # noqa: F401  bundled (Windows build)
                import ltworker
                self.eng = ltworker.Engine(self.event)
                self.ok = True
            except ImportError:
                self.ok = any(p and os.path.exists(p) for p in LT_PYTHON)
        return self.ok

    def ensure(self):
        if self.eng or (self.proc and self.proc.poll() is None):
            return True
        py = next((p for p in LT_PYTHON if p and os.path.exists(p)), None)
        if not py:
            return False
        self.proc = subprocess.Popen([py, "-u", str(ROOT / "ltworker.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        for j in jobs.values():        # engine restarted: running torrents need adding again
            if j.get("engine") == "lt" and j["status"] == "downloading":
                threading.Thread(target=lambda j=j: self.call(lt_add_req(j)), daemon=True).start()
        return True

    def read(self, proc):
        for line in proc.stdout:
            try:
                self.event(json.loads(line))
            except ValueError:
                pass
        for slot in list(self.pending.values()):
            slot[0].set()

    def event(self, m):
        if "ev" not in m:
            if slot := self.pending.pop(m.get("id"), None):
                slot[1] = m
                slot[0].set()
            return
        job = jobs.get(m.get("job"))
        if not job:
            return
        if m["ev"] == "status":
            wanted = m["wanted"] or 1
            job.update(progress=round(m["done"] * 100 / wanted, 1), total=f"{m['wanted']}B", bytes_done=m["done"],
                       speed=f"{m['rate']}B/s" if m["rate"] else None, peers=m["peers"], seeds=m["seeds"],
                       connections=m["peers"], upload=m["up"], checking=m["checking"],
                       eta=hms((m['wanted'] - m['done']) / m['rate']) if m["rate"] > 1000 else None)
            for f, d in zip(job.get("files") or [], m["files"]):
                f["done"] = d
        elif m["ev"] in ("finished", "error"):
            lt_ends[job["id"]] = m
            if ev := lt_waits.get(job["id"]):
                ev.set()

    def call(self, req, timeout=15):
        if self.eng:
            try:
                return self.eng.op(req)
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)}
        with self.lock:
            if not self.ensure():
                return {"error": "libtorrent isn't installed"}
            req["id"] = uuid.uuid4().hex
            slot = self.pending[req["id"]] = [threading.Event(), None]
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        slot[0].wait(timeout)
        self.pending.pop(req["id"], None)
        return slot[1] or {"error": "The torrent engine didn't answer"}

    def stop(self):
        if self.proc:
            self.proc.kill()


lt_engine = LT()


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"
lt_waits, lt_ends = {}, {}     # job id -> Event / final event (kept out of the job dict: it's JSON)


def lt_add_req(job):
    return {"op": "add", "job": job["id"], "torrent": job["torrent"], "save": torrent_folder(job, settings()),
            "prio": [f["prio"] for f in job.get("files") or []] or None}


def torrent_meta(src):
    """File list + sizes of a magnet / .torrent, before anything downloads."""
    TORRENT_DIR.mkdir(parents=True, exist_ok=True)
    if src.startswith(("http://", "https://")):          # a .torrent on the web: fetch it first
        import urllib.request
        with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            raw = r.read(20_000_000)
        if not raw.startswith(b"d"):
            return {"error": "That link didn't give a .torrent file"}
        src = str(TORRENT_DIR / f"{uuid.uuid4().hex[:8]}.torrent")
        Path(src).write_bytes(raw)
    if not lt_engine.available():
        return {"torrent": None, "name": torrent_name(src), "files": None}
    res = lt_engine.call({"op": "meta", "src": src, "cache": str(TORRENT_DIR)}, timeout=100)
    return res


def run_lt(job):
    """Download a torrent with libtorrent; returns when it finishes, fails, or is paused/cancelled."""
    lt_waits[job["id"]] = ev = threading.Event()
    lt_ends.pop(job["id"], None)
    s = settings()
    lt_engine.call({"op": "limit", "bps": int(float(s["speed_limit"] or 0) * 1e6)})
    os.makedirs(torrent_folder(job, s), exist_ok=True)
    res = lt_engine.call(lt_add_req(job))
    if "error" in res:
        job.update(status="error", error=res["error"])
    else:
        while not ev.wait(0.5):
            if stopped(job):
                return
        end = lt_ends.pop(job["id"], {})
        if end.get("ev") == "finished":
            files = [f for f in job.get("files") or [] if f["prio"]]
            root = os.path.join(torrent_folder(job, s), files[0]["path"].split("/")[0]) if files else None
            job.update(status="done", progress=100, speed=None, eta=None, file=root,
                       bytes=sum(f["size"] for f in files) or None)
        else:
            job.update(status="error", error=end.get("error") or "Torrent failed")
    job["finished_at"] = time.time()
    save_history()
    notify_done(job, s)
info_cache = {}      # (url, proxy, cookies) -> (time, result)
inflight = {}        # same key -> Event (dedupe: hover prefetch + click share one lookup)


def extract(url, s, outtmpl=None):
    """{"info": …, "filename"?} or {"error": …}. Cached 10 min; one lookup per URL at a time."""
    res = extract_info(url, s)
    if outtmpl and "info" in res:
        f = worker.call({"op": "filename", "info": res["info"], "outtmpl": outtmpl}, timeout=10)
        return {**res, "filename": (f or {}).get("filename")}
    return res


def extract_info(url, s):
    key = (url, s["proxy"], s["cookies_browser"])
    while True:
        hit = info_cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        with lock:
            ev = inflight.get(key)
            mine = ev is None
            if mine:
                ev = inflight[key] = threading.Event()
        if mine:
            break
        ev.wait(60)
        if hit := info_cache.get(key):
            return hit[1]
    try:
        req = {"url": url, "proxy": s["proxy"], "cookies": s["cookies_browser"]}
        res = worker.call(req)
        if res is None:                                   # no worker: fall back to the CLI
            r = subprocess.run([YTDLP, "--no-playlist", "-J", url], capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
            res = {"info": json.loads(r.stdout)} if r.returncode == 0 else {
                "error": next((l for l in r.stderr.splitlines() if "ERROR" in l), "Couldn't read this page")}
        # vimeo.com/<id> pages now demand a login; the embed player serves the same video.
        if "error" in res and (m := re.match(r"https?://(?:www\.)?vimeo\.com/(\d+)", url)):
            alt = worker.call({**req, "url": f"https://player.vimeo.com/video/{m.group(1)}"})
            if alt and "error" not in alt:
                res = alt
        if "error" not in res:
            info_cache[key] = (time.time(), res)
        return res
    finally:
        with lock:
            inflight.pop(key, None)
        ev.set()


formats_cache = {}   # url -> (time, result)
metas = {}           # cached .torrent path -> file list (from /api/torrent/meta)
prompts = []         # links waiting for the Add window to confirm them


def formats(url):
    hit = formats_cache.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    if is_torrent(url):
        return {"torrent": True, "title": torrent_name(url), "video": [], "audio_kbps": None}
    if is_file_url(url):
        size = None
        try:                          # the size up front, for the New download box
            import urllib.request
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=PIN_UA), timeout=6) as r:
                size = int(r.headers.get("Content-Length") or 0) or None
        except (OSError, ValueError):
            pass
        return {"file": True, "title": unquote(os.path.basename(urlparse(url).path)), "size": size, "video": [], "audio_kbps": None}
    res = extract(url, settings())
    if "error" in res and is_pinterest(url) and (media := pinterest_media(url)):
        vids = [i for i in media["items"] if i["kind"] == "video"]
        h = max((v.get("height") or 0 for v in vids), default=0)
        res = {"title": media["title"], "thumbnail": media["thumbnail"], "site": "Pinterest", "duration": None,
               "video": [{"height": h or "best", "label": f"{h}p" if h else "Best",
                          "fps": None, "count": len(vids)}] if vids else [],
               "images": len(media["items"]) - len(vids), "audio_kbps": None, "mp3": ["320", "192", "128"]}
        formats_cache[url] = (time.time(), res)
        return res
    if "error" in res:
        return {"error": re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[^:]+:\s*)?", "", res["error"])}
    info = res["info"]
    fs = info.get("formats") or []
    heights = sorted({f["height"] for f in fs if f.get("vcodec") not in (None, "none") and f.get("height")}, reverse=True)
    s = settings()
    sizes = (worker.call({"op": "sizes", "info": info, "sort": FORMAT_SORT,
                          "formats": {str(h): video_format(h, s["compatible"]) for h in heights}}, timeout=10) or {}).get("sizes") or {}
    video, seen = [], set()
    for h in heights:
        w = max((f.get("width") or 0) for f in fs if f.get("height") == h)
        lab = res_label(w, h)
        if lab in seen or max(w, h) < 300:
            continue
        seen.add(lab)
        video.append({"height": h, "label": lab, "size": sizes.get(str(h)) or None,
                      "fps": round(max((f.get("fps") or 0) for f in fs if f.get("height") == h)) or None})
    abr = max((f.get("abr") or 0 for f in fs if f.get("acodec") not in (None, "none")), default=0)
    dur = info.get("duration") or 0
    res = {
        "title": info.get("title"), "thumbnail": info.get("thumbnail"), "site": info.get("extractor_key"),
        "duration": dur,
        "video": video[:6] or ([{"height": 0, "label": "Best", "fps": None, "size": None}] if fs or info.get("url") else []),
        "audio_kbps": round(abr) or None,
        "mp3": [q for q in ("320", "192", "128")],
        # MP3 size is exact from bitrate x duration
        "mp3_size": {q: int(int(q) * 1000 / 8 * dur) for q in ("320", "192", "128")} if dur else {},
    }
    formats_cache[url] = (time.time(), res)
    return res


UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def speed_bps(text):
    m = re.match(r"([\d.]+)(B|KiB|MiB|GiB)/s", text or "")
    return float(m.group(1)) * UNITS[m.group(2)] if m else 0


def stats():
    with lock:
        js = list(jobs.values())
    active = [j for j in js if j["status"] in ("queued", "downloading", "converting")]
    try:
        du = shutil.disk_usage(settings()["download_dir"] if os.path.exists(settings()["download_dir"]) else str(Path.home()))
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    return {
        "disk": disk,
        "queued": sum(1 for j in js if j["status"] == "queued"),
        "downloading": sum(1 for j in js if j["status"] in ("downloading", "converting")),
        "connections": sum((j.get("connections") or 1) for j in js if j["status"] == "downloading"),
        "session_bytes": sum(j.get("bytes") or 0 for j in js
                             if j["status"] == "done" and (j.get("finished_at") or 0) >= SESSION_START),
        "speed_bps": sum(speed_bps(j.get("speed")) for j in active),
        "active": len(active),
        "counts": {k: sum(1 for j in js if j["status"] in v) for k, v in {
            "all": ("queued", "downloading", "converting", "paused", "done", "error", "cancelled"),
            "active": ("queued", "downloading", "converting", "paused"),
            "done": ("done",), "paused": ("paused",), "failed": ("error", "cancelled")}.items()},
        "download_dir": settings()["download_dir"],
        "prompts": len(prompts),
        "torrent_engine": "libtorrent" if lt_engine.available() else "aria2",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # --- security: only our extension or our own UI may call the API ---
    def allowed(self):
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith("chrome-extension://") \
            or origin.startswith("moz-extension://") or origin == f"http://{HOST}:{PORT}"

    def send(self, code, body=None, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        origin = self.headers.get("Origin")
        if origin and self.allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_OPTIONS(self):
        self.send(204 if self.allowed() else 403, b"")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self.send(200, {"ok": True, "version": "1.0.0", "app": IN_APP or bool(hooks["show"])})
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        if path == "/api/jobs":
            with lock:
                return self.send(200, sorted(jobs.values(), key=lambda j: -j["created_at"]))
        if path == "/api/settings":
            return self.send(200, {**settings(), "start_at_login": start_at_login()})
        if path == "/api/formats":
            from urllib.parse import parse_qs, urlparse
            url = (parse_qs(urlparse(self.path).query).get("url") or [""])[0]
            if not url.startswith(("http://", "https://")):
                return self.send(400, {"error": "need url"})
            return self.send(200, formats(url))
        if path == "/api/stats":
            return self.send(200, stats())
        # Static UI
        f = (UI_DIR / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if f.is_file() and UI_DIR.resolve() in f.parents:
            ctype = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript; charset=utf-8",
                     ".svg": "image/svg+xml", ".png": "image/png"}.get(f.suffix, "application/octet-stream")
            return self.send(200, f.read_bytes(), ctype)
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        path = self.path.split("?")[0]
        try:
            b = self.body()
        except Exception:
            return self.send(400, {"error": "bad json"})

        if path == "/api/download":
            url, mode = b.get("url", ""), b.get("mode", "video")
            meta = metas.get(b.get("torrent") or "")
            if url.startswith("magnet:") or meta:
                mode = "torrent"
            elif not url.startswith(("http://", "https://")) and not meta or mode not in ("mp3", "video", "file", "torrent"):
                return self.send(400, {"error": "need http(s)/magnet url and mode mp3|video"})
            if not b.get("force"):
                with lock:
                    dup = next((j for j in jobs.values() if j["url"] == url and j["mode"] == mode
                                and j["status"] in ("queued", "downloading", "converting", "paused")), None)
                if dup:
                    return self.send(409, {"error": "Already downloading this", "job": dup})
            try:
                return self.send(201, new_job(url, mode, b.get("quality"), b.get("folder"), b.get("filename"),
                                              b.get("connections"), meta, b.get("prio")))
            except OSError as e:
                return self.send(400, {"error": f"Can't use that folder: {e.strerror}"})

        if path == "/api/torrent/meta":   # what's inside a magnet / .torrent (the Add window shows it)
            src = b.get("url") or ""
            if b.get("data"):
                import base64
                raw = base64.b64decode(b["data"])
                if not raw.startswith(b"d"):
                    return self.send(400, {"error": "That isn't a .torrent file"})
                TORRENT_DIR.mkdir(parents=True, exist_ok=True)
                src = str(TORRENT_DIR / f"{uuid.uuid4().hex[:8]}.torrent")
                Path(src).write_bytes(raw)
            if not is_torrent(src):
                return self.send(400, {"error": "need a magnet link or .torrent"})
            try:
                res = torrent_meta(src)
            except OSError as e:
                return self.send(400, {"error": f"Couldn't read the torrent: {e}"})
            if res.get("torrent"):
                metas[res["torrent"]] = res
            return self.send(200 if "error" not in res else 400, {**res, "src": src})

        if path == "/api/prompt":         # magnet clicked in the browser / Finder: ask before downloading
            prompts.append({k: b.get(k) for k in ("url", "mode", "quality")})
            if hooks["show"]:
                hooks["show"]()
            return self.send(200, {"ok": True})
        if path == "/api/prompt/take":
            return self.send(200, prompts.pop(0) if prompts else {"url": None})

        if m := re.fullmatch(r"/api/jobs/(\w+)/prio", path):   # per-file torrent priorities
            job = jobs.get(m.group(1))
            if not job or not job.get("files") or len(b.get("prio") or []) != len(job["files"]):
                return self.send(400, {"error": "bad priorities"})
            for f, p in zip(job["files"], b["prio"]):
                f["prio"] = max(0, min(7, int(p)))
            job["est_bytes"] = sum(f["size"] for f in job["files"] if f["prio"])
            if job["status"] == "downloading":
                lt_engine.call({"op": "prio", "job": job["id"], "prio": [f["prio"] for f in job["files"]]})
            save_history()
            return self.send(200, job)

        if path == "/api/torrent":        # a .torrent file picked in the Add window (base64)
            import base64
            raw = base64.b64decode(b.get("data") or "")
            if not raw.startswith(b"d"):
                return self.send(400, {"error": "That isn't a .torrent file"})
            TORRENT_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^\w .()-]", "_", b.get("name") or "file.torrent")
            dest = TORRENT_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
            dest.write_bytes(raw)
            job = new_job(str(dest), "torrent", folder=b.get("folder"))
            job["title"] = re.sub(r"\.torrent$", "", name, flags=re.I)
            return self.send(201, job)

        if path == "/api/open-folder":
            d = settings()["download_dir"]
            os.makedirs(d, exist_ok=True)
            open_path(d)
            return self.send(200, {"ok": True})

        if path == "/api/app/show":       # extension asks the Mac app to come forward
            if hooks["show"]:
                hooks["show"]()
            elif not WIN:
                subprocess.Popen(["open", "peakdm://show"])
            return self.send(200, {"ok": True})

        if path == "/api/settings/pick-folder":
            picked = pick_folder()
            if not picked:
                return self.send(200, settings() if b.get("key") != "__pick" else {"path": None})   # user cancelled
            if b.get("key") == "__pick":            # one-off folder for a single download
                return self.send(200, {"path": picked})
            key = b.get("key", "download_dir")
            if key not in ("download_dir", "video_dir", "audio_dir", "image_dir"):
                key = "download_dir"
            b = {key: picked}
            path = "/api/settings"

        if path == "/api/settings":
            if "start_at_login" in b:
                set_start_at_login(bool(b["start_at_login"]))
            for k, (lo, hi) in NUMERIC.items():
                if k in b:
                    try:
                        b[k] = max(lo, min(hi, float(b[k]) if k == "speed_limit" else int(b[k])))
                    except (TypeError, ValueError):
                        b.pop(k)
            s = {**settings(), **{k: v for k, v in b.items() if k in DEFAULT_SETTINGS}}
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(s, indent=2))
            return self.send(200, {**s, "start_at_login": start_at_login()})

        if m := re.fullmatch(r"/api/jobs/(\w+)/(pause|resume)", path):
            job = jobs.get(m.group(1))
            if not job:
                return self.send(404, {"error": "no such job"})
            if m.group(2) == "pause" and job["status"] in ("queued", "downloading"):
                job["status"] = "paused"
                if job.get("engine") == "lt":
                    lt_engine.call({"op": "pause", "job": job["id"]})
                if proc := procs.get(job["id"]):
                    kill_tree(proc)       # yt-dlp keeps the .part file; resume continues from it
            elif m.group(2) == "resume" and job["status"] in ("paused", "cancelled", "error"):
                job.update(status="queued", error=None)
                start(job["id"])
            return self.send(200, job)

        if m := re.fullmatch(r"/api/jobs/(\w+)/(reveal|open|retry)", path):
            job = jobs.get(m.group(1))
            if not job:
                return self.send(404, {"error": "no such job"})
            if m.group(2) == "retry":
                job.update(status="queued", error=None, progress=0, finished_at=None)
                start(job["id"])
                return self.send(201, job)
            target = job["file"] if job["file"] and os.path.exists(job["file"]) else settings()["download_dir"]
            reveal(target) if m.group(2) == "reveal" else open_path(target)
            return self.send(200, {"ok": True})

        self.send(404, {"error": "not found"})

    def do_DELETE(self):
        if not self.allowed():
            return self.send(403, {"error": "forbidden"})
        m = re.fullmatch(r"/api/jobs/(\w+)", self.path)
        if not m or m.group(1) not in jobs:
            return self.send(404, {"error": "no such job"})
        job_id = m.group(1)
        job = jobs[job_id]
        if job["status"] in ("queued", "downloading", "converting"):
            job["status"] = "cancelled"   # queued jobs see this and never start
            if job.get("engine") == "lt":
                lt_engine.call({"op": "remove", "job": job_id})
            if proc := procs.get(job_id):
                kill_tree(proc)
                proc.wait(timeout=5)
            remove_partials(job)
            job["finished_at"] = time.time()
            save_history()
        else:                           # finished / paused -> remove from list
            if job["status"] == "paused":
                remove_partials(job)
            with lock:
                jobs.pop(job_id)
            save_history()
        self.send(200, {"ok": True})


def serve():
    for j in load_json(HISTORY_FILE, []):
        jobs[j["id"]] = {"bytes": None, **j}
    threading.Thread(target=worker.ensure, daemon=True).start()   # warm yt-dlp before the first click
    print(f"Peak Download Manager backend on http://{HOST}:{PORT}")
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    return httpd


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    serve().serve_forever()
