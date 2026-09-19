"""BitTorrent engine for Peak (libtorrent, the engine qBittorrent and FDM use).

Runs as its own process under a Python that has libtorrent, like ytworker.py.
Protocol, one JSON object per line:
  stdin   {"id", "op": "meta", "src", "cache"}          magnet or .torrent path -> file list
          {"op": "add", "job", "torrent", "save", "prio"?}
          {"op": "prio", "job", "prio"}   priorities per file: 0 skip, 1 low, 4 normal, 7 high
          {"op": "pause"|"remove", "job", "delete"?}
          {"op": "limit", "bps"}
  stdout  {"id", ...reply}  or  {"ev": "status"|"finished"|"error", "job", ...}
"""
import json
import os
import sys
import threading
import time
import warnings

import libtorrent as lt

warnings.filterwarnings("ignore", category=DeprecationWarning)

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce", "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce", "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce", "udp://open.demonii.com:1337/announce",
    "udp://explodie.org:6969/announce", "udp://tracker.dler.org:6969/announce",
    "https://tracker.tamersunion.org:443/announce",
]


class Engine:
    def __init__(self, emit):
        self.emit = emit
        self.lock = threading.Lock()
        self.handles = {}           # job id -> torrent_handle
        self.ses = lt.session({
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True, "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                   "router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "connections_limit": 800, "active_downloads": 20, "active_limit": 40,
            "alert_mask": lt.alert.category_t.error_notification | lt.alert.category_t.status_notification,
            "user_agent": "Peak/1.0 libtorrent/" + lt.__version__,
        })
        threading.Thread(target=self.loop, daemon=True).start()

    # ---- metadata: what's inside, before anything downloads ----
    def meta(self, req):
        src, cache = req["src"], req["cache"]
        if src.startswith("magnet:"):
            atp = lt.parse_magnet_uri(src)
            atp.save_path = cache
            atp.flags |= lt.torrent_flags.upload_mode      # fetch the file list only, no payload
            atp.trackers = list(atp.trackers) + TRACKERS
            h = self.ses.add_torrent(atp)
            end = time.time() + float(req.get("timeout") or 90)
            while not h.status().has_metadata:
                if time.time() > end:
                    self.ses.remove_torrent(h)
                    return {"error": "Couldn't find anyone sharing this magnet link yet. Try again in a minute."}
                time.sleep(0.25)
            ti = h.torrent_file()
            self.ses.remove_torrent(h)
            path = os.path.join(cache, str(ti.info_hashes().get_best()) + ".torrent")
            ct = lt.create_torrent(ti)
            for t in TRACKERS:
                ct.add_tracker(t)
            with open(path, "wb") as f:
                f.write(lt.bencode(ct.generate()))
        else:
            path, ti = src, lt.torrent_info(src)
        fs = ti.files()
        return {"torrent": path, "name": ti.name(), "size": ti.total_size(), "hash": str(ti.info_hashes().get_best()),
                "files": [{"path": fs.file_path(i), "size": fs.file_size(i)} for i in range(fs.num_files())]}

    # ---- downloads ----
    def add(self, req):
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(req["torrent"])
        atp.save_path = req["save"]
        atp.trackers = TRACKERS
        if req.get("prio"):
            atp.file_priorities = [int(p) for p in req["prio"]]
        with self.lock:
            old = self.handles.pop(req["job"], None)
            if old:
                self.ses.remove_torrent(old)
            self.handles[req["job"]] = self.ses.add_torrent(atp)   # existing data is re-checked, so resume works
        return {"ok": True}

    def op(self, req):
        o = req["op"]
        if o == "meta":
            return self.meta(req)
        if o == "add":
            return self.add(req)
        if o == "limit":
            self.ses.apply_settings({"download_rate_limit": int(req.get("bps") or 0)})
            return {"ok": True}
        with self.lock:
            h = self.handles.get(req.get("job"))
            if o == "prio" and h:
                h.prioritize_files([int(p) for p in req["prio"]])
            elif o in ("pause", "remove") and h:
                del self.handles[req["job"]]
                self.ses.remove_torrent(h, lt.session.delete_files if req.get("delete") else 0)
        return {"ok": True}

    def loop(self):
        while True:
            time.sleep(1)
            for a in self.ses.pop_alerts():
                if isinstance(a, lt.torrent_error_alert):
                    job = self.job_of(a.handle)
                    if job:
                        self.emit({"ev": "error", "job": job, "error": a.message()})
            with self.lock:
                items = list(self.handles.items())
            for job, h in items:
                try:
                    s = h.status()
                    done = h.file_progress(lt.torrent_handle.piece_granularity) if s.has_metadata else []
                    self.emit({"ev": "status", "job": job, "rate": s.download_rate, "up": s.upload_rate,
                               "peers": s.num_peers, "seeds": s.num_seeds, "wanted": s.total_wanted,
                               "done": s.total_wanted_done, "checking": s.state in (
                                   lt.torrent_status.checking_files, lt.torrent_status.checking_resume_data),
                               "files": list(done)})
                    if s.has_metadata and s.total_wanted and s.total_wanted_done >= s.total_wanted:
                        with self.lock:
                            self.handles.pop(job, None)
                        self.ses.remove_torrent(h)                  # like FDM's default: stop once complete
                        self.emit({"ev": "finished", "job": job})
                except RuntimeError:
                    pass

    def job_of(self, handle):
        with self.lock:
            return next((j for j, h in self.handles.items() if h == handle), None)


def serve_stdin():
    out = threading.Lock()

    def emit(msg):
        with out:
            sys.stdout.write(json.dumps(msg) + "\n")
            sys.stdout.flush()

    eng = Engine(emit)

    def run(req):
        try:
            res = eng.op(req)
        except Exception as e:  # noqa: BLE001 - report every failure to the caller
            res = {"error": str(e)}
        emit({"id": req.get("id"), **res})

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        threading.Thread(target=run, args=(req,), daemon=True).start()   # meta can take a while
    os._exit(0)          # app quit: stdin closed


if __name__ == "__main__":
    serve_stdin()
