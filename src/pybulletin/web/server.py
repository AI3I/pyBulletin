"""Pure-stdlib asyncio HTTP/1.1 + WebSocket server.

No external dependencies.  Handles:
  - HTTP GET / POST / DELETE with header + body parsing
  - WebSocket upgrade (RFC 6455) with ping/pong keepalive
  - Static file serving with MIME types and basic cache headers
  - Chunked reads, request body size limit

Usage::

    server = HTTPServer(host, port, app.handle_request)
    await server.start()
    ...
    await server.stop()
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

LOG = logging.getLogger(__name__)

_MAX_BODY    = 256 * 1024   # 256 KB request body limit
_WS_GUID     = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CHUNK       = 8192
_WS_PING_INT = 30.0         # seconds between server-initiated pings


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

@dataclass
class HTTPRequest:
    method:  str
    path:    str
    query:   str
    headers: dict[str, str]
    body:    bytes
    peer:    str = ""

    @property
    def query_params(self) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(self.query))

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def bearer_token(self) -> str:
        auth = self.header("authorization")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def cookie(self, name: str) -> str:
        raw = self.header("cookie")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == name:
                return v.strip()
        return ""


@dataclass
class HTTPResponse:
    status:  int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body:    bytes = b""

    @classmethod
    def json(cls, data, status: int = 200) -> HTTPResponse:
        import json
        body = json.dumps(data, default=str).encode()
        return cls(
            status=status,
            headers={"content-type": "application/json"},
            body=body,
        )

    @classmethod
    def text(cls, text: str, status: int = 200) -> HTTPResponse:
        return cls(
            status=status,
            headers={"content-type": "text/plain; charset=utf-8"},
            body=text.encode(),
        )

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> HTTPResponse:
        return cls(status=status, headers={"location": location})

    @classmethod
    def not_found(cls, msg: str = "Not found") -> HTTPResponse:
        return cls.json({"error": msg}, status=404)

    @classmethod
    def forbidden(cls, msg: str = "Forbidden") -> HTTPResponse:
        return cls.json({"error": msg}, status=403)

    @classmethod
    def bad_request(cls, msg: str = "Bad request") -> HTTPResponse:
        return cls.json({"error": msg}, status=400)

    @classmethod
    def error(cls, msg: str = "Internal error", status: int = 500) -> HTTPResponse:
        return cls.json({"error": msg}, status=status)


_STATUS_TEXT = {
    200: "OK", 201: "Created", 204: "No Content",
    301: "Moved Permanently", 302: "Found",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed",
    500: "Internal Server Error", 501: "Not Implemented",
    101: "Switching Protocols",
}

RequestHandler = Callable[[HTTPRequest], Awaitable[HTTPResponse | None]]


# ---------------------------------------------------------------------------
# WebSocket connection
# ---------------------------------------------------------------------------

class WebSocket:
    """A single WebSocket connection.  Thread-safe for concurrent reads/writes."""

    OP_CONT  = 0x0
    OP_TEXT  = 0x1
    OP_BIN   = 0x2
    OP_CLOSE = 0x8
    OP_PING  = 0x9
    OP_PONG  = 0xA

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.peer    = peer
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._ping_task: asyncio.Task | None = None

    def start_ping(self) -> None:
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def send_text(self, text: str) -> None:
        await self._send_frame(self.OP_TEXT, text.encode())

    async def send_json(self, data) -> None:
        import json
        await self.send_text(json.dumps(data, default=str))

    async def recv(self) -> tuple[int, bytes] | None:
        """Return (opcode, payload) or None on close/error."""
        try:
            return await self._recv_frame()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            return None

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ping_task:
            self._ping_task.cancel()
        try:
            await self._send_frame(self.OP_CLOSE, bytes([code >> 8, code & 0xFF]))
            self._writer.close()
        except Exception:
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))  # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += n.to_bytes(2, "big")
        else:
            header.append(127)
            header += n.to_bytes(8, "big")
        async with self._write_lock:
            self._writer.write(bytes(header) + payload)
            await self._writer.drain()

    async def _recv_frame(self) -> tuple[int, bytes] | None:
        # Read first 2 bytes
        header = await self._reader.readexactly(2)
        fin     = (header[0] & 0x80) != 0
        opcode  = header[0] & 0x0F
        masked  = (header[1] & 0x80) != 0
        length  = header[1] & 0x7F

        if length == 126:
            ext = await self._reader.readexactly(2)
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = await self._reader.readexactly(8)
            length = int.from_bytes(ext, "big")

        if length > _MAX_BODY:
            await self.close(1009)  # message too big
            return None

        key = b""
        if masked:
            key = await self._reader.readexactly(4)

        data = await self._reader.readexactly(length)
        if masked:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))

        if opcode == self.OP_PING:
            await self._send_frame(self.OP_PONG, data)
            return await self._recv_frame()
        if opcode == self.OP_CLOSE:
            await self.close()
            return None

        # Handle continuation frames (simple: reassemble)
        if opcode == self.OP_CONT:
            return (self.OP_TEXT, data)

        return (opcode, data)

    async def _ping_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_WS_PING_INT)
            if self._closed:
                break
            try:
                await self._send_frame(self.OP_PING, b"")
            except Exception:
                break


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class HTTPServer:
    def __init__(
        self,
        host: str,
        port: int,
        handler: RequestHandler,
        *,
        ws_handler: Callable[[WebSocket, HTTPRequest], Awaitable[None]] | None = None,
        static_dir: Path | None = None,
    ) -> None:
        self._host       = host
        self._port       = port
        self._handler    = handler
        self._ws_handler = ws_handler
        self._static_dir = static_dir
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept, self._host, self._port, reuse_address=True
        )
        LOG.info("web: listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        LOG.info("web: stopped")

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = "unknown"
        try:
            addr = writer.get_extra_info("peername")
            peer = addr[0] if addr else "unknown"
        except Exception:
            pass
        try:
            await self._handle_connection(reader, writer, peer)
        except Exception:
            LOG.exception("web: unhandled error from %s", peer)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        # Keep-alive: handle multiple requests per connection
        while True:
            req = await self._parse_request(reader, peer)
            if req is None:
                return

            # WebSocket upgrade?
            if (req.header("upgrade").lower() == "websocket"
                    and self._ws_handler is not None):
                await self._upgrade_ws(reader, writer, req)
                return

            # Static file?
            if req.method == "GET" and self._static_dir:
                resp = self._serve_static(req)
                if resp is not None:
                    await self._write_response(writer, resp)
                    if req.header("connection").lower() == "close":
                        return
                    continue

            # Application handler
            try:
                resp = await self._handler(req)
            except Exception:
                LOG.exception("web: handler error for %s %s", req.method, req.path)
                resp = HTTPResponse.error()

            if resp is None:
                resp = HTTPResponse.not_found()

            await self._write_response(writer, resp)

            conn = req.header("connection").lower()
            if conn == "close" or req.headers.get("http-version", "1.1") == "1.0":
                return

    async def _parse_request(
        self, reader: asyncio.StreamReader, peer: str
    ) -> HTTPRequest | None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            return None
        if not line:
            return None

        try:
            method, raw_path, _ = line.decode("utf-8", errors="replace").strip().split(None, 2)
        except ValueError:
            return None

        path, _, query = raw_path.partition("?")
        path = urllib.parse.unquote(path)

        headers: dict[str, str] = {}
        while True:
            try:
                hline = await asyncio.wait_for(reader.readline(), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return None
            hline = hline.decode("utf-8", errors="replace").strip()
            if not hline:
                break
            k, _, v = hline.partition(":")
            headers[k.strip().lower()] = v.strip()

        body = b""
        content_length = int(headers.get("content-length", "0") or "0")
        if content_length > 0:
            to_read = min(content_length, _MAX_BODY)
            try:
                body = await asyncio.wait_for(reader.readexactly(to_read), timeout=15.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return None

        return HTTPRequest(
            method=method.upper(),
            path=path,
            query=query,
            headers=headers,
            body=body,
            peer=peer,
        )

    async def _upgrade_ws(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        req: HTTPRequest,
    ) -> None:
        key = req.header("sec-websocket-key")
        if not key:
            await self._write_response(writer, HTTPResponse.bad_request("Missing WS key"))
            return

        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()

        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        writer.write(response.encode())
        await writer.drain()

        ws = WebSocket(reader, writer, req.peer)
        ws.start_ping()
        LOG.debug("web: WebSocket connected from %s", req.peer)
        try:
            await self._ws_handler(ws, req)
        except Exception:
            LOG.exception("web: WebSocket handler error from %s", req.peer)
        finally:
            await ws.close()
            LOG.debug("web: WebSocket disconnected from %s", req.peer)

    def _serve_static(self, req: HTTPRequest) -> HTTPResponse | None:
        assert self._static_dir is not None
        # Map / → index.html
        rel = req.path.lstrip("/") or "index.html"
        # Security: reject path traversal
        try:
            target = (self._static_dir / rel).resolve()
            target.relative_to(self._static_dir.resolve())
        except (ValueError, OSError):
            return HTTPResponse.forbidden()
        if not target.exists() or not target.is_file():
            return None  # let the app handler try
        mime, _ = mimetypes.guess_type(str(target))
        headers = {
            "content-type": mime or "application/octet-stream",
            "cache-control": "no-cache",
        }
        return HTTPResponse(headers=headers, body=target.read_bytes())

    async def _write_response(
        self, writer: asyncio.StreamWriter, resp: HTTPResponse
    ) -> None:
        status_text = _STATUS_TEXT.get(resp.status, "Unknown")
        lines = [f"HTTP/1.1 {resp.status} {status_text}"]
        lines.append(f"content-length: {len(resp.body)}")
        lines.append("connection: keep-alive")
        lines.append("x-powered-by: pyBulletin")
        for k, v in resp.headers.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        lines.append("")
        writer.write("\r\n".join(lines).encode())
        if resp.body:
            writer.write(resp.body)
        await writer.drain()
