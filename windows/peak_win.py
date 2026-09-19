"""Peak Download Manager: Windows shell.

The Windows counterpart of mac/PeakApp.swift. It runs the backend in-process,
shows the UI in an Edge WebView2 window (pywebview), and keeps a tray icon.
Closing the window hides it to the tray; downloads keep going until Quit.
"""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(HERE))

import webview                      # noqa: E402  pywebview (WebView2 on Windows)
import pystray                      # noqa: E402
from PIL import Image               # noqa: E402

import server                       # noqa: E402

URL = f"http://{server.HOST}:{server.PORT}"
COMPACT_W, COMPACT_H = 380, 350   # compact card size (fixed)
FULL = (1080, 680)
BG = "#0B0B0B"          # card background
CHROME_W, CHROME_H = 16, 39   # Windows title bar + borders: resize() takes the outer size
STATE_FILE = server.DATA_DIR / "window.json"

window = None
tray = None
view = {"size": "compact"}


def opened_link():
    """A magnet link or .torrent file Windows handed us (see installer.iss)."""
    arg = next((a for a in sys.argv[1:] if a.startswith("magnet:") or a.lower().endswith(".torrent")), None)
    return os.path.abspath(arg) if arg and not arg.startswith("magnet:") else arg


def post(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Origin": URL})
    urllib.request.urlopen(req, timeout=2)


def already_running():
    """Second launch: pass on any magnet/.torrent, ask the running copy to show itself, then exit."""
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as r:
            if json.load(r).get("ok"):
                if link := opened_link():
                    post("/api/prompt", {"url": link})
                post("/api/app/show", {})
                return True
    except OSError:
        pass
    return False


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**kw):
    s = {**load_state(), **kw}
    server.DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(s))


def show():
    if window:
        window.show()
        window.restore()


def js(code):
    try:
        window.evaluate_js(code)
    except Exception:  # noqa: BLE001 - page not ready yet
        pass


class Api:
    """Receives the same messages the Mac app gets from WKWebView (see app.js `shell()`)."""

    def post(self, msg):
        kind = msg.get("type")
        if kind == "resize":
            self.resize(msg.get("size"), msg.get("height"))
        elif kind == "pickFolder":
            res = window.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                js(f"window.peakShell.folderPicked({json.dumps(msg.get('key'))}, {json.dumps(res[0])})")
        elif kind == "login":
            on = bool(msg.get("on"))
            server.set_start_at_login(on)
            js(f"window.peakShell.login({json.dumps(on)})")

    def resize(self, size, height=None):
        if size == view["size"]:
            return
        if size == "full":
            w, h = load_state().get("full", FULL)
            window.resize(w, h)
            try:                                # centred: the full app isn't the small card grown bigger
                import ctypes
                sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
                window.move(max(0, (sw - w) // 2), max(0, (sh - h) // 2))
            except Exception:  # noqa: BLE001
                pass
        else:
            save_state(full=[window.width, window.height])     # remember the big window's size
            window.resize(COMPACT_W + CHROME_W, COMPACT_H + CHROME_H)
        view["size"] = size
        save_state(view=size)                                  # reopen the way it was left


# ---------- tray ----------
def tray_icon():
    return Image.open(HERE / "ui" / "icon.png").resize((64, 64))


def quit_app(*_):
    server.shutdown()                  # stop downloads, save them as paused
    if tray:
        tray.stop()
    window.destroy()
    os._exit(0)


def open_folder(*_):
    d = server.settings()["download_dir"]
    os.makedirs(d, exist_ok=True)
    os.startfile(d)


def tray_status():
    """Tooltip shows what the Mac Dock badge shows: active downloads + speed."""
    while True:
        try:
            s = server.stats()
            n = s["downloading"] + s["queued"]
            mb = (s.get("speed_bps") or 0) / 1e6
            tray.title = f"Peak · {n} active · {mb:.1f} MB/s" if n else "Peak Download Manager"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)


def notify(title, body):
    try:
        tray.notify(body, title)
    except Exception:  # noqa: BLE001
        pass


def on_closing():
    """The close button hides to the tray, like the Mac app."""
    window.hide()
    if not load_state().get("told_tray"):
        notify("Peak is still running", "Downloads continue in the background. Right-click the tray icon to quit.")
        save_state(told_tray=True)
    return False


def main():
    global window, tray
    if already_running():
        return
    os.environ["PEAK_APP"] = "1"
    server.hooks["notify"] = notify
    server.hooks["show"] = lambda: show()
    httpd = server.serve()
    if link := opened_link():              # launched by a magnet click: the New download box shows it
        server.prompts.append({"url": link})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tray = pystray.Icon("Peak", tray_icon(), "Peak Download Manager", pystray.Menu(
        pystray.MenuItem("Show Peak", lambda *_: show(), default=True),
        pystray.MenuItem("Open downloads folder", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Peak", quit_app),
    ))
    tray.run_detached()
    threading.Thread(target=tray_status, daemon=True).start()

    hidden = "--hidden" in sys.argv and not opened_link()               # started at login: stay in the tray
    st = load_state()
    view["size"] = st.get("view", "compact")
    w, h = st.get("full", FULL) if view["size"] == "full" else (COMPACT_W + CHROME_W, COMPACT_H + CHROME_H)
    window = webview.create_window(
        "Peak", f"{URL}/?surface=window&shell=win&view={view['size']}", width=w, height=h,
        min_size=(COMPACT_W + CHROME_W, COMPACT_H + CHROME_H), background_color=BG, js_api=Api(), hidden=hidden, text_select=False)
    window.events.closing += on_closing
    webview.start(private_mode=False, storage_path=str(server.DATA_DIR / "webview"))


if __name__ == "__main__":
    main()
