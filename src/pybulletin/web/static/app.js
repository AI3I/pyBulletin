"use strict";
// pyBulletin public SPA

const API = "";          // same origin
let   SESSION = null;    // { token, call, privilege }
let   WS      = null;
let   _msgs   = [];
let   _currentMsgId = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  applyTheme();
  // Try restoring session from localStorage
  const saved = localStorage.getItem("pb_session");
  if (saved) {
    try {
      SESSION = JSON.parse(saved);
      await loadMessages();
      showAuthUI();
      showView("messages");
      connectWS();
      return;
    } catch (_) {
      localStorage.removeItem("pb_session");
    }
  }
  // Load health to get node name for login page
  try {
    const h = await apiFetch("/api/health", {auth: false});
    document.getElementById("login-branding").textContent = h.node || "pyBulletin";
    document.getElementById("login-node").textContent = h.motd || "Packet Radio BBS";
  } catch (_) {}
});

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function login() {
  const call = document.getElementById("login-call").value.trim().toUpperCase();
  const pass = document.getElementById("login-pass").value;
  const err  = document.getElementById("login-error");
  err.classList.add("hidden");

  if (!call) { err.textContent = "Enter your callsign."; err.classList.remove("hidden"); return; }

  try {
    const data = await apiFetch("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ call, password: pass }),
      auth: false,
    });
    SESSION = { token: data.token, call: data.call, privilege: data.privilege };
    localStorage.setItem("pb_session", JSON.stringify(SESSION));
    showAuthUI();
    await loadMessages();
    showView("messages");
    connectWS();
  } catch (e) {
    err.textContent = e.message || "Login failed.";
    err.classList.remove("hidden");
  }
}

async function logout() {
  if (SESSION) {
    try { await apiFetch("/api/auth/logout", { method: "POST" }); } catch (_) {}
  }
  SESSION = null;
  localStorage.removeItem("pb_session");
  if (WS) { WS.close(); WS = null; }
  document.getElementById("nav-links").style.display = "none";
  document.getElementById("call-badge").style.display = "none";
  document.getElementById("logout-btn").style.display = "none";
  showView("login");
}

function showAuthUI() {
  document.getElementById("nav-links").style.display = "";
  document.getElementById("call-badge").style.display = "";
  document.getElementById("call-badge").textContent = SESSION.call;
  document.getElementById("logout-btn").style.display = "";
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connectWS() {
  if (!SESSION) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url   = `${proto}://${location.host}/ws?token=${SESSION.token}`;
  WS = new WebSocket(url);

  WS.onopen = () => setWsStatus(true);
  WS.onclose = () => { setWsStatus(false); WS = null; setTimeout(connectWS, 5000); };
  WS.onerror = () => setWsStatus(false);
  WS.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "new_message") {
        toast(`New message #${msg.id} for ${msg.to}`, "info");
        if (document.getElementById("view-messages").classList.contains("active")) {
          loadMessages();
        }
      }
      if (msg.type === "conference_joined") {
        confOnJoined(msg.room, msg.welcome);
      }
      if (msg.type === "conference_message") {
        confAppend(msg.text);
      }
      if (msg.type === "conference_left") {
        confOnLeft();
      }
    } catch (_) {}
  };
}

function setWsStatus(online) {
  const dot   = document.getElementById("ws-dot");
  const label = document.getElementById("ws-label");
  dot.className   = "dot " + (online ? "dot-green" : "dot-gray");
  label.textContent = online ? "online" : "offline";
}

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-links button").forEach(b => b.classList.remove("active"));
  document.getElementById("view-" + name).classList.add("active");
  const nb = document.getElementById("nav-" + name);
  if (nb) nb.classList.add("active");

  if (name === "profile"   && SESSION) loadProfile();
  if (name === "conference" && SESSION) loadConfRooms();
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------
async function loadMessages() {
  if (!SESSION) return;
  const type   = document.getElementById("msg-type")?.value   || "";
  const status = document.getElementById("msg-status")?.value || "";
  const toSel  = document.getElementById("msg-to")?.value     || "";
  const to     = toSel === "__me__" ? SESSION.call : "";

  let url = `/api/messages?limit=200`;
  if (type)   url += `&type=${type}`;
  if (status) url += `&status=${status}`;
  if (to)     url += `&to=${to}`;

  try {
    _msgs = await apiFetch(url);
    renderMessages(_msgs);
  } catch (e) {
    toast("Failed to load messages: " + e.message, "error");
  }
}

function filterMessages() {
  const q = document.getElementById("msg-search").value.toLowerCase();
  renderMessages(q ? _msgs.filter(m =>
    m.subject.toLowerCase().includes(q) ||
    m.from_call.toLowerCase().includes(q) ||
    m.to_call.toLowerCase().includes(q)
  ) : _msgs);
}

function renderMessages(msgs) {
  const tbody = document.getElementById("msg-tbody");
  if (!msgs.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state">No messages found.</td></tr>`;
    return;
  }
  tbody.innerHTML = msgs.map(m => {
    const date = new Date(m.created_at * 1000).toLocaleDateString("en-US",
                   { month: "short", day: "numeric" });
    const subj = escHtml(m.subject.length > 45 ? m.subject.slice(0,45)+"…" : m.subject);
    return `<tr onclick="readMessage(${m.id})" style="cursor:pointer">
      <td class="mono">${m.id}</td>
      <td>${typeBadge(m.type)}</td>
      <td>${statusBadge(m.status)}</td>
      <td class="call">${escHtml(m.to_call)}</td>
      <td class="call">${escHtml(m.from_call)}</td>
      <td class="dim">${date}</td>
      <td>${subj}</td>
      <td><button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();killMsg(${m.id},this)">Kill</button></td>
    </tr>`;
  }).join("");
}

async function readMessage(id) {
  try {
    const msg = await apiFetch(`/api/messages/${id}`);
    _currentMsgId = id;
    document.getElementById("read-id").textContent = "#" + id;
    const d = new Date(msg.created_at * 1000).toUTCString();
    document.getElementById("read-header").innerHTML =
      `<strong>From   :</strong> ${escHtml(msg.from_call)}<br>` +
      `<strong>To     :</strong> ${escHtml(msg.to_call)}` +
        (msg.at_bbs ? ` @ ${escHtml(msg.at_bbs)}` : "") + `<br>` +
      `<strong>Date   :</strong> ${d}<br>` +
      `<strong>Subject:</strong> ${escHtml(msg.subject)}<br>` +
      `<strong>BID    :</strong> ${escHtml(msg.bid)}&nbsp;&nbsp;` +
      `<strong>Size   :</strong> ${msg.size} bytes`;
    document.getElementById("read-body").textContent = msg.body;
    // Show kill button only if it's mine or sysop
    const isMyMsg = msg.from_call === SESSION.call || msg.to_call === SESSION.call;
    document.getElementById("read-kill-btn").style.display =
      (isMyMsg || SESSION.privilege === "sysop") ? "" : "none";
    showView("read");
    // Mark as read in local cache
    const m = _msgs.find(x => x.id === id);
    if (m && m.status === "N") { m.status = "Y"; }
  } catch (e) {
    toast("Failed to read message: " + e.message, "error");
  }
}

async function killCurrentMsg() {
  if (!_currentMsgId) return;
  if (!confirm(`Kill message #${_currentMsgId}?`)) return;
  await killMsg(_currentMsgId);
  showView("messages");
}

async function killMsg(id, btn) {
  if (!confirm(`Kill message #${id}?`)) return;
  try {
    await apiFetch(`/api/messages/${id}`, { method: "DELETE" });
    toast(`Message #${id} killed.`, "success");
    _msgs = _msgs.filter(m => m.id !== id);
    renderMessages(_msgs);
  } catch (e) {
    toast("Kill failed: " + e.message, "error");
  }
}

function replyToMsg() {
  const h = document.getElementById("read-header").innerText;
  const fromMatch = h.match(/From\s*:\s*(\S+)/);
  const subjMatch = h.match(/Subject\s*:\s*(.+)/);
  if (fromMatch) document.getElementById("compose-to").value = fromMatch[1];
  if (subjMatch) {
    const s = subjMatch[1].trim();
    document.getElementById("compose-subject").value =
      s.startsWith("Re:") ? s : "Re: " + s;
  }
  document.getElementById("compose-type").value = "P";
  showView("compose");
}

async function sendMessage() {
  const to      = document.getElementById("compose-to").value.trim().toUpperCase();
  const at_bbs  = document.getElementById("compose-at").value.trim().toUpperCase();
  const subject = document.getElementById("compose-subject").value.trim();
  const body    = document.getElementById("compose-body").value;
  const type    = document.getElementById("compose-type").value;

  if (!to || !subject) { toast("To and Subject are required.", "error"); return; }

  try {
    const r = await apiFetch("/api/messages", {
      method: "POST",
      body: JSON.stringify({ to, at_bbs, subject, body, type }),
    });
    toast(`Message #${r.id} sent.`, "success");
    document.getElementById("compose-to").value = "";
    document.getElementById("compose-at").value = "";
    document.getElementById("compose-subject").value = "";
    document.getElementById("compose-body").value = "";
    showView("messages");
    await loadMessages();
  } catch (e) {
    toast("Send failed: " + e.message, "error");
  }
}

// ---------------------------------------------------------------------------
// White Pages
// ---------------------------------------------------------------------------
async function wpLookup() {
  const call = document.getElementById("wp-search").value.trim().toUpperCase();
  if (!call) return;
  const div = document.getElementById("wp-result");
  try {
    const r = await apiFetch(`/api/wp?call=${encodeURIComponent(call)}`);
    const updated = r.updated_at ? new Date(r.updated_at*1000).toLocaleDateString() : "?";
    div.innerHTML = `
      <table><thead><tr><th>Call</th><th>Home BBS</th><th>Name</th><th>Source</th><th>Updated</th></tr></thead>
      <tbody><tr>
        <td class="call">${escHtml(r.call)}</td>
        <td class="mono">${escHtml(r.home_bbs)}</td>
        <td>${escHtml(r.name)}</td>
        <td class="mono dim">${escHtml(r.source_bbs)}</td>
        <td class="dim">${updated}</td>
      </tr></tbody></table>`;
  } catch (e) {
    div.innerHTML = `<p class="empty-state">${escHtml(e.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------
async function loadProfile() {
  if (!SESSION) return;
  const card = document.getElementById("profile-card");
  try {
    const u = await apiFetch(`/api/users/${SESSION.call}`);
    const ls = u.last_seen ? new Date(u.last_seen*1000).toLocaleString() : "never";
    const ll = u.last_login_at ? new Date(u.last_login_at*1000).toLocaleString() : "never";
    card.innerHTML = `
      <div class="stat-grid" style="margin-bottom:.75rem">
        <div class="stat-box"><div class="stat-value" style="font-size:1.1rem">${escHtml(u.call)}</div>
          <div class="stat-label">Callsign</div></div>
        <div class="stat-box"><div class="stat-value" style="font-size:1rem">${escHtml(u.privilege||'user')}</div>
          <div class="stat-label">Privilege</div></div>
        <div class="stat-box"><div class="stat-value" style="font-size:1rem">${u.msg_base}</div>
          <div class="stat-label">Msg Base</div></div>
        <div class="stat-box"><div class="stat-value" style="font-size:1rem">${u.page_length||'off'}</div>
          <div class="stat-label">Page Length</div></div>
      </div>
      <table><tbody>
        <tr><th>Home BBS</th><td class="mono">${escHtml(u.home_bbs||'—')}</td></tr>
        <tr><th>Locator</th><td class="mono">${escHtml(u.locator||'—')}</td></tr>
        <tr><th>City</th><td>${escHtml(u.city||'—')}</td></tr>
        <tr><th>Last Login</th><td class="dim">${ll}</td></tr>
        <tr><th>Last Login From</th><td class="mono dim">${escHtml(u.last_login_peer||'—')}</td></tr>
        <tr><th>Last Seen</th><td class="dim">${ls}</td></tr>
      </tbody></table>`;
  } catch (e) {
    card.innerHTML = `<p class="empty-state">${escHtml(e.message)}</p>`;
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
async function apiFetch(url, opts = {}) {
  const headers = { "content-type": "application/json" };
  if (opts.auth !== false && SESSION?.token) {
    headers["authorization"] = "Bearer " + SESSION.token;
  }
  const resp = await fetch(API + url, {
    method:  opts.method || "GET",
    headers: { ...headers, ...(opts.headers || {}) },
    body:    opts.body,
  });
  if (resp.status === 401 || resp.status === 403) {
    if (opts.auth !== false) { logout(); }
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${resp.status}`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function typeBadge(t) {
  const labels = { P:"P", B:"B", T:"T" };
  return `<span class="badge badge-${t}">${labels[t]||t}</span>`;
}
function statusBadge(s) {
  return `<span class="badge badge-${s}">${s}</span>`;
}
function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

let _toastTimer = {};
function toast(msg, type = "info") {
  const area = document.getElementById("toast-area");
  const el   = document.createElement("div");
  el.className = "toast " + (type === "error" ? "error" : type === "success" ? "success" : "");
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ---------------------------------------------------------------------------
// Conference
// ---------------------------------------------------------------------------
let _confRoom = null;

async function loadConfRooms() {
  const el = document.getElementById("conf-rooms-list");
  if (!el || !SESSION) return;
  const data = await apiFetch("/api/conference");
  if (!data || !data.available) { el.innerHTML = ""; return; }
  const rooms = Object.entries(data.rooms);
  if (rooms.length === 0) { el.innerHTML = '<p class="subtle" style="margin:0">There are no active rooms.</p>'; return; }
  let html = '<table class="data-table" style="margin-top:.25rem"><thead><tr><th>Room</th><th>Members</th></tr></thead><tbody>';
  for (const [name, r] of rooms.sort()) {
    html += `<tr><td><a href="#" onclick="event.preventDefault();document.getElementById('conf-room').value='${escHtml(name)}'">${escHtml(name)}</a></td><td>${r.members.map(escHtml).join(", ")}</td></tr>`;
  }
  html += "</tbody></table>";
  el.innerHTML = html;
}

function confJoin() {
  if (!WS || WS.readyState !== WebSocket.OPEN) {
    toast("Not connected.", "error"); return;
  }
  const room = (document.getElementById("conf-room").value.trim().toUpperCase()) || "CONF";
  WS.send(JSON.stringify({ type: "conference_join", room }));
}

function confLeave() {
  if (WS && WS.readyState === WebSocket.OPEN) {
    WS.send(JSON.stringify({ type: "conference_leave" }));
  }
}

function confSend() {
  const input = document.getElementById("conf-input");
  const text  = input.value.trim();
  if (!text || !WS || WS.readyState !== WebSocket.OPEN) return;
  input.value = "";

  const upper = text.toUpperCase();
  // Handle in-conference / commands from the web input too
  if (upper === "/X" || upper === "/Q" || upper === "/EXIT") {
    confLeave(); return;
  }
  if (upper.startsWith("/J ") || upper.startsWith("/JOIN ")) {
    const room = text.split(/\s+/, 2)[1] || "";
    if (room) { confLeave(); setTimeout(() => { document.getElementById("conf-room").value = room; confJoin(); }, 100); }
    return;
  }
  WS.send(JSON.stringify({ type: "conference_message", text }));
}

function confOnJoined(room, welcome) {
  _confRoom = room;
  document.getElementById("conf-join-btn").style.display  = "none";
  document.getElementById("conf-leave-btn").style.display = "";
  document.getElementById("conf-room-label").textContent  = `Room: ${room}`;
  document.getElementById("conf-chat-card").style.display = "flex";
  document.getElementById("conf-rooms-list").style.display = "none";
  const log = document.getElementById("conf-log");
  log.textContent = "";
  confAppend(welcome);
  document.getElementById("conf-input").focus();
}

function confOnLeft() {
  _confRoom = null;
  document.getElementById("conf-join-btn").style.display  = "";
  document.getElementById("conf-leave-btn").style.display = "none";
  document.getElementById("conf-room-label").textContent  = "";
  document.getElementById("conf-chat-card").style.display = "none";
  document.getElementById("conf-rooms-list").style.display = "";
  loadConfRooms();
}

function confAppend(text) {
  const log  = document.getElementById("conf-log");
  const isSystem = text.startsWith("***");
  const line = document.createElement("div");
  line.style.color = isSystem ? "var(--subtle)" : "";
  line.textContent = text;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function toggleTheme() {
  const html  = document.documentElement;
  const theme = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
  html.setAttribute("data-theme", theme);
  localStorage.setItem("pb_theme", theme);
}
function applyTheme() {
  const saved = localStorage.getItem("pb_theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}
