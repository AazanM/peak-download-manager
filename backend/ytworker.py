"""Long-lived yt-dlp extractor for Peak.

Runs under yt-dlp's own Python so `import yt_dlp` works, and stays warm so every
lookup skips interpreter start-up and module import (~1s saved per call).

Protocol: one JSON object per line on stdin  {"id", "url", "proxy"?, "cookies"?, "outtmpl"?}
          one JSON object per line on stdout {"id", "info"?, "filename"?, "error"?}
"""
import copy
import json
import sys
import threading

import yt_dlp

out_lock = threading.Lock()
local = threading.local()


def ydl_for(req):
    key = (req.get("proxy") or "", req.get("cookies") or "")
    cache = getattr(local, "ydls", None)
    if cache is None:
        cache = local.ydls = {}
    if key not in cache:
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "check_formats": False,
            # YouTube: the DASH/HLS manifests cost an extra round trip each and
            # the progressive/adaptive https formats already cover every resolution.
            "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
        }
        if key[0]:
            opts["proxy"] = key[0]
        if key[1]:
            opts["cookiesfrombrowser"] = (key[1],)
        cache[key] = yt_dlp.YoutubeDL(opts)
    return cache[key]


def sizes(req):
    """Bytes each quality will really download (video + audio), using yt-dlp's own picker."""
    from yt_dlp.utils import FormatSorter
    out = {}
    for key, fmt in req["formats"].items():
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format_sort": req["sort"]}) as y:
                fs = copy.deepcopy(req["info"].get("formats") or [])
                fs.sort(key=FormatSorter(y, req["sort"]).calculate_preference)
                sel = list(y.build_format_selector(fmt)({"formats": fs, "has_merged_format": True, "incomplete_formats": False}))
            parts = (sel[0].get("requested_formats") or [sel[0]]) if sel else []
            dur = req["info"].get("duration") or 0
            # sites that don't publish sizes: bitrate (kbit/s) x duration is within a few %
            out[key] = sum((p.get("filesize") or p.get("filesize_approx") or (p.get("tbr") or 0) * 125 * dur) for p in parts) or None
        except Exception:  # noqa: BLE001 - a size is a nice-to-have
            out[key] = None
    return out


def handle(req):
    """Also called directly (in-process) by the Windows build."""
    res = {"id": req.get("id")}
    try:
        if req.get("op") == "filename":          # no network: name from already-fetched info
            with yt_dlp.YoutubeDL({"outtmpl": req["outtmpl"], "quiet": True}) as f:
                res["filename"] = f.prepare_filename(req["info"])
            return res
        if req.get("op") == "sizes":            # no network: run yt-dlp's own format picker per quality
            res["sizes"] = sizes(req)
            return res
        ydl = ydl_for(req)
        info = ydl.extract_info(req["url"], download=False)
        res["info"] = ydl.sanitize_info(info)
    except Exception as e:  # noqa: BLE001 - report every failure back to the server
        res["error"] = str(e)
    return res


def reply(res):
    line = json.dumps(res, default=str)
    with out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def serve_stdin():
    for raw in sys.stdin:
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        threading.Thread(target=lambda r=req: reply(handle(r)), daemon=True).start()


if __name__ == "__main__":
    serve_stdin()
