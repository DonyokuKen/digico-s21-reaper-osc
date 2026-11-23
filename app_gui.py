"""Desktop GUI for DiGiCo S21 <-> REAPER OSC Bridge."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

from bridge_engine import BridgeConfig, DIGICO_PROFILES, DiGiCoReaperBridge
from reaper_osc_profile import REAPER_OSC_FILENAME, install_reaper_osc_profile


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_PATH = _app_dir() / "digico_s21_osc_config.json"

SETUP_GUIDE_TEXT = """QUICK SETUP GUIDE — DiGiCo S21 <-> REAPER via Ethernet (OSC)

1. ETHERNET CONNECTION
   - Connect an Ethernet cable from your DiGiCo S21 rear Ethernet port to your PC (direct or via switch).
   - Put both on the same subnet (Example: S21 = 192.168.1.21, PC = 192.168.1.50, Mask = 255.255.255.0).

2. CONFIGURE DiGiCo S21 (Firmware V3.0+)
   - On S21 touchscreen: Extensions -> OSC Control (or System -> External Control).
   - Enable OSC = YES.
   - Set S21 Receive Port = 8000 (matches "S21 Listen Port" here).
   - Touch "Add New Controller":
       * IP Address: your PC's IP (e.g. 192.168.1.50)
       * Send Port: 8001 (matches "Bridge RX Port" here)
   - Tip: Open "OSC Commands" on S21 to see your console's exact paths. If they differ,
     move a fader on S21 while Bridge is running — this app prints UNMAPPED paths in yellow
     so you can copy them into the Path fields!

3. CONFIGURE REAPER (One-Click!)
   - Click "Install REAPER .ReaperOSC" button in this app.
   - In REAPER: Options -> Preferences -> Control/OSC/web -> Add -> OSC (Open Sound Control):
       * Device name: DiGiCo S21 Bridge
       * Pattern config: DiGiCo_S21_Bridge
       * Mode: Configure device IP + local port
       * Local listen port: 9000   (matches "REAPER Listen Port")
       * Device IP: 127.0.0.1
       * Device port: 9001         (matches "Bridge REAPER RX Port")
       * Check "Allow binding messages to REAPER actions" (optional) -> Click OK.

4. START & VERIFY
   - Click "START BRIDGE".
   - Recommended live mode: "S21 -> REAPER" so S21 faders/pans/mutes safely control REAPER
     with zero risk of REAPER moving S21 faders during a live service!
   - Switch to "Bi-Directional" only when you want REAPER faders to drive S21 motorized faders.
"""


class BridgeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DiGiCo S21 <-> REAPER OSC Bridge")
        self.root.geometry("920x740")
        self.root.minsize(860, 660)
        self.root.configure(bg="#181a1f")

        self._log_q: queue.Queue = queue.Queue()
        self._stats_q: queue.Queue = queue.Queue()
        self.bridge: DiGiCoReaperBridge | None = None
        self.cfg = self._load_config()

        self._setup_style()
        self._build_ui()
        self._load_cfg_to_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queues()

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = "#181a1f"
        card = "#21252d"
        fg = "#e6eaf2"
        muted = "#9aa4b8"
        accent = "#3b82f6"
        style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 9))
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("TLabelframe", background=card, foreground="#8ab4f8", bordercolor="#323846")
        style.configure("TLabelframe.Label", background=card, foreground="#8ab4f8", font=("Segoe UI", 9, "bold"))
        style.configure("TLabel", background=card, foreground=fg)
        style.configure("Header.TLabel", background=bg, foreground=fg, font=("Segoe UI", 13, "bold"))
        style.configure("Sub.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=card, foreground=fg)
        style.map("TCheckbutton", background=[("active", card)])
        style.configure("TEntry", fieldbackground="#121419", foreground=fg, insertcolor=fg, bordercolor="#3a4152")
        style.configure(
            "TCombobox",
            fieldbackground="#121419",
            foreground="#ffffff",
            background="#2d3340",
            bordercolor="#3a4152",
            arrowcolor="#e6eaf2",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#121419"), ("focus", "#121419"), ("!disabled", "#121419")],
            foreground=[("readonly", "#ffffff"), ("focus", "#ffffff"), ("!disabled", "#ffffff")],
            selectbackground=[("readonly", "#2563eb"), ("focus", "#2563eb")],
            selectforeground=[("readonly", "#ffffff"), ("focus", "#ffffff")],
            background=[("active", "#3b4354"), ("readonly", "#2d3340")],
            arrowcolor=[("readonly", "#ffffff"), ("active", "#ffffff")],
        )
        self.root.option_add("*TCombobox*Listbox.background", "#121419")
        self.root.option_add("*TCombobox*Listbox.foreground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#2563eb")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("TButton", background="#2d3340", foreground=fg, padding=(10, 5), borderwidth=0)
        style.map("TButton", background=[("active", "#3b4354")])
        style.configure("Start.TButton", background="#16a34a", foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=(16, 7))
        style.map("Start.TButton", background=[("active", "#15803d")])
        style.configure("Stop.TButton", background="#dc2626", foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=(16, 7))
        style.map("Stop.TButton", background=[("active", "#b91c1c")])
        style.configure("Accent.TButton", background=accent, foreground="#ffffff", padding=(10, 6))
        style.map("Accent.TButton", background=[("active", "#2563eb")])

    def _load_config(self) -> BridgeConfig:
        if CONFIG_PATH.exists():
            try:
                return BridgeConfig.from_dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        return BridgeConfig()

    def _save_config(self) -> None:
        try:
            CONFIG_PATH.write_text(json.dumps(self.cfg.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

    def _build_ui(self) -> None:
        # Header
        hdr = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="DiGiCo S21  <->  REAPER OSC Bridge", style="Header.TLabel").pack(side="left")
        self.status_lbl = tk.Label(
            hdr, text="  STOPPED  ", bg="#3f272b", fg="#fca5a5",
            font=("Segoe UI", 9, "bold"), padx=8, pady=3
        )
        self.status_lbl.pack(side="right")
        ttk.Button(hdr, text="Setup Guide", command=self._show_guide).pack(side="right", padx=(0, 8))
        ttk.Button(hdr, text="Install REAPER .ReaperOSC", style="Accent.TButton", command=self._install_osc).pack(side="right", padx=(0, 8))

        # Top row: Network + Sync options
        top = ttk.Frame(self.root, padding=(14, 4))
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        # Network Card
        net = ttk.LabelFrame(top, text="Ethernet / UDP Ports", padding=10)
        net.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.v_s21_ip = tk.StringVar()
        self.v_s21_tx = tk.StringVar()
        self.v_s21_rx = tk.StringVar()
        self.v_rpr_ip = tk.StringVar()
        self.v_rpr_tx = tk.StringVar()
        self.v_rpr_rx = tk.StringVar()

        rows = [
            ("DiGiCo S21 IP:", self.v_s21_ip, "S21 Listen Port:", self.v_s21_tx),
            ("Bridge S21 RX Port:", self.v_s21_rx, "Echo Lock (ms):", None),
            ("REAPER IP:", self.v_rpr_ip, "REAPER Listen Port:", self.v_rpr_tx),
            ("Bridge REAPER RX:", self.v_rpr_rx, "Track Offset:", None),
        ]
        self.v_echo = tk.StringVar()
        self.v_offset = tk.StringVar()
        rows[1] = ("Bridge S21 RX Port:", self.v_s21_rx, "Echo Lock (ms):", self.v_echo)
        rows[3] = ("Bridge REAPER RX:", self.v_rpr_rx, "Track Offset:", self.v_offset)

        for r, (l1, var1, l2, var2) in enumerate(rows):
            ttk.Label(net, text=l1).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(net, textvariable=var1, width=15).grid(row=r, column=1, sticky="w", padx=(4, 12), pady=3)
            ttk.Label(net, text=l2).grid(row=r, column=2, sticky="w", pady=3)
            ttk.Entry(net, textvariable=var2, width=8).grid(row=r, column=3, sticky="w", padx=(4, 0), pady=3)

        # Sync Card
        sync = ttk.LabelFrame(top, text="Sync & Scaling", padding=10)
        sync.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.v_dir = tk.StringVar()
        self.v_fscale = tk.StringVar()
        self.v_pscale = tk.StringVar()
        self.v_maxch = tk.StringVar()

        ttk.Label(sync, text="Direction:").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(
            sync, textvariable=self.v_dir, state="readonly", width=19,
            values=["s21_to_reaper", "bidirectional", "reaper_to_s21"]
        ).grid(row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(sync, text="Max Ch:").grid(row=0, column=2, sticky="w", padx=(8, 2))
        ttk.Entry(sync, textvariable=self.v_maxch, width=5).grid(row=0, column=3, sticky="w")

        ttk.Label(sync, text="S21 Fader Scale:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(
            sync, textvariable=self.v_fscale, state="readonly", width=19,
            values=["db", "norm"]
        ).grid(row=1, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(sync, text="S21 Pan Scale:").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(
            sync, textvariable=self.v_pscale, state="readonly", width=19,
            values=["bipolar", "norm", "midi"]
        ).grid(row=2, column=1, sticky="w", padx=4, pady=3)

        chk = ttk.Frame(sync, style="Card.TFrame")
        chk.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.b_fader = tk.BooleanVar()
        self.b_pan = tk.BooleanVar()
        self.b_mute = tk.BooleanVar()
        self.b_solo = tk.BooleanVar()
        self.b_name = tk.BooleanVar()
        for txt, var in [("Fader", self.b_fader), ("Pan", self.b_pan), ("Mute", self.b_mute), ("Solo", self.b_solo), ("Name", self.b_name)]:
            ttk.Checkbutton(chk, text=txt, variable=var).pack(side="left", padx=(0, 8))

        # DiGiCo OSC Path Profile Card
        prof = ttk.LabelFrame(self.root, text="DiGiCo S21 OSC Path Mapping (Editable)", padding=10)
        prof.pack(fill="x", padx=14, pady=6)
        self.v_profile = tk.StringVar()
        top_p = ttk.Frame(prof, style="Card.TFrame")
        top_p.pack(fill="x", pady=(0, 6))
        ttk.Label(top_p, text="Preset Profile:").pack(side="left")
        cb = ttk.Combobox(
            top_p, textvariable=self.v_profile, state="readonly", width=44,
            values=list(DIGICO_PROFILES.keys()) + ["Custom"]
        )
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Label(top_p, text="Use {ch} for channel number (1..48)", foreground="#9aa4b8").pack(side="left", padx=8)

        grid_p = ttk.Frame(prof, style="Card.TFrame")
        grid_p.pack(fill="x")
        self.v_pfader = tk.StringVar()
        self.v_ppan = tk.StringVar()
        self.v_pmute = tk.StringVar()
        self.v_psolo = tk.StringVar()
        self.v_pname = tk.StringVar()
        fields = [
            ("Fader:", self.v_pfader), ("Pan:", self.v_ppan), ("Mute:", self.v_pmute),
            ("Solo:", self.v_psolo), ("Name:", self.v_pname)
        ]
        for idx, (lbl, var) in enumerate(fields):
            r, c = divmod(idx, 2)
            ttk.Label(grid_p, text=lbl).grid(row=r, column=c * 2, sticky="w", pady=2, padx=(0 if c == 0 else 14, 4))
            ttk.Entry(grid_p, textvariable=var, width=36).grid(row=r, column=c * 2 + 1, sticky="ew", pady=2)
        grid_p.columnconfigure(1, weight=1)
        grid_p.columnconfigure(3, weight=1)

        # Controls Bar
        ctrl = ttk.Frame(self.root, padding=(14, 4))
        ctrl.pack(fill="x")
        self.btn_toggle = ttk.Button(ctrl, text="START BRIDGE", style="Start.TButton", command=self._toggle_bridge)
        self.btn_toggle.pack(side="left")
        ttk.Button(ctrl, text="Test -> REAPER Ch 1", command=self._test_reaper).pack(side="left", padx=8)
        ttk.Button(ctrl, text="Clear Log", command=self._clear_log).pack(side="left")
        self.stats_lbl = ttk.Label(ctrl, text="S21 RX: 0  |  REAPER RX: 0  |  To REAPER: 0  |  To S21: 0  |  Unmapped: 0", style="Sub.TLabel")
        self.stats_lbl.pack(side="right")

        # Live OSC Monitor
        mon = ttk.LabelFrame(self.root, text="Live OSC Traffic Monitor (Move a DiGiCo S21 fader/pan to verify)", padding=6)
        mon.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.log_txt = tk.Text(
            mon, bg="#101216", fg="#d1d5db", insertbackground="#ffffff",
            font=("Consolas", 9), relief="flat", wrap="none", state="disabled"
        )
        sb = ttk.Scrollbar(mon, orient="vertical", command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(side="left", fill="both", expand=True)

        self.log_txt.tag_configure("S21->RPR", foreground="#4ade80")
        self.log_txt.tag_configure("RPR->S21", foreground="#60a5fa")
        self.log_txt.tag_configure("UNMAPPED", foreground="#facc15")
        self.log_txt.tag_configure("SYS", foreground="#c084fc")
        self.log_txt.tag_configure("TEST", foreground="#38bdf8")
        self.log_txt.tag_configure("ERR", foreground="#f87171")

    def _load_cfg_to_ui(self) -> None:
        c = self.cfg
        self.v_s21_ip.set(c.s21_ip)
        self.v_s21_tx.set(str(c.s21_tx_port))
        self.v_s21_rx.set(str(c.s21_rx_port))
        self.v_rpr_ip.set(c.reaper_ip)
        self.v_rpr_tx.set(str(c.reaper_tx_port))
        self.v_rpr_rx.set(str(c.reaper_rx_port))
        self.v_echo.set(str(c.echo_lock_ms))
        self.v_offset.set(str(c.track_offset))
        self.v_dir.set(c.direction)
        self.v_fscale.set(c.fader_scale)
        self.v_pscale.set(c.pan_scale)
        self.v_maxch.set(str(c.max_channels))
        self.b_fader.set(c.sync_fader)
        self.b_pan.set(c.sync_pan)
        self.b_mute.set(c.sync_mute)
        self.b_solo.set(c.sync_solo)
        self.b_name.set(c.sync_name)
        self.v_profile.set(c.profile)
        self.v_pfader.set(c.path_fader)
        self.v_ppan.set(c.path_pan)
        self.v_pmute.set(c.path_mute)
        self.v_psolo.set(c.path_solo)
        self.v_pname.set(c.path_name)

    def _read_ui_to_cfg(self) -> BridgeConfig:
        return BridgeConfig(
            s21_ip=self.v_s21_ip.get().strip() or "192.168.1.21",
            s21_tx_port=int(self.v_s21_tx.get().strip()),
            s21_rx_port=int(self.v_s21_rx.get().strip()),
            reaper_ip=self.v_rpr_ip.get().strip() or "127.0.0.1",
            reaper_tx_port=int(self.v_rpr_tx.get().strip()),
            reaper_rx_port=int(self.v_rpr_rx.get().strip()),
            echo_lock_ms=int(self.v_echo.get().strip()),
            track_offset=int(self.v_offset.get().strip()),
            max_channels=int(self.v_maxch.get().strip()),
            direction=self.v_dir.get(),
            fader_scale=self.v_fscale.get(),
            pan_scale=self.v_pscale.get(),
            sync_fader=self.b_fader.get(),
            sync_pan=self.b_pan.get(),
            sync_mute=self.b_mute.get(),
            sync_solo=self.b_solo.get(),
            sync_name=self.b_name.get(),
            profile=self.v_profile.get(),
            path_fader=self.v_pfader.get().strip(),
            path_pan=self.v_ppan.get().strip(),
            path_mute=self.v_pmute.get().strip(),
            path_solo=self.v_psolo.get().strip(),
            path_name=self.v_pname.get().strip(),
        )

    def _on_profile_selected(self, _evt=None) -> None:
        p = self.v_profile.get()
        if p in DIGICO_PROFILES:
            d = DIGICO_PROFILES[p]
            self.v_pfader.set(d["fader"])
            self.v_ppan.set(d["pan"])
            self.v_pmute.set(d["mute"])
            self.v_psolo.set(d["solo"])
            self.v_pname.set(d["name"])

    def _toggle_bridge(self) -> None:
        if self.bridge and self.bridge.running:
            self.bridge.stop()
            self.btn_toggle.configure(text="START BRIDGE", style="Start.TButton")
            self.status_lbl.configure(text="  STOPPED  ", bg="#3f272b", fg="#fca5a5")
            return
        try:
            self.cfg = self._read_ui_to_cfg()
            self._save_config()
            self.bridge = DiGiCoReaperBridge(
                self.cfg,
                on_log=lambda tag, msg: self._log_q.put((tag, msg)),
                on_stats=lambda st: self._stats_q.put(st),
            )
            self.bridge.start()
            self.btn_toggle.configure(text="STOP BRIDGE", style="Stop.TButton")
            self.status_lbl.configure(text="  RUNNING  ", bg="#14532d", fg="#86efac")
        except Exception as exc:
            messagebox.showerror("Cannot Start Bridge", f"Check your port numbers / firewall:\n\n{exc}")

    def _test_reaper(self) -> None:
        if not self.bridge or not self.bridge.running:
            messagebox.showinfo("Bridge Not Running", "Click START BRIDGE first, then click Test.")
            return
        self.bridge.send_test_to_reaper(channel=1)

    def _install_osc(self) -> None:
        try:
            path = install_reaper_osc_profile()
            self._append_log("SYS", f"Installed REAPER OSC config -> {path}")
            messagebox.showinfo(
                "REAPER OSC Installed",
                f"Installed:\n{path}\n\nNow in REAPER:\n"
                "1. Options -> Preferences -> Control/OSC/web -> Add -> OSC\n"
                f"2. Pattern config: {REAPER_OSC_FILENAME[:-10]}\n"
                "3. Local listen port: 9000 | Device IP: 127.0.0.1 | Device port: 9001"
            )
        except Exception as exc:
            messagebox.showerror("Install Failed", str(exc))

    def _show_guide(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("DiGiCo S21 <-> REAPER Setup Guide")
        win.geometry("680x500")
        win.configure(bg="#181a1f")
        txt = tk.Text(win, bg="#101216", fg="#e6eaf2", font=("Consolas", 9), wrap="word", padx=12, pady=12)
        txt.insert("1.0", SETUP_GUIDE_TEXT)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))

    def _clear_log(self) -> None:
        self.log_txt.configure(state="normal")
        self.log_txt.delete("1.0", "end")
        self.log_txt.configure(state="disabled")

    def _append_log(self, tag: str, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", f"[{ts}] [{tag:8s}] {msg}\n", tag)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_txt.delete("1.0", f"{lines - 400}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _poll_queues(self) -> None:
        for _ in range(40):
            try:
                tag, msg = self._log_q.get_nowait()
                self._append_log(tag, msg)
            except queue.Empty:
                break
        latest = None
        while True:
            try:
                latest = self._stats_q.get_nowait()
            except queue.Empty:
                break
        if latest:
            self.stats_lbl.configure(
                text=f"S21 RX: {latest['s21_rx']}  |  REAPER RX: {latest['reaper_rx']}  |  "
                     f"To REAPER: {latest['to_reaper']}  |  To S21: {latest['to_s21']}  |  Unmapped: {latest['unmapped']}"
            )
        self.root.after(60, self._poll_queues)

    def _on_close(self) -> None:
        try:
            self.cfg = self._read_ui_to_cfg()
            self._save_config()
        except Exception:
            pass
        if self.bridge and self.bridge.running:
            self.bridge.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    BridgeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
