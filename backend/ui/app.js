// Peak Download Manager UI: compact widget + full app. Talks to the local backend.
const IS_WIN = /Win/.test(navigator.platform);
// Mac app: WKWebView message handler. Windows app: pywebview's JS API (page loaded with ?shell=win).
const SHELL = window.webkit?.messageHandlers?.peak ||
  (new URLSearchParams(location.search).get('shell') === 'win' ? { postMessage: (m) => window.pywebview?.api?.post(m) } : null);
if (IS_WIN) {
  document.querySelectorAll('.set-row div, [aria-label]').forEach((el) => {
    if (el.childElementCount === 0 && el.textContent.includes('Finder')) el.textContent = el.textContent.replace('Finder', 'File Explorer');
    if (el.getAttribute('aria-label')?.includes('Finder')) el.setAttribute('aria-label', el.getAttribute('aria-label').replace('Finder', 'File Explorer'));
  });
}
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ACTIVE = ['queued', 'downloading', 'converting', 'paused'];
const RUNNING = ['queued', 'downloading', 'converting'];
const qs = new URLSearchParams(location.search);
const SURFACE = qs.get('surface') || 'window';
document.documentElement.dataset.surface = SURFACE;

const state = { mode: 'mp3', filter: 'all', search: '', settings: {}, jobs: [], stats: null, sel: new Set(), detail: null };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  return r.json();
}

// ---------- formatting ----------
const UNIT = { B: 1, KiB: 1024, MiB: 1024 ** 2, GiB: 1024 ** 3, TiB: 1024 ** 4, KB: 1e3, MB: 1e6, GB: 1e9 };
function parseSize(t) { const m = /([\d.]+)\s*([KMGT]?i?B)/.exec(t || ''); return m ? parseFloat(m[1]) * (UNIT[m[2]] || 1) : 0; }
function bytes(n, dp) {
  if (!n) return '0 MB';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return `${n >= 100 || i === 0 ? n.toFixed(0) : n.toFixed(dp ?? 1)} ${u[i]}`;
}
function rate(bps) {
  if (!bps) return '—';
  const u = ['B/s', 'KB/s', 'MB/s', 'GB/s']; let i = 0;
  while (bps >= 1000 && i < u.length - 1) { bps /= 1000; i++; }
  return `${bps >= 100 ? bps.toFixed(0) : bps.toFixed(1)} ${u[i]}`;
}
function eta(t) {
  if (!t || t === 'Unknown') return '—';
  if (/^\d+:\d+(:\d+)?$/.test(t)) { const p = t.split(':').map(Number); const s = p.reduce((a, b) => a * 60 + b, 0); return s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`; }
  return t.replace(/(\d+)([hms])/g, '$1$2 ').trim();
}
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; } };
const shortUrl = (u) => (u || '').replace(/^https?:\/\/(www\.)?/, '');
const tildify = (p) => (p || '').replace(/^\/Users\/[^/]+/, '~');
const cleanErr = (e) => (e || 'Download failed').replace(/^\[[^\]]+\]\s*[^:]+:\s*/, '');

// ---------- job helpers ----------
const DOC = /\.(pdf|epub|docx?|xlsx?|pptx?|csv|txt|rtf|pages|key|numbers)$/i;
const APPX = /\.(dmg|pkg|exe|msi|apk|ipa|deb|rpm|appimage|zip|rar|7z|tar|gz|tgz|bz2|xz|iso|img|jar)$/i;
function kind(j) {
  if (j.mode === 'mp3') return 'music';
  if (j.mode === 'video') return 'video';
  if (j.mode === 'torrent') return 'torrent';
  const n = j.file || j.title || j.url;
  return DOC.test(n) ? 'docs' : APPX.test(n) ? 'apps' : 'other';
}
const EMOJI = { music: '🎵', video: '🎬', docs: '📄', apps: '📦', other: '🗂️', torrent: '🧲' };
function totalBytes(j) { return j.status === 'done' && j.bytes ? j.bytes : parseSize(j.total) || j.est_bytes || 0; }
function jobSpeed(j) { return j.status === 'downloading' ? parseSize((j.speed || '').replace('/s', '')) : 0; }
function fileIcon(j, cls = 'fi') {
  const k = kind(j);
  if (j.thumbnail) return `<div class="${cls}" style="background-image:url(&quot;${esc(j.thumbnail)}&quot;)">${k === 'music' ? '<span class="tag">MP3</span>' : ''}</div>`;
  if (j.status === 'queued' && j.title === j.url) return `<div class="${cls}"><div class="spin"></div></div>`;
  return `<div class="${cls}">${EMOJI[k]}</div>`;
}
function badge(j) {
  const m = {
    downloading: ['dl', 'Active', true], converting: ['dl', j.mode === 'mp3' ? 'Converting' : 'Merging', true],
    queued: ['q', 'Queued'], paused: ['pz', 'Paused'], done: ['ok', 'Complete'], error: ['er', 'Failed'], cancelled: ['pz', 'Stopped'],
  }[j.status] || ['pz', j.status];
  return `<span class="bdg ${m[0]}"><span class="bdot${m[2] ? ' p' : ''}"></span>${m[1]}</span>`;
}
function typeLabel(j) {
  if (j.mode === 'mp3') return `MP3 ${j.quality || ''} kbps`;
  if (j.mode === 'video') return j.quality && j.quality !== 'best' ? `Video ${j.quality === '2160' ? '4K' : j.quality + 'p'}` : 'Video · best';
  return j.mode === 'torrent' ? 'Torrent' : 'File';
}
function matches(j) {
  const f = state.filter;
  const ok = f === 'all' || (f === 'active' && RUNNING.includes(j.status)) || (f === 'done' && j.status === 'done')
    || (f === 'paused' && j.status === 'paused') || (f === 'failed' && ['error', 'cancelled'].includes(j.status))
    || (f.startsWith('t-') && kind(j) === f.slice(2));
  if (!ok) return false;
  const q = state.search.trim().toLowerCase();
  return !q || `${j.title} ${j.url} ${j.site || ''} ${j.file || ''}`.toLowerCase().includes(q);
}

// ---------- full app: list ----------
// One line per download: name + a short status line, progress on the right. Everything else is in the details panel.
function statusLine(j) {
  const pct = Math.round(j.progress || 0), tot = totalBytes(j), sp = jobSpeed(j);
  if (j.status === 'error') return `<span class="err">${esc(cleanErr(j.error))}</span>`;
  if (j.status === 'done') return [tot ? bytes(tot) : '', host(j.url) || typeLabel(j)].filter(Boolean).map(esc).join(' · ');
  if (j.status === 'queued') return j.title === j.url ? 'Getting info…' : j.checking ? 'Checking files…' : 'Waiting in queue';
  if (j.status === 'converting') return j.mode === 'mp3' ? 'Converting to MP3…' : 'Merging video + audio…';
  if (j.status === 'paused' || j.status === 'cancelled') return `${j.status === 'paused' ? 'Paused' : 'Stopped'}${tot ? ` · ${bytes(tot * pct / 100)} of ${bytes(tot)}` : ''}`;
  if (j.checking) return 'Checking existing data…';
  return [sp ? `<span class="sp">${rate(sp)}</span>` : 'Connecting…', j.eta ? esc(eta(j.eta)) + ' left' : '',
    tot ? `${bytes(tot * pct / 100)} of ${bytes(tot)}` : '', j.mode === 'torrent' && j.peers != null ? `${j.peers} peers` : ''].filter(Boolean).join(' · ');
}
function row(j) {
  const pct = Math.round(j.progress || 0);
  const fillCls = j.status === 'paused' || j.status === 'cancelled' || j.status === 'error' ? 'pz' : '';
  const prog = j.status === 'done' ? `<div class="pc-done">${badge(j)}</div>`
    : ['error', 'cancelled'].includes(j.status) ? `<div class="pc-done">${badge(j)}</div>`
    : `<div class="pc-pct num">${j.status === 'queued' ? '' : (j.status === 'converting' ? 99 : pct) + '%'}</div><div class="trk${j.status === 'converting' || j.status === 'queued' ? ' sweep' : ''}"><div class="fill ${fillCls}" style="width:${pct}%"></div></div>`;
  const rmTitle = RUNNING.includes(j.status) ? 'Cancel' : 'Remove from list';
  return `<div class="ftr${state.sel.has(j.id) ? ' on' : ''}" data-id="${j.id}">
    <div class="ck${state.sel.has(j.id) ? ' on' : ''}" data-ck></div>
    <div class="fc" data-open>${fileIcon(j)}<div class="ft"><div class="fn">${esc(j.status === 'queued' && j.title === j.url ? shortUrl(j.url) : j.title)}</div><div class="fu num">${statusLine(j)}</div></div></div>
    <div class="pc" data-open>${prog}</div>
    <button class="rx" data-act="remove" title="${rmTitle}" aria-label="${rmTitle}"><svg class="icon" viewBox="0 0 24 24" style="stroke-width:2.2"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>`;
}

const EMPTY = `<div class="empty big"><div><img src="/mark.png" alt=""><h2>Nothing downloaded yet</h2>
  <p>Press ${IS_WIN ? 'Ctrl+V' : '⌘V'} to paste a link, drop in a magnet link, or click the Peak button on any video.</p>
  <button class="btn-grad" data-new>New download</button></div></div>`;

function renderList() {
  const jobs = state.jobs.filter(matches);
  const list = $('#list');
  if (!state.jobs.length) list.innerHTML = EMPTY;
  else if (!jobs.length) list.innerHTML = `<div class="empty"><p>${state.search ? 'No matches.' : 'Nothing here.'}</p></div>`;
  else list.innerHTML = jobs.map(row).join('');
  const sel = state.jobs.filter((j) => state.sel.has(j.id));
  $('#bulk').classList.toggle('on', sel.length > 0);
  $('#bulkN').textContent = `${sel.length} selected`;
  $('[data-bulk="resume"]').disabled = !sel.some((j) => ['paused', 'error', 'cancelled'].includes(j.status));
  $('[data-bulk="pause"]').disabled = !sel.some((j) => j.status === 'downloading');
  $('[data-bulk="delete"]').disabled = !sel.length;
  $('[data-bulk="stopall"]').disabled = !state.jobs.some((j) => RUNNING.includes(j.status));
  const tc = { 't-video': 0, 't-music': 0, 't-docs': 0, 't-apps': 0, 't-torrent': 0 };
  for (const j of state.jobs) { const k = 't-' + kind(j); if (k in tc) tc[k]++; }
  for (const [k, v] of Object.entries(tc)) $(`[data-tcount="${k}"]`).textContent = v || '';
}

function renderStats(s) {
  if (!s) return;
  for (const [k, v] of Object.entries(s.counts)) { const el = $(`[data-count="${k}"]`); if (el) el.textContent = v || ''; }
  $('#sSpeed').textContent = s.speed_bps ? rate(s.speed_bps) : '0 MB/s';
  $('#sCounts').textContent = `${s.downloading} active · ${s.queued} queued · ${s.counts.done} done`;
  $('#sSession').textContent = bytes(s.session_bytes);
  if (s.disk) {
    const used = Math.round((1 - s.disk.free / s.disk.total) * 100);
    $('#diskPct').textContent = used + '%';
    $('#diskFill').style.width = used + '%';
    $('#disk').classList.toggle('hi', used >= 90);
    $('#diskPath').textContent = `${bytes(s.disk.free, 0)} free · ${s.download_dir.split('/').pop()}`;
  }
}

function renderControls() {
  $$('[data-mode]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.mode === state.mode));
  $$('[data-filter]').forEach((b) => b.setAttribute('aria-pressed', b.dataset.filter === state.filter));
  const s = state.settings;
  $$('[data-setting]').forEach((seg) => $$('button', seg).forEach((b) => b.setAttribute('aria-pressed', b.dataset.v === String(s[seg.dataset.setting]))));
  $$('[data-folder-path]').forEach((el) => {
    const v = s[el.dataset.folderPath];
    el.innerHTML = `<span dir="ltr">${esc(v ? tildify(v) : `Same as main (${tildify(s.download_dir)})`)}</span>`;
    el.style.color = v ? '' : 'var(--t2)';
  });
  $$('[data-folder-clear]').forEach((b) => (b.style.display = s[b.dataset.folderClear] ? '' : 'none'));
  $$('[data-toggle]').forEach((b) => b.setAttribute('aria-checked', !!s[b.dataset.toggle]));
  $$('[data-num]').forEach((i) => { if (document.activeElement !== i) i.value = s[i.dataset.num] ?? ''; });
  $$('[data-text]').forEach((i) => { if (document.activeElement !== i) i.value = s[i.dataset.text] ?? ''; });
  $('#loginSwitch').setAttribute('aria-checked', SHELL && window.PEAK_LOGIN != null ? window.PEAK_LOGIN : s.start_at_login !== false);
}

// ---------- the download card (small window + details panel) ----------
const PRIO = [[0, "Don't download"], [1, 'Low'], [4, 'Normal'], [7, 'High']];
function cardStats(j) {
  const pct = Math.round(j.progress || 0), tot = totalBytes(j), sp = jobSpeed(j);
  const conns = j.status === 'downloading' ? (j.connections || 1) : 0;
  const maxC = Math.max(conns, j.mode === 'torrent' ? conns : (j.conns || state.settings.connections || 16), 1);
  const threads = Array.from({ length: Math.min(maxC, 64) }, (_, i) => `<div class="dp-thread${i < conns ? '' : ' idle'}"${i < conns ? ` style="opacity:${[.9, .6, .75, .45, .8][i % 5]}"` : ''}></div>`).join('');
  const done = j.status === 'done';
  return `<div class="dp-grid">
      <div class="dp-stat"><div class="dp-sv num ${sp ? 'gtext' : ''}">${sp ? rate(sp) : '—'}</div><div class="dp-sk">Current speed</div></div>
      <div class="dp-stat"><div class="dp-sv num">${done ? 'Done' : j.status === 'downloading' ? eta(j.eta) : '—'}</div><div class="dp-sk">Time remaining</div></div>
      <div class="dp-stat"><div class="dp-sv num">${tot ? bytes(done ? tot : tot * pct / 100) : '—'}</div><div class="dp-sk">Downloaded</div></div>
      <div class="dp-stat"><div class="dp-sv num">${conns || '—'}</div><div class="dp-sk">${j.mode === 'torrent' ? `Peers${j.seeds != null ? ` · ${j.seeds} seeds` : ''}` : 'Connections'}</div></div>
    </div>
    ${j.status === 'downloading' ? `<div class="dp-threads">${threads}</div>
    <div class="dp-thread-lbl">${j.checking ? 'Checking existing data…' : conns > 1 ? `${conns} parallel connections` : 'Single connection'}</div>` : ''}
    ${j.status === 'error' ? `<div class="dp-err">${esc(cleanErr(j.error))}</div>` : ''}
    <div class="dp-prog-lbl num"><span>${done ? 100 : pct}% complete</span><span>${tot ? (done ? bytes(tot) : `${bytes(tot * pct / 100)} / ${bytes(tot)}`) : ''}</span></div>
    <div class="trk${j.status === 'converting' ? ' sweep' : ''}"><div class="fill${done ? ' ok' : ''}" style="width:${done ? 100 : pct}%"></div></div>`;
}
function cardButtons(j) {
  const btn = {
    downloading: `<button class="btn-ghost" data-act="pause">Pause</button><button class="btn-ghost" data-act="remove">Stop</button>`,
    queued: `<button class="btn-ghost" data-act="remove">Cancel</button>`,
    converting: `<button class="btn-ghost" data-act="remove">Cancel</button>`,
    paused: `<button class="btn-grad" data-act="resume">Resume</button><button class="btn-ghost" data-act="remove">Remove</button>`,
    done: `<button class="btn-ghost" data-act="open">Open</button><button class="btn-ghost" data-act="remove">Remove</button>`,
    error: `<button class="btn-grad" data-act="retry">Retry</button><button class="btn-ghost" data-act="remove">Remove</button>`,
    cancelled: `<button class="btn-grad" data-act="retry">Retry</button><button class="btn-ghost" data-act="remove">Remove</button>`,
  }[j.status] || '';
  return `<div class="dp-actions">${btn}${j.file ? `<button class="btn-grad" data-act="reveal">Open folder</button>` : ''}</div>`;
}
function subtitle(j) {
  if (j.mode === 'torrent') return ['Torrent', j.files ? `${j.files.length} file${j.files.length > 1 ? 's' : ''}` : '', j.seeds != null && j.status === 'downloading' ? `${j.seeds} seeds` : ''].filter(Boolean).join(' · ');
  return [host(j.url), typeLabel(j)].filter(Boolean).join(' · ');
}

// uTorrent-style file list: what's done, what's downloading, and a priority per file.
function fileTable(j) {
  const fs = j.files || [];
  if (!fs.length) return '';
  const want = fs.filter((f) => f.prio).reduce((a, f) => a + f.size, 0);
  const rows = fs.map((f, i) => {
    const pct = f.size ? Math.min(100, Math.floor((f.done || 0) * 100 / f.size)) : 100;
    const parts = f.path.split('/'), nm = parts.pop();
    const st = !f.prio ? 'Skipped' : pct >= 100 ? 'Done' : `${pct}%`;
    return `<div class="file${f.prio ? '' : ' skip'}" data-frow="${i}">
      <div class="ck${f.prio ? ' on' : ''}" data-fck="${i}" title="Download this file"></div>
      <div class="nm" title="${esc(f.path)}">${esc(nm)}${parts.length > 1 ? ` <small>${esc(parts.slice(1).join('/'))}</small>` : ''}</div>
      <div class="sz num">${bytes(f.size)}</div>
      <div class="pr num"><div class="trk"><div class="fill${pct >= 100 ? ' ok' : ''}" style="width:${f.prio ? pct : 0}%"></div></div><span style="width:44px;text-align:right">${st}</span></div>
      <select data-fprio="${i}" aria-label="Priority">${PRIO.map(([v, l]) => `<option value="${v}"${Number(f.prio) === v || (v === 4 && ![0, 1, 7].includes(Number(f.prio))) ? ' selected' : ''}>${l}</option>`).join('')}</select>
    </div>`;
  }).join('');
  const nDone = fs.filter((f) => f.prio && f.done >= f.size).length;
  return `<div class="files"><div class="files-hd"><div></div><div>Files</div><div style="text-align:right">Size</div><div>Progress</div><div>Priority</div></div>
    <div class="files-bd">${rows}</div>
    <div class="files-ft"><span class="num">${fs.filter((f) => f.prio).length} of ${fs.length} files · ${bytes(want)}${j.status ? ` · ${nDone} done` : ''}</span><span><button type="button" data-fall="4">All</button> · <button type="button" data-fall="0">None</button></span></div></div>`;
}
function onFileTable(root, getFiles, save) {
  root.addEventListener('click', (e) => {
    const c = e.target.closest('[data-fck]'), a = e.target.closest('[data-fall]');
    if (!c && !a) return;
    const fs = getFiles(); if (!fs) return;
    if (c) { const f = fs[+c.dataset.fck]; f.prio = f.prio ? 0 : 4; }
    else fs.forEach((f) => (f.prio = +a.dataset.fall));
    save(fs);
  });
  root.addEventListener('change', (e) => {
    const s = e.target.closest('[data-fprio]'); if (!s) return;
    const fs = getFiles(); if (!fs) return;
    fs[+s.dataset.fprio].prio = +s.value;
    save(fs);
  });
}

// ---------- small window ----------
function currentJob() {
  const act = state.jobs.filter((j) => ACTIVE.includes(j.status))
    .sort((a, b) => (a.status === 'downloading' ? 0 : 1) - (b.status === 'downloading' ? 0 : 1) || a.created_at - b.created_at);
  return [act.find((j) => j.id === state.pin) || act[0], act.length];
}
const EXPAND_BTN = `<button class="modal-x" data-expand title="Open the full app" aria-label="Open the full app"><svg class="icon" viewBox="0 0 24 24" style="stroke-width:2.2"><path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7"/></svg></button>`;
function renderWidget() {
  const [j, n] = currentJob();
  let html;
  if (j) {
    html = `<div class="dp-hd" data-drag>${fileIcon(j, 'dp-icon')}<div style="min-width:0;flex:1"><div class="dp-filename">${esc(j.title === j.url ? shortUrl(j.url) : j.title)}</div><div class="dp-host">${esc(subtitle(j))}</div></div>
      ${n > 1 ? `<button class="w-more num" data-next title="Show the next download">1 of ${n}</button>` : ''}${EXPAND_BTN}</div>
      <div class="dp-body" data-id="${j.id}">${cardStats(j)}${cardButtons(j)}</div>`;
  } else {
    const last = state.jobs.find((x) => x.status === 'done');
    html = `<div class="dp-hd" data-drag style="border:0"><img class="mark" src="/mark.png" alt=""><div style="flex:1;font-weight:700;letter-spacing:-.3px">Peak</div>
        <button class="modal-x" data-settings title="Settings" aria-label="Settings"><svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>${EXPAND_BTN}</div>
      <div class="w-empty"><b>Nothing downloading</b><span>Paste a link or magnet here, or click the Peak button on any video.</span>
        <form class="w-paste" data-wpaste><input placeholder="Paste a link…" autocomplete="off" spellcheck="false"><button class="btn-grad sm">Add</button></form>
        ${last ? `<button class="w-last" data-last="${last.id}">${fileIcon(last)}<div><div class="fn">${esc(last.title)}</div><div class="fu">Last download · ${esc(bytes(totalBytes(last)))}</div></div></button>` : ''}
      </div>`;
  }
  const el = $('#widget');
  const focused = document.activeElement?.closest?.('[data-wpaste]');
  if (el.dataset.html !== html && !focused) { el.innerHTML = html; el.dataset.html = html; reportDrag(); }
}
$('#widget').addEventListener('click', async (e) => {
  if (e.target.closest('[data-expand]')) return setView('full');
  if (e.target.closest('[data-settings]')) return settingsEl.classList.add('open');
  if (e.target.closest('[data-next]')) {
    const act = state.jobs.filter((j) => ACTIVE.includes(j.status)); const [j] = currentJob();
    state.pin = act[(act.indexOf(j) + 1) % act.length]?.id; return renderWidget();
  }
  const l = e.target.closest('[data-last]'); if (l) return act(l.dataset.last, 'reveal');
  const b = e.target.closest('[data-act]'), id = e.target.closest('[data-id]')?.dataset.id;
  if (b && id) { await act(id, b.dataset.act); refresh(); }
});
$('#widget').addEventListener('submit', (e) => { e.preventDefault(); const i = $('input', e.target); submit(i.value); i.value = ''; });

// ---------- details panel ----------
function renderDetail() {
  const j = state.jobs.find((x) => x.id === state.detail);
  if (!j) { $('#detail').classList.remove('open'); state.detail = null; return; }
  const box = $('#detBody');
  if (box.contains(document.activeElement) && document.activeElement.tagName === 'SELECT') return;   // don't close an open menu
  box.classList.toggle('wide', !!j.files?.length);
  const sig = `${j.id}|${j.status}|${(j.files || []).map((f) => f.prio).join(',')}`;
  if (box.dataset.sig === sig) return patchDetail(box, j);      // same layout: just update numbers (keeps scroll, no lag)
  box.dataset.sig = sig;
  const folder = j.file ? j.file.replace(/[\\/][^\\/]*$/, '') : j.folder || '';
  const meta = [['Source', j.mode === 'torrent' ? (j.url.startsWith('magnet:') ? 'Magnet link' : j.url.split('/').pop()) : shortUrl(j.url)],
    ['Type', typeLabel(j)], ['Saved to', tildify(folder)], ['Added', new Date(j.created_at * 1000).toLocaleString()]]
    .filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd title="${esc(v)}">${esc(v)}</dd>`).join('');
  box.innerHTML = `<div data-id="${j.id}">
    <div class="dp-hd">${fileIcon(j, 'dp-icon')}<div style="min-width:0;flex:1"><div class="dp-filename">${esc(j.title)}</div><div class="dp-host">${esc(subtitle(j))}</div></div>
      <button class="modal-x" data-close aria-label="Close"><svg class="icon" viewBox="0 0 24 24" style="stroke-width:2.4"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <div class="dp-body"><div class="dp-stats">${cardStats(j)}</div><div style="height:14px"></div>${fileTable(j)}<dl class="dp-meta">${meta}</dl>${cardButtons(j)}</div></div>`;
}
function patchDetail(box, j) {
  const stats = $('.dp-stats', box);
  if (stats) { const html = cardStats(j); if (stats.dataset.html !== html) { stats.innerHTML = html; stats.dataset.html = html; } }
  (j.files || []).forEach((f, i) => {
    const r = box.querySelector(`[data-frow="${i}"]`); if (!r || !f.prio) return;
    const pct = f.size ? Math.min(100, Math.floor((f.done || 0) * 100 / f.size)) : 100;
    const fill = r.querySelector('.fill'), lbl = r.querySelector('.pr span');
    const txt = pct >= 100 ? 'Done' : `${pct}%`;
    if (lbl.textContent !== txt) { lbl.textContent = txt; fill.style.width = pct + '%'; fill.classList.toggle('ok', pct >= 100); }
  });
}
function openDetail(id) { $('#detBody').dataset.sig = ''; state.detail = id; renderDetail(); $('#detail').classList.add('open'); }
onFileTable($('#detBody'), () => state.jobs.find((x) => x.id === state.detail)?.files, async (fs) => {
  const prio = fs.map((f) => f.prio);
  if (!prio.some(Boolean)) { toast('Pick at least one file', true); return refresh(); }
  await api(`/api/jobs/${state.detail}/prio`, { method: 'POST', body: { prio } });
  lastJson = ''; refresh();
});

// ---------- actions ----------
function extractUrl(text) {
  text = (text || '').trim();
  const mag = text.match(/magnet:\?[^\s<>"']+/i);
  if (mag) return mag[0];
  const full = text.match(/https?:\/\/[^\s<>"']+/i);
  if (full) return full[0].replace(/[),.;!?]+$/, '');
  const bare = text.match(/(?:^|\s)((?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?\/[^\s<>"']*)/i)
    || text.match(/(?:^|\s)((?:www\.|m\.)?(?:youtube\.com|youtu\.be|pin\.it|pinterest\.[a-z.]+|instagram\.com|tiktok\.com|x\.com|twitter\.com|vimeo\.com|facebook\.com|reddit\.com)\S*)/i);
  return bare ? 'https://' + bare[1].replace(/[),.;!?]+$/, '') : null;
}

let toastTimer;
function toast(msg, err) {
  const t = $('#toast');
  t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}

async function startDownload(body) {
  const r = await fetch('/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'Could not start download');
  return j;
}

// Every pasted link opens the New download box first: size, folder and (for torrents) the files, before anything starts.
async function submit(text) {
  if (!(text || '').trim()) return;
  const url = extractUrl(text);
  if (!url) return toast("That doesn't look like a link", true);
  if (document.body.dataset.view === 'compact' && SURFACE === 'window') await setView('full');
  openAdd(url);
}
// A magnet/.torrent opened from the browser or Finder waits in the backend until this window shows it.
let taking = false;
async function takePrompt() {
  if (taking || $('#addModal').classList.contains('open')) return;
  taking = true;
  try { const r = await api('/api/prompt/take', { method: 'POST' }); if (r.url) { if (document.body.dataset.view === 'compact') await setView('full'); openAdd(r.url, r); } }
  finally { taking = false; }
}

$$('[data-form]').forEach((f) => f.addEventListener('submit', (e) => { e.preventDefault(); submit($('[data-url]', f).value); }));
$$('[data-url]').forEach((i) => i.addEventListener('keydown', (e) => {
  if ((e.key === 'Enter' || e.keyCode === 13) && !e.isComposing) { e.preventDefault(); submit(i.value); }
}));
$$('[data-url]').forEach((i) => i.addEventListener('paste', (e) => {
  const text = e.clipboardData?.getData('text');
  if (text && extractUrl(text)) { e.preventDefault(); e.stopPropagation(); i.value = extractUrl(text); setTimeout(() => submit(i.value), 200); }
}));
// ⌘V anywhere (not in a text field) downloads the link on the clipboard.
document.addEventListener('paste', (e) => {
  if (e.target.closest?.('input, textarea') || $('.scrim.open')) return;
  const text = e.clipboardData?.getData('text');
  if (text) { e.preventDefault(); submit(text); }
});
$$('[data-mode]').forEach((b) => b.addEventListener('click', () => { state.mode = b.dataset.mode; try { localStorage.setItem('peak-mode', state.mode); } catch {} renderControls(); }));
try { const m = localStorage.getItem('peak-mode'); if (m === 'mp3' || m === 'video') state.mode = m; } catch {}
$$('[data-filter]').forEach((b) => b.addEventListener('click', () => { state.filter = b.dataset.filter; state.sel.clear(); renderControls(); renderList(); }));
$('#search').addEventListener('input', (e) => { state.search = e.target.value; renderList(); });

async function act(id, a) {
  if (a === 'remove') await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
  else await fetch(`/api/jobs/${id}/${a}`, { method: 'POST' });
}
$('#list').addEventListener('click', async (e) => {
  if (e.target.closest('[data-new]')) return openAdd();
  const r = e.target.closest('[data-id]'); if (!r) return;
  const id = r.dataset.id;
  const b = e.target.closest('[data-act]');
  if (b) { e.stopPropagation(); await act(id, b.dataset.act); state.sel.delete(id); return refresh(); }
  if (e.target.closest('[data-open]') && !e.metaKey && !e.ctrlKey && !e.shiftKey) return openDetail(id);
  if (!(e.metaKey || e.ctrlKey || e.target.closest('[data-ck]'))) state.sel.clear();
  state.sel.has(id) ? state.sel.delete(id) : state.sel.add(id);
  renderList();
});
$('#list').addEventListener('dblclick', (e) => {
  const r = e.target.closest('[data-id]'); if (!r || e.target.closest('[data-act],[data-ck]')) return;
  const j = state.jobs.find((x) => x.id === r.dataset.id);
  if (j?.status === 'done') act(j.id, 'open');
});
$$('[data-bulk]').forEach((b) => b.addEventListener('click', async () => {
  const k = b.dataset.bulk;
  const sel = state.jobs.filter((j) => state.sel.has(j.id));
  if (k === 'stopall') { for (const j of state.jobs.filter((j) => j.status === 'downloading')) await act(j.id, 'pause'); for (const j of state.jobs.filter((j) => j.status === 'queued')) await act(j.id, 'remove'); toast('Stopped all downloads'); }
  if (k === 'pause') for (const j of sel.filter((j) => j.status === 'downloading')) await act(j.id, 'pause');
  if (k === 'resume') for (const j of sel) { if (j.status === 'paused') await act(j.id, 'resume'); else if (['error', 'cancelled'].includes(j.status)) await act(j.id, 'retry'); }
  if (k === 'delete') { for (const j of sel) await act(j.id, 'remove'); state.sel.clear(); }
  refresh();
}));
$('#detail').addEventListener('click', async (e) => {
  if (e.target === $('#detail') || e.target.closest('[data-close]')) { $('#detail').classList.remove('open'); state.detail = null; return; }
  const b = e.target.closest('[data-act]'); if (!b) return;
  await act(state.detail, b.dataset.act);
  if (b.dataset.act === 'remove') { $('#detail').classList.remove('open'); state.detail = null; }
  refresh();
});
$('#disk').addEventListener('click', () => fetch('/api/open-folder', { method: 'POST' }));

// ---------- new download box ----------
const add = { folder: '', tab: 'url', probe: null, probeUrl: '', meta: null };
function addType() { return $('#aType').value; }
function defaultFolder(type) {
  const s = state.settings;
  if (add.meta || add.tab === 'torrent') return s.torrent_dir || s.download_dir;
  return type === 'mp3' ? s.audio_dir || s.download_dir : type === 'video' ? s.video_dir || s.download_dir : s.download_dir;
}
function fillQuality() {
  const t = addType();
  const sel = $('#aQuality'), f = add.probe;
  if (f?.file) { sel.innerHTML = '<option value="">Original file</option>'; sel.disabled = true; return renderSize(); }
  sel.disabled = false;
  if (t === 'mp3' && add.wantQuality) state.settings = { ...state.settings, audio_quality: add.wantQuality };
  if (t === 'mp3') sel.innerHTML = ['320', '256', '192', '128'].map((q) => `<option value="${q}"${q === String(state.settings.audio_quality) ? ' selected' : ''}>${q} kbps${f?.mp3_size?.[q] ? ' · ' + bytes(f.mp3_size[q]) : ''}</option>`).join('');
  else {
    const hs = f?.video?.length ? f.video.map((v) => [String(v.height || 'best'), v.label + (v.size ? ` · ${bytes(v.size)}` : '')]) : [['best', 'Best'], ['2160', '4K'], ['1440', '1440p'], ['1080', '1080p'], ['720', '720p'], ['480', '480p']];
    sel.innerHTML = hs.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('');
    const want = add.wantQuality || String(state.settings.video_quality || 'best');
    if ([...sel.options].some((o) => o.value === want)) sel.value = want;
  }
  renderSize();
}
// "Size · free space" line, so you know before you start.
function renderSize() {
  const f = add.probe, free = state.stats?.disk?.free;
  let size = null;
  if (add.meta?.files) size = add.meta.files.filter((x) => x.prio).reduce((a, x) => a + x.size, 0);
  else if (f?.file) size = f.size;
  else if (f && addType() === 'mp3') size = f.mp3_size?.[$('#aQuality').value];
  else if (f?.video?.length) size = (f.video.find((v) => String(v.height) === $('#aQuality').value) || f.video[0]).size;
  const parts = [];
  if (size) parts.push(`Size <b class="num">${bytes(size)}</b>`);
  else if (f || add.meta) parts.push('Size <b>unknown until it starts</b>');
  if (free) parts.push(`<span class="num" style="${size && size > free ? 'color:var(--err)' : ''}">${bytes(free, 0)} free on disk</span>`);
  $('#aSize').innerHTML = parts.join('<span>·</span>');
}
function renderAddFolder() { $('#aFolder span').textContent = tildify(add.folder || defaultFolder(addType())); }
function renderFiles() {
  const on = !!add.meta?.files;
  $('#addModal .modal').classList.toggle('wide', on);
  $('#aFiles').innerHTML = on ? fileTable({ files: add.meta.files }) : '';
  $('.m-grid3').hidden = on || add.tab === 'torrent';
  $('#aName').parentElement.hidden = on || add.tab === 'torrent';
  renderSize(); renderAddFolder();
}
onFileTable($('#aFiles'), () => add.meta?.files, () => renderFiles());
function openAdd(prefill, opts = {}) {
  add.folder = ''; add.probe = null; add.probeUrl = ''; add.meta = null;
  $('#aUrl').value = prefill || ''; $('#aBatch').value = ''; $('#aName').value = ''; $('#aConn').value = '';
  try { const m = opts.mode || localStorage.getItem('peak-mode'); $('#aType').value = m === 'mp3' ? 'mp3' : 'video'; } catch {}
  add.wantQuality = opts.quality ? String(opts.quality) : null;     // picked on the video's Peak button
  setAddTab('url'); fillQuality(); renderFiles();
  $('#addModal').classList.add('open');
  setTimeout(() => $('#aUrl').focus(), 30);
  if (prefill) probe();
}
function setAddTab(t) {
  add.tab = t; add.meta = null; add.probeUrl = '';
  $$('[data-atab]').forEach((b) => b.setAttribute('aria-selected', b.dataset.atab === t));
  $('#aUrl').hidden = t === 'batch'; $('#aBatch').hidden = t !== 'batch';
  $('#aTorrent').hidden = t !== 'torrent';
  $('#aUrl').placeholder = t === 'torrent' ? 'Paste a magnet link…' : 'Paste a link';
  $('#aTorrentName').textContent = 'Choose a .torrent file';
  $('#aName').disabled = t === 'batch'; $('#aName').placeholder = t === 'batch' ? '—' : 'filename';
  $('#aProbe').innerHTML = ''; $('#aSize').innerHTML = '';
  renderFiles();
  (t === 'batch' ? $('#aBatch') : $('#aUrl')).focus();
}
async function probeTorrent(body, label) {
  add.meta = null; renderFiles();
  $('#aProbe').innerHTML = `<div class="spin"></div>${label}`;
  const key = add.probeUrl;
  const m = await api('/api/torrent/meta', { method: 'POST', body }).catch(() => ({ error: "Can't reach Peak" }));
  if (add.probeUrl !== key) return;
  if (m.error) { $('#aProbe').innerHTML = `<span style="color:var(--err)">${esc(m.error)}</span>`; return; }
  add.meta = m.files ? { ...m, files: m.files.map((f) => ({ ...f, prio: 4 })) } : null;
  $('#aProbe').innerHTML = `🧲 <b style="color:var(--t1);font-weight:600">${esc(m.name || 'Torrent')}</b>${m.files ? ` · ${m.files.length} file${m.files.length > 1 ? 's' : ''}` : ''}`;
  renderFiles();
}
let probeTimer;
async function probe() {
  const url = extractUrl($('#aUrl').value);
  if (!url || url === add.probeUrl) return;
  add.probeUrl = url; add.probe = null; add.meta = null;
  if (/^magnet:|\.torrent($|\?)/i.test(url)) return probeTorrent({ url }, url.startsWith('magnet:') ? 'Finding peers and reading the file list… (can take up to a minute)' : 'Reading the torrent…');
  renderFiles();
  $('#aProbe').innerHTML = `<div class="spin"></div>Checking link…`;
  const f = await api(`/api/formats?url=${encodeURIComponent(url)}`).catch(() => ({ error: "Can't reach Peak" }));
  if (add.probeUrl !== url) return;
  add.probe = f.error ? null : f;
  $('#aProbe').innerHTML = f.error ? `<span style="color:var(--err)">${esc(f.error)}</span>`
    : f.file ? `📦 ${esc(f.title)} · up to ${state.settings.connections || 16} connections`
    : `${EMOJI[f.video?.length ? 'video' : 'music']} ${esc(f.title || '')}${f.video?.[0] ? ` · up to ${esc(f.video[0].label)}` : ''}`;
  fillQuality();
}
$('#addBtn').addEventListener('click', () => openAdd());
$$('[data-atab]').forEach((b) => b.addEventListener('click', () => setAddTab(b.dataset.atab)));
$('#aUrl').addEventListener('input', () => { clearTimeout(probeTimer); probeTimer = setTimeout(probe, 350); });
$('#aUrl').addEventListener('paste', () => setTimeout(probe, 0));
$('#aType').addEventListener('change', () => { try { localStorage.setItem('peak-mode', addType()); } catch {} fillQuality(); renderAddFolder(); });
$('#aQuality').addEventListener('change', renderSize);
$('#aAdv').addEventListener('click', () => { $('#addModal').classList.remove('open'); settingsEl.classList.add('open'); showTab('quality'); });
$('#aFolder').addEventListener('click', async () => {
  if (SHELL) return shell({ type: 'pickFolder', key: '__add' });
  const r = await api('/api/settings/pick-folder', { method: 'POST', body: { key: '__pick' } });
  if (r.path) { add.folder = r.path; renderAddFolder(); }
});
$('#addModal').addEventListener('click', (e) => { if (e.target === $('#addModal') || e.target.closest('[data-close]')) $('#addModal').classList.remove('open'); });
$('#aTorrentBtn').addEventListener('click', () => $('#aTorrentFile').click());
$('#aTorrentFile').addEventListener('change', () => {
  const f = $('#aTorrentFile').files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => { $('#aTorrentName').textContent = f.name; add.probeUrl = 'file:' + f.name; probeTorrent({ name: f.name, data: r.result.split(',')[1] }, 'Reading the torrent…'); };
  r.readAsDataURL(f);
});
$('#addForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (add.meta) {
    const prio = add.meta.files ? add.meta.files.map((f) => f.prio) : undefined;
    if (prio && !prio.some(Boolean)) return toast('Pick at least one file', true);
    try {
      await startDownload({ url: add.meta.src, torrent: add.meta.torrent, prio, mode: 'torrent', folder: add.folder || undefined, force: true });
      $('#addModal').classList.remove('open'); toast('Torrent started'); refresh();
    } catch (x) { toast(x.message, true); }
    return;
  }
  const type = addType();
  const urls = add.tab === 'batch'
    ? [...new Set($('#aBatch').value.split(/\n+/).map(extractUrl).filter(Boolean))]
    : [extractUrl($('#aUrl').value)].filter(Boolean);
  if (!urls.length) return toast(add.tab === 'batch' ? 'No links found' : "That doesn't look like a link", true);
  const base = { mode: type, quality: $('#aQuality').disabled ? undefined : $('#aQuality').value,
    folder: add.folder || undefined, connections: $('#aConn').value || undefined, force: true };
  let ok = 0, err = '';
  for (const url of urls) {
    try { await startDownload({ ...base, url, filename: add.tab === 'url' ? $('#aName').value.trim() || undefined : undefined }); ok++; }
    catch (x) { err = x.message; }
  }
  if (ok) { $('#addModal').classList.remove('open'); toast(ok > 1 ? `Added ${ok} downloads` : 'Download started'); refresh(); }
  if (err) toast(err, true);
});

// ---------- settings ----------
const settingsEl = $('#settings');
$('#openSettings').onclick = () => settingsEl.classList.add('open');
$('#closeSettings').onclick = () => settingsEl.classList.remove('open');
settingsEl.addEventListener('click', (e) => { if (e.target === settingsEl) settingsEl.classList.remove('open'); });
addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { $$('.scrim.open').forEach((m) => m.classList.remove('open')); state.detail = null; }
  if ((e.metaKey || e.ctrlKey) && e.key === 'n' && document.body.dataset.view === 'full') { e.preventDefault(); openAdd(); }
  if ((e.metaKey || e.ctrlKey) && e.key === 'f' && document.body.dataset.view === 'full') { e.preventDefault(); $('#search').focus(); }
  if ((e.key === 'Backspace' || e.key === 'Delete') && !e.target.closest('input,textarea') && state.sel.size && !$('.scrim.open')) $('[data-bulk="delete"]').click();
});
async function saveSettings(patch) { state.settings = await api('/api/settings', { method: 'POST', body: patch }); renderControls(); }
$$('[data-setting]').forEach((seg) => $$('button', seg).forEach((b) => b.addEventListener('click', () => saveSettings({ [seg.dataset.setting]: b.dataset.v }))));
$$('[data-toggle]').forEach((b) => b.addEventListener('click', () => saveSettings({ [b.dataset.toggle]: !state.settings[b.dataset.toggle] })));
$$('[data-num]').forEach((i) => i.addEventListener('change', () => saveSettings({ [i.dataset.num]: i.value })));
$$('[data-text]').forEach((i) => i.addEventListener('change', () => saveSettings({ [i.dataset.text]: i.value.trim() })));
$$('[data-folder]').forEach((b) => b.addEventListener('click', async () => { if (SHELL) return shell({ type: 'pickFolder', key: b.dataset.folder }); state.settings = await api('/api/settings/pick-folder', { method: 'POST', body: { key: b.dataset.folder } }); renderControls(); }));
$$('[data-folder-clear]').forEach((b) => b.addEventListener('click', () => saveSettings({ [b.dataset.folderClear]: '' })));
function showTab(t) {
  $$('.tabs [data-tab]').forEach((b) => b.setAttribute('aria-selected', b.dataset.tab === t));
  $$('[data-pane]').forEach((p) => p.classList.toggle('on', p.dataset.pane === t));
}
$$('.tabs [data-tab]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
showTab('save');

// ---------- Mac window dragging ----------
// The page covers the whole window, so the Mac app asks where the draggable strips are.
function reportDrag() {
  if (!SHELL || SURFACE !== 'window' || qs.get('shell') === 'win') return;
  requestAnimationFrame(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; };
    const areas = $$('[data-drag]').filter((el) => el.offsetParent);
    shell({ type: 'drag', drag: areas.map(r), nodrag: areas.flatMap((a) => $$('button, input, select, label, a, .sbx', a).map(r)) });
  });
}
addEventListener('resize', reportDrag);

// ---------- Mac app / extension bridge ----------
const EXT = qs.get('ext');
function shell(msg) { if (SHELL) SHELL.postMessage(msg); }
if (SHELL) document.documentElement.dataset.shell = qs.get('shell') === 'win' ? 'win' : 'mac';
$('#installExt').onclick = async () => {
  const r = await api('/api/extension/install', { method: 'POST' });
  toast(r.error || 'Chrome opened. Turn on Developer mode → Load unpacked → pick the folder shown', !!r.error);
};
$('#loginSwitch').onclick = () => {
  if (SHELL) return shell({ type: 'login', on: $('#loginSwitch').getAttribute('aria-checked') !== 'true' });
  saveSettings({ start_at_login: state.settings.start_at_login === false });
};
window.peakShell = {
  folderPicked: async (key, path) => { if (key === '__add') { add.folder = path; renderAddFolder(); } else await saveSettings({ [key]: path }); },
  login: (on) => { window.PEAK_LOGIN = on; $('#loginSwitch').setAttribute('aria-checked', on); },
  setView: (v) => { document.body.dataset.view = v; renderWidget(); reportDrag(); },
};

async function setView(v) {
  document.body.dataset.view = v;
  try { localStorage.setItem('peak-view', v); } catch {}
  if (SHELL) shell({ type: 'resize', size: v });
  else if (EXT && window.chrome?.runtime?.sendMessage) { try { await chrome.runtime.sendMessage(EXT, { type: 'resize', size: v }); } catch {} }
  else { try { v === 'full' ? resizeTo(1080, 680) : resizeTo(380, 350 + (outerHeight - innerHeight)); } catch {} }
  reportDrag();
}
$('#collapse').onclick = () => setView('compact');
if (['full', 'compact'].includes(qs.get('view'))) document.body.dataset.view = qs.get('view');
else if (SURFACE !== 'popover') { try { const v = localStorage.getItem('peak-view'); if (v === 'full' || v === 'compact') document.body.dataset.view = v; } catch {} }

// ---------- polling ----------
let lastJson = '';
async function refresh() {
  try {
    const [jobs, stats] = await Promise.all([api('/api/jobs'), api('/api/stats')]);
    $('#offline').classList.remove('show');
    state.stats = stats;
    const json = JSON.stringify(jobs);
    if (json !== lastJson) {
      lastJson = json; state.jobs = jobs;
      for (const id of state.sel) if (!jobs.some((j) => j.id === id)) state.sel.delete(id);
      renderList();
      if (state.detail) renderDetail();
    }
    renderStats(stats);
    renderWidget();
    if (stats.prompts && SURFACE === 'window') takePrompt();
  } catch { $('#offline').classList.add('show'); }
}

(async () => {
  if (qs.get('mode')) state.mode = qs.get('mode');
  try { state.settings = await api('/api/settings'); } catch {}
  renderControls(); renderList(); refresh();
  setInterval(refresh, 800);
})();
