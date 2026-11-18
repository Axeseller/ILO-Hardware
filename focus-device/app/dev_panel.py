"""Developer panel: configure and debug the board, and leave notes for AI assistants.

Opened from the main window's "Developer" button (or Ctrl+D). Tabs:
  Board        what's connected, firmware install (with board-type override), restart / pairing / resets
  Config       every setting the firmware has, each with its own Set button, plus stability presets
  Signal       raw vs filtered RSSI per phone, with the zone thresholds drawn in
  Console      raw serial traffic; type any command (see PROTOCOL.md)
  Agent notes  notes saved to AGENT_NOTES.md, which Claude Code / Cursor read every session
"""
import collections
import math
import os
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import agent_notes
from device import DeviceError

FONT = "Segoe UI"
MONO = ("Consolas", 9)

# key, label, allowed values, what it does
CFG_SPEC = [
    ("near_m", "Near distance (m)", "0.1 to < far", "closer = 'phone is right here'"),
    ("far_m", "Far distance (m)", "> near, max 30", "further = 'far'"),
    ("hyst_db", "Hysteresis (dB)", "0-10", "each boundary is +/- this; bigger = steadier, slower"),
    ("near_in_s", "Enter NEAR after (s)", "0-30", "readings must say NEAR this long"),
    ("near_out_s", "Leave NEAR after (s)", "0-60", "readings must say not-NEAR this long"),
    ("zone_s", "ROOM <-> FAR after (s)", "0-60", ""),
    ("smooth_s", "Smoothing at rest (s)", "0-20", "steadies the distance of a phone lying still; 0 = off"),
    ("orient_db", "Orientation spread (dB)", "0-20", "signal change when the phone is turned; hysteresis is at "
                                                     "least half of it; set by Auto calibrate"),
    ("away_s", "Away after (s)", "5-600", "no signal this long = away"),
    ("grace_s", "Pickup grace (s)", "0-600", "in focus, NEAR this long = a pickup"),
    ("focus_min", "Button session (min)", "1-1440", "BOOT button session length"),
    ("path_n", "Path-loss exponent", "1.5-5", "2 = open space, 3-4 = walls/people; set by Auto calibrate"),
    ("ref_conn", "Connection RSSI at 1 m", "-100 to -20", "set by calibration"),
    ("ref_adv", "Broadcast RSSI at 1 m", "-100 to -20", "set by calibration"),
    ("stream_ms", "Phone event interval (ms)", "100-10000", ""),
    ("buzzer_pin", "Buzzer GPIO", "-1 = none", "flash/USB/LED/button pins refused"),
    ("mode", "Tracking mode", "hid / scan", "changing it restarts the board"),
]
PRESETS = [
    ("Snappy", {"hyst_db": 2, "near_in_s": 1.5, "near_out_s": 2, "zone_s": 2}),
    ("Balanced (default)", {"hyst_db": 3, "near_in_s": 3, "near_out_s": 5, "zone_s": 5}),
    ("Steady", {"hyst_db": 4, "near_in_s": 3, "near_out_s": 8, "zone_s": 6}),
]
QUICK_CMDS = ["help", "info", "phones", "cfg", "focus", "stream off", "stream on"]
CHART_S = 60


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


class DevPanel:
    def __init__(self, app):
        self.app = app
        self.win = w = tk.Toplevel(app.root)
        w.title("Focus Device - Developer")
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.nb = ttk.Notebook(w)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.msg = ttk.Label(w, text="", anchor="w")
        self.msg.pack(fill="x", padx=12, pady=(0, 8))
        self.sig = collections.defaultdict(lambda: collections.deque(maxlen=CHART_S * 25))
        self.sig_dirty = False
        self.last_seq = -1
        self.history, self.hist_pos = [], 0
        self.cfg_shown = {}
        self.nb.add(self._board_tab(), text="Board")
        self.nb.add(self._config_tab(), text="Config")
        self.nb.add(self._signal_tab(), text="Signal")
        self.nb.add(self._console_tab(), text="Console")
        self.nb.add(self._notes_tab(), text="Agent notes")
        self.refresh_all()
        # Size to the content (it grows with Windows display scaling) and open beside the main window.
        w.update_idletasks()
        w.minsize(w.winfo_reqwidth(), w.winfo_reqheight())
        x = app.root.winfo_rootx() + app.root.winfo_width() + 12
        if x + w.winfo_reqwidth() > w.winfo_screenwidth():
            x = max(0, w.winfo_screenwidth() - w.winfo_reqwidth() - 20)
        w.geometry("+%d+%d" % (x, max(0, app.root.winfo_rooty() - 30)))
        self.win.after(250, self._tick)

    # ---------------------------------------------------------------- common
    def say(self, text, ok=True):
        self.msg.configure(text=text, foreground="#1a7f37" if ok else "#c62828")

    def close(self):
        self.app.dev_panel = None
        self.win.destroy()

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def refresh_all(self):
        self.refresh_board()
        self.refresh_config()
        self.refresh_notes()

    def _link(self):
        if not self.app.link:
            self.say("Not connected to a board (plug one in, or use Demo mode).", ok=False)
            return None
        return self.app.link

    def run(self, cmd, after=None, refresh=True):
        """Send a command, report the result in the status line, then refresh the app's view."""
        if not self._link():
            return

        def done(r, e):
            if e:
                self.say("%s: %s" % (cmd, e), ok=False)
            else:
                self.say("%s: ok" % cmd)
                if after:
                    after(r)
            if refresh:
                self.app._fetch_info()
        self.app.request(cmd, done)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        self._drain_console()
        if self.sig_dirty:
            self.sig_dirty = False
            self._draw_signal()
        self.win.after(250, self._tick)

    def on_event(self, m):
        t = m.get("t")
        if t == "phone":
            self.sig[m["id"]].append((time.monotonic(), m.get("rssi"), m.get("rssi_f"), m.get("src")))
            self._update_signal_row(m)
            self.sig_dirty = True

    # ---------------------------------------------------------------- Board
    def _board_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        info = ttk.LabelFrame(f, text="Connected board", padding=10)
        info.pack(fill="x")
        self.board_vals = {}
        rows = [("port", "Port"), ("chip", "Chip"), ("fw", "Firmware"), ("mac", "Bluetooth address"),
                ("uptime", "Uptime"), ("mode", "Tracking mode"), ("ble", "Bluetooth started"),
                ("rpa", "Address self-test"), ("pairing", "Pairing open"), ("phones", "Phones paired")]
        for i, (key, label) in enumerate(rows):
            r, c = i % 5, (i // 5) * 2
            ttk.Label(info, text=label + ":", foreground="#555").grid(row=r, column=c, sticky="w", padx=(0, 8), pady=2)
            v = ttk.Label(info, text="-", font=(FONT, 10, "bold"))
            v.grid(row=r, column=c + 1, sticky="w", padx=(0, 40), pady=2)
            self.board_vals[key] = v

        act = ttk.LabelFrame(f, text="Actions", padding=10)
        act.pack(fill="x", pady=10)
        for text, fn in [("Refresh", lambda: self.app._fetch_info()),
                         ("Reconnect", self.app.reconnect),
                         ("Restart board", lambda: self.run("restart")),
                         ("Open pairing (2 min)", lambda: self.run("pair 120")),
                         ("Stop pairing", lambda: self.run("pair stop")),
                         ("Forget all phones", self._forget_all),
                         ("Factory reset", self._factory_reset)]:
            ttk.Button(act, text=text, command=fn).pack(side="left", padx=(0, 6))

        fw = ttk.LabelFrame(f, text="Firmware", padding=10)
        fw.pack(fill="x")
        ttk.Label(fw, text="Board type:").pack(side="left")
        self.env_var = tk.StringVar(value="Auto-detect")
        ttk.Combobox(fw, textvariable=self.env_var, state="readonly", width=14,
                     values=["Auto-detect", "esp32", "esp32-s3", "esp32-c3"]).pack(side="left", padx=6)
        ttk.Button(fw, text="Install / update firmware", command=self._flash).pack(side="left", padx=6)
        ttk.Label(fw, text="Builds from the firmware/ folder and uploads. The serial port is released while it runs.",
                  foreground="#666").pack(side="left", padx=6)
        ttk.Label(f, text="The flash log appears on the main window's Board tab and in the Console tab here.",
                  foreground="#666").pack(anchor="w", pady=(8, 0))
        return f

    def refresh_board(self):
        i, link = self.app.info, self.app.link
        cfg = self.app.cfg
        vals = {
            "port": link.name if link else "not connected",
            "chip": i.get("chip"), "fw": "%s %s" % (i.get("fw"), i.get("ver")) if i else None,
            "mac": i.get("mac"),
            "uptime": "%d s" % i["uptime_s"] if "uptime_s" in i else None,
            "mode": {"hid": "live (hid)", "scan": "quiet (scan)"}.get(cfg.get("mode")),
            "ble": {True: "yes", False: "NO"}.get(i.get("ble")),
            "rpa": {True: "passed", False: "FAILED"}.get(i.get("rpa_selftest")),
            "pairing": {True: "yes", False: "no"}.get(i.get("pairing")),
            "phones": "%d of %d" % (len(i.get("phones", [])), i.get("max_phones", 3)) if i else None,
        }
        for k, lab in self.board_vals.items():
            lab.configure(text=_fmt(vals.get(k)))

    def _forget_all(self):
        if messagebox.askyesno("Forget all phones", "Remove every paired phone from the board?", parent=self.win):
            self.run("forget all")

    def _factory_reset(self):
        if not messagebox.askyesno("Factory reset", "Forget all phones and restore every setting to its default "
                                   "(calibration too)? The tracking mode stays as it is.", parent=self.win):
            return
        self.run("forget all", after=lambda r: self.run("cfg reset"), refresh=False)

    def _flash(self):
        env = self.env_var.get()
        self.app.flash_firmware(env=None if env == "Auto-detect" else env)

    # ---------------------------------------------------------------- Config
    def _config_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        pre = ttk.Frame(f)
        pre.pack(fill="x", pady=(0, 8))
        ttk.Label(pre, text="Zone stability presets:").pack(side="left")
        for name, values in PRESETS:
            ttk.Button(pre, text=name, command=lambda v=values, n=name: self._apply(v, n)).pack(side="left", padx=4)

        grid = ttk.Frame(f)
        grid.pack(fill="x")
        for c, t in enumerate(["Setting", "On board", "New value", "", "Allowed / what it does"]):
            ttk.Label(grid, text=t, font=(FONT, 9, "bold")).grid(row=0, column=c, sticky="w", padx=4, pady=(0, 4))
        self.cfg_rows = {}
        for r, (key, label, allowed, hint) in enumerate(CFG_SPEC, start=1):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=1)
            cur = ttk.Label(grid, text="-", width=9, font=MONO)
            cur.grid(row=r, column=1, sticky="w", padx=4)
            var = tk.StringVar()
            if key == "mode":
                ent = ttk.Combobox(grid, textvariable=var, values=["hid", "scan"], state="readonly", width=9)
            else:
                ent = ttk.Entry(grid, textvariable=var, width=11)
                ent.bind("<Return>", lambda e, k=key: self._set_one(k))
            ent.grid(row=r, column=2, sticky="w", padx=4)
            ttk.Button(grid, text="Set", width=5, command=lambda k=key: self._set_one(k)).grid(row=r, column=3, padx=4)
            ttk.Label(grid, text=allowed + ("  -  " + hint if hint else ""), foreground="#666").grid(
                row=r, column=4, sticky="w", padx=4)
            self.cfg_rows[key] = (cur, var)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="Apply all changed", command=self._apply_changed).pack(side="left")
        ttk.Button(b, text="Reload from board", command=lambda: self.app._fetch_info()).pack(side="left", padx=6)
        ttk.Button(b, text="Restore defaults", command=lambda: self.run("cfg reset")).pack(side="left")
        return f

    def refresh_config(self):
        cfg = self.app.cfg
        for key, (cur, var) in self.cfg_rows.items():
            v = cfg.get(key)
            cur.configure(text=_fmt(v))
            # Fill the entry unless the user has typed something different from what was shown.
            if var.get() in ("", self.cfg_shown.get(key, "")):
                var.set(_fmt(v) if v is not None else "")
            self.cfg_shown[key] = _fmt(v) if v is not None else ""

    def _set_one(self, key):
        val = self.cfg_rows[key][1].get().strip()
        if not val:
            return
        self.cfg_shown[key] = val  # so the refresh replaces it with the board's value
        self.run("cfg %s %s" % (key, val))

    def _apply(self, values, name="changes"):
        link = self._link()
        if not link:
            return

        def work():
            errors = []
            for k, v in values.items():
                try:
                    link.request("cfg %s %s" % (k, v))
                except (DeviceError, TimeoutError) as e:
                    errors.append("%s: %s" % (k, e))
            return errors

        def done(errors, e):
            if e or errors:
                self.say("; ".join(errors or [str(e)]), ok=False)
            else:
                self.say("Applied %s." % name)
            for k in values:
                self.cfg_shown[k] = self.cfg_rows[k][1].get()
            self.app._fetch_info()
        self.app.bg(work, done)

    def _apply_changed(self):
        cfg = self.app.cfg
        changed = {}
        for key, (cur, var) in self.cfg_rows.items():
            v = var.get().strip()
            if v and v != _fmt(cfg.get(key)):
                changed[key] = v
        if not changed:
            self.say("Nothing changed.")
            return
        # Keep near < far at every step: widen first, narrow last.
        order = sorted(changed, key=lambda k: (k == "mode", k == "near_m" and float(changed[k]) > cfg.get("near_m", 0)))
        self._apply({k: changed[k] for k in order}, "%d change(s)" % len(changed))

    # ---------------------------------------------------------------- Signal
    def _signal_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        cols = [("id", 30), ("addr", 140), ("src", 50), ("connected", 70), ("rssi", 50), ("rssi_f", 60),
                ("dist_m", 60), ("zone", 60), ("pending", 90), ("age_ms", 60)]
        self.sig_tree = ttk.Treeview(f, columns=[c for c, _ in cols], show="headings", height=3)
        for c, w in cols:
            self.sig_tree.heading(c, text=c)
            self.sig_tree.column(c, width=w, anchor="w")
        self.sig_tree.pack(fill="x")
        self.sig_tree.bind("<<TreeviewSelect>>", lambda e: self._draw_signal())
        self.sig_canvas = tk.Canvas(f, height=300, bg="white", highlightthickness=0)
        self.sig_canvas.pack(fill="both", expand=True, pady=(8, 4))
        ttk.Label(f, text="Grey dots: raw RSSI.  Blue: filtered.  Dashed: zone thresholds (inner = enter, outer = "
                          "leave), from Near/Far distance, hysteresis and the 1 m reference for the current source.",
                  foreground="#555", wraplength=900).pack(anchor="w")
        return f

    def _update_signal_row(self, m):
        iid = str(m["id"])
        addr = next((p.get("addr") for p in self.app.info.get("phones", []) if p.get("id") == m["id"]), "?")
        pend = m.get("pending")
        vals = (m["id"], addr, m.get("src"), m.get("connected"), _fmt(m.get("rssi")), _fmt(m.get("rssi_f")),
                _fmt(m.get("dist_m")), m.get("zone"),
                "%s in %.1fs" % (pend, m.get("pending_ms", 0) / 1000) if pend else "-", m.get("age_ms"))
        if self.sig_tree.exists(iid):
            self.sig_tree.item(iid, values=vals)
        else:
            self.sig_tree.insert("", "end", iid=iid, values=vals)

    def _draw_signal(self):
        c = self.sig_canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 500), max(c.winfo_height(), 200)
        left, right, top, bottom = 46, 70, 10, h - 20
        lo, hi = -100.0, -25.0

        def y_of(r):
            return top + (hi - max(lo, min(hi, r))) / (hi - lo) * (bottom - top)

        for r in range(-100, -24, 10):
            c.create_line(left, y_of(r), w - right, y_of(r), fill="#eee")
            c.create_text(left - 6, y_of(r), text="%d" % r, anchor="e", fill="#888", font=(FONT, 8))
        c.create_text(left, h - 8, text="-%d s" % CHART_S, anchor="w", fill="#888", font=(FONT, 8))
        c.create_text(w - right, h - 8, text="now", anchor="e", fill="#888", font=(FONT, 8))
        sel = self.sig_tree.selection()
        pid = int(sel[0]) if sel else (min(self.sig) if self.sig else None)
        if pid is None or not self.sig.get(pid):
            c.create_text(w / 2, h / 2, text="No phone data yet", fill="#999", font=(FONT, 11))
            return
        data = self.sig[pid]
        src = next((s for _, _, _, s in reversed(data) if s in ("conn", "adv")), "conn")
        cfg = self.app.cfg
        ref = cfg.get("ref_conn" if src == "conn" else "ref_adv", -60)
        n, hy = cfg.get("path_n", 2.7), cfg.get("hyst_db", 3)
        for dist_key, color, name in (("near_m", "#e5484d", "near"), ("far_m", "#30a46c", "far")):
            base = ref - 10 * n * math.log10(max(cfg.get(dist_key, 1.0), 0.05))
            for off, dash in ((hy, (6, 3)), (-hy, (2, 3))):
                c.create_line(left, y_of(base + off), w - right, y_of(base + off), fill=color, dash=dash)
            c.create_text(w - right + 4, y_of(base), text="%s %.0f" % (name, base), anchor="w", fill=color, font=(FONT, 8))
        now = time.monotonic()

        def x_of(t):
            return left + (1 - (now - t) / CHART_S) * (w - left - right)

        line = []
        for t, raw, filt, s in data:
            if now - t > CHART_S:
                continue
            if raw is not None:
                x, y = x_of(t), y_of(raw)
                c.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill="#b0b4ba", outline="")
            if filt is None:
                if len(line) >= 4:
                    c.create_line(*line, fill="#2563eb", width=2)
                line = []
            else:
                line += [x_of(t), y_of(filt)]
        if len(line) >= 4:
            c.create_line(*line, fill="#2563eb", width=2)
        c.create_text(left + 4, top + 2, text="phone %d, source: %s, 1 m reference %.0f dBm" % (pid, src, ref),
                      anchor="nw", fill="#555", font=(FONT, 8))

    # ---------------------------------------------------------------- Console
    def _console_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.show_stream = tk.BooleanVar(value=False)
        self.autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Show phone stream", variable=self.show_stream).pack(side="left")
        ttk.Checkbutton(bar, text="Auto-scroll", variable=self.autoscroll).pack(side="left", padx=10)
        ttk.Button(bar, text="Save log...", command=self._save_log).pack(side="right")
        ttk.Button(bar, text="Clear", command=self._clear_console).pack(side="right", padx=6)
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=6)
        self.console = tk.Text(box, font=MONO, wrap="none", state="disabled", height=20)
        sb = ttk.Scrollbar(box, command=self.console.yview)
        self.console.configure(yscrollcommand=sb.set)
        self.console.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.console.tag_configure("tx", foreground="#1d4ed8")
        self.console.tag_configure("stream", foreground="#9aa0a6")
        self.console.tag_configure("err", foreground="#c62828")
        self.console.tag_configure("raw", foreground="#7c3aed")
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Command:").pack(side="left")
        self.cmd_var = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self.cmd_var, font=MONO)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self._send())
        ent.bind("<Up>", lambda e: self._history(-1))
        ent.bind("<Down>", lambda e: self._history(1))
        ttk.Button(row, text="Send", command=self._send).pack(side="left")
        quick = ttk.Frame(f)
        quick.pack(fill="x", pady=(6, 0))
        for cmd in QUICK_CMDS:
            ttk.Button(quick, text=cmd, command=lambda c=cmd: self._send(c)).pack(side="left", padx=(0, 4))
        ttk.Label(quick, text="All commands: PROTOCOL.md", foreground="#666").pack(side="right")
        return f

    def _drain_console(self):
        items = [x for x in list(self.app.traffic) if x[0] > self.last_seq]
        if not items:
            return
        self.last_seq = items[-1][0]
        show_stream = self.show_stream.get()
        self.console.configure(state="normal")
        for seq, ts, direction, text in items:
            is_stream = direction == "rx" and text.startswith('{"t":"phone"')
            if is_stream and not show_stream:
                continue
            tag = "tx" if direction == "tx" else ("stream" if is_stream else
                                                  "err" if '"t":"err"' in text else
                                                  "raw" if not text.startswith("{") else "")
            stamp = time.strftime("%H:%M:%S", time.localtime(ts))
            self.console.insert("end", "%s %s %s\n" % (stamp, ">" if direction == "tx" else "<", text), tag)
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 4000:
            self.console.delete("1.0", "%d.0" % (lines - 4000))
        self.console.configure(state="disabled")
        if self.autoscroll.get():
            self.console.see("end")

    def _clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _send(self, cmd=None):
        cmd = (cmd or self.cmd_var.get()).strip()
        if not cmd or not self._link():
            return
        self.app.link.send(cmd)
        if not self.history or self.history[-1] != cmd:
            self.history.append(cmd)
        self.hist_pos = len(self.history)
        self.cmd_var.set("")
        if cmd.split()[0].lower() in ("cfg", "forget", "pair", "calibrate", "autocal", "focus", "restart"):
            self.app.root.after(700, self.app._fetch_info)

    def _history(self, step):
        if not self.history:
            return "break"
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        self.cmd_var.set(self.history[self.hist_pos] if self.hist_pos < len(self.history) else "")
        return "break"

    def _save_log(self):
        path = filedialog.asksaveasfilename(parent=self.win, title="Save serial log", defaultextension=".log",
                                            initialfile=time.strftime("focus-device-%Y%m%d-%H%M%S.log"),
                                            filetypes=[("Log", "*.log"), ("Text", "*.txt")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            for seq, ts, direction, text in list(self.app.traffic):
                fh.write("%s.%03d %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                                              int(ts * 1000) % 1000, ">" if direction == "tx" else "<", text))
        self.say("Saved %s" % path)

    # ---------------------------------------------------------------- Agent notes
    def _notes_tab(self):
        f = ttk.Frame(self.nb, padding=12)
        ttk.Label(f, text="Notes for the AI that works on this project. They're saved to AGENT_NOTES.md in the "
                          "focus-device folder, and Claude Code and Cursor read that file at the start of every "
                          "session. Good for bugs you saw, results from testing the real board, ideas, and "
                          "standing instructions.", wraplength=900, foreground="#444").pack(anchor="w")
        new = ttk.LabelFrame(f, text="New note", padding=8)
        new.pack(fill="x", pady=8)
        top = ttk.Frame(new)
        top.pack(fill="x")
        ttk.Label(top, text="Kind:").pack(side="left")
        self.kind_var = tk.StringVar(value="bug")
        ttk.Combobox(top, textvariable=self.kind_var, values=agent_notes.KINDS, state="readonly", width=12).pack(
            side="left", padx=6)
        self.attach_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Attach a board snapshot (firmware, settings, phones, recent events)",
                        variable=self.attach_var).pack(side="left", padx=10)
        self.note_text = tk.Text(new, height=4, font=(FONT, 10), wrap="word")
        self.note_text.pack(fill="x", pady=6)
        ttk.Button(new, text="Add note", command=self._add_note).pack(anchor="e")

        lst = ttk.Frame(f)
        lst.pack(fill="both", expand=True)
        self.notes_tree = ttk.Treeview(lst, columns=("when", "kind", "status", "text"), show="headings", height=6)
        for c, w in (("when", 130), ("kind", 90), ("status", 60), ("text", 560)):
            self.notes_tree.heading(c, text=c.title())
            self.notes_tree.column(c, width=w, anchor="w")
        self.notes_tree.pack(fill="x")
        self.notes_tree.bind("<<TreeviewSelect>>", lambda e: self._show_note())
        self.note_view = tk.Text(lst, height=6, font=(FONT, 9), wrap="word", state="disabled", background="#fafafa")
        self.note_view.pack(fill="both", expand=True, pady=6)
        b = ttk.Frame(f)
        b.pack(fill="x")
        ttk.Button(b, text="Mark done", command=lambda: self._set_status("done")).pack(side="left")
        ttk.Button(b, text="Reopen", command=lambda: self._set_status("open")).pack(side="left", padx=6)
        ttk.Button(b, text="Delete", command=self._delete_note).pack(side="left")
        ttk.Button(b, text="Archive done notes", command=self._archive).pack(side="left", padx=6)
        ttk.Button(b, text="Open AGENT_NOTES.md", command=self._open_file).pack(side="right")
        return f

    def refresh_notes(self):
        sel = self.notes_tree.selection()
        self.notes_tree.delete(*self.notes_tree.get_children())
        self.notes = agent_notes.load()
        for i, n in enumerate(self.notes):
            first = next((ln for ln in n["body"].splitlines() if ln.strip()), "")
            self.notes_tree.insert("", "end", iid=str(i), values=(n["when"], n["kind"], n["status"], first[:120]))
        if sel and self.notes_tree.exists(sel[0]):
            self.notes_tree.selection_set(sel[0])
        self._show_note()

    def _selected_note(self):
        sel = self.notes_tree.selection()
        return int(sel[0]) if sel else None

    def _show_note(self):
        i = self._selected_note()
        self.note_view.configure(state="normal")
        self.note_view.delete("1.0", "end")
        if i is not None and i < len(self.notes):
            self.note_view.insert("1.0", self.notes[i]["body"])
        self.note_view.configure(state="disabled")

    def _add_note(self):
        text = self.note_text.get("1.0", "end").strip()
        if not text:
            self.say("Write the note first.", ok=False)
            return
        snap = self.app.snapshot_markdown() if self.attach_var.get() else None
        try:
            agent_notes.add(self.kind_var.get(), text, snap)
        except (OSError, ValueError) as e:
            self.say("Couldn't save the note: %s" % e, ok=False)
            return
        self.note_text.delete("1.0", "end")
        self.refresh_notes()
        self.say("Saved to AGENT_NOTES.md. The AI will see it next time it works on this project.")

    def _set_status(self, status):
        i = self._selected_note()
        if i is None:
            return
        agent_notes.set_status(i, status)
        self.refresh_notes()

    def _delete_note(self):
        i = self._selected_note()
        if i is not None and messagebox.askyesno("Delete note", "Delete this note?", parent=self.win):
            agent_notes.delete(i)
            self.refresh_notes()

    def _archive(self):
        n = agent_notes.archive_done()
        self.refresh_notes()
        self.say("Moved %d done note(s) to AGENT_NOTES_ARCHIVE.md." % n)

    def _open_file(self):
        agent_notes.ensure_file()
        os.startfile(agent_notes.NOTES_PATH)
