/* Eris CCTV - camera (agent) panel */
"use strict";

const ROTATIONS = [[0, "No rotation"], [90, "90 degrees"], [180, "180 degrees (upside down)"], [270, "270 degrees"]];
const QUALITY = [["low", "Low (saves bandwidth)"], ["medium", "Medium"], ["high", "High"]];
const FPS = [5, 8, 10, 12, 15, 20, 25, 30];
let devicesCache = null;

async function loadDevices(force) {
  if (!devicesCache || force) devicesCache = await api("GET", "/api/devices");
  return devicesCache;
}

function serverState(s) {
  if (!s.enabled) return stateBadge("disabled", "Disabled");
  if (s.state === "connected") return stateBadge("connected", "Connected");
  if (s.state === "connecting") return stateBadge("connecting", "Connecting");
  return stateBadge("error", "Not connected");
}

function camState(c) {
  if (!c.enabled) return stateBadge("disabled", "Disabled");
  if (c.state === "running") return stateBadge("running", "Live");
  if (c.state === "starting") return stateBadge("starting", "Starting");
  return stateBadge("error", "Error");
}

/* ---------------- Live ---------------- */
App.register("live", {
  title: "Live view", icon: "live", wide: true,
  async render(el, actions) {
    const st = await api("GET", "/api/status");
    const cams = st.cameras.filter((c) => c.enabled);
    if (!st.cameras.length) {
      el.appendChild(h("div.card.empty", h("h3", "No cameras yet"),
        h("p", "Add the webcams connected to this computer."),
        h("a.btn.primary", { href: "#cameras" }, icon("plus"), "Add a camera")));
      return;
    }
    const stats = h("div.stats",
      stat("Cameras", cams.filter((c) => c.online).length + " / " + st.cameras.length, "online"),
      stat("Servers", st.servers.filter((s) => s.state === "connected").length + " / " + st.servers.length, "connected"),
      stat("Recording", st.storage.state === "ok" ? "On" : st.storage.enabled ? "Problem" : "Off",
        st.storage.state === "ok" ? fmtBytes(st.storage.usage.free) + " free" : ""),
      stat("System", st.agent.name, ""));
    el.appendChild(stats);
    if (st.storage.enabled && st.storage.state === "error") {
      el.appendChild(h("div.alert.err", { style: { marginBottom: "14px" } }, "Recording problem: " + st.storage.message));
    }
    const cols = cams.length <= 1 ? 1 : cams.length <= 4 ? 2 : 3;
    const grid = h("div.tiles", { style: { "--cols": cols } });
    el.appendChild(grid);
    const tiles = {};
    for (const c of cams) {
      const t = cameraTile({ title: c.name, subtitle: (c.width ? c.width + "x" + c.height + " - " : "") + c.fps + " fps",
        url: "/api/live/" + encodeURIComponent(c.id),
        onFocus: (tile) => { tile.classList.toggle("focus"); } });
      t.setRecording(c.recording);
      grid.appendChild(t.el);
      t.start();
      tiles[c.id] = t;
    }
    App.onLeave(() => Object.values(tiles).forEach((t) => t.stop()));
    App.refreshEvery(5000, async () => {
      const s = await api("GET", "/api/status");
      for (const c of s.cameras) if (tiles[c.id]) {
        const live = c.live || {};
        tiles[c.id].setRecording(c.recording);
        tiles[c.id].setSubtitle((live.width ? live.width + "x" + live.height + " - " : "") + c.fps + " fps" +
          (c.bitrate ? " - " + fmtRate(c.bitrate) : ""));
      }
    });
  },
});

function stat(k, v, small) {
  return h("div.stat", h("div.k", k), h("div.v", v, small ? h("small", " " + small) : null));
}

/* ---------------- Cameras ---------------- */
App.register("cameras", {
  title: "Cameras", icon: "camera",
  async render(el, actions) {
    const addBtn = h("button.btn.primary", icon("plus"), "Add camera");
    actions.appendChild(addBtn);
    const list = h("div.items");
    el.appendChild(list);
    const draw = async () => {
      const st = await api("GET", "/api/status");
      clear(list);
      if (!st.cameras.length) {
        list.appendChild(h("div.card.empty", h("h3", "No cameras added"),
          h("p", "Click \"Add camera\" to choose which webcams this system streams and records.")));
      }
      for (const c of st.cameras) list.appendChild(cameraItem(c, draw));
    };
    addBtn.onclick = () => cameraDialog(null, draw);
    await draw();
    App.refreshEvery(4000, draw);
  },
});

function cameraItem(c, redraw) {
  const dev = c.device || {};
  const chips = [
    h("span.chip", c.width ? c.width + "x" + c.height : "Auto size"),
    h("span.chip", c.fps + " fps"),
    h("span.chip", "Quality: " + c.quality),
    h("span.chip", c.audio ? "Microphone: " + c.audio.name : "No audio"),
    c.rotate ? h("span.chip", "Rotated " + c.rotate + "°") : null,
    h("span.chip", c.record === false ? "Not recorded" : "Recorded"),
  ];
  if (c.bitrate) chips.push(h("span.chip", fmtRate(c.bitrate)));
  const restart = h("button.btn.sm", { title: "Restart this camera" }, icon("refresh"), "Restart");
  restart.onclick = () => busy(restart, async () => { await api("POST", "/api/cameras/" + c.id + "/restart", {}); toast("Restarting " + c.name); });
  const del = h("button.btn.sm.danger", icon("trash"), "Remove");
  del.onclick = async () => {
    if (!await confirmBox("Remove camera", "Remove \"" + c.name + "\"? Existing recordings stay on the drive.", "Remove", true)) return;
    await busy(del, async () => { await api("DELETE", "/api/cameras/" + c.id); toast("Camera removed"); redraw(); });
  };
  return h("div.item",
    h("div.grow",
      h("div.title", c.name, camState(c), c.recording ? h("span.badge.rec", "REC") : null),
      h("div.sub", (dev.kind === "test" ? "Test pattern" : dev.name || "") + (dev.kind === "v4l2" ? "  (" + dev.path + ")" : "")),
      c.error && c.state !== "running" ? h("div.err-line", c.error) : null,
      c.warning ? h("div.err-line", { style: { color: "var(--warn)" } }, c.warning) : null,
      h("div.chips", chips)),
    h("div.acts",
      h("button.btn.sm", { on: { click: () => cameraDialog(c, redraw) } }, "Edit"),
      h("button.btn.sm", { on: { click: () => cameraLog(c) } }, icon("logs"), "Log"),
      restart, del));
}

async function cameraLog(c) {
  const box = h("div.logbox", "Loading...");
  const m = modal({ title: "ffmpeg log - " + c.name, body: box, wide: true, actions: [] });
  const load = async () => {
    const d = await api("GET", "/api/cameras/" + c.id + "/log");
    box.textContent = d.lines.join("\n") || "(empty)";
    box.scrollTop = box.scrollHeight;
  };
  await busy(null, load);
  const t = setInterval(() => { if (!document.body.contains(box)) clearInterval(t); else load().catch(() => {}); }, 3000);
}

async function cameraDialog(cam, redraw) {
  const isNew = !cam;
  const body = h("div.stack", h("div.muted", "Loading devices..."));
  const save = h("button.btn.primary", isNew ? "Add camera" : "Save");
  const cancel = h("button.btn", "Cancel");
  const m = modal({ title: isNew ? "Add camera" : "Edit camera - " + cam.name, body, actions: [cancel, save] });
  cancel.onclick = m.close;
  let devs;
  try { devs = await loadDevices(isNew); } catch (e) { clear(body).appendChild(h("div.alert.err", e.message)); return; }
  clear(body);

  const free = devs.video.filter((v) => !v.used_by || (!isNew && v.key === keyOf(cam.device)));
  const devSel = select(isNew ? free.map((v) => [v.key, v.name + (v.kind === "v4l2" ? "  (" + v.path.split("/").pop() + ")" : "")])
    : [[keyOf(cam.device), cam.device.kind === "test" ? "Test pattern" : cam.device.name]], isNew ? (free[0] || {}).key : keyOf(cam.device),
  { disabled: !isNew });
  const name = input("text", cam ? cam.name : "", { placeholder: "e.g. Front door", maxlength: "60" });
  const audioSel = select([], "");
  const sizeSel = select([["auto", "Automatic"]], "auto");
  const fpsSel = select(FPS.map((f) => [f, f + " fps"]), cam ? cam.fps : 15);
  const qSel = select(QUALITY, cam ? cam.quality : "medium");
  const rotSel = select(ROTATIONS, cam ? cam.rotate : 0);
  const mirror = checkbox("Mirror the picture (flip left/right)", cam && cam.mirror);
  const stamp = checkbox("Show date and time on the video", cam ? cam.timestamp !== false : true);
  const record = checkbox("Record this camera (when recording is on)", cam ? cam.record !== false : true);
  const enabled = checkbox("Camera enabled", cam ? cam.enabled !== false : true);
  const micHint = h("div.hint");

  const fillAudio = () => {
    const key = devSel.value;
    const v = devs.video.find((x) => x.key === key) || {};
    const current = cam && cam.audio ? keyOf(cam.audio) : (isNew ? v.mic || "" : "");
    const opts = [["", "No audio"]].concat(devs.audio.map((a) => [a.key, a.name + (a.key === v.mic ? "  (this webcam's microphone)" : "")]));
    if (current && !devs.audio.find((a) => a.key === current)) opts.push([current, cam.audio.name + " (not connected)"]);
    setOptions(audioSel, opts, current);
    micHint.textContent = v.mic ? "This webcam has a built-in microphone. Choose \"No audio\" to stream video only."
      : "No microphone was detected on this webcam; you can still pick another microphone.";
  };
  const fillSizes = async () => {
    const key = devSel.value;
    if (!key) return;
    let modes = [];
    try { modes = (await api("GET", "/api/devices/modes?key=" + encodeURIComponent(key))).sizes || []; } catch (e) { /* ignore */ }
    if (!isNew && cam.modes) modes = Array.from(new Set(cam.modes.map((x) => x.width + "x" + x.height)));
    const cur = cam && cam.width ? cam.width + "x" + cam.height : (modes.includes("1280x720") ? "1280x720" : modes[0] || "auto");
    const opts = [["auto", "Automatic"]].concat(modes.map((s) => [s, s]));
    if (cur !== "auto" && !modes.includes(cur)) opts.push([cur, cur]);
    setOptions(sizeSel, opts, cur);
  };
  devSel.addEventListener("change", () => { fillAudio(); fillSizes(); if (!name.value || name.dataset.auto) { name.value = (devs.video.find((x) => x.key === devSel.value) || {}).name || ""; name.dataset.auto = "1"; } });

  if (isNew && !free.length) {
    body.appendChild(h("div.alert.warn", devs.video.length ? "All detected webcams are already added." :
      "No webcam was detected. Plug one in and press Refresh."));
  }
  const refresh = h("button.btn.sm", icon("refresh"), "Refresh devices");
  refresh.onclick = () => busy(refresh, async () => { m.close(); devicesCache = null; cameraDialog(cam, redraw); });
  body.appendChild(add(h("div.stack"), [
    h("div.row", h("div", { style: { flex: "1" } }, field("Webcam", devSel)), isNew ? h("div", { style: { alignSelf: "flex-end" } }, refresh) : null),
    field("Camera name", name, "Shown on the server and in recordings"),
    h("div", field("Microphone", audioSel), micHint),
    h("div.grid-2", field("Resolution", sizeSel), field("Frame rate", fpsSel)),
    h("div.grid-2", field("Quality", qSel), field("Rotation", rotSel)),
    mirror.el, stamp.el, record.el, isNew ? null : enabled.el,
  ]));
  if (isNew && free[0]) { name.value = free[0].name; name.dataset.auto = "1"; }
  name.addEventListener("input", () => { delete name.dataset.auto; });
  fillAudio();
  await fillSizes();

  save.onclick = () => busy(save, async () => {
    const body = {
      name: name.value.trim(), audio: audioSel.value, size: sizeSel.value, fps: Number(fpsSel.value),
      quality: qSel.value, rotate: Number(rotSel.value), mirror: mirror.input.checked,
      timestamp: stamp.input.checked, record: record.input.checked, enabled: enabled.input.checked,
    };
    if (isNew) {
      if (!devSel.value) throw new Error("Choose a webcam");
      body.device = devSel.value;
      await api("POST", "/api/cameras", body);
      toast("Camera added - it starts in a few seconds");
    } else {
      await api("PUT", "/api/cameras/" + cam.id, body);
      toast("Camera saved and restarted");
    }
    devicesCache = null;
    m.close();
    redraw();
  });
}

function keyOf(d) {
  if (!d) return "";
  if (d.kind === "dshow") return "dshow:" + d.name + "#" + (d.index || 0);
  return d.kind + ":" + d.path;
}

/* ---------------- Servers ---------------- */
App.register("servers", {
  title: "Servers", icon: "server",
  async render(el, actions) {
    const addBtn = h("button.btn.primary", icon("plus"), "Add server");
    actions.appendChild(addBtn);
    el.appendChild(h("div.alert.info", { style: { marginBottom: "16px" } },
      "This camera system connects out to each server (it needs no public IP). Enter the address, port, username and password ",
      "that were chosen when the server was set up. One camera can send to many servers."));
    const list = h("div.items");
    el.appendChild(list);
    let cams = [];
    const draw = async () => {
      const st = await api("GET", "/api/status");
      cams = st.cameras;
      clear(list);
      if (!st.servers.length) list.appendChild(h("div.card.empty", h("h3", "No servers yet"), h("p", "Add a server to watch these cameras from anywhere.")));
      for (const s of st.servers) list.appendChild(serverItem(s, cams, draw));
    };
    addBtn.onclick = () => serverDialog(null, cams, draw);
    await draw();
    App.refreshEvery(3000, draw);
  },
});

function serverItem(s, cams, redraw) {
  const sharing = s.cameras === "all" ? "All cameras" : (s.cameras.length ? s.cameras.map((id) => (cams.find((c) => c.id === id) || { name: id }).name).join(", ") : "No cameras");
  const watching = (s.watching || []).map((id) => (cams.find((c) => c.id === id) || { name: id }).name);
  const tgl = toggle(s.enabled, async (on, inp) => {
    try { await api("PUT", "/api/servers/" + s.id, { enabled: on }); toast(on ? "Server enabled" : "Server disabled"); redraw(); }
    catch (e) { inp.checked = !on; toast(e.message, true); }
  });
  const del = h("button.btn.sm.danger", icon("trash"), "Remove");
  del.onclick = async () => {
    if (!await confirmBox("Remove server", "Stop sending video to " + s.address + ":" + s.port + "?", "Remove", true)) return;
    await busy(del, async () => { await api("DELETE", "/api/servers/" + s.id); redraw(); });
  };
  const connected = s.state === "connected";
  let plainNote = null;
  if (connected && !s.secure) {
    plainNote = s.tls_mode === "off" ? "Encryption is off for this server. Many networks throttle unencrypted uploads."
      : s.server_tls ? "Connected without TLS this time - the next connection uses TLS."
        : "The server offers no TLS (Eris CCTV older than 1.1). Many networks throttle unencrypted uploads, so the video may never start there - update the server.";
  }
  return h("div.item",
    h("div.grow",
      h("div.title", s.address + ":" + s.port, serverState(s),
        connected ? (s.secure ? h("span.badge.info.plain", "Encrypted") : h("span.badge.warn.plain", "Not encrypted")) : null),
      h("div.sub", "User " + s.username + (s.server_name ? "  ·  " + s.server_name : "") +
        (connected ? "  ·  connected " + fmtAgo(s.connected_since) : "")),
      !connected && s.error && s.enabled ? h("div.err-line", s.error) : null,
      plainNote ? h("div.err-line", plainNote) : null,
      h("div.chips",
        h("span.chip", "Shares: " + sharing),
        h("span.chip", "Sent " + fmtBytes(s.bytes_sent)),
        watching.length ? h("span.chip", "Being watched: " + watching.join(", ") + " (" + fmtRate(s.rate * 8) + ")") : null,
        s.dropped ? h("span.chip", s.dropped + " fragments skipped (slow link)") : null)),
    h("div.acts", tgl.el,
      h("button.btn.sm", { on: { click: () => serverDialog(s, cams, redraw) } }, "Edit"), del));
}

function serverDialog(s, cams, redraw) {
  const isNew = !s;
  const address = input("text", s ? s.address : "", { placeholder: "203.0.113.10 or cctv.example.com" });
  const port = input("number", s ? s.port : 8800, { min: "1", max: "65535" });
  const user = input("text", s ? s.username : "", { autocomplete: "off" });
  const pass = input("password", "", { autocomplete: "new-password", placeholder: isNew ? "" : "(unchanged)" });
  const mode = select([["auto", "Automatic - TLS whenever the server offers it (recommended)"],
    ["on", "Always TLS - refuse unencrypted connections"], ["off", "Off - never encrypt"]], s ? s.tls_mode : "auto");
  const verify = select([["pin", "Trust this server's certificate on first connect (pin)"],
    ["ca", "Require a certificate signed by a public CA"], ["none", "Do not check the certificate (not safe)"]],
  s ? s.tls_verify : "pin");
  const pinInfo = h("div.hint", s && s.tls_pin ? "Pinned fingerprint: " + s.tls_pin.slice(0, 32) + "..." : "No certificate pinned yet.");
  const forget = h("button.btn.sm" + (s && s.tls_pin ? "" : ".hidden"), { type: "button" }, "Forget pinned certificate");
  forget.onclick = () => busy(forget, async () => {
    await api("PUT", "/api/servers/" + s.id, { tls_pin: "" });
    s.tls_pin = "";
    pinInfo.textContent = "Forgotten - the next connection pins the server's current certificate.";
    forget.classList.add("hidden");
    redraw();
  });
  const verifyBox = h("div", field("Certificate check", verify), h("div.row", pinInfo, forget));
  const syncTls = () => verifyBox.classList.toggle("hidden", mode.value === "off");
  mode.addEventListener("change", syncTls);
  const all = checkbox("Share all cameras (including ones added later)", !s || s.cameras === "all");
  const camChecks = cams.map((c) => {
    const cb = checkbox(c.name, s && s.cameras !== "all" && s.cameras.includes(c.id));
    cb.input.value = c.id;
    return cb;
  });
  const camBox = h("div.stack", { style: { paddingLeft: "26px" } }, camChecks.map((c) => c.el));
  const syncCams = () => camBox.classList.toggle("hidden", all.input.checked);
  all.input.addEventListener("change", syncCams);
  const result = h("div.hidden");
  const test = h("button.btn.left", "Test connection");
  const save = h("button.btn.primary", isNew ? "Add server" : "Save");
  const cancel = h("button.btn", "Cancel");
  const body = h("div.stack",
    h("div.grid-2", field("Server address (IP or domain)", address), field("Port", port)),
    h("div.grid-2", field("Username", user), field("Password", pass)),
    field("Encryption (TLS)", mode), verifyBox,
    h("div", h("div.small.muted", { style: { marginBottom: "8px" } }, "Cameras sent to this server"), all.el, camBox),
    result);
  const m = modal({ title: isNew ? "Add server" : "Edit server", body, actions: [test, cancel, save] });
  syncTls(); syncCams();
  cancel.onclick = m.close;
  const payload = () => {
    const b = { address: address.value.trim(), port: Number(port.value), username: user.value.trim(), tls_mode: mode.value,
      tls_verify: verify.value, cameras: all.input.checked ? "all" : camChecks.filter((c) => c.input.checked).map((c) => c.input.value) };
    if (pass.value) b.password = pass.value;
    return b;
  };
  test.onclick = () => busy(test, async () => {
    const b = payload();
    if (s) b.id = s.id;
    result.className = "alert info";
    result.textContent = "Testing...";
    const r = await api("POST", "/api/servers/test", b);
    if (r.ok) {
      result.className = "alert info";
      result.textContent = "Success - logged in to \"" + (r.server_name || "server") + "\", " +
        (r.secure ? "encrypted (TLS). Certificate fingerprint: " + r.fingerprint.slice(0, 32) + "..."
          : "NOT encrypted" + (r.server_tls ? "." : " - the server offers no TLS (older than 1.1); update it, many networks throttle unencrypted uploads."));
      if (!r.secure) result.className = "alert warn";
    } else {
      result.className = "alert err";
      result.textContent = r.error + (r.tls_untrusted ? " - choose \"Trust on first connect (pin)\"." : "");
    }
  });
  save.onclick = () => busy(save, async () => {
    if (isNew) await api("POST", "/api/servers", payload());
    else await api("PUT", "/api/servers/" + s.id, payload());
    toast(isNew ? "Server added - connecting..." : "Server saved");
    m.close();
    redraw();
  });
}

/* ---------------- Storage ---------------- */
App.register("storage", {
  title: "Storage", icon: "disk",
  async render(el, actions) {
    const d = await api("GET", "/api/storage/drives");
    const st = d.current;
    let chosen = null;
    const enabled = toggle(st.enabled);
    const statusBox = h("div");
    const drawStatus = (s) => {
      clear(statusBox);
      const usage = s.usage || {};
      const pct = usage.total ? 100 - usage.free / usage.total * 100 : 0;
      statusBox.appendChild(h("div.stack",
        h("div.row", h("b", "Status"), s.state === "ok" ? stateBadge("ok", "Recording") : s.state === "disabled" ? stateBadge("disabled", "Off") : stateBadge("error", "Problem")),
        s.state === "error" ? h("div.alert.err", s.message) : null,
        s.path ? h("dl.kv",
          h("dt", "Folder"), h("dd.mono", s.path),
          h("dt", "Recordings"), h("dd", fmtBytes(usage.recordings || 0) + (usage.files ? "  (" + usage.files + " files)" : "")),
          usage.total ? [h("dt", "Drive space"), h("dd", fmtBytes(usage.free) + " free of " + fmtBytes(usage.total))] : null,
          (s.recording || []).length ? [h("dt", "Recording now"), h("dd", s.recording.length + " camera(s)")] : null) : null,
        usage.total ? h("div.meter" + (pct > 95 ? ".full" : pct > 85 ? ".hot" : ""), h("i", { style: { width: pct.toFixed(1) + "%" } })) : null));
    };
    drawStatus(st);

    const drivesBox = h("div.stack");
    const radios = [];
    const drawDrives = (items) => {
      clear(drivesBox);
      if (!items.length) drivesBox.appendChild(h("div.muted", "No drives found."));
      for (const drv of items) {
        const isCurrent = drv.mounted && st.path && (st.mount ? st.mount === drv.mount : drv.system);
        const r = h("input", { type: "radio", name: "drive" });
        r.checked = !!isCurrent;
        if (isCurrent) chosen = drv;
        const pct = drv.total && drv.mounted ? 100 - drv.free / drv.total * 100 : 0;
        const title = (drv.label || (drv.system ? "System disk" : drv.mount || drv.device)) + (drv.model ? " - " + drv.model : "");
        const mountBtn = !drv.mounted ? h("button.btn.sm", "Mount & use") : null;
        const row = h("div.drive" + (isCurrent ? ".sel" : "") + (drv.mounted ? "" : ".off"),
          drv.mounted ? r : null,
          h("div.grow",
            h("div.row", h("b", title),
              drv.system ? h("span.badge.plain", "System") : null,
              drv.removable ? h("span.badge.info.plain", "Removable") : null,
              drv.network ? h("span.badge.info.plain", "Network") : null,
              !drv.mounted ? h("span.badge.warn.plain", "Not mounted") : null),
            h("div.small.muted", [drv.mount || drv.device, drv.fstype, drv.mounted ? fmtBytes(drv.free) + " free of " + fmtBytes(drv.total) : fmtBytes(drv.total)].filter(Boolean).join("  ·  ")),
            drv.mounted ? h("div.meter" + (pct > 95 ? ".full" : pct > 85 ? ".hot" : ""), { style: { marginTop: "8px" } }, h("i", { style: { width: pct.toFixed(1) + "%" } })) : null),
          mountBtn);
        if (drv.mounted) {
          row.onclick = () => {
            radios.forEach((x) => { x.r.checked = false; x.row.classList.remove("sel"); });
            r.checked = true; row.classList.add("sel"); chosen = drv;
            folder.value = drv.folder;
          };
          radios.push({ r, row });
        } else {
          mountBtn.onclick = () => busy(mountBtn, async () => {
            const res = await api("POST", "/api/storage/mount", drv);
            toast("Mounted at " + res.mount);
            const again = await api("GET", "/api/storage/drives");
            drawDrives(again.drives);
          });
        }
        drivesBox.appendChild(row);
      }
    };
    const folder = input("text", st.path || "", { placeholder: "Chosen automatically from the drive" });
    drawDrives(d.drives);
    const seg = input("number", st.segment_minutes, { min: "1", max: "120" });
    const days = input("number", st.retention_days, { min: "0", max: "3650" });
    const minFree = input("number", st.min_free_gb, { min: "0", step: "0.5" });
    const maxSize = input("number", st.max_size_gb, { min: "0", step: "1" });
    const save = h("button.btn.primary", "Save storage settings");
    save.onclick = () => busy(save, async () => {
      const body = { enabled: enabled.input.checked, path: folder.value.trim(), segment_minutes: Number(seg.value),
        retention_days: Number(days.value), min_free_gb: Number(minFree.value), max_size_gb: Number(maxSize.value) };
      if (chosen) body.drive = chosen;
      const r = await api("PUT", "/api/storage", body);
      drawStatus(r.storage);
      toast(body.enabled ? "Storage saved" : "Recording turned off");
    });
    const refresh = h("button.btn.sm", icon("refresh"), "Refresh");
    refresh.onclick = () => busy(refresh, async () => { const again = await api("GET", "/api/storage/drives"); drawDrives(again.drives); drawStatus(again.current); });

    el.appendChild(h("div.grid-2",
      h("div.card",
        h("div.card-head", h("h2", "Recording"), h("div.spacer"), h("label.row", h("span.muted", "Record to disk"), enabled.el)),
        statusBox),
      h("div.card",
        h("div.card-head", h("h2", "Rules")),
        h("div.stack",
          h("div.grid-2", field("File length (minutes)", seg, "A new file starts every N minutes"),
            field("Keep recordings for (days)", days, "0 = keep until space is needed")),
          h("div.grid-2", field("Always keep free (GB)", minFree, "Oldest files are deleted to keep this free"),
            field("Maximum size of recordings (GB)", maxSize, "0 = no limit"))))));
    el.appendChild(h("div.card", { style: { marginTop: "16px" } },
      h("div.card-head", h("h2", "Drive for recordings"), h("div.spacer"), refresh),
      drivesBox,
      h("div", { style: { marginTop: "14px" } }, field("Folder on the drive", folder, "Leave as is unless you need a specific folder.")),
      h("div", { style: { marginTop: "14px" } }, save)));
  },
});

/* ---------------- Recordings ---------------- */
App.register("recordings", {
  title: "Recordings", icon: "film",
  async render(el) {
    const params = new URLSearchParams((location.hash.split("?")[1] || ""));
    let cam = params.get("camera") || "", day = "";
    const camSel = h("select", { style: { maxWidth: "280px" } });
    const days = h("div.daylist");
    const table = h("div");
    el.appendChild(h("div.card",
      h("div.toolbar", h("b", "Camera"), camSel, h("div.spacer")),
      days, h("div", { style: { marginTop: "16px" } }, table)));
    const loadFiles = async () => {
      clear(table);
      if (!cam || !day) return;
      const d = await api("GET", "/api/recordings?camera=" + encodeURIComponent(cam) + "&day=" + encodeURIComponent(day));
      if (!d.files.length) { table.appendChild(h("div.muted", "No files.")); return; }
      const delDay = h("button.btn.sm.danger", icon("trash"), "Delete this day");
      delDay.onclick = async () => {
        if (!await confirmBox("Delete recordings", "Delete all finished recordings of " + day + "?", "Delete", true)) return;
        await busy(delDay, async () => { const r = await api("DELETE", "/api/recordings", { camera: cam, day }); toast(r.removed + " file(s) deleted"); load(); });
      };
      const total = d.files.reduce((a, f) => a + f.size, 0);
      table.appendChild(h("div.row.between", { style: { marginBottom: "10px" } },
        h("div.muted", d.files.length + " files  ·  " + fmtBytes(total)), delDay));
      const rows = d.files.slice().reverse().map((f) => {
        const url = "/rec/" + encodeURIComponent(cam) + "/" + day + "/" + encodeURIComponent(f.name);
        const del = h("button.btn.sm.icon.danger", { title: "Delete" }, icon("trash"));
        del.onclick = async () => {
          if (!await confirmBox("Delete recording", "Delete " + day + " " + f.start + "?", "Delete", true)) return;
          await busy(del, async () => { await api("DELETE", "/api/recordings", { camera: cam, day, file: f.name }); loadFiles(); });
        };
        return h("tr",
          h("td.mono", f.start), h("td", f.recording ? h("span.badge.rec", "Recording") : fmtDuration(f.duration)),
          h("td", fmtBytes(f.size)),
          h("td.actions",
            h("button.btn.sm", { on: { click: () => playDialog(url, (camSel.selectedOptions[0] || {}).textContent + " - " + day + " " + f.start) } }, icon("play"), "Play"), " ",
            h("a.btn.sm.icon", { href: url + "?download=1", title: "Download" }, icon("download")), " ",
            f.recording ? null : del));
      });
      table.appendChild(h("div.table-wrap", h("table.tbl",
        h("thead", h("tr", h("th", "Start"), h("th", "Length"), h("th", "Size"), h("th"))), h("tbody", rows))));
    };
    const load = async () => {
      const d = await api("GET", "/api/recordings" + (cam ? "?camera=" + encodeURIComponent(cam) : ""));
      clear(camSel);
      if (!d.cameras.length) {
        clear(days);
        clear(table).appendChild(h("div.empty", h("h3", "No recordings"), h("p", "Turn on recording on the Storage page.")));
        return;
      }
      if (!cam || !d.cameras.find((c) => c.id === cam)) {
        cam = d.cameras[0].id;
        return load();
      }
      for (const c of d.cameras) camSel.appendChild(h("option", { value: c.id, selected: c.id === cam }, c.name));
      clear(days);
      if (!d.days.includes(day)) day = d.days[0] || "";
      for (const dd of d.days) {
        days.appendChild(h("button" + (dd === day ? ".on" : ""), { on: { click: () => { day = dd; days.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent === dd)); loadFiles(); } } }, dd));
      }
      if (!d.days.length) days.appendChild(h("span.muted", "No recordings for this camera."));
      await loadFiles();
    };
    camSel.onchange = () => { cam = camSel.value; day = ""; load(); };
    await load();
  },
});

function playDialog(url, title) {
  const v = h("video.player-video", { controls: true, autoplay: true, src: url });
  modal({ title, body: v, wide: true, actions: [h("a.btn", { href: url + "?download=1" }, icon("download"), "Download")],
    onClose: () => { v.pause(); v.removeAttribute("src"); v.load(); } });
}

/* ---------------- Network ---------------- */
App.register("network", {
  title: "Network", icon: "network",
  async render(el) {
    const box = h("div");
    el.appendChild(box);
    const draw = async () => {
      const n = await api("GET", "/api/network");
      clear(box);
      const urls = n.bind === "127.0.0.1" ? ["http://127.0.0.1:" + n.port] : n.ips.map((ip) => "http://" + ip + ":" + n.port);
      box.appendChild(h("div.grid-2",
        h("div.card",
          h("div.card-head", h("h2", "This camera system")),
          h("dl.kv",
            h("dt", "Computer name"), h("dd", n.hostname),
            h("dt", "IP addresses"), h("dd", n.ips.length ? n.ips.join(", ") : "-"),
            h("dt", "Panel address"), h("dd", urls.map((u) => h("div", copyable(u)))),
            h("dt", "Panel access"), h("dd", n.bind === "127.0.0.1" ? "This computer only" : "Any device on the network"))),
        h("div.card",
          h("div.card-head", h("h2", "Cameras")),
          n.cameras.length ? h("table.tbl", h("tbody", n.cameras.map((c) => h("tr",
            h("td", c.name), h("td", c.online ? stateBadge("ok", "Live") : stateBadge("error", "Off")),
            h("td.mono", fmtRate(c.bitrate)))))) : h("div.muted", "No cameras."))));
      box.appendChild(h("div.card", { style: { marginTop: "16px" } },
        h("div.card-head", h("h2", "Server connections"), h("span.muted.small", "outgoing - no port forwarding needed")),
        n.servers.length ? h("div.table-wrap", h("table.tbl",
          h("thead", h("tr", h("th", "Server"), h("th", "Status"), h("th", "Connected"), h("th", "Sent"), h("th", "Watching"))),
          h("tbody", n.servers.map((s) => h("tr",
            h("td.mono", s.address + ":" + s.port + (s.secure ? " (TLS)" : "")),
            h("td", serverState(s), s.state !== "connected" && s.error ? h("div.small", { style: { color: "#ffb4b0" } }, s.error) : null),
            h("td", s.connected_since ? fmtAgo(s.connected_since).replace(" ago", "") : "-"),
            h("td", fmtBytes(s.bytes_sent)),
            h("td", (s.watching || []).length || "-")))))) : h("div.muted", "No servers configured.")));
    };
    await draw();
    App.refreshEvery(3000, draw);
  },
});

/* ---------------- Settings ---------------- */
App.register("settings", {
  title: "Settings", icon: "settings",
  async render(el) {
    const [s, st] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/status")]);
    const name = input("text", s.name, { maxlength: "60" });
    const port = input("number", s.port, { min: "1", max: "65535" });
    const bind = select([["0.0.0.0", "Any device on the network"], ["127.0.0.1", "This computer only"]], s.bind);
    const ffmpeg = input("text", s.ffmpeg, { placeholder: s.ffmpeg_found || "Detected automatically" });
    const save = h("button.btn.primary", "Save");
    save.onclick = () => busy(save, async () => {
      const r = await api("PUT", "/api/settings", { name: name.value, port: Number(port.value), bind: bind.value, ffmpeg: ffmpeg.value });
      toast(r.restart_needed ? "Saved. Restart Eris CCTV (eris-cctv > Restart) to use the new panel port/access." : "Settings saved");
    });
    el.appendChild(h("div.grid-2",
      h("div.card",
        h("div.card-head", h("h2", "General")),
        h("div.stack",
          field("Camera system name", name, "Servers show this name next to the cameras"),
          h("div.grid-2", field("Panel port", port), field("Who can open this panel", bind)),
          field("ffmpeg path", ffmpeg, "Leave empty to use the bundled / system ffmpeg"),
          h("div", save))),
      h("div.stack",
        passwordCard(true, s.username),
        h("div.card",
          h("div.card-head", h("h2", "About")),
          h("dl.kv",
            h("dt", "Version"), h("dd", "Eris CCTV " + st.agent.version),
            h("dt", "System ID"), h("dd.mono", st.agent.id),
            h("dt", "Platform"), h("dd", st.agent.platform),
            h("dt", "ffmpeg"), h("dd", st.agent.ffmpeg ? (st.agent.ffmpeg_version || "found") : h("span", { style: { color: "var(--danger)" } }, "not found")),
            h("dt", "Running for"), h("dd", fmtDuration(st.agent.uptime)))))));
  },
});

App.register("logs", { title: "Logs", icon: "logs", render: logPage("/api/logs") });

App.boot("Eris CCTV", "Camera system");
