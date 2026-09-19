// Floating Peak button on the main video of the page. Clicking it opens a
// small bento panel of quality tiles (4K, 1080p, MP3 320…).
// Same mounting approach as CaptionFlow: a fixed-position shadow-DOM host that
// tracks the video's bounding box and re-parents itself into fullscreen elements.
(() => {
  if (window.__peakOverlay) return;
  window.__peakOverlay = true;

  let enabled = true;
  let host, shadow, wrap, panel, video = null, lastUpdate = 0, open = false;

  const send = (msg) => new Promise((resolve) => {
    try { chrome.runtime.sendMessage(msg, (r) => resolve(chrome.runtime.lastError ? { ok: false, error: "Reload the page — Peak was updated" } : r)); }
    catch { resolve({ ok: false, error: "Reload the page — Peak was updated" }); }
  });

  function visibleArea(v) {
    const r = v.getBoundingClientRect();
    if (r.width < 200 || r.height < 110) return 0;
    const s = getComputedStyle(v);
    if (s.display === "none" || s.visibility === "hidden" || +s.opacity === 0) return 0;
    const w = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left));
    const h = Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
    return w * h;
  }

  // The site's main player, when we know it. Hover previews (YouTube sidebar,
  // feed autoplay) are also <video>s and must never win over the real one.
  const MAIN = ["#movie_player video", "ytd-player video", ".html5-main-video", "#player video", "[data-testid='videoPlayer'] video"];
  function findBestVideo() {
    for (const sel of MAIN) {
      const v = document.querySelector(sel);
      if (v && visibleArea(v) > 0) return v;
    }
    const vs = [...document.querySelectorAll("video")].map((v) => ({ v, a: visibleArea(v), p: !v.paused && !v.ended ? 1 : 0 })).filter((x) => x.a > 0);
    const big = Math.max(0, ...vs.map((x) => x.a));
    // Only big videos count; among those, a playing one wins.
    return vs.filter((x) => x.a >= big * 0.6).sort((a, b) => b.p - a.p || b.a - a.a)[0]?.v || null;
  }

  // Pin to the player box (not the <video>, which can be letterboxed inside it).
  function anchorRect(v) {
    const player = v.closest("#movie_player, .html5-video-player, [data-testid='videoPlayer']");
    return (player || v).getBoundingClientRect();
  }

  // The URL to download. On feeds (Pinterest, X, Instagram) the page URL isn't
  // the post, so look for the nearest permalink around the video.
  function targetUrl() {
    const here = location.href;
    if (/\/(pin|status|reel|p|video|shorts)\//.test(location.pathname) || /[?&]v=/.test(location.search)) return here;
    let el = video;
    for (let i = 0; el && i < 12; i++, el = el.parentElement) {
      const a = el.querySelector?.('a[href*="/pin/"], a[href*="/status/"], a[href*="/reel/"], a[href*="/p/"], a[href*="/video/"]');
      if (a) return new URL(a.getAttribute("href"), location.href).href;
      if (el.closest?.("a")?.href) return el.closest("a").href;
    }
    return here;
  }

  function parentForFullscreen() {
    const fs = document.fullscreenElement;
    return fs && fs.tagName !== "VIDEO" ? fs : document.documentElement;
  }

  const ICON = `<img src="${chrome.runtime.getURL("icons/mark.png")}" alt="" draggable="false">`;

  function ensureHost() {
    if (host?.isConnected) return;
    host = document.createElement("div");
    host.id = "peak-dm-overlay-host";
    Object.assign(host.style, {
      position: "fixed", left: "0", top: "0", width: "0", height: "0",
      zIndex: "2147483647", pointerEvents: "none", display: "none",
      margin: "0", padding: "0", border: "0",
    });
    shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        * { box-sizing: border-box; }
        .wrap { position: absolute; top: 12px; right: 12px; pointer-events: auto; display: flex; flex-direction: column; align-items: flex-end; gap: 8px;
          font: 500 12px/1.3 "Inter", -apple-system, BlinkMacSystemFont, system-ui, sans-serif; color: #FFFFFF;
          opacity: 0; transition: opacity .15s ease; -webkit-font-smoothing: antialiased; }
        :host(.show) .wrap, .wrap:hover, .wrap.open { opacity: 1; }
        .btn { all: unset; cursor: pointer; width: 38px; height: 42px; display: grid; place-items: center; transition: transform .12s ease; }
        .btn img { width: 34px; height: 40px; object-fit: contain; pointer-events: none;
          filter: drop-shadow(0 3px 8px rgba(0,0,0,.55)) drop-shadow(0 0 10px rgba(54,96,255,.45)); }
        .btn:hover { transform: scale(1.07); }
        .panel { display: none; width: 276px; padding: 12px; border-radius: 18px; background: rgba(10,10,10,.97);
          border: 1px solid rgba(255,255,255,.1); box-shadow: 0 28px 60px rgba(0,0,0,.8);
          backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); transform-origin: top right; }
        .wrap.open .panel { display: block; animation: pop .14s cubic-bezier(.2,.9,.3,1.2); }
        @keyframes pop { from { opacity: 0; transform: scale(.95); } }
        .label { font-size: 10.5px; color: #8A8A8A; padding: 0 2px 7px; display: flex; justify-content: space-between; letter-spacing: .02em; }
        .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 10px; }
        .grid:last-child { margin-bottom: 0; }
        .tile { all: unset; cursor: pointer; height: 50px; border-radius: 10px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px;
          background: #181818; border: 1px solid rgba(255,255,255,.07); transition: background .12s, border-color .12s, transform .1s; }
        .tile:hover { background: #232323; border-color: rgba(255,255,255,.22); }
        .tile:active { transform: scale(.96); }
        .tile b { font-size: 13.5px; font-weight: 600; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
        .tile small { font-size: 10px; color: #9A9A9A; font-variant-numeric: tabular-nums; }
        .tile.hero { background: linear-gradient(140deg, #00C4FF 0%, #3660FF 55%, #1111C8 100%); border-color: transparent; box-shadow: 0 6px 16px -6px rgba(54,96,255,.6); }
        .tile.hero small { color: rgba(255,255,255,.75); }
        .tile.ghost { cursor: default; background: linear-gradient(90deg, #161616 0%, #222 50%, #161616 100%); background-size: 200% 100%; animation: shimmer 1s linear infinite; }
        @keyframes shimmer { to { background-position: -200% 0; } }
        .msg { padding: 18px 8px; text-align: center; color: #FFFFFF; font-size: 12px; }
        .msg.err { color: #F87171; }
        .spin { width: 16px; height: 16px; margin: 0 auto 8px; border-radius: 50%; border: 2px solid rgba(255,255,255,.15); border-top-color: #00C4FF; animation: spin .8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
      </style>
      <div class="wrap">
        <button class="btn" title="Download with Peak">${ICON}</button>
        <div class="panel"></div>
      </div>`;
    wrap = shadow.querySelector(".wrap");
    panel = shadow.querySelector(".panel");
    shadow.querySelector(".btn").addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); toggle(); });
    // Keep clicks inside our panel from reaching the player (pause/play).
    for (const ev of ["click", "mousedown", "pointerdown", "dblclick"]) wrap.addEventListener(ev, (e) => e.stopPropagation());
    document.addEventListener("click", () => close());
    parentForFullscreen().appendChild(host);
  }

  function close() { open = false; wrap?.classList.remove("open"); }

  // Quality lookups are started as soon as the mouse is over a video, so by the
  // time the button is clicked the answer is usually already here.
  const lookups = new Map();   // url -> Promise<formats>
  function prefetch(url) {
    if (!lookups.has(url)) {
      const p = send({ type: "formats", url });
      lookups.set(url, p);
      p.then((f) => { if (!f || f.error) setTimeout(() => lookups.delete(url), 3000); });
    }
    return lookups.get(url);
  }

  const size = (n) => !n ? "" : n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e8 ? Math.round(n / 1e6) + " MB" : (n / 1e6).toFixed(1) + " MB";
  const mp3Tiles = (hero, f) => ["320", "192", "128"].map((q, i) => `
        <button class="tile${hero && i === 0 ? " hero" : ""}" data-mode="mp3" data-q="${q}"><b>${q}</b><small>${size(f?.mp3_size?.[q]) || "kbps"}</small></button>`).join("");

  function render(url, f) {
    const video = (f?.video || []).slice(0, 6);
    const loading = !f;
    panel.innerHTML = `
      <div class="label"><span>Video</span><span>${loading ? "checking…" : video.length ? `${video.length} available` : "—"}</span></div>
      <div class="grid">${loading ? '<div class="tile ghost"></div>'.repeat(3) : video.map((v, i) => `
        <button class="tile${i === 0 ? " hero" : ""}" data-mode="video" data-q="${v.height || "best"}">
          <b>${escapeHtml(v.label)}</b><small>${size(v.size) || (v.fps > 30 ? v.fps + " fps" : "MP4")}</small></button>`).join("")}</div>
      <div class="label"><span>Audio · MP3</span><span>${f?.audio_kbps ? `source ${f.audio_kbps} kbps` : ""}</span></div>
      <div class="grid">${mp3Tiles(!loading && !video.length, f)}</div>`;
    panel.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", async () => {
      panel.innerHTML = `<div class="msg"><div class="spin"></div>Sending to Peak…</div>`;
      const r = await send({ type: "grab", url, mode: b.dataset.mode, quality: b.dataset.q });
      panel.innerHTML = r?.ok ? `<div class="msg">Downloading ✓</div>` : `<div class="msg err">${escapeHtml(r?.error || "Peak isn't running")}</div>`;
      setTimeout(close, 1400);
    }));
  }

  async function toggle() {
    if (open) return close();
    open = true;
    wrap.classList.add("open");
    const url = targetUrl();
    let done = false;
    const p = prefetch(url);
    // MP3 tiles need no lookup — show them straight away; video tiles fill in.
    setTimeout(() => { if (!done && open) render(url, null); }, 0);
    const f = await p;
    done = true;
    if (!open) return;
    if (!f || f.error) {
      if (f?.error && !/isn't running|Reload/.test(f.error)) return render(url, { video: [], audio_kbps: null });
      panel.innerHTML = `<div class="msg err">${escapeHtml(f?.error || "Peak isn't running")}</div>`;
      return;
    }
    render(url, f);
  }

  function escapeHtml(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  function hide() { if (host) host.style.display = "none"; close(); }

  function update(force) {
    if (!enabled) return hide();
    const now = performance.now();
    if (!force && now - lastUpdate < 90) return;
    lastUpdate = now;
    if (!open) {
      const v = findBestVideo();
      if (v !== video && host) delete host.dataset.pre;   // new video -> prefetch again on hover
      video = v;
    }
    if (!video) return hide();
    ensureHost();
    const r = anchorRect(video);
    if (r.bottom < 0 || r.top > innerHeight) return hide();
    Object.assign(host.style, {
      display: "block", left: `${Math.round(r.left)}px`, top: `${Math.round(r.top)}px`,
      width: `${Math.round(r.width)}px`, height: `${Math.round(r.height)}px`,
    });
  }

  // Show the button while the mouse is over the video (like native controls).
  document.addEventListener("mousemove", (e) => {
    if (!host || !video) return;
    const r = anchorRect(video);
    const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    host.classList.toggle("show", inside);
    if (inside && host.dataset.pre !== location.href) { host.dataset.pre = location.href; prefetch(targetUrl()); }
  }, { passive: true });

  document.addEventListener("fullscreenchange", () => {
    if (host) parentForFullscreen().appendChild(host);
    update(true);
  });
  addEventListener("scroll", () => update(), { passive: true, capture: true });
  addEventListener("resize", () => update(true));
  setInterval(() => update(), 700);   // catches SPA navigation and late-loading players

  // Magnet / .torrent links go to Peak instead of the system torrent app (like FDM).
  document.addEventListener("click", (e) => {
    const a = e.target.closest?.("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const href = a.href;
    if (!/^magnet:\?/i.test(href) && !/\.torrent$/i.test(new URL(href, location.href).pathname)) return;
    e.preventDefault();
    e.stopPropagation();
    send({ type: "torrent", url: href }).then((r) => toast(r?.ok ? "Opened in Peak ✓" : r?.error || "Peak isn't running", !r?.ok));
  }, true);

  function toast(text, err) {
    const t = document.createElement("div");
    Object.assign(t.style, { position: "fixed", right: "20px", bottom: "20px", zIndex: "2147483647", padding: "10px 14px",
      borderRadius: "12px", background: "#121212", color: err ? "#F87171" : "#fff", border: "1px solid rgba(255,255,255,.12)",
      font: "500 13px -apple-system, system-ui, sans-serif", boxShadow: "0 12px 30px rgba(0,0,0,.5)" });
    t.textContent = text;
    document.documentElement.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }

  chrome.storage.sync.get({ showOverlay: true }, (s) => { enabled = s.showOverlay; update(true); });
  chrome.storage.onChanged.addListener((c) => {
    if (c.showOverlay) { enabled = c.showOverlay.newValue; update(true); }
  });
})();
