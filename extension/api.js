// Shared helpers for talking to the local Peak Download Manager backend.
export const APP = "http://127.0.0.1:7878";
export const SIZES = { compact: { width: 380, height: 382 }, full: { width: 1080, height: 680 } };

export async function isRunning() {
  try { return (await fetch(`${APP}/api/health`)).ok; } catch { return false; }
}

export async function grab(url, mode, quality) {
  const r = await fetch(`${APP}/api/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, mode, quality }),
  });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return r.json();
}

// Torrents open Peak's New download box (file list, sizes, folder) instead of starting straight away.
// Every download opens Peak's New download box first (size, folder, quality or torrent files).
export async function prompt(url, mode, quality) {
  const r = await fetch(`${APP}/api/prompt`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url, mode, quality }),
  });
  if (!r.ok) throw new Error(r.statusText);
  return openApp();
}

export async function formats(url) {
  try {
    return await (await fetch(`${APP}/api/formats?url=${encodeURIComponent(url)}`)).json();
  } catch {
    return { error: "Peak isn't running" };
  }
}

// Settings → General → "Open Peak window when a download starts".
export async function openAppIfWanted() {
  try {
    const s = await (await fetch(`${APP}/api/settings`)).json();
    if (s.open_app_on_start === false) return;
  } catch {}
  return openApp();
}

// Opens the Mac app if it's installed, otherwise a Chrome window with the bento view.
export async function openApp() {
  try {
    const h = await (await fetch(`${APP}/api/health`)).json();
    if (h.app) return fetch(`${APP}/api/app/show`, { method: "POST" });
  } catch {}
  const [existing] = await chrome.tabs.query({ url: `${APP}/*` });
  if (existing) {
    await chrome.windows.update(existing.windowId, { focused: true });
    return chrome.tabs.update(existing.id, { active: true });
  }
  return chrome.windows.create({
    url: `${APP}/?ext=${chrome.runtime.id}`, type: "popup", ...SIZES.compact,
  });
}
