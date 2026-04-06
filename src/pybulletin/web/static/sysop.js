"use strict";
// pyBulletin system operator console

const API = "";
let SESSION = null;
let WS      = null;
let _msgs   = [];
let _cfg    = {};
let _currentMsgId = null;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  applyTheme();
  const saved = localStorage.getItem("pb_sysop_session");
  if (saved) {
    try {
      SESSION = JSON.parse(saved);
      if (SESSION.privilege !== "sysop") { SESSION = null; throw new Error(); }
      showAuthUI();
      await loadDashboard();
      showView("dashboard");
      connectWS();
      return;
    } catch (_) {
      localStorage.removeItem("pb_sysop_session");
    }
  }
});

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function login() {
  const call   = document.getElementById("login-call").value.trim().toUpperCase();
  const pass   = document.getElementById("login-pass").value;
  const errEl  = document.getElementById("login-error");
  const statEl = document.getElementById("login-status");
  errEl.classList.add("hidden");
  if (!call) { errEl.textContent = "Enter callsign."; errEl.classList.remove("hidden"); return; }
  statEl.textContent = "Signing in…";
  try {
    const data = await apiFetch("/api/auth/login", {
      method: "POST", body: JSON.stringify({ call, password: pass }), auth: false,
    });
    if (data.privilege !== "sysop") throw new Error("System Operator privilege required.");
    SESSION = { token: data.token, call: data.call, privilege: data.privilege };
    localStorage.setItem("pb_sysop_session", JSON.stringify(SESSION));
    statEl.textContent = "Awaiting System Operator login.";
    showAuthUI();
    await loadDashboard();
    showView("dashboard");
    connectWS();
  } catch (e) {
    statEl.textContent = "Awaiting System Operator login.";
    errEl.textContent = e.message || "Login failed.";
    errEl.classList.remove("hidden");
  }
}

function logout() {
  if (SESSION) apiFetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  SESSION = null;
  localStorage.removeItem("pb_sysop_session");
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
  WS = new WebSocket(`${proto}://${location.host}/ws?token=${SESSION.token}`);
  WS.onopen  = () => setWsStatus(true);
  WS.onclose = () => { setWsStatus(false); WS = null; setTimeout(connectWS, 5000); };
  WS.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "new_message") {
        toast(`New message #${msg.id} → ${msg.to}`, "info");
        if (document.getElementById("view-messages").classList.contains("active")) loadMessages();
        if (document.getElementById("view-dashboard").classList.contains("active")) loadDashboard();
      }
    } catch (_) {}
  };
}

function setWsStatus(on) {
  document.getElementById("ws-dot").className = "dot " + (on ? "dot-green" : "dot-gray");
  document.getElementById("ws-label").textContent = on ? "live" : "offline";
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
  const loaders = {
    dashboard: loadDashboard, messages: loadMessages,
    users: loadUsers, neighbors: loadNeighbors, config: loadConfig,
  };
  if (loaders[name]) loaders[name]();
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
async function loadDashboard() {
  if (!SESSION) return;
  try {
    const s = await apiFetch("/api/stats");
    const m = s.messages;
    document.getElementById("s-msgs").textContent   = m.total;
    document.getElementById("s-new-p").textContent  = m.new_private;
    document.getElementById("s-new-b").textContent  = m.new_bulletin;
    document.getElementById("s-new-t").textContent  = m.new_nts;
    document.getElementById("s-users").textContent  = s.users;
    document.getElementById("s-wp").textContent     = s.wp_entries;
    document.getElementById("dash-node-info").innerHTML = `
      <tr><th>Node</th><td class="mono">${escHtml(s.node)}</td></tr>
      <tr><th>QTH</th><td>${escHtml(s.qth)}</td></tr>
      <tr><th>Version</th><td class="mono">${escHtml(s.version)}</td></tr>`;

    // Neighbors
    const nbrTbody = document.getElementById("dash-nbr-tbody");
    if (s.neighbors && s.neighbors.length) {
      nbrTbody.innerHTML = s.neighbors.map(n => {
        const lc = n.last_connect
          ? new Date(n.last_connect * 1000).toLocaleString("en-US",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})
          : "never";
        const st = n.enabled
          ? `<span class="badge badge-N">enabled</span>`
          : `<span class="badge badge-K">disabled</span>`;
        return `<tr><td class="call">${escHtml(n.call)}</td><td>${st}</td>
          <td class="mono">${n.msgs_sent}</td><td class="mono">${n.msgs_received}</td>
          <td class="dim">${lc}</td></tr>`;
      }).join("");
    } else {
      nbrTbody.innerHTML = `<tr><td colspan="5" class="empty-state">No neighbors configured.</td></tr>`;
    }

    // Recent messages
    const msgTbody = document.getElementById("dash-msgs-tbody");
    if (s.recent_messages && s.recent_messages.length) {
      msgTbody.innerHTML = s.recent_messages.map(msg => {
        const subj = escHtml(msg.subject.length > 30 ? msg.subject.slice(0,30)+"…" : msg.subject);
        return `<tr onclick="showView('messages');setTimeout(()=>readMessage(${msg.id}),300)" style="cursor:pointer">
          <td class="mono">${msg.id}</td>
          <td>${typeBadge(msg.type)}</td>
          <td class="call">${escHtml(msg.to_call)}</td>
          <td class="call">${escHtml(msg.from_call)}</td>
          <td>${subj}</td></tr>`;
      }).join("");
    } else {
      msgTbody.innerHTML = `<tr><td colspan="5" class="empty-state">No messages.</td></tr>`;
    }

    // Recent logins
    const loginTbody = document.getElementById("dash-logins-tbody");
    if (s.recent_logins && s.recent_logins.length) {
      loginTbody.innerHTML = s.recent_logins.map(u => {
        const when = new Date(u.last_login * 1000).toLocaleString("en-US",
          {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
        return `<tr><td class="call">${escHtml(u.call)}</td>
          <td class="dim">${when}</td>
          <td class="dim mono" style="font-size:.7rem">${escHtml(u.peer||"")}</td></tr>`;
      }).join("");
    } else {
      loginTbody.innerHTML = `<tr><td colspan="3" class="empty-state">No logins recorded.</td></tr>`;
    }
  } catch (e) { toast("Dashboard load failed: " + e.message, "error"); }
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------
async function loadMessages() {
  if (!SESSION) return;
  const type   = document.getElementById("msg-type")?.value   || "";
  const status = document.getElementById("msg-status")?.value || "";
  let url = `/api/messages?limit=300`;
  if (type)   url += `&type=${type}`;
  if (status) url += `&status=${status}`;
  try {
    _msgs = await apiFetch(url);
    renderMessages(_msgs);
  } catch (e) { toast(e.message, "error"); }
}

function filterMessages() {
  const q = document.getElementById("msg-search").value.toLowerCase();
  renderMessages(q ? _msgs.filter(m =>
    m.subject.toLowerCase().includes(q) || m.from_call.toLowerCase().includes(q) ||
    m.to_call.toLowerCase().includes(q)) : _msgs);
}

function renderMessages(msgs) {
  const tbody = document.getElementById("msg-tbody");
  if (!msgs.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No messages.</td></tr>`;
    return;
  }
  tbody.innerHTML = msgs.map(m => {
    const date = new Date(m.created_at*1000).toLocaleDateString("en-US",{month:"short",day:"numeric"});
    const subj = escHtml(m.subject.length>40 ? m.subject.slice(0,40)+"…" : m.subject);
    const heldBtn = m.status === "H"
      ? `<button class="btn btn-success btn-sm" onclick="releaseMsg(${m.id})">Release</button>`
      : `<button class="btn btn-ghost btn-sm" onclick="holdMsg(${m.id})">Hold</button>`;
    return `<tr onclick="readMessage(${m.id})" style="cursor:pointer">
      <td class="mono">${m.id}</td>
      <td>${typeBadge(m.type)}</td>
      <td>${statusBadge(m.status)}</td>
      <td class="call">${escHtml(m.to_call)}</td>
      <td class="call">${escHtml(m.from_call)}</td>
      <td class="dim">${date}</td>
      <td>${subj}</td>
      <td class="dim mono">${m.size}</td>
      <td style="white-space:nowrap" onclick="event.stopPropagation()">
        ${heldBtn}
        <button class="btn btn-danger btn-sm" onclick="killMsg(${m.id})">Kill</button>
      </td>
    </tr>`;
  }).join("");
}

async function readMessage(id) {
  try {
    const msg = await apiFetch(`/api/messages/${id}`);
    _currentMsgId = id;
    document.getElementById("read-title").textContent = `Message #${id}`;
    const d = new Date(msg.created_at * 1000).toUTCString();
    let editNote = "";
    if (msg.edited_by) {
      const ed = msg.edited_at ? new Date(msg.edited_at * 1000).toUTCString() : "?";
      editNote = `<br><em style="color:var(--warn)">Edited by ${escHtml(msg.edited_by)} on ${ed}</em>`;
    }
    document.getElementById("read-header").innerHTML =
      `<strong>From   :</strong> ${escHtml(msg.from_call)}<br>` +
      `<strong>To     :</strong> ${escHtml(msg.to_call)}` +
        (msg.at_bbs ? ` @ ${escHtml(msg.at_bbs)}` : "") + `<br>` +
      `<strong>Date   :</strong> ${d}<br>` +
      `<strong>Subject:</strong> ${escHtml(msg.subject)}<br>` +
      `<strong>BID    :</strong> ${escHtml(msg.bid)}&nbsp;&nbsp;` +
      `<strong>Size   :</strong> ${msg.size} bytes&nbsp;&nbsp;` +
      `<strong>Status :</strong> ${escHtml(msg.status)}` +
      editNote;
    document.getElementById("read-body").textContent = msg.body;
    // Pre-populate edit form fields
    document.getElementById("edit-subject").value = msg.subject;
    document.getElementById("edit-body").value = msg.body;
    document.getElementById("msg-edit-form").style.display = "none";
    document.getElementById("msg-read-card").style.display = "";
    document.getElementById("msg-read-card").scrollIntoView({ behavior: "smooth" });
    document.getElementById("read-kill-btn").style.display = msg.status === "K" ? "none" : "";
  } catch (e) { toast("Failed to load message: " + e.message, "error"); }
}

function closeMsgRead() {
  document.getElementById("msg-read-card").style.display = "none";
  document.getElementById("msg-edit-form").style.display = "none";
  _currentMsgId = null;
}

function toggleMsgEdit() {
  const form = document.getElementById("msg-edit-form");
  const body = document.getElementById("read-body");
  const visible = form.style.display !== "none";
  form.style.display = visible ? "none" : "";
  body.style.display = visible ? "" : "none";
  if (!visible) document.getElementById("edit-subject").focus();
}

async function saveEditMsg() {
  if (!_currentMsgId) return;
  const subject = document.getElementById("edit-subject").value.trim();
  const body    = document.getElementById("edit-body").value;
  if (!subject) { toast("Subject is required.", "error"); return; }
  try {
    await apiFetch(`/api/messages/${_currentMsgId}`, {
      method: "PUT", body: JSON.stringify({ subject, body }),
    });
    toast("Message updated.", "success");
    await readMessage(_currentMsgId);  // reload to show edit note
    document.getElementById("msg-edit-form").style.display = "none";
    document.getElementById("read-body").style.display = "";
    loadMessages();
  } catch (e) { toast("Edit failed: " + e.message, "error"); }
}

function replyToMsg() {
  const hdr = document.getElementById("read-header").innerText;
  const fromM = hdr.match(/From\s*:\s*(\S+)/);
  const subjM = hdr.match(/Subject\s*:\s*(.+)/);
  if (fromM) document.getElementById("cmp-to").value = fromM[1].trim();
  if (subjM) {
    const s = subjM[1].trim();
    document.getElementById("cmp-subject").value = s.startsWith("Re:") ? s : "Re: " + s;
  }
  document.getElementById("cmp-type").value = "P";
  closeMsgRead();
  document.getElementById("msg-compose-card").style.display = "";
  document.getElementById("cmp-to").focus();
}

async function killCurrentMsg() {
  if (!_currentMsgId || !confirm(`Kill message #${_currentMsgId}?`)) return;
  await killMsg(_currentMsgId);
  closeMsgRead();
}

function toggleMsgCompose() {
  const card = document.getElementById("msg-compose-card");
  const visible = card.style.display !== "none";
  card.style.display = visible ? "none" : "";
  if (!visible) document.getElementById("cmp-to").focus();
}

async function sendMessage() {
  const to      = document.getElementById("cmp-to").value.trim().toUpperCase();
  const at_bbs  = document.getElementById("cmp-at").value.trim().toUpperCase();
  const subject = document.getElementById("cmp-subject").value.trim();
  const body    = document.getElementById("cmp-body").value;
  const type    = document.getElementById("cmp-type").value;
  if (!to || !subject) { toast("To and Subject are required.", "error"); return; }
  try {
    const r = await apiFetch("/api/messages", {
      method: "POST", body: JSON.stringify({ to, at_bbs, subject, body, type }),
    });
    toast(`Message #${r.id} sent.`, "success");
    document.getElementById("cmp-to").value = "";
    document.getElementById("cmp-at").value = "";
    document.getElementById("cmp-subject").value = "";
    document.getElementById("cmp-body").value = "";
    toggleMsgCompose();
    loadMessages();
  } catch (e) { toast("Send failed: " + e.message, "error"); }
}

async function holdMsg(id) {
  try {
    await apiFetch(`/api/messages/${id}/hold`, { method: "POST" });
    toast(`Message #${id} held.`, "success");
    loadMessages();
  } catch (e) { toast(e.message, "error"); }
}

async function releaseMsg(id) {
  try {
    await apiFetch(`/api/messages/${id}/release`, { method: "POST" });
    toast(`Message #${id} released.`, "success");
    loadMessages();
  } catch (e) { toast(e.message, "error"); }
}

async function killMsg(id) {
  if (!confirm(`Kill message #${id}?`)) return;
  try {
    await apiFetch(`/api/messages/${id}`, { method: "DELETE" });
    toast(`Message #${id} killed.`, "success");
    _msgs = _msgs.filter(m => m.id !== id);
    renderMessages(_msgs);
  } catch (e) { toast(e.message, "error"); }
}

// ---------------------------------------------------------------------------
// Users
// ---------------------------------------------------------------------------
async function loadUsers() {
  if (!SESSION) return;
  const search = document.getElementById("user-search")?.value || "";
  const priv   = document.getElementById("user-priv")?.value   || "";
  let url = `/api/users?limit=500`;
  if (search) url += `&search=${encodeURIComponent(search)}`;
  if (priv)   url += `&privilege=${encodeURIComponent(priv)}`;
  try {
    const users = await apiFetch(url);
    renderUsers(users);
  } catch (e) { toast(e.message, "error"); }
}

function renderUsers(users) {
  const tbody = document.getElementById("user-tbody");
  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No users found.</td></tr>`;
    return;
  }
  tbody.innerHTML = users.map(u => {
    const ll   = u.last_login_at ? new Date(u.last_login_at*1000).toLocaleDateString() : "never";
    const priv = u.privilege || "";
    const call = escHtml(u.call);
    const privSelect = `<select style="width:auto;padding:3px 28px 3px 8px;font-size:var(--text-xs);border-radius:6px"
        onchange="setPrivilege('${call}',this.value)">
      <option value="sysop" ${priv==="sysop"?"selected":""}>System Operator</option>
      <option value="user"  ${priv==="user" ?"selected":""}>User</option>
      <option value=""      ${priv===""     ?"selected":""}>Guest</option>
    </select>`;
    return `<tr>
      <td class="call">${call}</td>
      <td>${escHtml(u.display_name||"—")}</td>
      <td>${privSelect}</td>
      <td class="mono dim">${escHtml(u.home_bbs||"—")}</td>
      <td class="dim">${ll}</td>
      <td class="mono dim">${escHtml(u.last_login_peer||"—")}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="promptResetPw('${call}')">Reset Password</button>
        <button class="btn btn-danger btn-sm" onclick="deleteUser('${call}')">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

function toggleCreateUser() {
  const card = document.getElementById("create-user-card");
  const visible = card.style.display !== "none";
  card.style.display = visible ? "none" : "";
  if (!visible) document.getElementById("new-call").focus();
}

async function createUser() {
  const call = document.getElementById("new-call").value.trim().toUpperCase();
  const pass = document.getElementById("new-pass").value;
  const priv = document.getElementById("new-priv").value;
  if (!call) { toast("Enter a callsign.", "error"); return; }
  if (pass.length < 6) { toast("Password must be at least 6 characters.", "error"); return; }
  try {
    await apiFetch("/api/users", {
      method: "POST", body: JSON.stringify({ call, password: pass, privilege: priv }),
    });
    toast(`User ${call} created.`, "success");
    document.getElementById("new-call").value = "";
    document.getElementById("new-pass").value = "";
    toggleCreateUser();
    loadUsers();
  } catch (e) { toast(e.message, "error"); }
}

async function setPrivilege(call, priv) {
  if (priv === undefined) return;
  try {
    await apiFetch(`/api/users/${call}/privilege`, {
      method: "POST", body: JSON.stringify({ privilege: priv }),
    });
    toast(`${call} privilege → ${priv||"guest"}.`, "success");
    loadUsers();
  } catch (e) { toast(e.message, "error"); }
}

async function promptResetPw(call) {
  const pw = prompt(`New password for ${call} (min 6 chars):`);
  if (pw === null) return;
  if (pw.length < 6) { toast("Password must be at least 6 characters.", "error"); return; }
  try {
    await apiFetch(`/api/users/${call}/password`, {
      method: "POST", body: JSON.stringify({ password: pw }),
    });
    toast(`Password for ${call} reset.`, "success");
  } catch (e) { toast(e.message, "error"); }
}

async function deleteUser(call) {
  if (!confirm(`Delete user ${call}? This cannot be undone.`)) return;
  try {
    await apiFetch(`/api/users/${call}`, { method: "DELETE" });
    toast(`User ${call} deleted.`, "success");
    loadUsers();
  } catch (e) { toast(e.message, "error"); }
}

// ---------------------------------------------------------------------------
// Neighbors
// ---------------------------------------------------------------------------
async function loadNeighbors() {
  if (!SESSION) return;
  try {
    const nbrs = await apiFetch("/api/neighbors");
    renderNeighbors(nbrs);
  } catch (e) { toast(e.message, "error"); }
}

function renderNeighbors(nbrs) {
  const tbody = document.getElementById("nbr-tbody");
  if (!nbrs.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">No neighbors configured.</td></tr>`;
    return;
  }
  tbody.innerHTML = nbrs.map(n => {
    const lc  = n.last_connect_at ? new Date(n.last_connect_at*1000).toLocaleString() : "—";
    const dot = n.session_active ? "dot-green" : n.enabled ? "dot-gray" : "dot-red";
    const status = n.session_active ? "active" : n.enabled ? "idle" : "disabled";
    const cats = Array.isArray(n.categories) ? n.categories.join(" ") : "—";
    const call = escHtml(n.call);
    return `<tr>
      <td class="call">${call}</td>
      <td class="mono dim">${escHtml(n.address||"—")}</td>
      <td class="mono dim">${escHtml(n.protocol||"—")}</td>
      <td class="mono dim" style="font-size:.7rem">${escHtml(n.schedule||"—")}</td>
      <td class="mono dim">${escHtml(cats)}</td>
      <td><span class="dot ${dot}"></span> ${status}</td>
      <td class="mono">${n.msgs_sent}</td>
      <td class="mono">${n.msgs_received}</td>
      <td class="dim">${lc}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="editNeighbor(${JSON.stringify(n).replace(/"/g,'&quot;')})">Edit</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleNeighbor('${call}',${!n.enabled})">${n.enabled?'Disable':'Enable'}</button>
        <button class="btn btn-danger btn-sm" onclick="deleteNeighbor('${call}')">Delete</button>
      </td>
    </tr>`;
  }).join("");
}

function toggleAddNeighbor() {
  const card = document.getElementById("nbr-form-card");
  const visible = card.style.display !== "none";
  if (visible) {
    card.style.display = "none";
  } else {
    // Reset to add mode
    document.getElementById("nbr-form-title").textContent = "Add Neighbor";
    document.getElementById("nbr-save-btn").textContent = "Add";
    document.getElementById("nbr-editing-call").value = "";
    document.getElementById("nbr-call").value = "";
    document.getElementById("nbr-call").disabled = false;
    document.getElementById("nbr-address").value = "";
    document.getElementById("nbr-protocol").value = "b2";
    document.getElementById("nbr-schedule").value = "0 */2 * * *";
    document.getElementById("nbr-categories").value = "WW";
    document.getElementById("nbr-binmode").value = "true";
    document.getElementById("nbr-enabled").value = "true";
    card.style.display = "";
    document.getElementById("nbr-call").focus();
  }
}

function editNeighbor(n) {
  document.getElementById("nbr-form-title").textContent = "Edit Neighbor";
  document.getElementById("nbr-save-btn").textContent = "Save";
  document.getElementById("nbr-editing-call").value = n.call;
  document.getElementById("nbr-call").value = n.call;
  document.getElementById("nbr-call").disabled = true;
  document.getElementById("nbr-address").value = n.address || "";
  document.getElementById("nbr-protocol").value = n.protocol || "b2";
  document.getElementById("nbr-schedule").value = n.schedule || "0 */2 * * *";
  document.getElementById("nbr-categories").value = Array.isArray(n.categories) ? n.categories.join(" ") : "";
  document.getElementById("nbr-binmode").value = n.bin_mode ? "true" : "false";
  document.getElementById("nbr-enabled").value = n.enabled ? "true" : "false";
  document.getElementById("nbr-form-card").style.display = "";
  document.getElementById("nbr-form-card").scrollIntoView({ behavior: "smooth" });
}

async function saveNeighbor() {
  const editingCall = document.getElementById("nbr-editing-call").value;
  const call      = document.getElementById("nbr-call").value.trim().toUpperCase();
  const address   = document.getElementById("nbr-address").value.trim();
  const protocol  = document.getElementById("nbr-protocol").value;
  const schedule  = document.getElementById("nbr-schedule").value.trim();
  const catStr    = document.getElementById("nbr-categories").value.trim().toUpperCase();
  const categories= catStr ? catStr.split(/\s+/) : ["WW"];
  const bin_mode  = document.getElementById("nbr-binmode").value === "true";
  const enabled   = document.getElementById("nbr-enabled").value === "true";

  if (!call) { toast("Callsign required.", "error"); return; }
  if (!address) { toast("Address required.", "error"); return; }

  const body = { address, protocol, schedule, categories, bin_mode, enabled };

  try {
    if (editingCall) {
      await apiFetch(`/api/neighbors/${editingCall}`, {
        method: "PUT", body: JSON.stringify(body),
      });
      toast(`Neighbor ${editingCall} updated.`, "success");
    } else {
      await apiFetch("/api/neighbors", {
        method: "POST", body: JSON.stringify({ call, ...body }),
      });
      toast(`Neighbor ${call} added.`, "success");
    }
    toggleAddNeighbor();
    loadNeighbors();
  } catch (e) { toast(e.message, "error"); }
}

async function toggleNeighbor(call, enabled) {
  try {
    await apiFetch(`/api/neighbors/${call}`, {
      method: "PUT", body: JSON.stringify({ enabled }),
    });
    toast(`${call} ${enabled ? "enabled" : "disabled"}.`, "success");
    loadNeighbors();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteNeighbor(call) {
  if (!confirm(`Remove neighbor ${call}? This will delete it from the config.`)) return;
  try {
    await apiFetch(`/api/neighbors/${call}`, { method: "DELETE" });
    toast(`Neighbor ${call} removed.`, "success");
    loadNeighbors();
  } catch (e) { toast(e.message, "error"); }
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
async function loadConfig() {
  if (!SESSION) return;
  try {
    _cfg = await apiFetch("/api/config");
    const _FIELDS = [
      ["node_call",    "Node Callsign",    "text",     true],
      ["node_alias",   "Node Alias",       "text",     true],
      ["owner_name",   "Owner Name",       "text",     false],
      ["qth",          "QTH",              "text",     false],
      ["node_locator", "Locator (QRA)",    "text",     false],
      ["branding_name","Branding Name",    "text",     false],
      ["motd",         "MOTD",             "textarea", false],
      ["welcome_title","Welcome Title",    "text",     false],
      ["welcome_body", "Welcome Body",     "textarea", false],
      ["login_tip",    "Login Tip",        "text",     false],
    ];
    const form = document.getElementById("config-form");
    form.innerHTML = _FIELDS.map(([key, label, type, ro]) => {
      const val = escHtml(_cfg[key] || "");
      if (type === "textarea") {
        return `<div class="form-row"><label>${label}</label>
          <textarea id="cfg-${key}" rows="3">${val}</textarea></div>`;
      }
      return `<div class="form-row"><label>${label}</label>
        <input type="text" id="cfg-${key}" value="${val}" ${ro?"readonly style='opacity:.6'":""}></div>`;
    }).join("");
  } catch (e) { toast(e.message, "error"); }
}

async function saveConfig() {
  const _EDITABLE = [
    "owner_name","qth","node_locator","branding_name",
    "motd","welcome_title","welcome_body","login_tip",
  ];
  const body = {};
  for (const k of _EDITABLE) {
    const el = document.getElementById("cfg-" + k);
    if (el) body[k] = el.value;
  }
  try {
    await apiFetch("/api/config", { method: "POST", body: JSON.stringify(body) });
    toast("Configuration saved.", "success");
  } catch (e) { toast(e.message, "error"); }
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------
async function apiFetch(url, opts = {}) {
  const headers = { "content-type": "application/json" };
  if (opts.auth !== false && SESSION?.token) {
    headers["authorization"] = "Bearer " + SESSION.token;
  }
  const resp = await fetch(API + url, {
    method: opts.method || "GET",
    headers: { ...headers, ...(opts.headers || {}) },
    body: opts.body,
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

function typeBadge(t)   { return `<span class="badge badge-${t}">${t}</span>`; }
function statusBadge(s) { return `<span class="badge badge-${s}">${s}</span>`; }

function escHtml(s) {
  return String(s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function toast(msg, type = "info") {
  const area = document.getElementById("toast-area");
  const el   = document.createElement("div");
  el.className = "toast " + (type==="error"?"error":type==="success"?"success":"");
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 4000);
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
