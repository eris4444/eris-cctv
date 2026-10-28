/* Eris CCTV - server (viewing) panel */
"use strict";

const LAYOUTS = [["auto", "Auto"], ["1", "1"], ["2", "2×2"], ["3", "3×3"], ["4", "4×4"]];

function pref(key, def) {
  try { return localStorage.getItem("ecctv." + key) || def; } catch (e) { return def; }
}
function setPref(key, val) {
  try { localStorage.setItem("ecctv." + key, val); } catch (e) { /* ignore */ }
}

/* ---------------- Live grid ---------------- */
App.register("live", {
  title: "Cameras", icon: "live", wide: true,
  async render(el, actions) {
    let data = await api("GET", "/api/cameras");
    let layout = pref("layout", "auto");
    let system = pref("system", "");
    let showOffline = pref("offline", "1") === "1";
    const tiles = {};
    const grid = h("div.tiles");
    const empty = h("div");
    const sysSel = h("select", { style: { width: "auto", minWidth: "170px" } });
    const seg = h("div.seg");
    const off = checkbox("Show offline", showOffline);
    const counter = h("span.muted.small");
    el.appendChild(h("div.toolbar",
      h("span.muted", "Camera system"), sysSel,
      off.el, h("div.spacer"), counter, h("span.muted", "Layout"), seg));
    el.appendChild(empty);
    el.appendChild(grid);

    for (const [v, label] of LAYOUTS) {
      seg.appendChild(h("button" + (v === layout ? ".on" : ""), { on: { click: (e) => {
        layout = v; setPref("layout", v);
        seg.querySelectorAll("button").forEach((b) => b.classList.remove("on"));
        e.currentTarget.classList.add("on");
        sync();
      } } }, label));
    }
    sysSel.onchange = () => { system = sysSel.value; setPref("system", system); sync(); };
    off.input.onchange = () => { showOffline = off.input.checked; setPref("offline", showOffline ? "1" : "0"); sync(); };

    const fillSystems = () => {
      const systems = {};
      for (const c of data.cameras) systems[c.agent_id] = c.agent_name;
      setOptions(sysSel, [["", "All systems"]].concat(Object.entries(systems).sort((a, b) => a[1].localeCompare(b[1]))), system);
    };

    const sync = () => {
      fillSystems();
      const list = data.cameras.filter((c) => (!system || c.agent_id === system) && (showOffline || c.online || c.agent_online));
      const n = list.length;
      const cols = layout === "auto" ? (n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4) : Number(layout);
      grid.style.setProperty("--cols", cols);
      const online = data.cameras.filter((c) => c.online).length;
      counter.textContent = online + " of " + data.cameras.length + " online";
      clear(empty);
      if (!data.cameras.length) {
        empty.appendChild(h("div.card.empty", h("h3", "No cameras yet"),
          h("p", "Install Eris CCTV on a computer with a webcam, choose \"This system is a camera\" and enter this server's address, port, username and password."),
          App.session.role === "admin" ? h("a.btn.primary", { href: "#accounts" }, "Show connection details") : null));
      } else if (!n) {
        empty.appendChild(h("div.card.empty", h("h3", "Nothing to show"), h("p", "No cameras match the filter.")));
      }
      const keep = new Set(list.map((c) => c.key));
      for (const key of Object.keys(tiles)) {
        if (!keep.has(key)) { tiles[key].stop(); tiles[key].el.remove(); delete tiles[key]; }
      }
      for (const c of list) {
        let t = tiles[c.key];
        if (!t) {
          t = cameraTile({ title: c.name, subtitle: c.agent_name, url: "/api/view/" + c.key.split("/").map(encodeURIComponent).join("/"),
            onFocus: (tile) => {
              const on = !tile.classList.contains("focus");
              grid.querySelectorAll(".tile.focus").forEach((x) => x.classList.remove("focus"));
              if (on) { tile.classList.add("focus"); tile.scrollIntoView({ behavior: "smooth", block: "start" }); }
            } });
          tiles[c.key] = t;
          t.start();
        }
        t.setRecording(c.recording && c.online);
        t.setSubtitle(c.agent_name + (c.online && c.width ? "  ·  " + c.width + "x" + c.height : "") +
          (!c.agent_online && c.last_seen ? "  ·  seen " + fmtAgo(c.last_seen) : ""));
        grid.appendChild(t.el);  /* keeps the sorted order */
      }
    };
    sync();
    App.onLeave(() => Object.values(tiles).forEach((t) => t.stop()));
    App.refreshEvery(5000, async () => { data = await api("GET", "/api/cameras"); sync(); });
  },
});

/* ---------------- Camera systems (agents) ---------------- */
App.register("systems", {
  title: "Camera systems", icon: "camera", admin: true,
  async render(el) {
    const box = h("div");
    el.appendChild(box);
    const draw = async () => {
      const [a, c] = await Promise.all([api("GET", "/api/agents"), api("GET", "/api/cameras")]);
      clear(box);
      if (!a.agents.length) {
        box.appendChild(h("div.card.empty", h("h3", "No camera systems have connected yet"),
          h("p", "They appear here automatically after their first connection."),
          h("a.btn.primary", { href: "#accounts" }, "Show connection details")));
        return;
      }
      const items = h("div.items");
      for (const ag of a.agents) {
        const cams = c.cameras.filter((x) => x.agent_id === ag.id);
        const forget = h("button.btn.sm.danger", icon("trash"), "Forget");
        forget.onclick = async () => {
          if (!await confirmBox("Forget camera system", "Remove \"" + ag.name + "\" and its cameras from this list? If it connects again it comes back.", "Forget", true)) return;
          await busy(forget, async () => { await api("DELETE", "/api/agents/" + ag.id); draw(); });
        };
        items.appendChild(h("div.item",
          h("div.grow",
            h("div.title", ag.name, ag.online ? stateBadge("online", "Online") : stateBadge("offline", "Offline"),
              ag.online ? (ag.secure ? h("span.badge.info.plain", "Encrypted") : h("span.badge.warn.plain", "Not encrypted")) : null),
            h("div.sub", [ag.online ? "Connected " + fmtAgo(ag.connected_at) : "Last seen " + fmtAgo(ag.last_seen),
              "from " + (ag.ip || "?"), "account " + (ag.account || "?"), ag.version ? "v" + ag.version : ""].filter(Boolean).join("  ·  ")),
            ag.online && !ag.secure ? h("div.err-line", "Unencrypted link. Many networks throttle unencrypted uploads, which leaves the video stuck on \"Starting video\". " +
              "Update this camera system to Eris CCTV 1.1 or newer - it then encrypts automatically.") : null,
            h("div.chips",
              cams.map((x) => h("span.chip", x.name + (x.online ? "" : " (off)"))),
              ag.online ? h("span.chip", "Received " + fmtBytes(ag.bytes_in)) : null,
              ag.online && ag.streaming ? h("span.chip", "Streaming " + ag.streaming + " now, " + fmtRate(ag.rate_in * 8)) : null)),
          h("div.acts", ag.online ? null : forget)));
      }
      box.appendChild(items);
    };
    await draw();
    App.refreshEvery(4000, draw);
  },
});

/* ---------------- Camera accounts ---------------- */
App.register("accounts", {
  title: "Camera accounts", icon: "key", admin: true,
  async render(el, actions) {
    const addBtn = h("button.btn.primary", icon("plus"), "Add camera account");
    actions.appendChild(addBtn);
    const box = h("div");
    el.appendChild(box);
    const draw = async () => {
      const d = await api("GET", "/api/accounts");
      const cn = d.connection;
      const addr = cn.address || location.hostname;
      clear(box);
      box.appendChild(h("div.card",
        h("div.card-head", h("h2", "Connection details for camera systems")),
        h("p.muted", { style: { marginTop: 0 } }, "On each computer with a webcam, run the Eris CCTV installer, choose \"This system is a camera\", then add this server with:"),
        h("dl.kv",
          h("dt", "Server address"), h("dd", copyable(addr), cn.address ? null : h("span.faint.small", "  (set the public address in Settings)")),
          h("dt", "Port"), h("dd", copyable(String(cn.port))),
          h("dt", "Username / password"), h("dd", "one of the camera accounts below"),
          h("dt", "Encryption"), h("dd", cn.tls ? "Automatic (TLS) - nothing to set on the camera system" : "Off on this server"),
          cn.tls && cn.fingerprint ? [h("dt", "Certificate"), h("dd", h("span.mono.small", cn.fingerprint))] : null)));
      const rows = d.accounts.map((a) => {
        const pw = h("button.btn.sm", "New password");
        pw.onclick = () => accountPassword(a.username, draw);
        const del = h("button.btn.sm.danger", icon("trash"), "Delete");
        del.onclick = async () => {
          if (!await confirmBox("Delete camera account", "Delete \"" + a.username + "\"? Camera systems using it are disconnected immediately.", "Delete", true)) return;
          await busy(del, async () => { await api("DELETE", "/api/accounts/" + encodeURIComponent(a.username)); toast("Account deleted"); draw(); });
        };
        return h("tr", h("td", h("b", a.username), a.note ? h("div.small.muted", a.note) : null),
          h("td", a.connected.length ? a.connected.join(", ") : h("span.muted", "-")),
          h("td", a.created ? new Date(a.created * 1000).toLocaleDateString() : "-"),
          h("td.actions", pw, " ", del));
      });
      box.appendChild(h("div.card",
        h("div.card-head", h("h2", "Camera accounts"), h("span.muted.small", "username + password that camera systems log in with")),
        d.accounts.length ? h("div.table-wrap", h("table.tbl",
          h("thead", h("tr", h("th", "Username"), h("th", "Connected systems"), h("th", "Created"), h("th"))),
          h("tbody", rows))) : h("div.muted", "No camera accounts - add one so camera systems can connect.")));
    };
    addBtn.onclick = () => {
      const user = input("text", "", { autocomplete: "off", placeholder: "e.g. home-cams" });
      const p1 = input("password", "", { autocomplete: "new-password" });
      const note = input("text", "", { placeholder: "optional" });
      const save = h("button.btn.primary", "Add account");
      const cancel = h("button.btn", "Cancel");
      const m = modal({ title: "Add camera account", body: h("div.stack", field("Username", user), field("Password", p1, "At least 6 characters"), field("Note", note)), actions: [cancel, save] });
      cancel.onclick = m.close;
      save.onclick = () => busy(save, async () => {
        await api("POST", "/api/accounts", { username: user.value.trim(), password: p1.value, note: note.value });
        toast("Camera account added"); m.close(); draw();
      });
    };
    await draw();
    App.refreshEvery(5000, draw);
  },
});

function accountPassword(username, done) {
  const p1 = input("password", "", { autocomplete: "new-password" });
  const save = h("button.btn.primary", "Save");
  const cancel = h("button.btn", "Cancel");
  const m = modal({ title: "New password - " + username, body: h("div.stack",
    h("div.alert.warn", "Camera systems using this account disconnect until you enter the new password on them."),
    field("New password", p1, "At least 6 characters")), actions: [cancel, save] });
  cancel.onclick = m.close;
  save.onclick = () => busy(save, async () => {
    await api("PUT", "/api/accounts/" + encodeURIComponent(username), { password: p1.value });
    toast("Password changed"); m.close(); done();
  });
}

/* ---------------- Users ---------------- */
App.register("users", {
  title: "Users", icon: "users", admin: true,
  async render(el, actions) {
    const addBtn = h("button.btn.primary", icon("plus"), "Add user");
    actions.appendChild(addBtn);
    const box = h("div");
    el.appendChild(box);
    let cams = [];
    const draw = async () => {
      const d = await api("GET", "/api/users");
      cams = d.cameras;
      clear(box);
      const rows = d.users.map((u) => {
        const edit = h("button.btn.sm", "Edit");
        edit.onclick = () => userDialog(u, cams, draw);
        const del = h("button.btn.sm.danger", icon("trash"), "Delete");
        del.onclick = async () => {
          if (!await confirmBox("Delete user", "Delete \"" + u.username + "\"?", "Delete", true)) return;
          await busy(del, async () => { await api("DELETE", "/api/users/" + encodeURIComponent(u.username)); draw(); });
        };
        const access = u.role === "admin" || u.cameras === "all" ? "All cameras" :
          (u.cameras.length ? u.cameras.map((k) => (cams.find((c) => c.key === k) || { name: k }).name).join(", ") : "No cameras");
        return h("tr", h("td", h("b", u.username), u.username === App.session.username ? h("span.muted", "  (you)") : null),
          h("td", u.role === "admin" ? h("span.badge.info.plain", "Administrator") : h("span.badge.plain", "Viewer")),
          h("td", access),
          h("td.actions", edit, " ", u.username === App.session.username ? null : del));
      });
      box.appendChild(h("div.card",
        h("div.card-head", h("h2", "Panel users"), h("span.muted.small", "people who can sign in to watch")),
        h("div.table-wrap", h("table.tbl", h("thead", h("tr", h("th", "Username"), h("th", "Role"), h("th", "Can watch"), h("th"))), h("tbody", rows)))));
    };
    addBtn.onclick = () => userDialog(null, cams, draw);
    await draw();
  },
});

function userDialog(u, cams, done) {
  const isNew = !u;
  const user = input("text", u ? u.username : "", { autocomplete: "off", disabled: !isNew });
  const pass = input("password", "", { autocomplete: "new-password", placeholder: isNew ? "" : "(unchanged)" });
  const role = select([["viewer", "Viewer - can only watch"], ["admin", "Administrator - full control"]], u ? u.role : "viewer");
  const all = checkbox("All cameras (including future ones)", !u || u.cameras === "all");
  const checks = cams.map((c) => {
    const cb = checkbox(c.name + "  ·  " + c.agent_name, u && u.cameras !== "all" && u.cameras.includes(c.key));
    cb.input.value = c.key;
    return cb;
  });
  const camBox = h("div.stack", { style: { paddingLeft: "26px", maxHeight: "220px", overflow: "auto" } },
    checks.length ? checks.map((c) => c.el) : h("div.muted.small", "No cameras known yet."));
  const access = h("div", h("div.small.muted", { style: { marginBottom: "8px" } }, "Cameras this user can watch"), all.el, camBox);
  const sync = () => { camBox.classList.toggle("hidden", all.input.checked); access.classList.toggle("hidden", role.value === "admin"); };
  all.input.onchange = sync; role.onchange = sync;
  const save = h("button.btn.primary", isNew ? "Add user" : "Save");
  const cancel = h("button.btn", "Cancel");
  const m = modal({ title: isNew ? "Add user" : "Edit user - " + u.username,
    body: h("div.stack", h("div.grid-2", field("Username", user), field("Password", pass, isNew ? "At least 6 characters" : "Leave empty to keep")), field("Role", role), access),
    actions: [cancel, save] });
  sync();
  cancel.onclick = m.close;
  save.onclick = () => busy(save, async () => {
    const body = { role: role.value, cameras: all.input.checked ? "all" : checks.filter((c) => c.input.checked).map((c) => c.input.value) };
    if (pass.value) body.password = pass.value;
    if (isNew) { body.username = user.value.trim(); await api("POST", "/api/users", body); }
    else await api("PUT", "/api/users/" + encodeURIComponent(u.username), body);
    toast(isNew ? "User added" : "User saved"); m.close(); done();
  });
}

/* ---------------- Settings ---------------- */
App.register("settings", {
  title: "Settings", icon: "settings",
  async render(el) {
    if (App.session.role !== "admin") {
      el.appendChild(h("div", { style: { maxWidth: "560px" } }, passwordCard(false)));
      return;
    }
    const s = await api("GET", "/api/settings");
    const name = input("text", s.name, { maxlength: "60" });
    const pub = input("text", s.public_address, { placeholder: "e.g. 203.0.113.10 or cctv.example.com" });
    const save = h("button.btn.primary", "Save");
    save.onclick = () => busy(save, async () => { await api("PUT", "/api/settings", { name: name.value, public_address: pub.value }); toast("Settings saved"); });
    el.appendChild(h("div.grid-2",
      h("div.stack",
        h("div.card",
          h("div.card-head", h("h2", "Server")),
          h("div.stack",
            field("Server name", name, "Camera systems show this name after connecting"),
            field("Public address", pub, "IP or domain that camera systems use to reach this server"),
            h("div", save))),
        passwordCard(false)),
      h("div.card",
        h("div.card-head", h("h2", "About")),
        h("dl.kv",
          h("dt", "Version"), h("dd", "Eris CCTV " + s.version),
          h("dt", "Port"), h("dd", String(s.port)),
          h("dt", "Listening on"), h("dd", s.bind === "0.0.0.0" ? "all network interfaces" : s.bind),
          h("dt", "TLS (https)"), h("dd", s.tls ? (s.tls_mode === "files" ? "On - own certificate" : "On - automatic self-signed certificate") +
            " (http and https both work on port " + s.port + ")" : "Off"),
          s.tls ? [h("dt", "Certificate"), h("dd.mono.small", s.tls_cert)] : null,
          s.tls && s.fingerprint ? [h("dt", "Fingerprint"), h("dd.mono.small", s.fingerprint)] : null,
          h("dt", "Running for"), h("dd", fmtDuration(s.uptime))),
        h("p.muted.small", { style: { marginBottom: 0 } }, "Port and TLS are changed with the eris-cctv command on the server (menu: Server settings)."))));
  },
});

App.register("logs", { title: "Logs", icon: "logs", admin: true, render: logPage("/api/logs") });

App.boot("Eris CCTV", "Monitoring server");
