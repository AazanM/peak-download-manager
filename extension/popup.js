import { APP, grab, openApp, openAppIfWanted, isRunning, formats } from "./api.js";
const size = (n) => !n ? "" : n >= 1e9 ? (n / 1e9).toFixed(2) + " GB" : n >= 1e8 ? Math.round(n / 1e6) + " MB" : (n / 1e6).toFixed(1) + " MB";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
const supported = /^https?:\/\//.test(tab?.url || "") && !tab.url.startsWith(APP);

// Overlay toggle (read by overlay.js on every page)
const { showOverlay } = await chrome.storage.sync.get({ showOverlay: true });
$("overlaySwitch").setAttribute("aria-checked", showOverlay);
$("overlaySwitch").onclick = async () => {
  const next = $("overlaySwitch").getAttribute("aria-checked") !== "true";
  $("overlaySwitch").setAttribute("aria-checked", next);
  await chrome.storage.sync.set({ showOverlay: next });
};
document.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => openApp().then(() => window.close())));

function setStatus(text, off) {
  $("status").classList.toggle("off", !!off);
  $("status").querySelector("span").textContent = text;
}
function note(text) { $("note").hidden = !text; $("note").textContent = text || ""; }

let info = null;         // formats from the backend
let openMode = null;     // which quality bento is showing

function showQualities(mode) {
  const sub = $("sub");
  if (openMode === mode) { openMode = null; sub.classList.remove("open"); markSel(); return; }
  openMode = mode;
  markSel();
  if (!info && mode === "video") {
    sub.innerHTML = `<div class="sub-label"><span>Resolution</span><span>checking…</span></div>` + '<div class="tile ghost"></div>'.repeat(3);
  } else if (mode === "video") {
    const v = info.video?.length ? info.video : [{ height: "best", label: "Best", fps: null }];
    sub.innerHTML = `<div class="sub-label"><span>Resolution</span><span>${v.length} available</span></div>` +
      v.map((x, i) => `<button class="tile${i === 0 ? " hero" : ""}" data-q="${x.height || "best"}"><div class="v num">${esc(x.label)}</div><div class="k num">${size(x.size) || (x.fps > 30 ? x.fps + " fps" : "MP4")}</div></button>`).join("");
  } else {
    sub.innerHTML = `<div class="sub-label"><span>MP3 bitrate</span><span class="num">${info?.audio_kbps ? `source ${info.audio_kbps} kbps` : ""}</span></div>` +
      ["320", "192", "128"].map((q, i) => `<button class="tile${i === 0 ? " hero" : ""}" data-q="${q}"><div class="v num">${q}</div><div class="k num">${size(info?.mp3_size?.[q]) || "kbps"}</div></button>`).join("");
  }
  sub.classList.add("open");
  sub.querySelectorAll("[data-q]").forEach((b) => (b.onclick = () => send(mode, b.dataset.q)));
}

function markSel() {
  $("mp3").classList.toggle("hero", openMode !== "video");
  $("video").classList.toggle("hero", openMode === "video");
}

async function send(mode, quality) {
  $("sub").innerHTML = `<div class="msg"><div class="spin"></div>Sending to Peak…</div>`;
  try {
    await grab(tab.url, mode, quality);
    await openAppIfWanted();
    window.close();
  } catch (e) {
    $("sub").innerHTML = `<div class="msg">${esc(e.message)}</div>`;
  }
}

$("mp3").onclick = () => showQualities("mp3");
$("video").onclick = () => showQualities("video");

// ---- initial state ----
if (!supported) {
  $("pageTitle").textContent = "Nothing to download here";
  note("Open a video page: YouTube, Pinterest, Instagram, TikTok, X.");
  $("mp3").disabled = $("video").disabled = true;
  $("videoSub").textContent = "—";
  setStatus("idle");
} else if (!(await isRunning())) {
  $("pageTitle").textContent = "Peak isn't running";
  note("It starts at login and restarts itself. If this keeps showing, run scripts/install.sh again.");
  $("mp3").disabled = $("video").disabled = true;
  $("videoSub").textContent = "—";
  setStatus("offline", true);
} else {
  $("pageTitle").textContent = tab.title || tab.url;
  $("pageHost").textContent = new URL(tab.url).hostname.replace(/^www\./, "");
  formats(tab.url).then((f) => {
    if (f.error) {
      $("videoSub").textContent = "not found";
      note(f.error);
      return;
    }
    info = f;
    $("videoSub").textContent = f.video?.[0] ? `up to ${f.video[0].label}` : "best";
    $("mp3Sub").textContent = f.audio_kbps ? `source ${f.audio_kbps} kbps` : "audio";
    if (openMode === "video") { openMode = null; showQualities("video"); }
  });
}
