/* Eris CCTV - shared panel code: API calls, DOM helpers, dialogs, layout shell, live player. */
"use strict";

const ICONS = {
  live: '<path d="M3 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="m16 10 5-3v10l-5-3"/>',
  camera: '<circle cx="12" cy="11" r="6"/><circle cx="12" cy="11" r="2.2"/><path d="M8 19h8"/><path d="M12 17v2"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  network: '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M12 7v5M12 12l-6 5M12 12l6 5"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  logs: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6.5 6.5 0 0 1 3.5 6"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 9.2-9.2M17 6l3 3M14 9l2 2"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  sound: '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/>',
  mute: '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
  full: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  snap: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  expand: '<path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  play: '<path d="m6 4 14 8-14 8z"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  trash: '<path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
};

function icon(name, cls) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "1.8");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  if (cls) s.setAttribute("class", cls);
  s.innerHTML = ICONS[name] || "";
  return s;
}

/* h("div.card#id", {attrs, on:{click}}, children...) */
function h(sel, attrs, ...kids) {
  const m = sel.match(/^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i) || [];
  const el = document.createElement(m[1] || "div");
  (m[2] || "").replace(/([.#])([\w-]+)/g, (_, t, v) => {
    if (t === ".") el.classList.add(v); else el.id = v;
  });
  if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    kids.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "style" && typeof v === "object") {
      for (const [sk, sv] of Object.entries(v)) {
        if (sk.startsWith("--")) el.style.setProperty(sk, String(sv)); else el.style[sk] = sv;
      }
    }
    else if (k === "class") el.className += " " + v;
    else if (k in el && k !== "list" && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, kids);
  return el;
}

function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

/* ---------- formatting ---------- */
function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 2 : 1) : n.toFixed(0)) + " " + u[i];
}
function fmtRate(bps) {
  if (!bps) return "0 kbps";
  return bps >= 1e6 ? (bps / 1e6).toFixed(2) + " Mbps" : Math.round(bps / 1000) + " kbps";
}
function fmtDuration(s) {
  s = Math.max(0, Math.round(s || 0));
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), mm = Math.floor(s % 3600 / 60), ss = s % 60;
  if (d) return d + "d " + hh + "h";
  if (hh) return hh + "h " + mm + "m";
  if (mm) return mm + "m " + ss + "s";
  return ss + "s";
}
function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  return fmtDuration(s) + " ago";
}
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "-"; }

/* ---------- API ---------- */
class ApiError extends Error {
  constructor(status, msg) { super(msg); this.status = status; }
}

async function api(method, path, body) {
  const opt = { method, headers: { "X-Requested-With": "eris-cctv" }, credentials: "same-origin" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    throw new ApiError(0, "Cannot reach the panel - check the connection");
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) {
    if (r.status === 401 && path !== "/api/login" && window.App) App.showLogin();
    throw new ApiError(r.status, (data && data.error) || ("Request failed (" + r.status + ")"));
  }
  return data;
}

/* ---------- toasts / dialogs ---------- */
function toast(msg, err) {
  let box = document.querySelector(".toasts");
  if (!box) box = document.body.appendChild(h("div.toasts"));
  const t = h("div.toast" + (err ? ".err" : ""), msg);
  box.appendChild(t);
  setTimeout(() => t.remove(), err ? 6500 : 3200);
}

function modal({ title, body, actions, wide, onClose }) {
  const back = h("div.modal-back");
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); if (onClose) onClose(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", esc);
  const foot = h("div.modal-foot");
  const m = h("div.modal" + (wide ? ".wide" : ""),
    h("div.modal-head", h("h2", title),
      h("button.btn.ghost.icon", { title: "Close", on: { click: close } }, icon("x"))),
    h("div.modal-body", body), foot);
  for (const a of actions || []) foot.appendChild(a);
  back.appendChild(m);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  document.body.appendChild(back);
  const first = m.querySelector("input:not([type=checkbox]):not([type=radio]), select");
  if (first) setTimeout(() => first.focus(), 30);
  return { close, el: m, body: m.querySelector(".modal-body"), foot };
}

function confirmBox(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    let done = false;
    const ok = h("button.btn" + (danger ? ".danger" : ".primary"), okLabel || "OK");
    const cancel = h("button.btn", "Cancel");
    const m = modal({ title, body: h("p.muted", { style: { margin: 0 } }, text), actions: [cancel, ok],
      onClose: () => { if (!done) resolve(false); } });
    ok.onclick = () => { done = true; m.close(); resolve(true); };
    cancel.onclick = () => m.close();
  });
}

/* run an async action with a busy button and error toast */
async function busy(btn, fn) {
  const old = btn ? btn.disabled : false;
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message || String(e), true); return undefined; }
  finally { if (btn) btn.disabled = old; }
}

/* ---------- form helpers ---------- */
function field(label, input, hint) {
  return h("label.field", h("span", label), input, hint ? h("div.hint", hint) : null);
}
function input(type, value, attrs) {
  return h("input", Object.assign({ type, value: value === undefined || value === null ? "" : String(value) }, attrs || {}));
}
function select(options, value, attrs) {
  return setOptions(h("select", attrs || {}), options, value);
}
function setOptions(s, options, value) {
  clear(s);
  for (const o of options) {
    const [v, label] = Array.isArray(o) ? o : [o, o];
    const opt = h("option", { value: String(v) }, label);
    if (String(v) === String(value)) opt.selected = true;
    s.appendChild(opt);
  }
  return s;
}
function checkbox(label, checked, attrs) {
  const c = h("input", Object.assign({ type: "checkbox" }, attrs || {}));
  c.checked = !!checked;
  return { el: h("label.check", c, h("span", label)), input: c };
}
function toggle(checked, onChange) {
  const c = h("input", { type: "checkbox" });
  c.checked = !!checked;
  if (onChange) c.addEventListener("change", () => onChange(c.checked, c));
  return { el: h("label.switch", c, h("i")), input: c };
}
function copyText(text) {
  const done = () => toast("Copied: " + text);
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, () => {});
  else {
    const t = h("textarea", { style: { position: "fixed", opacity: "0" } }, text);
    document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
    t.remove();
  }
}
function copyable(text) {
  return h("span.copy.mono", { title: "Click to copy", on: { click: () => copyText(text) } }, text);
}
function stateBadge(state, label) {
  const map = { connected: "ok", running: "ok", ok: "ok", online: "ok", starting: "info", connecting: "info",
    error: "err", offline: "plain", disabled: "plain", stopped: "plain" };
  return h("span.badge." + (map[state] || "plain"), label || (state ? state[0].toUpperCase() + state.slice(1) : "-"));
}

/* ---------- live player (MSE over WebSocket) ---------- */
class LivePlayer {
  constructor(video, url, onMeta) {
    this.video = video;
    this.url = url;
    this.onMeta = onMeta || (() => {});
    this.meta = {};
    this.queue = [];
    this.stopped = true;
  }
  start() {
    this.stopped = false;
    this.connect();
    this.timer = setInterval(() => this.tick(), 500);
  }
  stop() {
    this.stopped = true;
    clearInterval(this.timer);
    clearTimeout(this.retry);
    if (this.ws) { this.ws.onclose = null; try { this.ws.close(); } catch (e) { /* ignore */ } }
    this.teardown();
  }
  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (e) => this.message(e.data);
    ws.onclose = () => {
      if (this.stopped) return;
      this.teardown();
      this.onMeta(Object.assign({}, this.meta, { online: false, reconnecting: true }));
      this.retry = setTimeout(() => this.connect(), 3000);
    };
  }
  message(d) {
    if (typeof d === "string") {
      let m;
      try { m = JSON.parse(d); } catch (e) { return; }
      if (m.type === "meta") {
        this.meta = m;
        if (!m.online) this.teardown();
        this.onMeta(m);
      } else if (m.type === "error") {
        this.stopped = true;
        this.onMeta({ online: false, error: m.message, fatal: true });
      }
      return;
    }
    const u8 = new Uint8Array(d);
    const kind = u8[0];
    const data = u8.subarray(1);
    if (kind === 1) this.setup(data);
    else if (kind === 2 && this.ms) { this.queue.push(data); this.pump(); }
  }
  setup(init) {
    this.teardown();
    const MS = window.ManagedMediaSource || window.MediaSource;
    const mime = 'video/mp4; codecs="' + (this.meta.codec || "avc1.42E01E") + '"';
    if (!MS || !MS.isTypeSupported(mime)) {
      this.onMeta(Object.assign({}, this.meta, { online: false, error: "This browser cannot play " + mime }));
      return;
    }
    const ms = new MS();
    this.ms = ms;
    this.queue = [];
    this.initSeg = init;
    this.video.disableRemotePlayback = true;
    this.objectUrl = URL.createObjectURL(ms);
    this.video.src = this.objectUrl;
    ms.addEventListener("sourceopen", () => {
      if (this.ms !== ms) return;
      try {
        this.sb = ms.addSourceBuffer(mime);
        this.sb.mode = "segments";
        this.sb.addEventListener("updateend", () => this.pump());
        this.sb.addEventListener("error", () => this.restart());
        this.sb.appendBuffer(this.initSeg);
      } catch (e) {
        this.onMeta(Object.assign({}, this.meta, { online: false, error: "Player error: " + e.message }));
      }
    }, { once: true });
  }
  restart() {
    if (this.initSeg) { const i = this.initSeg; this.setup(i); }
  }
  pump() {
    const sb = this.sb;
    if (!sb || sb.updating || !this.queue.length) return;
    if (this.queue.length > 6) this.queue.splice(0, this.queue.length - 2);  /* tab was hidden: jump ahead */
    const seg = this.queue.shift();
    try {
      sb.appendBuffer(seg);
    } catch (e) {
      if (e.name === "QuotaExceededError") {
        this.queue.unshift(seg);
        const v = this.video;
        if (v.buffered.length) try { sb.remove(0, Math.max(0, v.currentTime - 2)); } catch (x) { /* ignore */ }
      } else {
        this.restart();
      }
    }
  }
  tick() {
    const v = this.video, sb = this.sb;
    if (!sb || !v.buffered.length) return;
    const n = v.buffered.length;
    const end = v.buffered.end(n - 1), lastStart = v.buffered.start(n - 1);
    if (end - v.currentTime > 3.5 || v.currentTime < lastStart - 0.05 && n > 1 || v.currentTime < v.buffered.start(0)) {
      v.currentTime = Math.max(lastStart, end - 0.8);
    }
    if (v.paused && !document.hidden) v.play().catch(() => {});
    if (!sb.updating && v.currentTime - v.buffered.start(0) > 40) {
      try { sb.remove(0, v.currentTime - 15); } catch (e) { /* ignore */ }
    }
  }
  teardown() {
    this.queue = [];
    if (this.sb) { this.sb = null; }
    if (this.ms) {
      const ms = this.ms;
      this.ms = null;
      try { if (ms.readyState === "open") ms.endOfStream(); } catch (e) { /* ignore */ }
    }
    if (this.objectUrl) {
      this.video.removeAttribute("src");
      try { this.video.load(); } catch (e) { /* ignore */ }
      URL.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
  }
}

/* A camera tile: video + status overlay + controls. opts: {title, subtitle, url, onFocus} */
function cameraTile(opts) {
  const video = h("video", { muted: true, autoplay: true, playsInline: true });
  video.muted = true;
  video.setAttribute("playsinline", "");
  const ovTitle = h("b", "Connecting...");
  const ovText = h("div.err");
  const ovSpin = h("div.spinner");
  const overlay = h("div.overlay", ovSpin, ovTitle, ovText);
  const liveBadge = h("span.badge.info", "Connecting");
  const recBadge = h("span.badge.rec.hidden", "REC");
  const sub = h("span", opts.subtitle || "");
  const soundBtn = h("button.btn.sm.icon.hidden", { title: "Listen (audio)" }, icon("mute"));
  const tile = h("div.tile", { tabindex: "0" },
    video, h("div.shade.top"), h("div.shade.bot"),
    h("div.label", h("b", opts.title), sub),
    h("div.flags", recBadge, liveBadge),
    overlay,
    h("div.ctrls",
      soundBtn,
      h("button.btn.sm.icon", { title: "Save snapshot", on: { click: (e) => { e.stopPropagation(); snapshot(video, opts.title); } } }, icon("snap")),
      opts.onFocus ? h("button.btn.sm.icon", { title: "Enlarge", on: { click: (e) => { e.stopPropagation(); opts.onFocus(tile); } } }, icon("expand")) : null,
      h("button.btn.sm.icon", { title: "Full screen", on: { click: (e) => { e.stopPropagation(); fullscreen(tile); } } }, icon("full"))));
  soundBtn.onclick = (e) => {
    e.stopPropagation();
    video.muted = !video.muted;
    clear(soundBtn).appendChild(icon(video.muted ? "mute" : "sound"));
    soundBtn.classList.toggle("on", !video.muted);
    soundBtn.title = video.muted ? "Listen (audio)" : "Mute";
    if (!video.muted) video.play().catch(() => {});
  };
  tile.addEventListener("dblclick", () => fullscreen(tile));
  const show = (title, text, spin) => {
    overlay.classList.remove("hidden");
    ovTitle.textContent = title;
    ovText.textContent = text || "";
    ovSpin.classList.toggle("hidden", !spin);
  };
  video.addEventListener("playing", () => overlay.classList.add("hidden"));
  const player = new LivePlayer(video, opts.url, (m) => {
    if (m.name) tile.querySelector(".label b").textContent = m.name;
    soundBtn.classList.toggle("hidden", !m.audio);
    if (m.online) {
      liveBadge.className = "badge ok"; liveBadge.textContent = "Live";
      if (video.paused || video.readyState < 2) show("Starting video...", "", true);
    } else {
      liveBadge.className = "badge plain"; liveBadge.textContent = m.reconnecting ? "Reconnecting" : "Offline";
      const state = m.state === "starting" ? "Camera is starting..." : (m.reconnecting ? "Reconnecting..." : "Camera offline");
      show(state, m.error || "", m.state === "starting" || m.reconnecting);
    }
  });
  show("Connecting...", "", true);
  return {
    el: tile,
    start: () => player.start(),
    stop: () => player.stop(),
    setRecording: (on) => recBadge.classList.toggle("hidden", !on),
    setSubtitle: (t) => { sub.textContent = t; },
  };
}

function fullscreen(el) {
  if (document.fullscreenElement) document.exitFullscreen();
  else if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  else if (el.querySelector("video").webkitEnterFullscreen) el.querySelector("video").webkitEnterFullscreen();
}

function snapshot(video, name) {
  if (!video.videoWidth) { toast("No picture yet", true); return; }
  const c = document.createElement("canvas");
  c.width = video.videoWidth;
  c.height = video.videoHeight;
  c.getContext("2d").drawImage(video, 0, 0);
  const a = document.createElement("a");
  const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
  a.download = (name || "camera").replace(/[^\w-]+/g, "_") + "_" + stamp + ".png";
  a.href = c.toDataURL("image/png");
  a.click();
}

/* ---------- shell: login, navigation, pages ---------- */
const App = {
  pages: {},      // id -> {title, icon, render(container), admin}
  order: [],
  cleanup: [],
  session: null,
  appName: "",
  subtitle: "",

  register(id, page) { this.pages[id] = page; this.order.push(id); },

  onLeave(fn) { this.cleanup.push(fn); },

  async boot(appName, subtitle) {
    this.appName = appName;
    this.subtitle = subtitle;
    window.addEventListener("hashchange", () => this.route());
    try {
      this.session = await api("GET", "/api/session");
    } catch (e) {
      this.session = { logged_in: false };
    }
    if (!this.session.logged_in) this.showLogin();
    else this.shell();
  },

  brand() {
    return h("div.brand", h("div.brand-mark"),
      h("div", h("div.brand-name", this.appName), h("div.brand-sub", this.subtitle)));
  },

  showLogin() {
    this.leave();
    document.title = this.appName + " - Sign in";
    const root = clear(document.getElementById("root"));
    const user = input("text", "", { autocomplete: "username", placeholder: "Username" });
    const pass = input("password", "", { autocomplete: "current-password", placeholder: "Password" });
    const err = h("div.alert.err.hidden");
    const btn = h("button.btn.primary", { type: "submit", style: { width: "100%" } }, "Sign in");
    const form = h("form.card.login.stack",
      this.brand(),
      field("Username", user), field("Password", pass), err, btn);
    form.onsubmit = async (e) => {
      e.preventDefault();
      err.classList.add("hidden");
      btn.disabled = true;
      try {
        await api("POST", "/api/login", { username: user.value.trim(), password: pass.value });
        this.session = await api("GET", "/api/session");
        this.shell();
      } catch (ex) {
        err.textContent = ex.message;
        err.classList.remove("hidden");
        pass.select();
      } finally {
        btn.disabled = false;
      }
    };
    root.appendChild(h("div.login-wrap", form));
    setTimeout(() => user.focus(), 30);
  },

  shell() {
    const root = clear(document.getElementById("root"));
    const nav = h("nav.nav");
    const side = h("aside.side", this.brand(), nav,
      h("div.side-foot",
        h("div.row.between",
          h("div", h("div.who", this.session.username), h("div", this.session.name || "")),
          h("button.btn.ghost.icon", { title: "Sign out", on: { click: () => this.logout() } }, icon("logout"))),
        h("div.faint", { style: { marginTop: "6px" } }, "v" + this.session.version)));
    for (const id of this.order) {
      const p = this.pages[id];
      if (p.admin && this.session.role && this.session.role !== "admin") continue;
      nav.appendChild(h("a", { href: "#" + id, "data-page": id, on: { click: () => side.classList.remove("open") } },
        icon(p.icon, "ico"), p.title));
    }
    this.titleEl = h("h1");
    this.actionsEl = h("div.row");
    const top = h("header.topbar",
      h("button.btn.ghost.icon.menu-btn", { on: { click: () => side.classList.toggle("open") } }, icon("menu")),
      this.titleEl, h("div.spacer"), this.actionsEl);
    this.contentEl = h("div.content");
    root.appendChild(h("div.app", side, h("main.main", top, this.contentEl)));
    this.route();
  },

  leave() {
    for (const fn of this.cleanup.splice(0)) { try { fn(); } catch (e) { /* ignore */ } }
  },

  route() {
    if (!this.contentEl || !this.session || !this.session.logged_in) return;
    let id = (location.hash || "").slice(1).split("?")[0];
    const allowed = this.order.filter((p) => !(this.pages[p].admin && this.session.role && this.session.role !== "admin"));
    if (!allowed.includes(id)) id = allowed[0];
    this.leave();
    const page = this.pages[id];
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === id));
    this.titleEl.textContent = page.title;
    document.title = page.title + " - " + this.appName;
    clear(this.actionsEl);
    clear(this.contentEl);
    this.contentEl.classList.toggle("wide", !!page.wide);
    Promise.resolve(page.render(this.contentEl, this.actionsEl)).catch((e) => {
      if (e && e.status === 401) return;
      this.contentEl.appendChild(h("div.alert.err", e.message || String(e)));
    });
  },

  refreshEvery(ms, fn) {
    let stopped = false;
    const t = setInterval(() => { if (!document.hidden && !stopped) fn(); }, ms);
    this.onLeave(() => { stopped = true; clearInterval(t); });
  },

  async logout() {
    try { await api("POST", "/api/logout", {}); } catch (e) { /* ignore */ }
    this.session = { logged_in: false };
    this.showLogin();
  },
};

/* Shared "Change password" card used by both panels */
function passwordCard(withUsername, currentUser) {
  const cur = input("password", "", { autocomplete: "current-password" });
  const user = withUsername ? input("text", currentUser || "", { autocomplete: "username" }) : null;
  const p1 = input("password", "", { autocomplete: "new-password" });
  const p2 = input("password", "", { autocomplete: "new-password" });
  const btn = h("button.btn.primary", "Change password");
  btn.onclick = () => busy(btn, async () => {
    if (p1.value !== p2.value) throw new Error("The new passwords do not match");
    const body = { current: cur.value, password: p1.value };
    if (user) body.username = user.value.trim();
    await api("POST", "/api/password", body);
    cur.value = p1.value = p2.value = "";
    toast("Password changed");
  });
  return h("div.card",
    h("div.card-head", h("h2", "Sign-in details")),
    h("div.stack",
      user ? field("Username", user) : null,
      field("Current password", cur),
      h("div.grid-2", field("New password", p1, "At least 6 characters"), field("Repeat new password", p2)),
      h("div", btn)));
}

function logPage(path) {
  return async (el, actions) => {
    const box = h("div.logbox", "Loading...");
    const auto = checkbox("Auto refresh", true);
    actions.appendChild(auto.el);
    el.appendChild(box);
    const load = async () => {
      const d = await api("GET", path);
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.textContent = (d.lines || []).join("\n") || "(empty)";
      if (atBottom) box.scrollTop = box.scrollHeight;
    };
    await load();
    box.scrollTop = box.scrollHeight;
    App.refreshEvery(3000, () => { if (auto.input.checked) load().catch(() => {}); });
  };
}
