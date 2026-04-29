from __future__ import annotations

import asyncio

from pybulletin.web.server import HTTPRequest, HTTPResponse, HTTPServer


class _MemoryWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name: str):
        if name == "peername":
            return ("127.0.0.1", 12345)
        return None

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


async def _memory_request(server: HTTPServer, raw: bytes) -> bytes:
    reader = asyncio.StreamReader()
    writer = _MemoryWriter()
    reader.feed_data(raw)
    reader.feed_eof()
    await server._handle_connection(reader, writer, "127.0.0.1")
    return bytes(writer.data)


def test_head_routes_to_app_handler_without_body():
    seen: list[HTTPRequest] = []

    async def handler(req: HTTPRequest) -> HTTPResponse:
        seen.append(req)
        return HTTPResponse.text("ok")

    async def run() -> bytes:
        server = HTTPServer("127.0.0.1", 0, handler)
        return await _memory_request(
            server,
            b"HEAD /sysop HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )

    response = asyncio.run(run())

    headers, _, body = response.partition(b"\r\n\r\n")
    assert headers.startswith(b"HTTP/1.1 200 OK")
    assert b"content-length: 2" in headers
    assert body == b""
    assert seen[0].method == "GET"
    assert seen[0].path == "/sysop"


def test_head_static_response_omits_body(tmp_path):
    (tmp_path / "index.html").write_text("hello", encoding="utf-8")

    async def handler(req: HTTPRequest) -> HTTPResponse | None:
        return None

    async def run() -> bytes:
        server = HTTPServer("127.0.0.1", 0, handler, static_dir=tmp_path)
        return await _memory_request(
            server,
            b"HEAD /index.html HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )

    response = asyncio.run(run())

    headers, _, body = response.partition(b"\r\n\r\n")
    assert headers.startswith(b"HTTP/1.1 200 OK")
    assert b"content-length: 5" in headers
    assert b"content-type: text/html" in headers
    assert body == b""
