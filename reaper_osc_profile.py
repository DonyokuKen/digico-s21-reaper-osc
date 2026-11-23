"""REAPER .ReaperOSC pattern configuration generator & installer."""
from __future__ import annotations
import os
from pathlib import Path

REAPER_OSC_FILENAME = "DiGiCo_S21_Bridge.ReaperOSC"

REAPER_OSC_CONTENT = """# DiGiCo S21 <-> REAPER OSC Bridge Pattern Config
# Installed automatically by DiGiCo S21 REAPER OSC Bridge.
# Supports up to 64 channels with VU metering disabled for clean live Ethernet traffic.

DEVICE_TRACK_COUNT 64
DEVICE_SEND_COUNT 4
DEVICE_RECEIVE_COUNT 4
DEVICE_FX_COUNT 8
DEVICE_FX_PARAM_COUNT 16
DEVICE_FX_INST_PARAM_COUNT 16
DEVICE_MARKER_COUNT 0
DEVICE_REGION_COUNT 0

REAPER_TRACK_FOLLOWS REAPER
DEVICE_TRACK_FOLLOWS DEVICE
DEVICE_TRACK_BANK_FOLLOWS DEVICE
DEVICE_FX_FOLLOWS DEVICE
DEVICE_EQ EXISTING
DEVICE_ROTARY_CENTER 0

# Master control
MASTER_VOLUME n/master/volume f/master/volume/db
MASTER_PAN n/master/pan

# Per-track Fader (dB and normalized), Pan (0.0=L, 0.5=C, 1.0=R), Mute, Solo, Name
TRACK_NAME s/track/@/name
TRACK_VOLUME f/track/@/volume/db n/track/@/volume
TRACK_PAN n/track/@/pan
TRACK_MUTE b/track/@/mute
TRACK_SOLO b/track/@/solo
TRACK_REC_ARM b/track/@/recarm
TRACK_MONITOR b/track/@/monitor
"""


def get_reaper_osc_dir() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "REAPER" / "OSC"
    return Path.home() / "AppData" / "Roaming" / "REAPER" / "OSC"


def install_reaper_osc_profile(target_dir: Path | None = None) -> Path:
    osc_dir = target_dir if target_dir is not None else get_reaper_osc_dir()
    osc_dir.mkdir(parents=True, exist_ok=True)
    out_path = osc_dir / REAPER_OSC_FILENAME
    out_path.write_text(REAPER_OSC_CONTENT, encoding="utf-8")
    return out_path
