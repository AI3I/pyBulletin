"""pyBulletin web application — HTTP routes, REST API, WebSocket hub.

Mounts at serve-web.  All handlers are async functions that receive an
HTTPRequest and return an HTTPResponse (or None → 404).

Public endpoints (no auth required):
  GET  /                          → index.html (public SPA)
  GET  /sysop                     → sysop.html (sysop console)
  GET  /api/health                → node health JSON
  POST /api/auth/login            → { call, password } → session token
  POST /api/auth/logout           → revoke session
  GET  /ws                        → WebSocket (auth required)

Authenticated endpoints:
  GET  /api/messages              → list messages (query: type, status, since, limit)
  GET  /api/messages/{id}         → read message
  POST /api/messages              → send message
  DELETE /api/messages/{id}       → kill message

System-operator-only endpoints:
  GET  /api/users                    → list users (query: search, privilege)
  POST /api/users                    → create user { call, password, privilege }
  GET  /api/users/{call}             → get user
  POST /api/users/{call}/privilege   → set privilege
  POST /api/users/{call}/password    → reset password { password }
  DELETE /api/users/{call}           → delete user
  POST /api/messages/{id}/hold       → hold message
  POST /api/messages/{id}/release    → release message
  GET  /api/neighbors                → list neighbors (config + runtime stats)
  POST /api/neighbors                → add neighbor
  PUT  /api/neighbors/{call}         → update neighbor config
  DELETE /api/neighbors/{call}       → remove neighbor
  GET  /api/config                   → get editable config
  POST /api/config                   → update config
  GET  /api/wp                       → white pages (query: call)
  GET  /api/stats                    → extended node stats
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..auth import verify_password
from ..store.models import (
    Message, MSG_PRIVATE, MSG_BULLETIN, MSG_NTS,
    STATUS_NEW, STATUS_KILLED, PRIV_SYSOP, PRIV_USER,
)
from .server import HTTPRequest, HTTPResponse, WebSocket
from .auth import SessionStore

if TYPE_CHECKING:
    from ..config import AppConfig
    from ..store.store import BBSStore

LOG = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


class WebApp:
    """Request router and WebSocket hub for the pyBulletin web interface."""

    def __init__(self, cfg: AppConfig, store: BBSStore, conference_hub=None) -> None:
        self._cfg         = cfg
        self._store       = store
        self._conf_hub    = conference_hub
        self._sessions    = SessionStore()
        self._ws_clients: set[WebSocket] = set()
        self._ws_lock     = asyncio.Lock()

    def start(self) -> None:
        self._sessions.start()

    def stop(self) -> None:
        self._sessions.stop()

    # ------------------------------------------------------------------
    # Main request dispatcher
    # ------------------------------------------------------------------

    async def handle_request(self, req: HTTPRequest) -> HTTPResponse | None:
        p = req.path.rstrip("/") or "/"
        m = req.method

        # --- Auth ---
        if m == "POST" and p == "/api/auth/login":
            return await self._login(req)
        if m == "POST" and p == "/api/auth/logout":
            return await self._logout(req)

        # --- Public ---
        if m == "GET" and p == "/api/health":
            return await self._health(req)

        # --- Root / sysop SPA redirects ---
        if m == "GET" and p in ("/", ""):
            return self._static("index.html")
        if m == "GET" and p in ("/sysop", "/sysop/"):
            return self._static("sysop.html")

        # --- Auth-required ---
        sess = await self._sessions.get_from_request(req)

        # Messages
        if m == "GET" and p == "/api/messages":
            return await self._list_messages(req, sess)
        if m == "POST" and p == "/api/messages":
            return await self._send_message(req, sess)
        msg_m = re.fullmatch(r"/api/messages/(\d+)", p)
        if msg_m:
            msg_id = int(msg_m.group(1))
            if m == "GET":
                return await self._read_message(req, sess, msg_id)
            if m == "PUT":
                return await self._edit_message(req, sess, msg_id)
            if m == "DELETE":
                return await self._kill_message(req, sess, msg_id)
        hold_m = re.fullmatch(r"/api/messages/(\d+)/(hold|release)", p)
        if hold_m and m == "POST":
            return await self._hold_release(req, sess, int(hold_m.group(1)), hold_m.group(2))

        # Users (sysop)
        if m == "GET" and p == "/api/users":
            return await self._list_users(req, sess)
        if m == "POST" and p == "/api/users":
            return await self._create_user(req, sess)
        user_m = re.fullmatch(r"/api/users/([A-Z0-9\-]+)", p)
        if user_m:
            call = user_m.group(1)
            if m == "GET":
                return await self._get_user(req, sess, call)
            if m == "DELETE":
                return await self._delete_user(req, sess, call)
        priv_m = re.fullmatch(r"/api/users/([A-Z0-9\-]+)/privilege", p)
        if priv_m and m == "POST":
            return await self._set_privilege(req, sess, priv_m.group(1))
        pw_m = re.fullmatch(r"/api/users/([A-Z0-9\-]+)/password", p)
        if pw_m and m == "POST":
            return await self._reset_password(req, sess, pw_m.group(1))

        # Neighbors
        if m == "GET" and p == "/api/neighbors":
            return await self._list_neighbors(req, sess)
        if m == "POST" and p == "/api/neighbors":
            return await self._add_neighbor(req, sess)
        nbr_m = re.fullmatch(r"/api/neighbors/([A-Za-z0-9\-]+)", p)
        if nbr_m:
            ncall = nbr_m.group(1).upper()
            if m == "PUT":
                return await self._update_neighbor(req, sess, ncall)
            if m == "DELETE":
                return await self._delete_neighbor(req, sess, ncall)

        # Config, wp, stats
        if m == "GET" and p == "/api/wp":
            return await self._wp_lookup(req, sess)
        if m == "GET" and p == "/api/stats":
            return await self._stats(req, sess)
        if m == "GET" and p == "/api/config":
            return await self._get_config(req, sess)
        if m == "POST" and p == "/api/config":
            return await self._set_config(req, sess)
        if m == "GET" and p == "/api/conference":
            return await self._conference_status(req, sess)

        return None

    # ------------------------------------------------------------------
    # WebSocket hub
    # ------------------------------------------------------------------

    async def handle_ws(self, ws: WebSocket, req: HTTPRequest) -> None:
        sess = await self._sessions.get_from_request(req)
        if sess is None:
            await ws.send_json({"type": "error", "message": "Unauthorized"})
            await ws.close(1008)
            return

        async with self._ws_lock:
            self._ws_clients.add(ws)

        conf_room: str | None        = None
        conf_key:  str | None        = None
        drain_task: asyncio.Task | None = None

        async def _drain(queue: asyncio.Queue) -> None:
            while True:
                text = await queue.get()
                text = text.rstrip("\r\n")
                await ws.send_json({"type": "conference_message", "text": text})

        async def _leave_conf() -> None:
            nonlocal conf_room, conf_key, drain_task
            if conf_room and conf_key and self._conf_hub:
                await self._conf_hub.leave_room_ws(conf_room, conf_key)
            if drain_task:
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
            conf_room  = None
            conf_key   = None
            drain_task = None

        async def _join_conf(room_name: str) -> None:
            nonlocal conf_room, conf_key, drain_task
            if not self._conf_hub:
                return
            key, queue, welcome = await self._conf_hub.enter_room_ws(room_name, sess.call)
            conf_room  = room_name
            conf_key   = key
            drain_task = asyncio.create_task(_drain(queue))
            await ws.send_json({"type": "conference_joined",
                                "room": room_name, "welcome": welcome})

        try:
            await ws.send_json({
                "type": "hello",
                "call": sess.call,
                "node": self._cfg.node.node_call,
                "version": __version__,
            })
            while not ws.closed:
                result = await ws.recv()
                if result is None:
                    break
                try:
                    data = json.loads(result[1].decode())
                except Exception:
                    continue
                t = data.get("type")
                if t == "ping":
                    await ws.send_json({"type": "pong"})
                elif t == "conference_join" and self._conf_hub:
                    if conf_room:
                        await _leave_conf()
                    room = data.get("room") or self._conf_hub.DEFAULT_ROOM
                    await _join_conf(room)
                elif t == "conference_message" and conf_room and conf_key and self._conf_hub:
                    text = str(data.get("text", "")).strip()
                    if text:
                        await self._conf_hub.send_from_ws(conf_room, conf_key, text)
                elif t == "conference_leave":
                    await _leave_conf()
                    await ws.send_json({"type": "conference_left"})
        finally:
            await _leave_conf()
            async with self._ws_lock:
                self._ws_clients.discard(ws)

    async def broadcast(self, event: dict) -> None:
        """Broadcast a JSON event to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        dead = set()
        async with self._ws_lock:
            clients = set(self._ws_clients)
        for ws in clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._ws_lock:
                self._ws_clients -= dead

    # ------------------------------------------------------------------
    # Auth endpoints
    # ------------------------------------------------------------------

    async def _login(self, req: HTTPRequest) -> HTTPResponse:
        try:
            body = json.loads(req.body)
            call     = str(body.get("call", "")).strip().upper()
            password = str(body.get("password", ""))
        except (json.JSONDecodeError, AttributeError):
            return HTTPResponse.bad_request("Invalid JSON")

        if not call:
            return HTTPResponse.bad_request("call required")

        user = await self._store.get_user(call)
        if user is None:
            return HTTPResponse(
                status=401,
                headers={"content-type": "application/json"},
                body=json.dumps({"error": "Invalid credentials"}).encode(),
            )

        if user.password_hash and not verify_password(password, user.password_hash):
            return HTTPResponse(
                status=401,
                headers={"content-type": "application/json"},
                body=json.dumps({"error": "Invalid credentials"}).encode(),
            )

        sess = await self._sessions.create(call, user.privilege)
        resp = HTTPResponse.json({
            "token": sess.token,
            "call":  sess.call,
            "privilege": sess.privilege,
            "expires_at": int(sess.expires_at),
        })
        # Also set cookie for browser-based access
        resp.headers["set-cookie"] = (
            f"pb_session={sess.token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={int(sess.expires_at - time.time())}"
        )
        LOG.info("web: login %s from %s", call, req.peer)
        return resp

    async def _logout(self, req: HTTPRequest) -> HTTPResponse:
        token = req.bearer_token() or req.cookie("pb_session")
        if token:
            await self._sessions.revoke(token)
        resp = HTTPResponse.json({"ok": True})
        resp.headers["set-cookie"] = "pb_session=; Path=/; Max-Age=0"
        return resp

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    async def _health(self, req: HTTPRequest) -> HTTPResponse:
        cfg = self._cfg
        return HTTPResponse.json({
            "ok":        True,
            "node":      cfg.node.node_call,
            "version":   __version__,
            "uptime":    int(time.time()),
        })

    def _static(self, filename: str) -> HTTPResponse:
        path = _STATIC_DIR / filename
        if not path.exists():
            return HTTPResponse.not_found()
        import mimetypes
        mime, _ = mimetypes.guess_type(filename)
        return HTTPResponse(
            headers={"content-type": mime or "text/html"},
            body=path.read_bytes(),
        )

    # ------------------------------------------------------------------
    # Message endpoints
    # ------------------------------------------------------------------

    async def _list_messages(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        qp     = req.query_params
        limit  = min(int(qp.get("limit", "100")), 500)
        since  = int(qp.get("since", "0"))
        mtype  = qp.get("type")   # P / B / T
        status = qp.get("status")
        to     = qp.get("to", "").upper() or None
        search = qp.get("search") or None

        msgs = await self._store.list_messages(
            msg_type=mtype,
            status=status,
            since_id=since,
            to_call=to,
            search=search,
            limit=limit,
        )
        return HTTPResponse.json([_msg_summary(m) for m in msgs])

    async def _read_message(self, req: HTTPRequest, sess, msg_id: int) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        msg = await self._store.get_message(msg_id)
        if msg is None:
            return HTTPResponse.not_found()
        if msg.status == STATUS_KILLED and not (sess and sess.is_sysop):
            return HTTPResponse.not_found()
        # Private mail access control
        if msg.msg_type == MSG_PRIVATE and not sess.is_sysop:
            if sess.call not in (msg.to_call, msg.from_call):
                return HTTPResponse.forbidden()
        if msg.msg_type == MSG_PRIVATE and msg.status == STATUS_NEW:
            await self._store.mark_read(msg_id, sess.call)
        return HTTPResponse.json(_msg_detail(msg))

    async def _send_message(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError:
            return HTTPResponse.bad_request("Invalid JSON")

        to_call  = str(body.get("to", "")).strip().upper()
        at_bbs   = str(body.get("at_bbs", "")).strip().upper()
        subject  = str(body.get("subject", "")).strip()
        text     = str(body.get("body", ""))
        msg_type = str(body.get("type", MSG_PRIVATE)).upper()

        if not to_call or not subject:
            return HTTPResponse.bad_request("to and subject required")
        if msg_type not in (MSG_PRIVATE, MSG_BULLETIN, MSG_NTS):
            msg_type = MSG_PRIVATE

        max_bytes = self._cfg.rate_limit.max_message_body_bytes
        if len(text.encode()) > max_bytes:
            return HTTPResponse.bad_request(f"Body exceeds {max_bytes} bytes")

        msg = Message(
            msg_type=msg_type,
            from_call=sess.call,
            to_call=to_call,
            at_bbs=at_bbs,
            subject=subject,
            body=text,
        )
        msg_id = await self._store.insert_message(msg)
        await self.broadcast({"type": "new_message", "id": msg_id, "to": to_call})
        LOG.info("web: %s sent message %d to %s", sess.call, msg_id, to_call)
        return HTTPResponse.json({"id": msg_id}, status=201)

    async def _edit_message(self, req: HTTPRequest, sess, msg_id: int) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        msg = await self._store.get_message(msg_id)
        if msg is None:
            return HTTPResponse.not_found()
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError:
            return HTTPResponse.bad_request("Invalid JSON")
        subject = str(body.get("subject", msg.subject)).strip()
        text    = str(body.get("body",    msg.body))
        if not subject:
            return HTTPResponse.bad_request("subject required")
        from datetime import datetime, timezone
        ok = await self._store.update_message(
            msg_id,
            subject=subject,
            body=text,
            edited_by=sess.call,
            edited_at=datetime.now(timezone.utc),
        )
        if not ok:
            return HTTPResponse.not_found()
        LOG.info("web: sysop %s edited message %d", sess.call, msg_id)
        updated = await self._store.get_message(msg_id)
        return HTTPResponse.json(_msg_detail(updated))

    async def _kill_message(self, req: HTTPRequest, sess, msg_id: int) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        msg = await self._store.get_message(msg_id)
        if msg is None:
            return HTTPResponse.not_found()
        if not sess.is_sysop and sess.call not in (msg.from_call, msg.to_call):
            return HTTPResponse.forbidden()
        await self._store.kill_message(msg_id)
        return HTTPResponse.json({"ok": True})

    async def _hold_release(
        self, req: HTTPRequest, sess, msg_id: int, action: str
    ) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        if action == "hold":
            ok = await self._store.hold_message(msg_id)
        else:
            ok = await self._store.release_message(msg_id)
        return HTTPResponse.json({"ok": ok})

    # ------------------------------------------------------------------
    # User endpoints (sysop)
    # ------------------------------------------------------------------

    async def _list_users(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        qp     = req.query_params
        search = qp.get("search")
        priv   = qp.get("privilege")
        limit  = min(int(qp.get("limit", "200")), 1000)
        users  = await self._store.list_users(search=search, privilege=priv, limit=limit)
        return HTTPResponse.json([_user_summary(u) for u in users])

    async def _get_user(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        if not sess.is_sysop and sess.call != call.upper():
            return HTTPResponse.forbidden()
        user = await self._store.get_user(call)
        if user is None:
            return HTTPResponse.not_found()
        return HTTPResponse.json(_user_detail(user))

    async def _delete_user(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        ok = await self._store.delete_user(call)
        return HTTPResponse.json({"ok": ok})

    async def _set_privilege(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
            priv = str(body.get("privilege", "")).strip()
        except (json.JSONDecodeError, AttributeError):
            return HTTPResponse.bad_request()
        if priv not in ("", PRIV_USER, PRIV_SYSOP):
            return HTTPResponse.bad_request("Invalid privilege level")
        ok = await self._store.set_privilege(call.upper(), priv)
        return HTTPResponse.json({"ok": ok})

    async def _create_user(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
            call     = str(body.get("call", "")).strip().upper()
            password = str(body.get("password", ""))
            privilege = str(body.get("privilege", PRIV_USER)).strip()
        except (json.JSONDecodeError, AttributeError):
            return HTTPResponse.bad_request("Invalid JSON")

        from ..auth import hash_password, is_valid_call
        if not call or not is_valid_call(call):
            return HTTPResponse.bad_request("Invalid callsign")
        if len(password) < 6:
            return HTTPResponse.bad_request("Password must be at least 6 characters")
        if privilege not in ("", PRIV_USER, PRIV_SYSOP):
            privilege = PRIV_USER

        existing = await self._store.get_user(call)
        if existing is not None:
            return HTTPResponse(
                status=409,
                headers={"content-type": "application/json"},
                body=json.dumps({"error": f"User {call} already exists"}).encode(),
            )

        from ..store.models import User as UserModel
        user = UserModel(call=call, privilege=privilege,
                         password_hash=hash_password(password))
        await self._store.upsert_user(user)
        LOG.info("web: sysop %s created user %s (priv=%s)", sess.call, call, privilege)
        return HTTPResponse.json({"call": call}, status=201)

    async def _reset_password(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
            password = str(body.get("password", ""))
        except (json.JSONDecodeError, AttributeError):
            return HTTPResponse.bad_request("Invalid JSON")

        if len(password) < 6:
            return HTTPResponse.bad_request("Password must be at least 6 characters")

        user = await self._store.get_user(call.upper())
        if user is None:
            return HTTPResponse.not_found()

        from ..auth import hash_password
        user.password_hash = hash_password(password)
        await self._store.upsert_user(user)
        LOG.info("web: sysop %s reset password for %s", sess.call, call)
        return HTTPResponse.json({"ok": True})

    # ------------------------------------------------------------------
    # Neighbor / config / wp / stats
    # ------------------------------------------------------------------

    async def _list_neighbors(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        stats_list = await self._store.list_neighbors()
        stats_map  = {n.call: n for n in stats_list}
        result = []
        for cfg in self._cfg.forward.neighbors:
            st = stats_map.get(cfg.call)
            result.append({
                # Config fields
                "call":       cfg.call,
                "address":    cfg.address,
                "protocol":   cfg.protocol,
                "schedule":   cfg.schedule,
                "categories": cfg.categories,
                "bin_mode":   cfg.bin_mode,
                "enabled":    cfg.enabled,
                # Runtime stats
                "session_active":  st.session_active  if st else False,
                "msgs_sent":       st.msgs_sent        if st else 0,
                "msgs_received":   st.msgs_received    if st else 0,
                "last_connect_at": int(st.last_connect_at.timestamp()) if st and st.last_connect_at else None,
                "last_success_at": int(st.last_success_at.timestamp()) if st and st.last_success_at else None,
            })
        return HTTPResponse.json(result)

    def _save_config_async(self) -> None:
        from ..config import save_config
        import threading
        threading.Thread(
            target=save_config, args=(self._cfg, "config/pybulletin.toml"),
            daemon=True,
        ).start()

    async def _add_neighbor(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError:
            return HTTPResponse.bad_request("Invalid JSON")
        call = str(body.get("call", "")).strip().upper()
        if not call:
            return HTTPResponse.bad_request("call required")
        if any(n.call == call for n in self._cfg.forward.neighbors):
            return HTTPResponse(status=409,
                headers={"content-type": "application/json"},
                body=json.dumps({"error": f"Neighbor {call} already exists"}).encode())
        from ..config import ForwardNeighborConfig
        n = ForwardNeighborConfig()
        object.__setattr__(n, "call", call)
        for field in ("address", "protocol", "schedule"):
            if field in body:
                object.__setattr__(n, field, str(body[field]))
        if "categories" in body:
            object.__setattr__(n, "categories", [str(x).upper() for x in body["categories"]])
        if "bin_mode" in body:
            object.__setattr__(n, "bin_mode", bool(body["bin_mode"]))
        if "enabled" in body:
            object.__setattr__(n, "enabled", bool(body["enabled"]))
        self._cfg.forward.neighbors.append(n)
        self._save_config_async()
        LOG.info("web: %s added neighbor %s", sess.call, call)
        return HTTPResponse.json({"call": call}, status=201)

    async def _update_neighbor(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        n = next((x for x in self._cfg.forward.neighbors if x.call == call), None)
        if n is None:
            return HTTPResponse.not_found()
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError:
            return HTTPResponse.bad_request("Invalid JSON")
        for field in ("address", "protocol", "schedule"):
            if field in body:
                object.__setattr__(n, field, str(body[field]))
        if "categories" in body:
            object.__setattr__(n, "categories", [str(x).upper() for x in body["categories"]])
        if "bin_mode" in body:
            object.__setattr__(n, "bin_mode", bool(body["bin_mode"]))
        if "enabled" in body:
            object.__setattr__(n, "enabled", bool(body["enabled"]))
        self._save_config_async()
        LOG.info("web: %s updated neighbor %s", sess.call, call)
        return HTTPResponse.json({"ok": True})

    async def _delete_neighbor(self, req: HTTPRequest, sess, call: str) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        before = self._cfg.forward.neighbors
        after  = [n for n in before if n.call != call]
        if len(after) == len(before):
            return HTTPResponse.not_found()
        object.__setattr__(self._cfg.forward, "neighbors", after)
        self._save_config_async()
        LOG.info("web: %s deleted neighbor %s", sess.call, call)
        return HTTPResponse.json({"ok": True})

    async def _wp_lookup(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        call = req.query_params.get("call", "").strip().upper()
        if not call:
            count = await self._store.count_wp_entries()
            return HTTPResponse.json({"count": count})
        entry = await self._store.get_wp_entry(call)
        if entry is None:
            return HTTPResponse.not_found(f"No WP entry for {call}")
        return HTTPResponse.json({
            "call":      entry.call,
            "home_bbs":  entry.home_bbs,
            "name":      entry.name,
            "source_bbs": entry.source_bbs,
            "updated_at": int(entry.updated_at.timestamp()),
        })

    async def _stats(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None:
            return HTTPResponse.forbidden()
        total   = await self._store.count_messages()
        new_p   = await self._store.count_messages(msg_type=MSG_PRIVATE,  status=STATUS_NEW)
        new_b   = await self._store.count_messages(msg_type=MSG_BULLETIN, status=STATUS_NEW)
        new_t   = await self._store.count_messages(msg_type=MSG_NTS,      status=STATUS_NEW)
        users   = len(await self._store.list_users())
        wp      = await self._store.count_wp_entries()
        cfg     = self._cfg

        # Recent messages (newest 10)
        recent_msgs = await self._store.list_messages(limit=10, reverse=True)
        recent_msgs_out = [_msg_summary(m) for m in reversed(recent_msgs)]

        # Recent logins (last 10 users by last_login_at)
        all_users = await self._store.list_users(limit=200)
        logged_in = [u for u in all_users if u.last_login_at]
        logged_in.sort(key=lambda u: u.last_login_at, reverse=True)
        recent_logins = [
            {
                "call":       u.call,
                "last_login": int(u.last_login_at.timestamp()),
                "peer":       u.last_login_peer,
            }
            for u in logged_in[:10]
        ]

        # Neighbor status from configured neighbors merged with runtime state
        nbr_cfg  = cfg.forward.neighbors
        nbr_rt   = {n.call: n for n in await self._store.list_neighbors()}
        neighbors_out = []
        for nc in nbr_cfg:
            rt = nbr_rt.get(nc.call)
            neighbors_out.append({
                "call":           nc.call,
                "enabled":        nc.enabled,
                "last_connect":   int(rt.last_connect_at.timestamp()) if rt and rt.last_connect_at else None,
                "msgs_sent":      rt.msgs_sent     if rt else 0,
                "msgs_received":  rt.msgs_received if rt else 0,
            })

        return HTTPResponse.json({
            "node":          cfg.node.node_call,
            "qth":           cfg.node.qth,
            "version":       __version__,
            "messages":      {"total": total, "new_private": new_p,
                              "new_bulletin": new_b, "new_nts": new_t},
            "users":         users,
            "wp_entries":    wp,
            "recent_messages": recent_msgs_out,
            "recent_logins":   recent_logins,
            "neighbors":       neighbors_out,
        })

    async def _conference_status(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        if self._conf_hub is None:
            return HTTPResponse.json({"available": False, "rooms": {}})
        snapshot = self._conf_hub.rooms_snapshot()
        rooms_out = {name: {"members": members, "count": len(members)}
                     for name, members in snapshot.items()}
        return HTTPResponse.json({"available": True, "rooms": rooms_out})

    async def _get_config(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        cfg = self._cfg.node
        return HTTPResponse.json({
            "node_call":    cfg.node_call,
            "node_alias":   cfg.node_alias,
            "owner_name":   cfg.owner_name,
            "qth":          cfg.qth,
            "node_locator": cfg.node_locator,
            "motd":         cfg.motd,
            "branding_name":cfg.branding_name,
            "welcome_title":cfg.welcome_title,
            "welcome_body": cfg.welcome_body,
            "login_tip":    cfg.login_tip,
            "require_password": self._cfg.node.require_password,
        })

    async def _set_config(self, req: HTTPRequest, sess) -> HTTPResponse:
        if sess is None or not sess.is_sysop:
            return HTTPResponse.forbidden()
        try:
            body = json.loads(req.body)
        except json.JSONDecodeError:
            return HTTPResponse.bad_request()
        # Only allow updating safe presentation fields
        _ALLOWED = {
            "motd", "welcome_title", "welcome_body", "login_tip",
            "owner_name", "qth", "node_locator", "branding_name",
        }
        cfg = self._cfg.node
        for k, v in body.items():
            if k in _ALLOWED:
                object.__setattr__(cfg, k, str(v))
        self._save_config_async()
        return HTTPResponse.json({"ok": True})


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _msg_summary(m: Message) -> dict:
    return {
        "id":         m.id,
        "bid":        m.bid,
        "type":       m.msg_type,
        "status":     m.status,
        "from_call":  m.from_call,
        "to_call":    m.to_call,
        "at_bbs":     m.at_bbs,
        "subject":    m.subject,
        "size":       m.size,
        "created_at": int(m.created_at.timestamp()) if m.created_at else 0,
    }

def _msg_detail(m: Message) -> dict:
    d = _msg_summary(m)
    d["body"]         = m.body
    d["forward_path"] = m.forward_path
    d["read_by"]      = m.read_by
    d["read_at"]      = int(m.read_at.timestamp()) if m.read_at else None
    d["expires_at"]   = int(m.expires_at.timestamp()) if m.expires_at else None
    d["edited_by"]    = m.edited_by
    d["edited_at"]    = int(m.edited_at.timestamp()) if m.edited_at else None
    return d

def _user_summary(u) -> dict:
    return {
        "call":         u.call,
        "display_name": u.display_name,
        "privilege":    u.privilege,
        "home_bbs":     u.home_bbs,
        "last_seen":    int(u.last_seen.timestamp()) if u.last_seen else 0,
        "last_login_at": int(u.last_login_at.timestamp()) if u.last_login_at else None,
        "last_login_peer": u.last_login_peer,
    }

def _user_detail(u) -> dict:
    d = _user_summary(u)
    d.update({
        "email":      u.email,
        "address":    u.address,
        "locator":    u.locator,
        "city":       u.city,
        "zip_code":   u.zip_code,
        "msg_base":   u.msg_base,
        "page_length":u.page_length,
        "expert_mode":u.expert_mode,
        "language":   u.language,
        "created_at": int(u.created_at.timestamp()) if u.created_at else 0,
    })
    return d
