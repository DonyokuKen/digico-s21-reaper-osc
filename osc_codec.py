"""Minimal, spec-compliant OSC 1.0 binary encoder & decoder (stdlib only)."""
from __future__ import annotations
import struct
from typing import Any, List, Tuple


def _pad4(data: bytes) -> bytes:
    rem = len(data) % 4
    return data if rem == 0 else data + b"\x00" * (4 - rem)


def _encode_str(s: str) -> bytes:
    return _pad4(s.encode("utf-8") + b"\x00")


def encode_message(address: str, *args: Any) -> bytes:
    """Encode a single OSC message into bytes."""
    if not address.startswith("/"):
        address = "/" + address
    tags = [","]
    payload = bytearray()
    for a in args:
        if isinstance(a, bool):
            tags.append("T" if a else "F")
        elif isinstance(a, int):
            tags.append("i")
            payload.extend(struct.pack(">i", a))
        elif isinstance(a, float):
            tags.append("f")
            payload.extend(struct.pack(">f", a))
        elif isinstance(a, str):
            tags.append("s")
            payload.extend(_encode_str(a))
        elif isinstance(a, (bytes, bytearray)):
            tags.append("b")
            payload.extend(struct.pack(">i", len(a)))
            payload.extend(_pad4(bytes(a)))
        else:
            tags.append("s")
            payload.extend(_encode_str(str(a)))
    return _encode_str(address) + _encode_str("".join(tags)) + bytes(payload)


def _read_str(buf: bytes, pos: int) -> Tuple[str, int]:
    end = buf.find(b"\x00", pos)
    if end == -1:
        raise ValueError("Unterminated OSC string")
    s = buf[pos:end].decode("utf-8", errors="replace")
    next_pos = ((end + 4) // 4) * 4
    return s, next_pos


def decode_packet(data: bytes) -> List[Tuple[str, List[Any]]]:
    """Decode an OSC message or #bundle into a list of (address, args)."""
    if not data:
        return []
    if data.startswith(b"#bundle\x00"):
        msgs: List[Tuple[str, List[Any]]] = []
        pos = 16  # 8 bytes "#bundle\0" + 8 bytes timetag
        while pos + 4 <= len(data):
            size = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4
            if size <= 0 or pos + size > len(data):
                break
            msgs.extend(decode_packet(data[pos:pos + size]))
            pos += size
        return msgs

    addr, pos = _read_str(data, 0)
    if pos >= len(data):
        return [(addr, [])]
    typetags, pos = _read_str(data, pos)
    if not typetags.startswith(","):
        return [(addr, [])]

    args: List[Any] = []
    for t in typetags[1:]:
        if t == "f":
            if pos + 4 > len(data):
                break
            args.append(struct.unpack(">f", data[pos:pos + 4])[0])
            pos += 4
        elif t == "i":
            if pos + 4 > len(data):
                break
            args.append(struct.unpack(">i", data[pos:pos + 4])[0])
            pos += 4
        elif t == "d":
            if pos + 8 > len(data):
                break
            args.append(float(struct.unpack(">d", data[pos:pos + 8])[0]))
            pos += 8
        elif t == "s":
            s, pos = _read_str(data, pos)
            args.append(s)
        elif t == "b":
            if pos + 4 > len(data):
                break
            blen = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4
            args.append(data[pos:pos + blen])
            pos += ((blen + 3) // 4) * 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "N":
            args.append(None)
        elif t == "I":
            args.append(1)
    return [(addr, args)]
