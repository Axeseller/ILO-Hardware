"""Focus Device: desktop app for the phone-distance focus gadget.

Start it with "Focus Device.cmd" (double-click). It finds the ESP32 on USB by itself, can flash the
firmware onto it, pair your phone, show how far away the phone is, and run focus sessions.
Without a board, use Demo mode to try everything with a simulated phone.
"""
import collections
import csv
import datetime
import itertools
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from device import DeviceError, Link, SerialTransport, find_ports, guess_port  # noqa: E402
from simulator import SimTransport  # noqa: E402
import dev_panel  # noqa: E402

PENV = os.path.join(os.path.expanduser("~"), ".platformio", "penv", "Scripts")
PIO = os.path.join(PENV, "pio.exe")
PENV_PY = os.path.join(PENV, "python.exe")
DATA_DIR = os.path.join(os.environ.get("APPDATA", HERE), "FocusDevice")
SESSIONS_CSV = os.path.join(DATA_DIR, "sessions.csv")
CHIP_TO_ENV = {"ESP32": "esp32", "ESP32-S3": "esp32-s3", "ESP32-C3": "esp32-c3"}

ZONES = {
    "near": ("#e5484d", "NEAR", "Phone is right here"),
    "room": ("#f5a524", "IN THE ROOM", "Phone is nearby"),
    "far": ("#30a46c", "FAR", "Phone is across the room"),
    "away": ("#3e8e9e", "AWAY", "Phone not detected"),
}
SRC_TEXT = {"conn": "live connection", "adv": "phone's own broadcasts", "none": "no signal"}
FONT = "Segoe UI"
CHART_SECONDS = 120


def chip_from_esptool(text):
    """'ESP32', 'ESP32-S3', ... from esptool output, or None if it never reached the chip.

    A classic ESP32 reports a package name such as 'ESP32-D0WD-V3', so only the known variant
    suffixes count as variants; anything else that esptool identified is a classic ESP32.
    """
    m = re.search(r"\bESP32-(S2|S3|C2|C3|C5|C6|C61|H2|P4)\b", text)
    if m:
        return "ESP32-" + m.group(1)
    if re.search(r"(Chip type|Chip is|Detecting chip type)[^\n]*ESP32", text):
        return "ESP32"
    return None


def fmt_mmss(s):
    s = max(0, int(s))
    return "%d:%02d" % (s // 60, s % 60) if s < 3600 else "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


class App:
    def __init__(self, root):
        self.root = root
        self.link = None
        self.sim = None
        self.info = {}
        self.cfg = {}
        self.phones = {}  # id -> latest phone event
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=CHART_SECONDS * 4))
        self.focus = {}
        self.focus_tick_base = None  # (monotonic time, focus obj) for a smooth countdown
        self.ui_q = queue.Queue()
        self.busy = False  # flashing / detecting: don't auto-connect
        self.offered_flash = set()
        self.pair_dialog = None
        self.calib_dialog = None
        self.autocal_dialog = None
        self.dev_panel = None
        # Every serial line in and out, for the developer console: (seq, time, "tx"/"rx", text).
        self.traffic = collections.deque(maxlen=5000)
        self._seq = itertools.count()
        self.events_log = collections.deque(maxlen=60)  # notable events, for note snapshots
        self.truth = collections.deque(maxlen=CHART_SECONDS * 4)  # demo: the simulated true distance

        root.title("Focus Device")
        root.bind_all("<Control-d>", lambda e: self.open_dev_panel())
        self._style()
        self._build()
        # Size to the content (it grows with Windows display scaling), never smaller.
        root.update_idletasks()
        root.minsize(max(860, root.winfo_reqwidth()), root.winfo_reqheight())
        self.root.after(50, self._poll)
        self.root.after(300, self._auto_connect)

    # ------------------------------------------------------------------ layout
    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("vista")
        except tk.TclError:
            pass
        s.configure("Title.TLabel", font=(FONT, 18, "bold"))
        s.configure("Big.TLabel", font=(FONT, 34, "bold"))
        s.configure("Card.TLabelframe.Label", font=(FONT, 11, "bold"))
        s.configure("Status.TLabel", font=(FONT, 10))
        s.configure("Accent.TButton", font=(FONT, 11, "bold"), padding=(14, 6))

    def _build(self):
        top = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Focus Device", style="Title.TLabel").pack(side="left")
        self.status = ttk.Label(top, text="Looking for the board...", style="Status.TLabel")
        self.status.pack(side="right")
        ttk.Button(top, text="Developer", command=self.open_dev_panel).pack(side="right", padx=(8, 0))
        self.demo_btn = ttk.Button(top, text="Demo mode", command=self.toggle_demo)
        self.demo_btn.pack(side="right", padx=8)

        mid = ttk.Frame(self.root, padding=(16, 4))
        mid.pack(fill="x")

        # Zone indicator
        self.dial = tk.Canvas(mid, width=280, height=300, highlightthickness=0, bg=self._bg())
        self.dial.pack(side="left")

        # Focus card
        card = ttk.LabelFrame(mid, text="Focus session", style="Card.TLabelframe", padding=14)
        card.pack(side="left", fill="both", expand=True, padx=(16, 0))
        row = ttk.Frame(card)
        row.pack(fill="x")
        ttk.Label(row, text="Minutes:").pack(side="left")
        self.minutes = tk.StringVar(value="25")
        ttk.Spinbox(row, from_=1, to=600, width=5, textvariable=self.minutes).pack(side="left", padx=6)
        self.focus_btn = ttk.Button(row, text="Start focus", style="Accent.TButton", command=self.toggle_focus)
        self.focus_btn.pack(side="left", padx=8)
        self.timer = ttk.Label(card, text="--:--", style="Big.TLabel")
        self.timer.pack(anchor="w", pady=(10, 0))
        self.focus_stats = ttk.Label(card, text="Start a session, then put your phone away.", font=(FONT, 11))
        self.focus_stats.pack(anchor="w")
        self.banner = tk.Label(card, text="", font=(FONT, 12, "bold"), fg="white", bg=self._bg(), anchor="w", padx=8, pady=4)
        self.banner.pack(fill="x", pady=(10, 0))
        self.today = ttk.Label(card, text="", font=(FONT, 10))
        self.today.pack(anchor="w", pady=(8, 0))

        # Chart
        chart_box = ttk.LabelFrame(self.root, text="Distance, last 2 minutes", style="Card.TLabelframe", padding=(8, 4))
        chart_box.pack(fill="x", padx=16, pady=(10, 4))
        self.chart = tk.Canvas(chart_box, height=170, highlightthickness=0, bg="white")
        self.chart.pack(fill="x")

        # Tabs
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        self.nb = nb
        nb.add(self._phones_tab(nb), text="Phones")
        nb.add(self._settings_tab(nb), text="Settings")
        nb.add(self._board_tab(nb), text="Board")
        self.demo_tab = self._demo_tab(nb)

        self._draw_dial()
        self._draw_chart()
        self._update_today()

    def _bg(self):
        return ttk.Style().lookup("TFrame", "background") or "#f0f0f0"

    def _phones_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        cols = ("id", "addr", "zone", "dist", "signal", "key")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=4)
        for c, w, t in [("id", 40, "#"), ("addr", 170, "Bluetooth address"), ("zone", 110, "Zone"),
                        ("dist", 90, "Distance"), ("signal", 190, "Measured via"), ("key", 110, "Identity key")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x")
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Pair a phone...", style="Accent.TButton", command=self.open_pair).pack(side="left")
        ttk.Button(btns, text="Auto calibrate...", command=self.open_autocal).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Quick calibrate...", command=self.open_calibrate).pack(side="left", padx=8)
        ttk.Button(btns, text="Forget selected", command=self.forget_selected).pack(side="left")
        self.phones_hint = ttk.Label(f, text="", foreground="#555")
        self.phones_hint.pack(anchor="w", pady=(10, 0))
        return f

    def _settings_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        self.setting_vars = {}
        fields = [
            ("near_m", "Near distance (m)", "Closer than this counts as 'phone is right here'"),
            ("far_m", "Far distance (m)", "Further than this counts as 'far'"),
            ("grace_s", "Grace period (s)", "How long the phone may sit nearby during focus before it counts"),
            ("focus_min", "Button session (min)", "Session length when you press the board's button"),
            ("away_s", "Away after (s)", "No signal for this long = phone is away"),
            ("path_n", "Room factor", "2 = open space, 3-4 = walls/people (affects distance estimate)"),
            ("buzzer_pin", "Buzzer GPIO", "-1 = no buzzer. Wire a buzzer between this pin and GND"),
        ]
        for r, (key, label, hint) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            self.setting_vars[key] = v
            ttk.Entry(f, textvariable=v, width=8).grid(row=r, column=1, sticky="w", padx=8)
            ttk.Label(f, text=hint, foreground="#666").grid(row=r, column=2, sticky="w")
        r = len(fields)
        ttk.Label(f, text="Tracking mode").grid(row=r, column=0, sticky="w", pady=(10, 3))
        self.mode_var = tk.StringVar(value="hid")
        mf = ttk.Frame(f)
        mf.grid(row=r, column=1, columnspan=2, sticky="w", pady=(10, 3))
        ttk.Radiobutton(mf, text="Live (phone stays connected; works on iPhone and Android)", value="hid",
                        variable=self.mode_var).pack(anchor="w")
        ttk.Radiobutton(mf, text="Quiet (iPhone only; uses the phone's own broadcasts; never hides the keyboard)",
                        value="scan", variable=self.mode_var).pack(anchor="w")
        b = ttk.Frame(f)
        b.grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(b, text="Save to device", style="Accent.TButton", command=self.save_settings).pack(side="left")
        ttk.Button(b, text="Restore defaults", command=self.reset_settings).pack(side="left", padx=8)
        return f

    def _board_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.board_label = ttk.Label(row, text="No board connected.")
        self.board_label.pack(side="left")
        ttk.Button(row, text="Install / update firmware", command=self.flash_firmware).pack(side="right")
        ttk.Button(row, text="Reconnect", command=self.reconnect).pack(side="right", padx=8)
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(box, height=8, font=("Consolas", 9), wrap="none", state="disabled")
        sb = ttk.Scrollbar(box, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return f

    def _demo_tab(self, nb):
        f = ttk.Frame(nb, padding=10)
        ttk.Label(f, text="Simulated phone distance. Drag it and watch the zone and the focus session react.").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        self.demo_dist = tk.DoubleVar(value=0.5)
        self.demo_scale = ttk.Scale(row, from_=0.1, to=10, variable=self.demo_dist, command=self._demo_moved)
        self.demo_scale.pack(side="left", fill="x", expand=True)
        self.demo_label = ttk.Label(row, text="0.5 m", width=8)
        self.demo_label.pack(side="left", padx=8)
        self.demo_gone = tk.BooleanVar(value=False)
        self.demo_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Phone out of range (other room)", variable=self.demo_gone, command=self._demo_moved).pack(anchor="w")
        ttk.Checkbutton(f, text="Walk around by itself", variable=self.demo_auto, command=self._demo_auto).pack(anchor="w")
        return f

    # ------------------------------------------------------------------ helpers
    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.log.see("end")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 800:
            self.log.delete("1.0", "%d.0" % (lines - 800))
        self.log.configure(state="disabled")

    def bg(self, fn, done=None):
        """Run fn in a thread; done(result, error) runs later on the UI thread."""
        def work():
            try:
                r, e = fn(), None
            except Exception as ex:  # noqa: BLE001 - shown to the user
                r, e = None, ex
            if done:
                self.ui_q.put(lambda: done(r, e))
        threading.Thread(target=work, daemon=True).start()

    def request(self, cmd, done=None, timeout=3.0):
        link = self.link
        if not link:
            if done:
                done(None, RuntimeError("not connected"))
            return
        self.bg(lambda: link.request(cmd, timeout), done)

    def need_link(self):
        if self.link:
            return True
        messagebox.showinfo("Focus Device", "Connect the board first (or turn on Demo mode).")
        return False

    def on_tk_error(self, exc, val, tb):
        """The app runs without a console (pythonw), so show and keep errors instead of losing them."""
        import traceback
        text = "".join(traceback.format_exception(exc, val, tb))
        self.log_line("APP ERROR: %s (details in %s)" % (val, os.path.join(DATA_DIR, "errors.log")))
        self.traffic.append((next(self._seq), time.time(), "rx", "[app error] %s: %s" % (exc.__name__, val)))
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "errors.log"), "a", encoding="utf-8") as fh:
                fh.write("%s\n%s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), text))
        except OSError:
            pass

    def _tap(self, direction, text):  # called from the link's reader thread; deque appends are thread-safe
        self.traffic.append((next(self._seq), time.time(), direction, text))

    def _new_link(self, transport):
        return Link(transport, tap=self._tap)

    def open_dev_panel(self):
        if self.dev_panel:
            self.dev_panel.lift()
        else:
            self.dev_panel = dev_panel.DevPanel(self)

    def snapshot_markdown(self):
        """A short description of the board's state, attached to agent notes."""
        i, cfg = self.info, self.cfg
        lines = []
        if self.link and i:
            lines.append("- Board: %s %s on %s (%s)%s, mode %s, uptime %s s" % (
                i.get("fw"), i.get("ver"), i.get("chip"), self.link.name, " [DEMO]" if self.sim else "",
                cfg.get("mode"), i.get("uptime_s")))
            lines.append("- Settings: " + ", ".join("%s=%s" % kv for kv in sorted(cfg.items())))
        else:
            lines.append("- Board: not connected")
        for pid, p in sorted(self.phones.items()):
            lines.append("- Phone %s: %s, irk=%s, zone=%s, dist=%s m, src=%s, rssi=%s, rssi_f=%s, connected=%s" % (
                pid, p.get("addr", "?"), p.get("irk", "?"), p.get("zone"), p.get("dist_m"), p.get("src"),
                p.get("rssi"), p.get("rssi_f"), p.get("connected")))
        if self.focus:
            f = self.focus
            lines.append("- Focus: active=%s, elapsed %s s, pickups %s, near %s s" % (
                f.get("active"), f.get("elapsed_s"), f.get("pickups"), f.get("near_s")))
        recent = list(self.events_log)[-15:]
        if recent:
            lines.append("- Recent events (newest last):")
            lines += ["  - %s %s" % (time.strftime("%H:%M:%S", time.localtime(ts)), text) for ts, text in recent]
        return "\n".join(lines)

    # ------------------------------------------------------------------ connection
    def _auto_connect(self):
        if not self.link and not self.busy:
            port = guess_port()
            if port:
                self.connect(port)
            else:
                self.status.configure(text="No board found. Plug in the ESP32, or try Demo mode.")
        self.root.after(2500, self._auto_connect)

    def connect(self, port):
        try:
            link = self._new_link(SerialTransport(port))
        except Exception as e:  # noqa: BLE001
            self.status.configure(text="%s is busy or unavailable (%s)" % (port, str(e).split(":")[0]))
            return
        self.link = link
        self.status.configure(text="Connecting on %s..." % port)
        self.log_line("Opened %s" % port)
        self._fetch_info(first=True)

    def _fetch_info(self, first=False):
        def done(r, e):
            if e or not r:
                if first and self.link and not self.sim:
                    self._not_our_firmware()
                return
            self.info = r
            self.cfg = r.get("cfg", {})
            self.focus = r.get("focus", {})
            self.focus_tick_base = (time.monotonic(), dict(self.focus))
            for p in r.get("phones", []):
                self.phones.setdefault(p["id"], {}).update(p)
            self._sync_phone_table()
            self._fill_settings()
            self._update_status()
            self._refresh_focus_ui()
            if self.dev_panel:
                self.dev_panel.refresh_board()
                self.dev_panel.refresh_config()
        self.request("info", done, timeout=4.0)

    def _not_our_firmware(self):
        port = self.link.name if self.link else "?"
        self.status.configure(text="Board on %s doesn't run the focus firmware yet" % port)
        self.board_label.configure(text="Board on %s: focus firmware not installed." % port)
        if port not in self.offered_flash:
            self.offered_flash.add(port)
            if messagebox.askyesno("Install firmware?",
                                   "Found a board on %s, but it isn't running the Focus Device firmware.\n\n"
                                   "Install it now? (Takes 1-3 minutes.)" % port):
                self.flash_firmware()

    def reconnect(self):
        self.disconnect()
        self.root.after(200, self._auto_connect_once)

    def _auto_connect_once(self):
        port = guess_port()
        if port:
            self.connect(port)
        else:
            self.status.configure(text="No board found. Plug in the ESP32, or try Demo mode.")

    def disconnect(self):
        if self.link:
            self.link.close()
        self.link = None
        self.sim = None
        self.info = {}
        self.phones.clear()
        self._sync_phone_table()
        self._update_status()

    def toggle_demo(self):
        if self.sim:
            self.disconnect()
            self.nb.forget(self.demo_tab)
            self.demo_btn.configure(text="Demo mode")
            return
        self.disconnect()
        self.busy = False
        self.sim = SimTransport()
        self.link = self._new_link(self.sim)
        self.demo_btn.configure(text="Leave demo")
        self.nb.add(self.demo_tab, text="Demo")
        self.nb.select(self.demo_tab)
        self.log_line("Demo mode: simulated board and phone")
        self._fetch_info()

    def _demo_moved(self, *_):
        d = self.demo_dist.get()
        self.demo_label.configure(text="%.1f m" % d)
        if self.sim:
            self.demo_auto.set(False)
            self.sim.set_distance(None if self.demo_gone.get() else d)

    def _demo_auto(self):
        if self.sim:
            self.sim.set_auto(self.demo_auto.get())

    def _demo_follow(self):
        """While the simulated phone walks by itself, move the slider with it."""
        if not self.sim or not self.demo_auto.get():
            return
        d = self.sim.true_distance
        self.demo_gone.set(d is None)
        if d is not None:
            self.demo_dist.set(min(10.0, d))
            self.demo_label.configure(text="%.1f m" % d)
        else:
            self.demo_label.configure(text="gone")

    def _update_status(self):
        if not self.link:
            self.board_label.configure(text="No board connected.")
            return
        i = self.info
        if not i:
            return
        n = len([p for p in i.get("phones", [])])
        self.status.configure(text="Connected: %s on %s  |  %d phone%s paired" % (
            i.get("chip", "?"), self.link.name, n, "" if n == 1 else "s"))
        self.board_label.configure(text="%s, firmware %s %s, Bluetooth %s, mode: %s" % (
            i.get("chip", "?"), i.get("fw", "?"), i.get("ver", "?"), i.get("mac", "?"),
            "live" if self.cfg.get("mode") == "hid" else "quiet"))
        if not i.get("phones"):
            self.phones_hint.configure(text="No phone paired yet. Click 'Pair a phone...' to start.")
        else:
            self.phones_hint.configure(text="Tip: run Auto calibrate once in the room where you work, so the "
                                            "distances fit it.")

    # ------------------------------------------------------------------ events
    def _poll(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        link = self.link
        if link:
            for _ in range(200):
                try:
                    msg = link.events.get_nowait()
                except queue.Empty:
                    break
                self._on_event(msg)
        self._tick_focus_timer()
        self._demo_follow()
        self.root.after(50, self._poll)

    def _on_event(self, m):
        t = m.get("t")
        if self.dev_panel:
            self.dev_panel.on_event(m)
        if t not in ("phone", "_raw"):
            self.events_log.append((time.time(), " ".join(
                "%s=%s" % (k, v) for k, v in m.items() if k not in ("cfg", "phones"))))
        if t == "phone":
            pid = m["id"]
            p = self.phones.setdefault(pid, {})
            p.update(m)
            if "pending" not in m:
                p.pop("pending", None)
                p.pop("pending_ms", None)
            now = time.monotonic()
            self.history[pid].append((now, m.get("dist_m")))
            if self.sim and pid == min(self.phones):
                self.truth.append((now, self.sim.true_distance))
            self._draw_dial()
            self._draw_chart()
            self._sync_phone_table()
        elif t == "zone":
            self.phones.setdefault(m["id"], {})["zone"] = m["zone"]
            self.log_line("Phone %s: %s -> %s (%s m)" % (m["id"], m.get("prev"), m["zone"], m.get("dist_m")))
            self._draw_dial()
        elif t == "focus":
            self.focus = {k: v for k, v in m.items() if k not in ("t", "state")}
            self.focus_tick_base = (time.monotonic(), dict(self.focus))
            self._on_focus_event(m["state"])
        elif t == "pair":
            self.log_line("Pairing: %s %s" % (m.get("state"), m.get("addr", m.get("why", ""))))
            if self.pair_dialog:
                self.pair_dialog.on_pair_event(m)
            if m.get("state") in ("bonded",):
                self._fetch_info()
        elif t == "conn":
            self.log_line("Phone %s %s" % (m.get("id"), m.get("state")))
            if m.get("id") in self.phones:
                self.phones[m["id"]]["connected"] = m.get("state") == "connected"
        elif t == "calibrated":
            if self.calib_dialog:
                self.calib_dialog.on_result(m)
            self._fetch_info()
        elif t == "autocal":
            if self.autocal_dialog:
                self.autocal_dialog.on_event(m)
            if m.get("state") == "done":
                self.log_line("Auto calibration done: room factor %s, %s dBm at 1 m" % (m.get("path_n"), m.get("ref_conn")))
                self._fetch_info()
            elif m.get("state") == "failed":
                self.log_line("Auto calibration failed: " + m.get("msg", ""))
        elif t == "hello":
            self.log_line("Board started: %s %s on %s" % (m.get("fw"), m.get("ver"), m.get("chip")))
            if m.get("fw") == "focus-device":
                self._fetch_info()
            if self.pair_dialog:
                self.pair_dialog.on_hello()
        elif t == "log":
            self.log_line("Board: " + m.get("msg", ""))
        elif t == "_raw":
            self.log_line("| " + m["line"])
        elif t == "_link_lost":
            self.log_line("Board disconnected (%s)" % m.get("error", ""))
            self.link = None
            self.info = {}
            self.status.configure(text="Board unplugged. Waiting for it...")
            self._update_status()

    # ------------------------------------------------------------------ focus
    def toggle_focus(self):
        if not self.need_link():
            return
        if self.focus.get("active"):
            self.request("focus stop", self._focus_reply)
        else:
            try:
                minutes = max(1, min(600, int(self.minutes.get())))
            except ValueError:
                minutes = 25
            self.request("focus start %d" % minutes, self._focus_reply)

    def _focus_reply(self, r, e):
        if e:
            messagebox.showerror("Focus Device", str(e))
            return
        self.focus = r.get("focus", {})
        self.focus_tick_base = (time.monotonic(), dict(self.focus))
        self._refresh_focus_ui()

    def _on_focus_event(self, state):
        if state == "distracted":
            self._alert()
        elif state in ("done", "stopped"):
            self._save_session(state)
            if state == "done":
                self.root.bell()
                messagebox.showinfo("Focus Device", "Session complete! Pickups: %d, phone nearby for %s." % (
                    self.focus.get("pickups", 0), fmt_mmss(self.focus.get("near_s", 0))))
        self._refresh_focus_ui()

    def _alert(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:  # noqa: BLE001
            self.root.bell()
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _tick_focus_timer(self):
        if not self.focus_tick_base:
            return
        base_t, f = self.focus_tick_base
        if f.get("active"):
            dt = time.monotonic() - base_t
            left = f.get("left_s", -1)
            self.timer.configure(text=fmt_mmss(f.get("elapsed_s", 0) + dt) if left < 0 else fmt_mmss(left - dt))

    def _refresh_focus_ui(self):
        f = self.focus
        active = f.get("active")
        self.focus_btn.configure(text="Stop" if active else "Start focus")
        if active:
            self.focus_stats.configure(text="Pickups: %d   |   phone nearby: %s" % (
                f.get("pickups", 0), fmt_mmss(f.get("near_s", 0))))
        elif f.get("elapsed_s"):
            self.timer.configure(text=fmt_mmss(f.get("elapsed_s", 0)))
            self.focus_stats.configure(text="Last session: %s, %d pickup%s, phone nearby %s" % (
                fmt_mmss(f.get("elapsed_s", 0)), f.get("pickups", 0), "" if f.get("pickups") == 1 else "s",
                fmt_mmss(f.get("near_s", 0))))
        if active and f.get("distracted"):
            self.banner.configure(text="  Phone is next to you. Put it away!", bg="#e5484d")
        elif active:
            self.banner.configure(text="  Focusing. Phone is out of reach.", bg="#30a46c")
        else:
            self.banner.configure(text="", bg=self._bg())

    def _save_session(self, how):
        if self.sim:
            return
        f = self.focus
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            new = not os.path.exists(SESSIONS_CSV)
            with open(SESSIONS_CSV, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["ended", "how", "minutes", "pickups", "phone_nearby_s"])
                w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), how,
                            round(f.get("elapsed_s", 0) / 60, 1), f.get("pickups", 0), f.get("near_s", 0)])
        except OSError as e:
            self.log_line("Couldn't save the session: %s" % e)
        self._update_today()

    def _update_today(self):
        today = datetime.date.today().isoformat()
        n, minutes, pickups = 0, 0.0, 0
        try:
            with open(SESSIONS_CSV, encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if row["ended"].startswith(today):
                        n += 1
                        minutes += float(row["minutes"])
                        pickups += int(row["pickups"])
        except (OSError, KeyError, ValueError):
            pass
        self.today.configure(text="Today: %d session%s, %d min focused, %d pickup%s" % (
            n, "" if n == 1 else "s", round(minutes), pickups, "" if pickups == 1 else "s") if n else "")

    # ------------------------------------------------------------------ drawing
    def _primary_phone(self):
        order = {"near": 0, "room": 1, "far": 2, "away": 3}
        ps = [p for p in self.phones.values() if "zone" in p]
        if not ps:
            return None
        return min(ps, key=lambda p: (order.get(p.get("zone"), 4), p.get("dist_m") or 99))

    def _draw_dial(self):
        c = self.dial
        c.delete("all")
        p = self._primary_phone()
        if not self.link:
            color, title, sub, dist, via = "#b0b4ba", "NO BOARD", "Plug in the ESP32", "", ""
        elif not p:
            color, title, sub, dist, via = "#b0b4ba", "NO PHONE", "Pair a phone to start", "", ""
        else:
            zone = p.get("zone")
            color, title, sub = ZONES.get(zone, ZONES["away"])
            d, age = p.get("dist_m"), p.get("age_ms", -1)
            if zone == "away":
                dist, via = "", ""
            elif d is None:  # still counted as here (zone held), but no fresh signal right now
                dist = "?"
                sub = "Signal lost, phone probably moved"
                via = "last heard %d s ago" % (age / 1000) if age is not None and age >= 0 else ""
            else:
                dist = "%.1f m" % d
                via = "via " + SRC_TEXT.get(p.get("src"), "?")
        pend = p.get("pending") if p and self.link else None
        c.create_oval(20, 10, 260, 250, fill=color, outline="")
        c.create_text(140, 105, text=title, fill="white", font=(FONT, 20, "bold"))
        c.create_text(140, 145, text=dist, fill="white", font=(FONT, 26, "bold"))
        c.create_text(140, 185, text=sub, fill="white", font=(FONT, 10), width=200, justify="center")
        if pend in ZONES:  # a zone change is waiting out its dwell time: say so, so the lag isn't a mystery
            c.create_text(140, 268, text="changing to %s in %.0f s" % (ZONES[pend][1], p.get("pending_ms", 0) / 1000.0),
                          fill=ZONES[pend][0], font=(FONT, 10, "bold"))
        c.create_text(140, 290, text=via, fill="#555", font=(FONT, 9))

    def _draw_chart(self):
        c = self.chart
        c.delete("all")
        w = max(c.winfo_width(), 400)
        h = int(c["height"])
        left, right, top, bottom = 44, 10, 8, h - 18
        lo, hi = 0.1, 20.0

        def y_of(d):
            d = min(hi, max(lo, d))
            return top + (math.log10(hi) - math.log10(d)) / (math.log10(hi) - math.log10(lo)) * (bottom - top)

        for d in (0.1, 0.5, 1, 2, 5, 10, 20):
            y = y_of(d)
            c.create_line(left, y, w - right, y, fill="#eee")
            c.create_text(left - 6, y, text=("%g m" % d), anchor="e", fill="#888", font=(FONT, 8))
        near_m, far_m = self.cfg.get("near_m", 1.0), self.cfg.get("far_m", 4.0)
        c.create_line(left, y_of(near_m), w - right, y_of(near_m), fill="#e5484d", dash=(4, 3))
        c.create_text(w - right - 2, y_of(near_m) - 7, text="near", anchor="e", fill="#e5484d", font=(FONT, 8))
        c.create_line(left, y_of(far_m), w - right, y_of(far_m), fill="#30a46c", dash=(4, 3))
        c.create_text(w - right - 2, y_of(far_m) - 7, text="far", anchor="e", fill="#30a46c", font=(FONT, 8))
        c.create_text(left, h - 8, text="-2 min", anchor="w", fill="#888", font=(FONT, 8))
        c.create_text(w - right, h - 8, text="now", anchor="e", fill="#888", font=(FONT, 8))
        now = time.monotonic()
        if self.sim and self.truth:  # demo: where the simulated phone really is, to compare with the estimate
            seg = []
            for t, d in list(self.truth) + [(None, None)]:
                if t is not None and now - t > CHART_SECONDS:
                    continue
                if d is None:
                    if len(seg) >= 4:
                        c.create_line(*seg, fill="#9aa0a6", dash=(3, 3))
                    seg = []
                else:
                    seg += [left + (1 - (now - t) / CHART_SECONDS) * (w - left - right), y_of(d)]
            c.create_text(left + 6, top + 2, text="dashed grey = true distance (demo)", anchor="nw",
                          fill="#9aa0a6", font=(FONT, 8))
        colors = ["#2563eb", "#9333ea", "#ea580c"]
        for pid, hist in self.history.items():
            seg = []
            for t, d in hist:
                if now - t > CHART_SECONDS:
                    continue
                x = left + (1 - (now - t) / CHART_SECONDS) * (w - left - right)
                if d is None:
                    if len(seg) >= 4:
                        c.create_line(*seg, fill=colors[pid % 3], width=2)
                    seg = []
                else:
                    seg += [x, y_of(d)]
            if len(seg) >= 4:
                c.create_line(*seg, fill=colors[pid % 3], width=2)

    def _sync_phone_table(self):
        seen = set()
        for pid, p in sorted(self.phones.items()):
            if "addr" not in p and not self.info:
                continue
            iid = str(pid)
            seen.add(iid)
            d = p.get("dist_m")
            vals = (pid, p.get("addr", "?"), ZONES.get(p.get("zone"), ("", "?"))[1].title(),
                    ("%.1f m" % d) if d is not None and p.get("zone") != "away" else "-",
                    SRC_TEXT.get(p.get("src"), "-"), "yes" if p.get("irk") else ("no" if "irk" in p else "?"))
            if self.tree.exists(iid):
                self.tree.item(iid, values=vals)
            else:
                self.tree.insert("", "end", iid=iid, values=vals)
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)

    # ------------------------------------------------------------------ phones
    def open_pair(self):
        if self.need_link():
            self.pair_dialog = PairDialog(self)

    def open_calibrate(self):
        if self.need_link():
            if not self.phones:
                messagebox.showinfo("Calibrate", "Pair a phone first.")
                return
            self.calib_dialog = CalibrateDialog(self)

    def open_autocal(self):
        if self.need_link():
            if not self.phones:
                messagebox.showinfo("Auto calibrate", "Pair a phone first.")
                return
            if not self.autocal_dialog:
                self.autocal_dialog = AutoCalibrateDialog(self)

    def forget_selected(self):
        sel = self.tree.selection()
        if not sel or not self.need_link():
            return
        pid = int(sel[0])
        if not messagebox.askyesno("Forget phone", "Forget phone %d? You'll need to pair it again, and also "
                                   "remove 'Focus Device' from the phone's Bluetooth list." % pid):
            return

        def done(r, e):
            if e:
                messagebox.showerror("Forget phone", str(e))
            self.phones.clear()
            self.history.clear()
            self._fetch_info()
        self.request("forget %d" % pid, done)

    # ------------------------------------------------------------------ settings
    def _fill_settings(self):
        for k, v in self.setting_vars.items():
            if k in self.cfg:
                v.set(str(self.cfg[k]))
        self.mode_var.set(self.cfg.get("mode", "hid"))
        self.minutes.set(str(self.cfg.get("focus_min", 25)))
        self._draw_chart()

    def save_settings(self):
        if not self.need_link():
            return
        changes = [(k, v.get().strip()) for k, v in self.setting_vars.items()
                   if v.get().strip() and str(self.cfg.get(k)) != v.get().strip()]
        # near/far must stay ordered: apply in an order that never crosses them
        changes.sort(key=lambda kv: 0 if kv[0] == "far_m" and float(kv[1] or 0) > self.cfg.get("far_m", 0) else 1)
        mode = self.mode_var.get()
        mode_change = mode != self.cfg.get("mode")
        link = self.link

        def work():
            errors = []
            for k, v in changes:
                try:
                    link.request("cfg %s %s" % (k, v))
                except (DeviceError, TimeoutError) as e:
                    errors.append("%s: %s" % (k, e))
            if mode_change:
                try:
                    link.request("cfg mode %s" % mode)
                except (DeviceError, TimeoutError) as e:
                    errors.append("mode: %s" % e)
            return errors

        def done(errors, e):
            if e or errors:
                messagebox.showerror("Settings", "\n".join(errors or [str(e)]))
            elif mode_change:
                messagebox.showinfo("Settings", "Saved. The board restarts to switch mode. If your phone was "
                                    "paired in the other mode, pair it again.")
            self._fetch_info()
        self.bg(work, done)

    def reset_settings(self):
        if self.need_link() and messagebox.askyesno("Settings", "Restore the default settings (calibration too)?"):
            self.request("cfg reset", lambda r, e: self._fetch_info())

    # ------------------------------------------------------------------ firmware
    def flash_firmware(self, env=None):
        """Build and upload the firmware. env=None detects the chip; or force 'esp32', 'esp32-s3', 'esp32-c3'."""
        if self.sim:
            messagebox.showinfo("Firmware", "Leave Demo mode first.")
            return
        if not os.path.exists(PIO):
            messagebox.showerror("Firmware", "PlatformIO isn't installed. Run setup.cmd in the focus-device folder.")
            return
        port = self.link.name if self.link else guess_port()
        if not port:
            messagebox.showinfo("Firmware", "Plug in the ESP32 with a USB data cable first.")
            return
        self.busy = True
        if self.link:
            self.link.close()
            self.link = None
        self.nb.select(2)
        self.status.configure(text="Installing firmware on %s..." % port)
        self.log_line("Detecting the chip on %s..." % port if not env else "Board type set to %s" % env)
        self.bg(lambda: self._flash_work(port, env), self._flash_done)

    def _run_logged(self, args):
        env = dict(os.environ, PLATFORMIO_DISABLE_PROGRESSBAR="true", PYTHONUNBUFFERED="1")
        p = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             text=True, errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = []
        for line in p.stdout:
            line = line.rstrip()
            out.append(line)
            if line:
                self.ui_q.put(lambda l=line: self.log_line(l))
                self.traffic.append((next(self._seq), time.time(), "rx", "[flash] " + line))
        return p.wait(), "\n".join(out)

    def _flash_work(self, port, env=None):
        if not env:
            code, text = self._run_logged([PENV_PY, "-m", "esptool", "--port", port, "chip-id"])
            if code != 0:
                code, text = self._run_logged([PENV_PY, "-m", "esptool", "--port", port, "chip_id"])
            chip = chip_from_esptool(text)
            if not chip:
                raise RuntimeError("Couldn't talk to the board's bootloader. Hold the BOOT button, press and release "
                                   "EN/RST, release BOOT, then try again.")
            env = CHIP_TO_ENV.get(chip)
            if not env:
                raise RuntimeError("This is an %s, which this project doesn't support (needs ESP32, S3 or C3)." % chip)
            self.ui_q.put(lambda: self.log_line("Chip: %s" % chip))
        self.ui_q.put(lambda: self.log_line("Building '%s' and uploading (first time: a few minutes)" % env))
        code, text = self._run_logged([PIO, "run", "-e", env, "-t", "upload", "--upload-port", port])
        if code != 0:
            raise RuntimeError("Upload failed; see the log on the Board tab.")
        return port

    def _flash_done(self, port, e):
        self.busy = False
        if e:
            self.status.configure(text="Firmware install failed")
            messagebox.showerror("Firmware", str(e))
            return
        self.log_line("Firmware installed.")
        self.offered_flash.discard(port)
        self.root.after(1500, lambda: self.connect(port))


class PairDialog:
    """Walks the user through pairing their phone."""

    def __init__(self, app):
        self.app = app
        self.win = w = tk.Toplevel(app.root)
        w.title("Pair a phone")
        w.geometry("520x380")
        w.transient(app.root)
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.body = ttk.Frame(w, padding=18)
        self.body.pack(fill="both", expand=True)
        self.waiting_restart = None
        self.until = 0
        self.step_choose()

    def clear(self):
        for c in self.body.winfo_children():
            c.destroy()

    def step_choose(self):
        self.clear()
        ttk.Label(self.body, text="Which phone are you pairing?", font=(FONT, 14, "bold")).pack(anchor="w")
        self.kind = tk.StringVar(value="iphone")
        ttk.Radiobutton(self.body, text="iPhone", value="iphone", variable=self.kind, command=self._kind_changed).pack(anchor="w", pady=(10, 0))
        ttk.Radiobutton(self.body, text="Android", value="android", variable=self.kind, command=self._kind_changed).pack(anchor="w")
        self.mode = tk.StringVar(value=self.app.cfg.get("mode", "hid"))
        self.mode_box = ttk.LabelFrame(self.body, text="How should it track the phone?", padding=10)
        self.mode_box.pack(fill="x", pady=12)
        self.r_live = ttk.Radiobutton(self.mode_box, value="hid", variable=self.mode,
                                      text="Live: the phone stays connected. Fast and accurate.\n"
                                           "On iPhone the on-screen keyboard may hide while connected.")
        self.r_live.pack(anchor="w")
        self.r_quiet = ttk.Radiobutton(self.mode_box, value="scan", variable=self.mode,
                                       text="Quiet (iPhone only): listens to the phone's own broadcasts.\n"
                                            "Keyboard unaffected; updates slower when the phone is locked.")
        self.r_quiet.pack(anchor="w", pady=(6, 0))
        ttk.Button(self.body, text="Next", style="Accent.TButton", command=self.step_start).pack(anchor="e", side="bottom")
        self._kind_changed()

    def _kind_changed(self):
        if self.kind.get() == "android":
            self.mode.set("hid")
            self.r_quiet.state(["disabled"])
        else:
            self.r_quiet.state(["!disabled"])

    def step_start(self):
        mode = self.mode.get()
        if mode != self.app.cfg.get("mode", "hid"):
            self.clear()
            ttk.Label(self.body, text="Switching the board to %s mode (it restarts)..." % (
                "live" if mode == "hid" else "quiet"), font=(FONT, 11)).pack(anchor="w")
            self.waiting_restart = time.monotonic()

            def done(r, e):
                if e:
                    messagebox.showerror("Pair", str(e))
                    self.close()
                    return
                self.app.cfg["mode"] = mode
                if self.app.sim:  # the simulator doesn't restart
                    self.on_hello()
            self.app.request("cfg mode %s" % mode, done)
            self.win.after(15000, self._restart_timeout)
        else:
            self.open_pairing()

    def _restart_timeout(self):
        if self.waiting_restart:
            self.waiting_restart = None
            self.open_pairing()

    def on_hello(self):
        if self.waiting_restart:
            self.waiting_restart = None
            self.win.after(1500, self.open_pairing)  # give the board a moment to come up

    def open_pairing(self):
        if not self.app.link:  # the restart can reopen the port; wait for the reconnect
            self.win.after(1000, self.open_pairing)
            return

        def done(r, e):
            if e:
                messagebox.showerror("Pair", str(e))
                self.close()
                return
            self.until = time.monotonic() + r.get("left_s", 120)
            self.step_instructions()
        self.app.request("pair 120", done)

    def step_instructions(self):
        self.clear()
        ttk.Label(self.body, text="Now, on your phone:", font=(FONT, 14, "bold")).pack(anchor="w")
        if self.kind.get() == "iphone":
            steps = ["Open Settings > Bluetooth (keep Bluetooth on).",
                     "Under 'Other Devices', tap 'Focus Device'.",
                     "Tap 'Pair' when the pairing request appears."]
        else:
            steps = ["Open Settings > Connected devices > Pair new device.",
                     "Tap 'Focus Device' in the list.",
                     "Tap 'Pair' when asked."]
        for i, s in enumerate(steps, 1):
            ttk.Label(self.body, text="%d. %s" % (i, s), font=(FONT, 11)).pack(anchor="w", pady=2)
        self.wait_label = ttk.Label(self.body, text="", foreground="#555")
        self.wait_label.pack(anchor="w", pady=(14, 0))
        self.pb = ttk.Progressbar(self.body, mode="indeterminate")
        self.pb.pack(fill="x", pady=6)
        self.pb.start(12)
        ttk.Button(self.body, text="Cancel", command=self.cancel).pack(anchor="e", side="bottom")
        self._countdown()

    def _countdown(self):
        if not self.win.winfo_exists() or not hasattr(self, "wait_label") or not self.wait_label.winfo_exists():
            return
        left = int(self.until - time.monotonic())
        if left >= 0:
            self.wait_label.configure(text="Waiting for your phone... (%d s left)" % left)
            self.win.after(500, self._countdown)

    def on_pair_event(self, m):
        st = m.get("state")
        if st == "bonded":
            self.clear()
            ttk.Label(self.body, text="Paired!", font=(FONT, 16, "bold"), foreground="#30a46c").pack(anchor="w")
            msg = "Your phone is now known to the device."
            if not m.get("irk"):
                msg += ("\n\nThe phone didn't share its identity key, so the device can only measure it "
                        "while it's connected." + (" Quiet mode won't work with it: use Live mode." if self.mode.get() == "scan" else ""))
            msg += "\n\nNext: calibrate, so the distances fit your room. It takes about a minute."
            ttk.Label(self.body, text=msg, wraplength=470, font=(FONT, 11)).pack(anchor="w", pady=10)
            row = ttk.Frame(self.body)
            row.pack(side="bottom", fill="x")
            ttk.Button(row, text="Calibrate now", style="Accent.TButton",
                       command=lambda: (self.close(), self.app.open_autocal())).pack(side="right")
            ttk.Button(row, text="Done", command=self.close).pack(side="right", padx=8)
        elif st == "failed":
            self.wait_label.configure(text="Pairing failed on the phone. Try again: tap 'Focus Device' once more.")
        elif st == "closed" and m.get("why") in ("timeout", "stopped"):
            self.clear()
            ttk.Label(self.body, text="Pairing timed out.", font=(FONT, 14, "bold")).pack(anchor="w")
            ttk.Label(self.body, text="Didn't see 'Focus Device' on the phone? Make sure the board is powered and "
                                      "close by, then try again.", wraplength=470).pack(anchor="w", pady=10)
            ttk.Button(self.body, text="Try again", command=self.step_choose).pack(anchor="e", side="bottom")

    def cancel(self):
        self.app.request("pair stop")
        self.close()

    def close(self):
        self.app.pair_dialog = None
        self.win.destroy()


class CalibrateDialog:
    def __init__(self, app):
        self.app = app
        self.win = w = tk.Toplevel(app.root)
        w.title("Calibrate")
        w.geometry("480x300")
        w.transient(app.root)
        w.protocol("WM_DELETE_WINDOW", self.close)
        b = self.body = ttk.Frame(w, padding=18)
        b.pack(fill="both", expand=True)
        ttk.Label(b, text="Quick calibrate (one spot)", font=(FONT, 14, "bold")).pack(anchor="w")
        ttk.Label(b, text="Put your phone where it normally sits on your desk, screen up, and don't touch it. "
                          "Measure roughly how far it is from the board. (Auto calibrate uses two spots and "
                          "is more accurate.)", wraplength=440).pack(anchor="w", pady=8)
        row = ttk.Frame(b)
        row.pack(anchor="w")
        ttk.Label(row, text="Distance to the board (m):").pack(side="left")
        self.metres = tk.StringVar(value="1.0")
        ttk.Entry(row, textvariable=self.metres, width=6).pack(side="left", padx=6)
        self.msg = ttk.Label(b, text="", wraplength=440)
        self.msg.pack(anchor="w", pady=10)
        self.pb = ttk.Progressbar(b, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.start_btn = ttk.Button(b, text="Start (8 seconds)", style="Accent.TButton", command=self.start)
        self.start_btn.pack(anchor="e", side="bottom")

    def start(self):
        try:
            m = float(self.metres.get())
            assert 0.1 <= m <= 5
        except (ValueError, AssertionError):
            self.msg.configure(text="Enter a distance between 0.1 and 5 metres.")
            return
        self.start_btn.state(["disabled"])

        def done(r, e):
            if e:
                self.msg.configure(text=str(e))
                self.start_btn.state(["!disabled"])
                return
            self.t0 = time.monotonic()
            self.msg.configure(text="Measuring... keep the phone still.")
            self._progress()
        self.app.request("calibrate 8 %.2f" % m, done)

    def _progress(self):
        if not self.win.winfo_exists():
            return
        frac = (time.monotonic() - self.t0) / 8.0
        self.pb["value"] = min(100, frac * 100)
        if frac < 1.2:
            self.win.after(200, self._progress)

    def on_result(self, m):
        if not self.win.winfo_exists():
            return
        self.pb["value"] = 100
        if m.get("ok"):
            parts = []
            if "ref_conn" in m:
                parts.append("live connection: %.0f dBm at 1 m" % m["ref_conn"])
            if "ref_adv" in m:
                parts.append("broadcasts: %.0f dBm at 1 m" % m["ref_adv"])
            self.msg.configure(text="Done! Saved on the board (%s)." % "; ".join(parts), foreground="#30a46c")
        else:
            self.msg.configure(text=m.get("msg", "Calibration failed."), foreground="#e5484d")
        self.start_btn.configure(text="Close", command=self.close)
        self.start_btn.state(["!disabled"])

    def close(self):
        self.app.calib_dialog = None
        self.win.destroy()


class AutoCalibrateDialog:
    """Two-spot calibration (`autocal`). The board waits until the phone lies still at each spot,
    measures it by itself, then fits the distance model to the room and saves it."""

    INTRO = ("The board measures your phone at two distances and works out how the signal fades in "
             "this room, so the distances it shows come out right.\n\n"
             "1. Lay the phone flat at the first distance from the board (use a ruler) and take your hand away.\n"
             "2. Click Start. When this window says so, move the phone to the second distance.\n"
             "3. There, turn the phone when asked: its signal is stronger in some directions than others, "
             "and the board learns how much, so that turning the phone doesn't look like moving it.\n\n"
             "The board notices by itself when the phone lies still, and measures by itself.")

    def __init__(self, app):
        self.app = app
        self.running = False
        self.state = "idle"   # idle / place / measuring / done / failed (tests read these)
        self.step = 0
        self.result = None
        self.win = w = tk.Toplevel(app.root)
        w.title("Auto calibrate")
        w.geometry("540x450")
        w.transient(app.root)
        w.protocol("WM_DELETE_WINDOW", self.close)
        b = ttk.Frame(w, padding=18)
        b.pack(fill="both", expand=True)
        self.title = ttk.Label(b, text="Auto calibrate", font=(FONT, 14, "bold"))
        self.title.pack(anchor="w")
        self.text = ttk.Label(b, text=self.INTRO, wraplength=480, font=(FONT, 10))
        self.text.pack(anchor="w", pady=8)
        row = ttk.Frame(b)
        row.pack(anchor="w")
        ttk.Label(row, text="First spot (m):").pack(side="left")
        self.near = tk.StringVar(value="0.5")
        self.near_entry = ttk.Entry(row, textvariable=self.near, width=6)
        self.near_entry.pack(side="left", padx=(6, 16))
        ttk.Label(row, text="Second spot (m):").pack(side="left")
        self.far = tk.StringVar(value="2.0")
        self.far_entry = ttk.Entry(row, textvariable=self.far, width=6)
        self.far_entry.pack(side="left", padx=6)
        self.orient = tk.BooleanVar(value=True)
        self.orient_check = ttk.Checkbutton(b, text="Also measure the phone's orientation (about 1 minute more)",
                                            variable=self.orient)
        self.orient_check.pack(anchor="w", pady=(8, 0))
        self.msg = ttk.Label(b, text="", wraplength=480)
        self.msg.pack(anchor="w", pady=8)
        self.pb = ttk.Progressbar(b, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        btns = ttk.Frame(b)
        btns.pack(side="bottom", fill="x")
        self.btn = ttk.Button(btns, text="Start", style="Accent.TButton", command=self.start)
        self.btn.pack(side="right")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.close)
        self.cancel_btn.pack(side="right", padx=8)

    def start(self):
        try:
            m1, m2 = float(self.near.get()), float(self.far.get())
            assert 0.2 <= m1 <= 3 and m1 * 1.5 <= m2 <= 10
        except (ValueError, AssertionError):
            self.msg.configure(text="First spot: 0.2 to 3 m. Second spot: at least 1.5 times as far, up to 10 m.",
                               foreground="#e5484d")
            return
        self.btn.state(["disabled"])
        self.msg.configure(text="", foreground="")
        self.result = None

        def done(r, e):
            if not self.win.winfo_exists():
                return
            if e:
                self.msg.configure(text=str(e), foreground="#e5484d")
                self.btn.state(["!disabled"])
                return
            self.running = True
            for entry in (self.near_entry, self.far_entry, self.orient_check):
                entry.state(["disabled"])
        self.app.request("autocal %.2f %.2f %d" % (m1, m2, 3 if self.orient.get() else 0), done)

    def on_event(self, m):
        if not self.win.winfo_exists():
            return
        st = m.get("state")
        if st == "place":
            self.state, self.step = "place", m.get("step", 1)
            self.title.configure(text="Step %d of 2" % self.step)
            if self.step == 1:
                text = ("Lay the phone flat, %s m from the board, and take your hand away. "
                        "Measuring starts by itself once the signal is steady." % _metres(m.get("metres")))
            else:
                text = ("Now move the phone to %s m from the board, lay it down pointing the same way as "
                        "before, and step back. Measuring starts by itself once the signal is steady."
                        % _metres(m.get("metres")))
            if m.get("retry"):
                text = "The signal changed while measuring, so that spot is measured again. " + text
            self.text.configure(text=text)
            self.pb.configure(mode="indeterminate")
            self.pb.start(12)
        elif st == "turn":
            self.state, self.turn = "turn", m.get("turn", 1)
            self.title.configure(text="Orientation %d of %d" % (self.turn, m.get("of", 3)))
            if m.get("how") == "face_down":
                text = "Now flip the phone face down on the same spot, then let go and step back."
            elif self.turn == 1:
                text = "Leave the phone on its spot but turn it a quarter turn (still flat), then let go and step back."
            else:
                text = "Turn it another quarter turn, then let go and step back."
            if m.get("retry"):
                text = "The signal changed while measuring, so this is measured again. " + text
            self.text.configure(text=text + " Measuring starts by itself after a few seconds.")
            self.pb.configure(mode="indeterminate")
            self.pb.start(12)
        elif st == "measuring":
            self.state, self.step = "measuring", m.get("step", self.step)
            if "turn" in m:
                self.text.configure(text="Measuring this orientation... Don't touch the phone, and stay where you are.")
            else:
                self.text.configure(text="Measuring spot %d... Don't touch the phone, and stay where you are." % self.step)
            self.pb.stop()
            self.pb.configure(mode="determinate")
            self.t0, self.secs = time.monotonic(), max(1, m.get("seconds", 6))
            self._progress()
        elif st == "done":
            self.state, self.running, self.result = "done", False, m
            self._finished()
            self.title.configure(text="Calibrated")
            text = ("Saved on the board.\n\nRoom factor: %.2f (2 = open space, 3-4 = walls and people)\n"
                    "Signal at 1 m: %.0f dBm" % (m.get("path_n", 0), m.get("ref_conn", 0)))
            if "orient_db" in m:
                text += ("\nOrientation: turning the phone changes its signal by up to %.0f dB here. The board "
                         "now allows for that, so turning the phone in place won't change its zone."
                         % m["orient_db"])
            if m.get("clamped"):
                text += ("\n\nThe signal changed unusually little or much between the two spots, so the room "
                         "factor was held at its limit. Check both distances and run it again if distances look off.")
            self.text.configure(text=text)
            self.msg.configure(text="Done!", foreground="#30a46c")
            self.btn.configure(text="Close", command=self.close)
            self.btn.state(["!disabled"])
        elif st in ("failed", "stopped"):
            self.state, self.running = "failed", False
            self._finished()
            self.title.configure(text="Auto calibrate")
            self.text.configure(text=self.INTRO)
            self.msg.configure(text=m.get("msg", "Calibration was stopped."), foreground="#e5484d")
            self.btn.configure(text="Try again", command=self.start)
            self.btn.state(["!disabled"])
            for entry in (self.near_entry, self.far_entry, self.orient_check):
                entry.state(["!disabled"])

    def _finished(self):
        self.pb.stop()
        self.pb.configure(mode="determinate")
        self.pb["value"] = 100 if self.state == "done" else 0

    def _progress(self):
        if not self.win.winfo_exists() or self.state != "measuring":
            return
        self.pb["value"] = min(100, (time.monotonic() - self.t0) / self.secs * 100)
        self.win.after(200, self._progress)

    def close(self):
        if self.running:
            self.app.request("autocal stop")
        self.app.autocal_dialog = None
        self.win.destroy()


def _metres(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if isinstance(v, (int, float)) else "?"


def main():
    root = tk.Tk()
    try:  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    app = App(root)
    root.report_callback_exception = app.on_tk_error
    if "--demo" in sys.argv:
        root.after(200, app.toggle_demo)
    root.mainloop()


if __name__ == "__main__":
    main()
