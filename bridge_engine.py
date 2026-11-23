"""Bi-directional UDP OSC Bridge between DiGiCo S21 and REAPER."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import re
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from osc_codec import decode_packet, encode_message


DIGICO_PROFILES: Dict[str, Dict[str, str]] = {
    "DiGiCo S21 Standard (/Input_Channels/{ch}/...)": {
        "fader": "/Input_Channels/{ch}/fader",
        "mute": "/Input_Channels/{ch}/mute",
        "pan": "/Input_Channels/{ch}/pan",
        "solo": "/Input_Channels/{ch}/solo",
        "name": "/Input_Channels/{ch}/name",
    },
    "DiGiCo SD Prefix (/sd/Input_Channels/{ch}/...)": {
        "fader": "/sd/Input_Channels/{ch}/fader",
        "mute": "/sd/Input_Channels/{ch}/mute",
        "pan": "/sd/Input_Channels/{ch}/pan",
        "solo": "/sd/Input_Channels/{ch}/solo",
        "name": "/sd/Input_Channels/{ch}/name",
    },
    "DiGiCo Flat (/channel/{ch}/...)": {
        "fader": "/channel/{ch}/fader",
        "mute": "/channel/{ch}/mute",
        "pan": "/channel/{ch}/pan",
        "solo": "/channel/{ch}/solo",
        "name": "/channel/{ch}/name",
    },
}


@dataclass
class BridgeConfig:
    s21_ip: str = "192.168.1.21"
    s21_tx_port: int = 8000      # Port S21 listens on
    s21_rx_port: int = 8001      # Local port this bridge listens on for S21
    reaper_ip: str = "127.0.0.1"
    reaper_tx_port: int = 9000   # Port REAPER listens on
    reaper_rx_port: int = 9001   # Local port this bridge listens on for REAPER

    direction: str = "s21_to_reaper"  # "s21_to_reaper", "bidirectional", "reaper_to_s21"
    profile: str = "DiGiCo S21 Standard (/Input_Channels/{ch}/...)"
    path_fader: str = "/Input_Channels/{ch}/fader"
    path_mute: str = "/Input_Channels/{ch}/mute"
    path_pan: str = "/Input_Channels/{ch}/pan"
    path_solo: str = "/Input_Channels/{ch}/solo"
    path_name: str = "/Input_Channels/{ch}/name"

    sync_fader: bool = True
    sync_pan: bool = True
    sync_mute: bool = True
    sync_solo: bool = False
    sync_name: bool = False

    fader_scale: str = "db"       # "db" (-150..+10 float dB) or "norm" (0.0..1.0)
    pan_scale: str = "bipolar"    # "bipolar" (-1..+1), "norm" (0..1), "midi" (0..127)
    track_offset: int = 0         # REAPER track = S21 ch + track_offset
    max_channels: int = 48
    echo_lock_ms: int = 120

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BridgeConfig":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


def _compile_template(template: str) -> Optional[re.Pattern]:
    t = template.strip()
    if not t or "{ch}" not in t:
        return None
    escaped = re.escape(t).replace(r"\{ch\}", r"(\d+)")
    return re.compile("^" + escaped + "$")


def _to_float(val: Any) -> Optional[float]:
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.replace("dB", "").strip())
        except ValueError:
            return None
    return None


def _to_bool_int(val: Any) -> int:
    if isinstance(val, bool):
        return 1 if val else 0
    if isinstance(val, (int, float)):
        return 1 if float(val) >= 0.5 else 0
    if isinstance(val, str):
        return 1 if val.strip().lower() in ("1", "true", "on", "yes") else 0
    return 0


class DiGiCoReaperBridge:
    """Runs two non-blocking UDP listener threads and translates OSC cleanly."""

    def __init__(
        self,
        config: BridgeConfig,
        on_log: Optional[Callable[[str, str], None]] = None,
        on_stats: Optional[Callable[[Dict[str, int]], None]] = None,
    ):
        self.cfg = config
        self.on_log = on_log
        self.on_stats = on_stats
        self._running = False
        self._s21_sock: Optional[socket.socket] = None
        self._reaper_sock: Optional[socket.socket] = None
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        # Echo suppression: key = (origin_side, ch, param) -> monotonic timestamp
        self._recent: Dict[Tuple[str, int, str], float] = {}
        self.stats = {"s21_rx": 0, "reaper_rx": 0, "to_reaper": 0, "to_s21": 0, "unmapped": 0}
        self._patterns: Dict[str, Optional[re.Pattern]] = {}
        self._reaper_pat = re.compile(r"^/track/(\d+)/(volume/db|volume|pan|mute|solo|name)$")

    @property
    def running(self) -> bool:
        return self._running

    def _emit_log(self, tag: str, msg: str) -> None:
        if self.on_log:
            try:
                self.on_log(tag, msg)
            except Exception:
                pass

    def _emit_stats(self) -> None:
        if self.on_stats:
            try:
                self.on_stats(dict(self.stats))
            except Exception:
                pass

    def _rebuild_patterns(self) -> None:
        self._patterns = {
            "fader": _compile_template(self.cfg.path_fader),
            "mute": _compile_template(self.cfg.path_mute),
            "pan": _compile_template(self.cfg.path_pan),
            "solo": _compile_template(self.cfg.path_solo),
            "name": _compile_template(self.cfg.path_name),
        }

    def start(self) -> None:
        if self._running:
            return
        self._rebuild_patterns()
        self.stats = {"s21_rx": 0, "reaper_rx": 0, "to_reaper": 0, "to_s21": 0, "unmapped": 0}
        self._recent.clear()

        s21_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s21_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s21_sock.bind(("0.0.0.0", int(self.cfg.s21_rx_port)))
        s21_sock.settimeout(0.25)

        reaper_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reaper_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            reaper_sock.bind(("0.0.0.0", int(self.cfg.reaper_rx_port)))
        except OSError:
            s21_sock.close()
            reaper_sock.close()
            raise
        reaper_sock.settimeout(0.25)

        self._s21_sock = s21_sock
        self._reaper_sock = reaper_sock
        self._running = True

        t1 = threading.Thread(target=self._loop_s21, name="S21-OSC-RX", daemon=True)
        t2 = threading.Thread(target=self._loop_reaper, name="REAPER-OSC-RX", daemon=True)
        self._threads = [t1, t2]
        t1.start()
        t2.start()
        self._emit_log(
            "SYS",
            f"Bridge STARTED | S21 {self.cfg.s21_ip}:{self.cfg.s21_tx_port} (RX:{self.cfg.s21_rx_port}) "
            f"<-> REAPER {self.cfg.reaper_ip}:{self.cfg.reaper_tx_port} (RX:{self.cfg.reaper_rx_port}) "
            f"| Mode={self.cfg.direction}",
        )
        self._emit_stats()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for sock in (self._s21_sock, self._reaper_sock):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()
        self._s21_sock = None
        self._reaper_sock = None
        self._emit_log("SYS", "Bridge STOPPED.")

    def _is_echo_blocked(self, opposite_origin: str, ch: int, param: str) -> bool:
        lock_s = max(0, int(self.cfg.echo_lock_ms)) / 1000.0
        if lock_s <= 0:
            return False
        now = time.monotonic()
        with self._lock:
            ts = self._recent.get((opposite_origin, ch, param))
            return ts is not None and (now - ts) < lock_s

    def _mark_forwarded(self, origin: str, ch: int, param: str) -> None:
        with self._lock:
            self._recent[(origin, ch, param)] = time.monotonic()

    def _s21_pan_to_reaper(self, v: float) -> float:
        if self.cfg.pan_scale == "bipolar":
            # S21 -1..+1 -> REAPER 0..1
            return max(0.0, min(1.0, (v + 1.0) * 0.5))
        if self.cfg.pan_scale == "midi":
            return max(0.0, min(1.0, v / 127.0))
        return max(0.0, min(1.0, v))

    def _reaper_pan_to_s21(self, v: float) -> float:
        v = max(0.0, min(1.0, v))
        if self.cfg.pan_scale == "bipolar":
            return (v * 2.0) - 1.0
        if self.cfg.pan_scale == "midi":
            return v * 127.0
        return v

    def send_to_reaper(self, address: str, *args: Any) -> None:
        if not self._reaper_sock:
            return
        pkt = encode_message(address, *args)
        self._reaper_sock.sendto(pkt, (self.cfg.reaper_ip, int(self.cfg.reaper_tx_port)))
        self.stats["to_reaper"] += 1

    def send_to_s21(self, address: str, *args: Any) -> None:
        if not self._s21_sock:
            return
        pkt = encode_message(address, *args)
        self._s21_sock.sendto(pkt, (self.cfg.s21_ip, int(self.cfg.s21_tx_port)))
        self.stats["to_s21"] += 1

    def _handle_s21_msg(self, addr: str, args: list[Any]) -> None:
        self.stats["s21_rx"] += 1
        if self.cfg.direction == "reaper_to_s21":
            return
        if not args:
            return

        matched = False
        for param, pat in self._patterns.items():
            if not pat:
                continue
            m = pat.match(addr)
            if not m:
                continue
            matched = True
            ch = int(m.group(1))
            if ch < 1 or ch > int(self.cfg.max_channels):
                return
            tr = ch + int(self.cfg.track_offset)
            if tr < 1:
                return
            if self._is_echo_blocked("reaper", ch, param):
                return

            if param == "fader" and self.cfg.sync_fader:
                val = _to_float(args[0])
                if val is None:
                    return
                if self.cfg.fader_scale == "db":
                    db = max(-150.0, min(12.0, val))
                    self.send_to_reaper(f"/track/{tr}/volume/db", float(db))
                    self._mark_forwarded("s21", ch, "fader")
                    self._emit_log("S21->RPR", f"Ch {ch} Fader {db:+.1f} dB -> Track {tr}")
                else:
                    norm = max(0.0, min(1.0, val))
                    self.send_to_reaper(f"/track/{tr}/volume", float(norm))
                    self._mark_forwarded("s21", ch, "fader")
                    self._emit_log("S21->RPR", f"Ch {ch} Fader {norm:.3f} -> Track {tr}")

            elif param == "pan" and self.cfg.sync_pan:
                val = _to_float(args[0])
                if val is None:
                    return
                rpan = self._s21_pan_to_reaper(val)
                self.send_to_reaper(f"/track/{tr}/pan", float(rpan))
                self._mark_forwarded("s21", ch, "pan")
                pct = int(round((rpan - 0.5) * 200))
                pstr = "C" if pct == 0 else (f"{abs(pct)}%L" if pct < 0 else f"{pct}%R")
                self._emit_log("S21->RPR", f"Ch {ch} Pan {val:+.2f} -> Track {tr} ({pstr})")

            elif param == "mute" and self.cfg.sync_mute:
                b = _to_bool_int(args[0])
                self.send_to_reaper(f"/track/{tr}/mute", int(b))
                self._mark_forwarded("s21", ch, "mute")
                self._emit_log("S21->RPR", f"Ch {ch} Mute={'ON' if b else 'OFF'} -> Track {tr}")

            elif param == "solo" and self.cfg.sync_solo:
                b = _to_bool_int(args[0])
                self.send_to_reaper(f"/track/{tr}/solo", int(b))
                self._mark_forwarded("s21", ch, "solo")
                self._emit_log("S21->RPR", f"Ch {ch} Solo={'ON' if b else 'OFF'} -> Track {tr}")

            elif param == "name" and self.cfg.sync_name:
                nm = str(args[0])
                self.send_to_reaper(f"/track/{tr}/name", nm)
                self._mark_forwarded("s21", ch, "name")
                self._emit_log("S21->RPR", f"Ch {ch} Name '{nm}' -> Track {tr}")
            break

        if not matched:
            self.stats["unmapped"] += 1
            self._emit_log("UNMAPPED", f"S21 sent {addr} {args}")

    def _handle_reaper_msg(self, addr: str, args: list[Any]) -> None:
        self.stats["reaper_rx"] += 1
        if self.cfg.direction == "s21_to_reaper":
            return
        if not args:
            return

        m = self._reaper_pat.match(addr)
        if not m:
            return
        tr = int(m.group(1))
        kind = m.group(2)
        ch = tr - int(self.cfg.track_offset)
        if ch < 1 or ch > int(self.cfg.max_channels):
            return

        if kind == "volume/db" and self.cfg.sync_fader and self.cfg.fader_scale == "db":
            if self._is_echo_blocked("s21", ch, "fader"):
                return
            val = _to_float(args[0])
            if val is None:
                return
            db = max(-150.0, min(10.0, val))
            out_addr = self.cfg.path_fader.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, float(db))
            self._mark_forwarded("reaper", ch, "fader")
            self._emit_log("RPR->S21", f"Track {tr} Vol {db:+.1f} dB -> Ch {ch}")

        elif kind == "volume" and self.cfg.sync_fader and self.cfg.fader_scale == "norm":
            if self._is_echo_blocked("s21", ch, "fader"):
                return
            val = _to_float(args[0])
            if val is None:
                return
            norm = max(0.0, min(1.0, val))
            out_addr = self.cfg.path_fader.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, float(norm))
            self._mark_forwarded("reaper", ch, "fader")
            self._emit_log("RPR->S21", f"Track {tr} Vol {norm:.3f} -> Ch {ch}")

        elif kind == "pan" and self.cfg.sync_pan:
            if self._is_echo_blocked("s21", ch, "pan"):
                return
            val = _to_float(args[0])
            if val is None:
                return
            s_pan = self._reaper_pan_to_s21(val)
            out_addr = self.cfg.path_pan.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, float(s_pan))
            self._mark_forwarded("reaper", ch, "pan")
            self._emit_log("RPR->S21", f"Track {tr} Pan {val:.2f} -> Ch {ch} ({s_pan:+.2f})")

        elif kind == "mute" and self.cfg.sync_mute:
            if self._is_echo_blocked("s21", ch, "mute"):
                return
            b = _to_bool_int(args[0])
            out_addr = self.cfg.path_mute.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, int(b))
            self._mark_forwarded("reaper", ch, "mute")
            self._emit_log("RPR->S21", f"Track {tr} Mute={'ON' if b else 'OFF'} -> Ch {ch}")

        elif kind == "solo" and self.cfg.sync_solo:
            if self._is_echo_blocked("s21", ch, "solo"):
                return
            b = _to_bool_int(args[0])
            out_addr = self.cfg.path_solo.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, int(b))
            self._mark_forwarded("reaper", ch, "solo")
            self._emit_log("RPR->S21", f"Track {tr} Solo={'ON' if b else 'OFF'} -> Ch {ch}")

        elif kind == "name" and self.cfg.sync_name:
            if self._is_echo_blocked("s21", ch, "name"):
                return
            nm = str(args[0])
            out_addr = self.cfg.path_name.replace("{ch}", str(ch))
            self.send_to_s21(out_addr, nm)
            self._mark_forwarded("reaper", ch, "name")
            self._emit_log("RPR->S21", f"Track {tr} Name '{nm}' -> Ch {ch}")

    def _loop_s21(self) -> None:
        while self._running and self._s21_sock:
            try:
                data, _ = self._s21_sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                for addr, args in decode_packet(data):
                    self._handle_s21_msg(addr, args)
                self._emit_stats()
            except Exception as exc:
                self._emit_log("ERR", f"S21 decode error: {exc}")

    def _loop_reaper(self) -> None:
        while self._running and self._reaper_sock:
            try:
                data, _ = self._reaper_sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                for addr, args in decode_packet(data):
                    self._handle_reaper_msg(addr, args)
                self._emit_stats()
            except Exception as exc:
                self._emit_log("ERR", f"REAPER decode error: {exc}")

    def send_test_to_reaper(self, channel: int = 1) -> None:
        """Send a harmless test pulse (0.0 dB / center pan) to verify REAPER connection."""
        tr = channel + int(self.cfg.track_offset)
        if self.cfg.fader_scale == "db":
            self.send_to_reaper(f"/track/{tr}/volume/db", 0.0)
        else:
            self.send_to_reaper(f"/track/{tr}/volume", 0.716)
        self.send_to_reaper(f"/track/{tr}/pan", 0.5)
        self._emit_log("TEST", f"Sent Test -> REAPER Track {tr} (0.0 dB, Pan Center)")
        self._emit_stats()
