from __future__ import annotations

from pybulletin.transport.pactor import (
    PactorHostFrame,
    decode_host_stream,
    encode_host_frame,
)


def test_pactor_host_frame_roundtrip():
    buf = bytearray(encode_host_frame(1, 2, b"hello"))

    frames = decode_host_stream(buf)

    assert frames == [PactorHostFrame(channel=1, status=2, payload=b"hello")]
    assert buf == bytearray()


def test_pactor_host_stream_buffers_incomplete_frame():
    buf = bytearray(encode_host_frame(2, 2, b"abc")[:-1])

    assert decode_host_stream(buf) == []
    assert buf

    buf.append(ord("c"))
    assert decode_host_stream(buf) == [PactorHostFrame(channel=2, status=2, payload=b"abc")]


def test_pactor_host_stream_discards_noise_before_marker():
    buf = bytearray(b"noise") + bytearray(encode_host_frame(3, 1, b"bye"))

    assert decode_host_stream(buf) == [PactorHostFrame(channel=3, status=1, payload=b"bye")]


def test_pactor_host_frame_rejects_oversized_payload():
    try:
        encode_host_frame(1, 2, b"x" * 256)
    except ValueError as exc:
        assert "payload" in str(exc)
    else:
        raise AssertionError("expected ValueError")
