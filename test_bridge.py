"""Automated UDP loopback tests for DiGiCo S21 <-> REAPER OSC Bridge."""
import socket
import struct
import tempfile
import time
from pathlib import Path

from osc_codec import decode_packet, encode_message
from bridge_engine import BridgeConfig, DiGiCoReaperBridge
from reaper_osc_profile import install_reaper_osc_profile


def _recv_osc(sock: socket.socket, timeout: float = 0.5):
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(4096)
    return decode_packet(data)


def test_osc_codec():
    pkt = encode_message("/Input_Channels/5/fader", -6.5)
    msgs = decode_packet(pkt)
    assert len(msgs) == 1
    assert msgs[0][0] == "/Input_Channels/5/fader"
    assert abs(msgs[0][1][0] - (-6.5)) < 1e-4

    # Test bundle containing two messages
    m1 = encode_message("/Input_Channels/1/mute", 1)
    m2 = encode_message("/Input_Channels/1/pan", -0.5)
    bundle = b"#bundle\x00" + b"\x00" * 8 + struct.pack(">i", len(m1)) + m1 + struct.pack(">i", len(m2)) + m2
    bmsgs = decode_packet(bundle)
    assert len(bmsgs) == 2
    assert bmsgs[0] == ("/Input_Channels/1/mute", [1])
    assert bmsgs[1][0] == "/Input_Channels/1/pan" and abs(bmsgs[1][1][0] + 0.5) < 1e-4
    print("[PASS] OSC codec single + bundle")


def test_bidirectional_and_echo_lock():
    # Mock S21 & Mock REAPER UDP endpoints
    mock_s21 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mock_s21.bind(("127.0.0.1", 0))
    s21_listen_port = mock_s21.getsockname()[1]

    mock_rpr = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mock_rpr.bind(("127.0.0.1", 0))
    rpr_listen_port = mock_rpr.getsockname()[1]

    cfg = BridgeConfig(
        s21_ip="127.0.0.1",
        s21_tx_port=s21_listen_port,
        s21_rx_port=18001,
        reaper_ip="127.0.0.1",
        reaper_tx_port=rpr_listen_port,
        reaper_rx_port=19001,
        direction="bidirectional",
        fader_scale="db",
        pan_scale="bipolar",
        track_offset=2,  # S21 Ch 1 -> REAPER Track 3
        echo_lock_ms=150,
    )
    bridge = DiGiCoReaperBridge(cfg)
    bridge.start()
    try:
        # 1. S21 -> REAPER: Fader (-12.5 dB), Pan (-1.0 full left -> 0.0), Mute (1)
        mock_s21.sendto(encode_message("/Input_Channels/1/fader", -12.5), ("127.0.0.1", 18001))
        msgs = _recv_osc(mock_rpr)
        assert msgs[0][0] == "/track/3/volume/db" and abs(msgs[0][1][0] - (-12.5)) < 1e-3

        mock_s21.sendto(encode_message("/Input_Channels/1/pan", -1.0), ("127.0.0.1", 18001))
        msgs = _recv_osc(mock_rpr)
        assert msgs[0][0] == "/track/3/pan" and abs(msgs[0][1][0] - 0.0) < 1e-3

        mock_s21.sendto(encode_message("/Input_Channels/1/mute", 1), ("127.0.0.1", 18001))
        msgs = _recv_osc(mock_rpr)
        assert msgs[0] == ("/track/3/mute", [1])

        # 2. Anti-jitter echo suppression: immediate REAPER echo on Track 3 volume must be dropped!
        mock_rpr.sendto(encode_message("/track/3/volume/db", -12.5), ("127.0.0.1", 19001))
        try:
            _recv_osc(mock_s21, timeout=0.12)
            raise AssertionError("Echo should have been suppressed!")
        except socket.timeout:
            pass

        # 3. After echo lock expires, REAPER -> S21 works cleanly with strict float dB & bipolar pan!
        time.sleep(0.18)
        mock_rpr.sendto(encode_message("/track/3/volume/db", -3.0), ("127.0.0.1", 19001))
        msgs = _recv_osc(mock_s21)
        assert msgs[0][0] == "/Input_Channels/1/fader"
        assert isinstance(msgs[0][1][0], float) and abs(msgs[0][1][0] - (-3.0)) < 1e-3

        mock_rpr.sendto(encode_message("/track/3/pan", 0.75), ("127.0.0.1", 19001))
        msgs = _recv_osc(mock_s21)
        assert msgs[0][0] == "/Input_Channels/1/pan"
        assert abs(msgs[0][1][0] - 0.5) < 1e-3

        print("[PASS] Bi-directional S21 <-> REAPER + track offset + echo suppression")
    finally:
        bridge.stop()
        mock_s21.close()
        mock_rpr.close()


def test_reaper_osc_installer():
    with tempfile.TemporaryDirectory() as td:
        out = install_reaper_osc_profile(Path(td))
        txt = out.read_text(encoding="utf-8")
        assert "DEVICE_TRACK_COUNT 64" in txt
        assert "TRACK_VOLUME f/track/@/volume/db" in txt
        assert "TRACK_PAN n/track/@/pan" in txt
        print("[PASS] REAPER .ReaperOSC installer")


if __name__ == "__main__":
    test_osc_codec()
    test_bidirectional_and_echo_lock()
    test_reaper_osc_installer()
    print("ALL TESTS PASSED!")
