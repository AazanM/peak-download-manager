import { grab, prompt, openApp, openAppIfWanted, isRunning, formats, SIZES } from "./api.js";

chrome.runtime.onInstalled.addListener(() => {
  const ctx = ["page", "link", "video", "audio"];
  chrome.contextMenus.create({ id: "mp3", title: "Peak: download as MP3", contexts: ctx });
  chrome.contextMenus.create({ id: "video", title: "Peak: download video", contexts: ctx });
  chrome.contextMenus.create({ id: "torrent", title: "Peak: download torrent", contexts: ["link"],
    targetUrlPatterns: ["magnet:*", "*://*/*.torrent", "*://*/*.torrent?*"] });
});

async function send(url, mode, quality) {
  if (!(await isRunning())) {
    notify("Peak isn't running", "It restarts automatically. If this keeps happening, run scripts/install.sh again.");
    return { ok: false, error: "Peak isn't running" };
  }
  try {
    if (mode === "torrent") { await prompt(url); return { ok: true }; }
    await grab(url, mode, quality);
    await openAppIfWanted();
    return { ok: true };
  } catch (e) {
    notify("Couldn't start download", e.message);
    return { ok: false, error: e.message };
  }
}

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title, message });
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || (info.srcUrl?.startsWith("http") && !info.srcUrl.startsWith("blob:") ? info.srcUrl : null) || tab.url;
  send(url, info.menuItemId);
});

chrome.commands.onCommand.addListener(async (cmd) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  send(tab.url, cmd === "grab-mp3" ? "mp3" : "video");
});

// Messages from overlay.js (the button on videos).
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "grab") send(msg.url, msg.mode, msg.quality).then(reply);
  else if (msg?.type === "torrent") send(msg.url, "torrent").then(reply);
  else if (msg?.type === "formats") formats(msg.url).then(reply);
  else return;
  return true; // async reply
});

// Messages from the app window: expand to the full view / shrink back to the bento view.
chrome.runtime.onMessageExternal.addListener((msg, sender, reply) => {
  if (msg?.type !== "resize" || !sender.tab) return;
  const size = SIZES[msg.size];
  if (!size) return;
  chrome.windows.update(sender.tab.windowId, size).then(() => reply({ ok: true }));
  return true;
});
