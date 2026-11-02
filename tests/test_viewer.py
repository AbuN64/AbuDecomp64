"""The browser viewer's WebSocket bridge: framing and byte carrying, with socket pairs standing in for both ends."""

import socket
import struct
import threading

from abu_decomp import display, viewer


def masked(payload: bytes, opcode: int = 2) -> bytes:
    mask = b"\x01\x02\x03\x04"
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def test_accept_key_is_the_rfc_6455_example() -> None:
    assert viewer.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_frame_sizes() -> None:
    assert viewer.frame(b"ab") == b"\x82\x02ab"
    assert viewer.frame(bytes(200))[:4] == b"\x82\x7e" + struct.pack(">H", 200)
    assert viewer.frame(bytes(70000))[:2] == b"\x82\x7f"


def test_unframe_waits_for_a_whole_frame_and_stops_at_close() -> None:
    whole = masked(b"hello")
    buffer = bytearray(whole[:4])
    assert viewer.unframe(buffer) == ([], False)
    buffer += whole[4:] + masked(b"", opcode=8) + masked(b"late")
    assert viewer.unframe(buffer) == ([b"hello"], True)


def test_bridge_carries_bytes_both_ways_until_the_client_closes() -> None:
    client, browser = socket.socketpair()
    upstream, server = socket.socketpair()
    thread = threading.Thread(target=viewer.bridge, args=(client, upstream), daemon=True)
    thread.start()
    server.sendall(b"RFB 003.008\n")
    assert browser.recv(64) == viewer.frame(b"RFB 003.008\n")
    browser.sendall(masked(b"RFB 003.008\n"))
    assert server.recv(64) == b"RFB 003.008\n"
    browser.sendall(masked(b"", opcode=8))
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_pages_name_the_display() -> None:
    record = display.Record("d-aa", 640, 480, "tcp:g:1", {}, "/x")
    assert 'data-ws="/viewer/d-aa/ws"' in viewer.viewer_page(record)
    assert 'data-audio="/viewer/d-aa/audio"' in viewer.viewer_page(record)
    assert "allow=autoplay" in viewer.live_page([record])
    assert 'src="/viewer/d-aa"' in viewer.live_page([record])
    assert "No live display" in viewer.live_page([])
