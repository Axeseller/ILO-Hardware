"""A fake focus device that speaks PROTOCOL.md, for trying the app without a board.

It mirrors the firmware's logic (firmware/proximity.h): a time window of RSSI samples averaged as
power inside a Tukey fence, adaptive smoothing, zones with a 6 dB hysteresis gap and dwell times,
one- and two-spot calibration (with the orientation check), and the same focus rules.
The simulated phone's true distance comes from `set_distance()` (the app's demo slider) or, in
"auto" mode, from a script that wanders between the desk, the room and another room. Its signal
follows a fixed "real" room (SIM_REF, SIM_N) with fading, whatever the settings say, so a wrong
calibration shows up as a wrong distance, as it would on a real board. `set_orientation_loss()`
weakens it by a few dB, as turning a real phone does.
"""
import json
import math
import queue
import random
import threading
import time

NEAR, ROOM, FAR, AWAY = 0, 1, 2, 3
ZONE_NAMES = ["near", "room", "far", "away"]


def loss_for_distance(m, n):
    return 10.0 * n * math.log10(max(m, 0.05))


def distance_for_loss(loss, n):
    return min(50.0, max(0.05, 10 ** (loss / (10.0 * max(n, 1.0)))))


def robust_level(samples):
    """prox::robustLevel: drop samples outside the Tukey fence, average the rest as power."""
    s = sorted(samples)
    if not s:
        return None

    def q(p):
        pos = p * (len(s) - 1)
        i = int(pos)
        return s[i] + (pos - i) * (s[i + 1] - s[i]) if i + 1 < len(s) else s[i]

    q1, q3 = q(0.25), q(0.75)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    kept = [x for x in s if lo <= x <= hi]
    if not kept:
        return q(0.5)
    return 10 * math.log10(sum(10 ** (x / 10.0) for x in kept) / len(kept))


def fit_path_loss(d1, r1, d2, r2, n_min=1.5, n_max=5.0):
    """prox::fitPathLoss: (ref, n, clamped) through two measured points, or None."""
    if d1 <= 0 or d2 <= 0 or abs(math.log10(d2 / d1)) < 0.17:
        return None
    n = (r1 - r2) / (10 * math.log10(d2 / d1))
    clamped = not n_min <= n <= n_max
    n = min(n_max, max(n_min, n))
    ref = 0.5 * ((r1 + 10 * n * math.log10(d1)) + (r2 + 10 * n * math.log10(d2)))
    return ref, n, clamped


def fit_orientation(levels):
    """prox::fitOrientation: (spread, offset of the average orientation from levels[0])."""
    return max(levels) - min(levels), sum(levels) / len(levels) - levels[0]


def effective_hyst(hyst_db, orient_db):
    """prox::effectiveHyst: at least half the orientation spread, so turning can't flip a zone."""
    return max(hyst_db, min(8.0, 0.5 * orient_db))


class RssiWindow:
    def __init__(self):
        self.samples = []  # (ms, rssi)

    def add(self, rssi, ms):
        if rssi >= 0 or rssi < -110:
            return
        self.samples.append((ms, rssi))
        del self.samples[:-64]

    def last_ms(self):
        return self.samples[-1][0] if self.samples else 0

    def level(self, now, min_age, max_age, min_samples):
        s = [r for t, r in self.samples if min_age <= now - t <= max_age]
        if not s or len(s) < min_samples:
            return None
        return robust_level(s)

    def value(self, now, window_ms, min_samples):
        return self.level(now, 0, window_ms, min_samples)

    def steady(self, now, half_ms, min_samples, tol_db):
        a = self.level(now, 0, half_ms, min_samples)
        b = self.level(now, half_ms + 1, 2 * half_ms, min_samples)
        return a is not None and b is not None and abs(a - b) <= tol_db


class LevelSmoother:
    """prox::LevelSmoother: slow while the level only jitters, quick when it really moves."""
    QUIET_DB, MOVE_DB, GAP_MS = 1.5, 5.0, 3000

    def __init__(self):
        self.x = None
        self.last = 0

    def reset(self):
        self.x = None

    def update(self, z, now, tau_s):
        if self.x is None or tau_s <= 0 or now - self.last > self.GAP_MS:
            self.x, self.last = z, now
            return z
        dt, self.last = (now - self.last) / 1000.0, now
        err = z - self.x
        slow = 1 - math.exp(-dt / tau_s)
        k = min(1.0, max(0.0, (abs(err) - self.QUIET_DB) / (self.MOVE_DB - self.QUIET_DB)))
        self.x += (slow + (1 - slow) * k) * err
        return self.x


class ZoneTracker:
    """Same rules as prox::ZoneTracker: one clock for "readings point away from the current zone"
    (not restarted when they flip between other zones) and one for "continuously NEAR"."""

    def __init__(self):
        self.zone = self.target = AWAY
        self.out = self.near_run = False
        self.out_since = self.near_since = 0

    def _target(self, loss, c):
        near_t, far_t = loss_for_distance(c["near_m"], c["path_n"]), loss_for_distance(c["far_m"], c["path_n"])
        h = 0 if self.zone == AWAY else effective_hyst(c["hyst_db"], c.get("orient_db", 0))
        is_near = loss < near_t + h if self.zone == NEAR else loss < near_t - h
        if is_near:
            return NEAR
        is_far = loss > far_t - h if self.zone == FAR else loss > far_t + h
        return FAR if is_far else ROOM

    def _need(self, target, c):
        s = c["near_in_s"] if target == NEAR else (c["near_out_s"] if self.zone == NEAR else c["zone_s"])
        return int(s * 1000)

    def _since(self, target):
        return self.near_since if target == NEAR else self.out_since

    def update(self, loss, now, c):
        target = self.target = self._target(loss, c)
        if self.zone == AWAY:
            self.out = self.near_run = False
            return self._set(target)
        if target == self.zone:
            self.out = self.near_run = False
            return False
        if not self.out:
            self.out, self.out_since = True, now
        if target == NEAR:
            if not self.near_run:
                self.near_run, self.near_since = True, now
        else:
            self.near_run = False
        if now - self._since(target) >= self._need(target, c):
            self.out = self.near_run = False
            return self._set(target)
        return False

    def pending(self):
        return self.target if self.out else self.zone

    def pending_left_ms(self, now, c):
        if not self.out:
            return 0
        return max(0, self._need(self.target, c) - (now - self._since(self.target)))

    def go_away(self):
        self.out = self.near_run = False
        self.target = AWAY
        return self._set(AWAY)

    def cap_at_far(self):
        if self.zone in (NEAR, ROOM):
            self.out = self.near_run = False
            self.target = FAR
            return self._set(FAR)
        return False

    def _set(self, z):
        if z == self.zone:
            return False
        self.zone = z
        return True


class Focus:
    def __init__(self):
        self.active = False
        self.limit_s = self.near_ms = self.pickups = 0
        self.start = self.last = self.near_since = 0
        self.in_near = self.distracted = False

    def begin(self, now, limit_s):
        self.__init__()
        self.active, self.start, self.last, self.limit_s = True, now, now, limit_s

    def stop(self, now):
        if self.active:
            self.last = now
        self.active = self.distracted = False

    def elapsed_s(self, now):
        return ((now if self.active else self.last) - self.start) // 1000

    def left_s(self, now):
        return -1 if not self.limit_s else max(0, self.limit_s - self.elapsed_s(now))

    def tick(self, now, nearest, grace_ms):
        if not self.active:
            return None
        dt, self.last, ev = now - self.last, now, None
        if nearest == NEAR:
            self.near_ms += dt
            if not self.in_near:
                self.in_near, self.near_since = True, now
            if not self.distracted and now - self.near_since >= grace_ms:
                self.distracted, ev = True, "distracted"
                self.pickups += 1
        else:
            self.in_near = False
            if self.distracted:
                self.distracted, ev = False, "recovered"
        if self.limit_s and self.elapsed_s(now) >= self.limit_s:
            self.active = self.distracted = False
            return "done"
        return ev

    def obj(self, now):
        return {"active": self.active, "limit_s": self.limit_s, "elapsed_s": self.elapsed_s(now),
                "left_s": self.left_s(now), "near_s": self.near_ms // 1000, "pickups": self.pickups,
                "distracted": self.distracted}


DEFAULT_CFG = {"near_m": 1.0, "far_m": 4.0, "path_n": 2.7, "ref_conn": -60.0, "ref_adv": -65.0,
               "hyst_db": 3.0, "near_in_s": 3.0, "near_out_s": 5.0, "zone_s": 5.0, "smooth_s": 4.0,
               "orient_db": 0.0,
               "away_s": 30, "grace_s": 5, "focus_min": 25, "stream_ms": 500, "buzzer_pin": -1, "mode": "hid"}
# Same limits the firmware enforces (cmdCfg in firmware/main.cpp).
CFG_RANGES = {"near_m": (0.1, 30), "far_m": (0.1, 30), "path_n": (1.5, 5), "ref_conn": (-100, -20),
              "ref_adv": (-100, -20), "hyst_db": (0, 10), "near_in_s": (0, 30), "near_out_s": (0, 60),
              "zone_s": (0, 60), "smooth_s": (0, 20), "orient_db": (0, 20), "away_s": (5, 600), "grace_s": (0, 600), "focus_min": (1, 1440),
              "stream_ms": (100, 10000), "buzzer_pin": (-1, 48)}

# The simulated room and phone: what the signal really does, whatever the settings say.
SIM_REF, SIM_N = -60.0, 2.7  # dBm at 1 m, path-loss exponent
SIM_K = 4.0                  # Rician fading: direct-path power / reflected power
SAMPLE_MS, CONN_WINDOW_MS, MIN_CONN_SAMPLES = 100, 2000, 5
VERSION = "0.2.0-sim"


def faded_rssi(distance):
    """One RSSI reading of the simulated phone: path loss, fading, and now and then a blocked packet."""
    los, sc = math.sqrt(SIM_K / (SIM_K + 1)), math.sqrt(0.5 / (SIM_K + 1))
    re, im = los + sc * random.gauss(0, 1), sc * random.gauss(0, 1)
    fade = 10 * math.log10(max(re * re + im * im, 1e-6))
    blocked = 15 if random.random() < 0.05 else 0
    return int(round(SIM_REF - loss_for_distance(distance, SIM_N) + fade - blocked))


class SimTransport:
    """Looks like a serial port to device.Link."""

    name = "Demo (simulated board)"

    def __init__(self, paired=True, distance=0.5):
        self.out = queue.Queue()
        self.lock = threading.RLock()
        self.t0 = time.monotonic()
        self.cfg = dict(DEFAULT_CFG)
        self.phones = []
        if paired:
            self._add_phone()
        self.pairing_until = 0
        self.focus = Focus()
        self.stream = True
        self.true_distance = distance  # metres; None = out of range
        self.auto = False
        self.calib = None
        self.autocal = None
        self.autocal_measure_ms, self.autocal_place_timeout_ms = 6000, 90000  # tests shorten these
        self.autocal_turn_min_ms, self.autocal_turn_max_ms, self.autocal_turn_measure_ms = 7000, 20000, 4000
        self.orient_loss = 0.0  # dB lost to the phone's orientation right now
        self.alive = True
        self._emit({"t": "hello", "fw": "focus-device", "ver": VERSION, "chip": "Simulated ESP32", "mac": "de:mo:00:00:00:01"})
        threading.Thread(target=self._run, daemon=True).start()

    # --- serial-port interface -------------------------------------------------------------
    def write(self, data):
        for line in data.decode().splitlines():
            if line.strip():
                with self.lock:
                    self._command(line.strip())

    def readline(self):
        try:
            return (self.out.get(timeout=0.2) + "\n").encode()
        except queue.Empty:
            return b""

    def close(self):
        self.alive = False

    # --- demo controls ---------------------------------------------------------------------
    def set_distance(self, metres):
        """True phone distance in metres, or None for 'out of range'."""
        with self.lock:
            self.auto = False
            self.true_distance = metres

    def set_orientation_loss(self, db):
        """Weaken the phone's signal by db, as turning it away from the board would."""
        with self.lock:
            self.orient_loss = db

    def set_auto(self, on):
        with self.lock:
            self.auto = on

    # --- internals -------------------------------------------------------------------------
    def _now(self):
        return int((time.monotonic() - self.t0) * 1000)

    def _emit(self, obj):
        self.out.put(json.dumps(obj, separators=(",", ":")))

    def _add_phone(self):
        self.phones.append({"id": len(self.phones), "addr": "4c:57:%02x:12:34:56" % (0x10 + len(self.phones)),
                            "irk": True, "connected": True, "conn": RssiWindow(), "adv": RssiWindow(),
                            "zone": ZoneTracker(), "smooth": LevelSmoother(), "src": "none", "rssi": None, "rssi_f": None, "dist_m": None})

    def _auto_distance(self, now):
        # 0-40 s on the desk, 40-60 s across the room, 60-80 s out of range, then back
        t = (now / 1000.0) % 90
        if t < 40:
            return 0.4
        if t < 60:
            return 2.5 + math.sin(t) * 0.5
        if t < 80:
            return None
        return 1.5

    def _run(self):
        last_stream = last_eval = 0
        while self.alive:
            time.sleep(0.05)
            with self.lock:
                now = self._now()
                if self.auto:
                    self.true_distance = self._auto_distance(now)
                if self.pairing_until and now >= self.pairing_until:
                    self.pairing_until = 0
                    self._emit({"t": "pair", "state": "closed", "why": "timeout"})
                if now - last_eval >= SAMPLE_MS:
                    # Keep to 10 samples a second on average, however coarse this PC's sleep timer is.
                    last_eval = last_eval + SAMPLE_MS if now - last_eval < 1000 else now
                    self._sample_and_evaluate(now)
                if self.stream and now - last_stream >= self.cfg["stream_ms"]:
                    last_stream = now
                    for p in self.phones:
                        self._emit(self._phone_event(p, now))
                nearest = min([p["zone"].zone for p in self.phones] or [AWAY])
                ev = self.focus.tick(now, nearest, self.cfg["grace_s"] * 1000)
                if ev:
                    self._emit(dict({"t": "focus", "state": ev}, **self.focus.obj(now)))
                if self.calib and now >= self.calib["until"]:
                    self._finish_calibration()
                self._autocal_tick(now)

    def _sample_and_evaluate(self, now):
        d = self.true_distance
        for p in self.phones:
            in_range = d is not None and d < 12
            if p["connected"] != in_range:  # link drops out of range, comes back in range
                p["connected"] = in_range
                if not in_range:
                    prev = p["zone"].zone
                    p["rssi_f"] = p["dist_m"] = None
                    p["smooth"].reset()
                    if p["zone"].cap_at_far():
                        self._emit_zone(p, prev)
                self._emit({"t": "conn", "id": p["id"], "addr": p["addr"],
                            "state": "connected" if in_range else "disconnected", "reason": 0 if in_range else 520})
            if in_range:
                rssi = faded_rssi(d) - int(round(self.orient_loss))
                p["rssi"] = rssi
                p["conn"].add(rssi, now)
                if p["id"] == 0:  # calibration measures phone 0
                    if self.calib:
                        self.calib["samples"].append(rssi)
                    if self.autocal and self.autocal["state"] == "measure":
                        self.autocal["samples"].append(rssi)
            r, src = None, "none"
            if p["connected"]:
                r = p["conn"].value(now, CONN_WINDOW_MS, MIN_CONN_SAMPLES)
                src = "conn" if r is not None else "none"
            p["src"] = src
            prev = p["zone"].zone
            if r is not None:
                loss = p["smooth"].update(self.cfg["ref_conn"] - r, now, self.cfg["smooth_s"])
                p["rssi_f"], p["dist_m"] = self.cfg["ref_conn"] - loss, distance_for_loss(loss, self.cfg["path_n"])
                if p["zone"].update(loss, now, self.cfg):
                    self._emit_zone(p, prev)
            else:
                p["rssi_f"] = p["dist_m"] = None  # no fresh signal: no distance, never a stale one
                if not p["conn"].samples or now - p["conn"].last_ms() >= self.cfg["away_s"] * 1000:
                    if p["zone"].go_away():
                        self._emit_zone(p, prev)

    def _emit_zone(self, p, prev):
        self._emit({"t": "zone", "id": p["id"], "zone": ZONE_NAMES[p["zone"].zone], "prev": ZONE_NAMES[prev],
                    "dist_m": None if p["dist_m"] is None else round(p["dist_m"], 2)})

    def _phone_event(self, p, now):
        heard = bool(p["conn"].samples)
        ev = {"t": "phone", "id": p["id"], "src": p["src"]}
        if heard:
            ev["rssi"] = p["rssi"]
        ev.update({"rssi_f": None if p["rssi_f"] is None else round(p["rssi_f"], 1),
                   "dist_m": None if p["dist_m"] is None else round(p["dist_m"], 2),
                   "zone": ZONE_NAMES[p["zone"].zone], "age_ms": now - p["conn"].last_ms() if heard else -1,
                   "connected": p["connected"]})
        z = p["zone"]
        if z.pending() != z.zone:
            ev["pending"] = ZONE_NAMES[z.pending()]
            ev["pending_ms"] = z.pending_left_ms(now, self.cfg)
        return ev

    def _phones_json(self):
        return [{"id": p["id"], "addr": p["addr"], "irk": p["irk"], "connected": p["connected"], "src": p["src"],
                 "zone": ZONE_NAMES[p["zone"].zone], "dist_m": p["dist_m"], "rssi_f": p["rssi_f"]} for p in self.phones]

    def _finish_calibration(self):
        c, self.calib = self.calib, None
        s = c["samples"]
        if len(s) < 5:
            self._emit({"t": "calibrated", "ok": False, "id": 0, "samples_conn": len(s), "samples_adv": 0,
                        "msg": "No signal from the phone. Is it paired and nearby?"})
            return
        self.cfg["ref_conn"] = round(robust_level(s) + loss_for_distance(c["metres"], self.cfg["path_n"]), 1)
        self._emit({"t": "calibrated", "ok": True, "id": 0, "metres": c["metres"], "samples_conn": len(s),
                    "samples_adv": 0, "ref_conn": self.cfg["ref_conn"]})

    # --- two-spot calibration (autocal in firmware/main.cpp) ---------------------------------
    def _autocal_place(self, retry=False):
        a = self.autocal
        ev = {"t": "autocal", "state": "place", "step": a["step"] + 1, "metres": a["m"][a["step"]],
              "left_s": self.autocal_place_timeout_ms // 1000}
        if retry:
            ev["retry"] = True
        self._emit(ev)

    def _autocal_turn(self, retry=False):
        a = self.autocal
        k = a["step"] - 1
        ev = {"t": "autocal", "state": "turn", "turn": k, "of": a["turns"], "how": "face_down" if k == 3 else "quarter"}
        if retry:
            ev["retry"] = True
        self._emit(ev)

    def _autocal_fail(self, msg):
        self.autocal = None
        self._emit({"t": "autocal", "state": "failed", "msg": msg})

    def _autocal_start_measure(self, now):
        a = self.autocal
        turn = a["step"] >= 2
        a.update(state="measure", since=now, samples=[])
        ev = {"t": "autocal", "state": "measuring", "step": 2 if turn else a["step"] + 1,
              "metres": a["m"][1 if turn else a["step"]]}
        if turn:
            ev["turn"] = a["step"] - 1
        ev["seconds"] = (self.autocal_turn_measure_ms if turn else self.autocal_measure_ms) // 1000
        self._emit(ev)

    def _autocal_tick(self, now):
        a = self.autocal
        if not a:
            return
        if not self.phones:
            self.autocal = None
            return self._emit({"t": "autocal", "state": "stopped"})
        waited = now - a["since"]
        if a["state"] == "measure":
            if waited >= (self.autocal_turn_measure_ms if a["step"] >= 2 else self.autocal_measure_ms):
                self._autocal_measured(now)
            return
        p = self.phones[0]
        w = p["conn"]
        steady = p["src"] == "conn" and w.steady(now, 2000, 10, 2.0)
        if a["state"] == "turn":
            if waited >= self.autocal_turn_min_ms and (steady or waited >= self.autocal_turn_max_ms):
                self._autocal_start_measure(now)
            return
        if waited >= self.autocal_place_timeout_ms:
            return self._autocal_fail(
                "The signal never settled. Lay the phone down at the first spot, step back and try again."
                if a["step"] == 0 else
                "The signal never got weaker and settled. Lay the phone down further from the board and try again.")
        if not steady:
            return
        if a["step"] == 1:  # the second spot only counts once the phone has clearly moved away
            cur = w.value(now, 2000, 10)
            if cur is None or cur > a["levels"][0] - 4.0:
                return
        self._autocal_start_measure(now)

    def _autocal_measured(self, now):
        a = self.autocal
        s = a["samples"]
        turn = a["step"] >= 2
        if len(s) < 20:
            if turn:
                return self._autocal_finish()
            return self._autocal_fail("Lost the phone's signal while measuring. Is it still connected?")
        if abs(robust_level(s[:len(s) // 2]) - robust_level(s[len(s) // 2:])) > 3.0:  # it moved meanwhile
            a["retries"] += 1
            if a["retries"] > 4:
                if turn:
                    return self._autocal_finish()
                return self._autocal_fail("The signal kept changing while measuring. Keep the phone and "
                                          "yourself still, then try again.")
            a.update(state="turn" if turn else "place", since=now)
            return self._autocal_turn(retry=True) if turn else self._autocal_place(retry=True)
        level = robust_level(s)
        if a["step"] == 0:
            a["levels"].append(level)
            a.update(step=1, retries=0, state="place", since=now)
            return self._autocal_place()
        if a["step"] == 1:
            a["levels"].append(level)
        a["orient"].append(level)  # [0] = as at spot 2
        if a["step"] - 1 < a["turns"]:  # another orientation to go
            a.update(step=a["step"] + 1, retries=0, state="turn", since=now)
            return self._autocal_turn()
        self._autocal_finish()

    def _autocal_finish(self):
        a, self.autocal = self.autocal, None
        r1, r2 = a["levels"]
        fit = fit_path_loss(a["m"][0], r1, a["m"][1], r2) if r1 - r2 >= 3.0 else None
        if not fit:
            return self._autocal_fail("The signal was not clearly weaker at the second spot. Check both "
                                      "distances and try again.")
        ref, n, clamped = fit

        def clamp_ref(v):
            return min(-20.0, max(-100.0, v))

        ref = clamp_ref(ref)
        ref_adv = clamp_ref(self.cfg["ref_adv"] + ref - self.cfg["ref_conn"])
        ev = {"t": "autocal", "state": "done", "id": 0, "src": "conn"}
        extra = {}
        if len(a["orient"]) >= 2:  # centre the reference on the average orientation, remember the spread
            spread, offset = fit_orientation(a["orient"])
            self.cfg["orient_db"] = round(min(20.0, spread), 1)
            ref, ref_adv = clamp_ref(ref + offset), clamp_ref(ref_adv + offset)
            extra = {"orientations": len(a["orient"]), "orient_db": self.cfg["orient_db"],
                     "orient_offset": round(offset, 1)}
        self.cfg["ref_conn"], self.cfg["ref_adv"], self.cfg["path_n"] = round(ref, 1), round(ref_adv, 1), round(n, 2)
        ev.update({"path_n": self.cfg["path_n"], "ref_conn": self.cfg["ref_conn"], "ref_adv": self.cfg["ref_adv"],
                   "rssi1": round(r1, 1), "rssi2": round(r2, 1), "clamped": clamped})
        ev.update(extra)
        self._emit(ev)

    def _command(self, line):
        now = self._now()
        rid = None
        if line.startswith("#"):
            head, _, line = line.partition(" ")
            try:
                rid = int(head[1:])
            except ValueError:
                pass
        tok = line.lower().split()
        if not tok:
            return
        name = tok[0]

        def ok(**data):
            assert "id" not in data, "a reply's id is the request id"
            msg = {"t": "ok", "cmd": name}
            if rid is not None:
                msg["id"] = rid
            msg.update(data)
            self._emit(msg)

        def err(text):
            msg = {"t": "err", "cmd": name}
            if rid is not None:
                msg["id"] = rid
            msg["msg"] = text
            self._emit(msg)

        if name == "info":
            ok(fw="focus-device", ver=VERSION, chip="Simulated ESP32", mac="de:mo:00:00:00:01",
               uptime_s=now // 1000, ble=True, rpa_selftest=True, pairing=bool(self.pairing_until),
               max_phones=3, phones=self._phones_json(), cfg=dict(self.cfg), focus=self.focus.obj(now))
        elif name == "help":
            ok(cmds=["info", "phones", "pair [seconds]", "pair stop", "forget <id>|all", "focus",
                     "focus start [minutes]", "focus stop", "cfg", "cfg <key> <value>", "cfg reset",
                     "calibrate [seconds] [metres]", "autocal [near_m] [far_m] [turns]", "autocal stop",
                     "stream on|off", "restart"])
        elif name == "phones":
            ok(phones=self._phones_json())
        elif name == "pair":
            if len(tok) > 1 and tok[1] == "stop":
                if self.pairing_until:
                    self.pairing_until = 0
                    self._emit({"t": "pair", "state": "closed", "why": "stopped"})
                return ok(pairing=False)
            secs = int(tok[1]) if len(tok) > 1 and tok[1].isdigit() else 120
            if len(self.phones) >= 3:
                return err("all phone slots are used; forget a phone first")
            self.pairing_until = now + secs * 1000
            ok(pairing=True, left_s=secs)
            self._emit({"t": "pair", "state": "open", "left_s": secs, "mode": self.cfg["mode"]})

            def bond():  # the simulated phone pairs a few seconds later
                time.sleep(4)
                with self.lock:
                    if not self.pairing_until:
                        return
                    self._add_phone()
                    p = self.phones[-1]
                    self.pairing_until = 0
                    self._emit({"t": "pair", "state": "bonded", "id": p["id"], "addr": p["addr"], "irk": True})
                    self._emit({"t": "pair", "state": "closed", "why": "bonded"})
            threading.Thread(target=bond, daemon=True).start()
        elif name == "forget":
            if len(tok) < 2:
                return err("usage: forget <id> | forget all")
            if tok[1] == "all":
                n, self.phones = len(self.phones), []
            else:
                try:
                    i = int(tok[1])
                    assert 0 <= i < len(self.phones)
                except (ValueError, AssertionError):
                    return err("no phone with that id")
                del self.phones[i]
                for j, p in enumerate(self.phones):
                    p["id"] = j
                n = 1
            ok(forgot=n)
        elif name == "focus":
            if len(tok) > 1 and tok[1] == "start":
                minutes = int(tok[2]) if len(tok) > 2 and tok[2].isdigit() else self.cfg["focus_min"]
                self.focus.begin(now, minutes * 60)
                self._emit(dict({"t": "focus", "state": "started"}, **self.focus.obj(now)))
            elif len(tok) > 1 and tok[1] == "stop":
                if self.focus.active:
                    self.focus.stop(now)
                    self._emit(dict({"t": "focus", "state": "stopped"}, **self.focus.obj(now)))
            ok(focus=self.focus.obj(now))
        elif name == "cfg":
            if len(tok) == 1:
                return ok(cfg=dict(self.cfg))
            if tok[1] == "reset":
                mode = self.cfg["mode"]
                self.cfg = dict(DEFAULT_CFG, mode=mode)
                return ok(cfg=dict(self.cfg))
            if len(tok) < 3 or tok[1] not in self.cfg:
                return err("unknown key or value out of range (see PROTOCOL.md)")
            key, val = tok[1], tok[2]
            try:
                if key == "mode":
                    assert val in ("hid", "scan")
                    new = val
                else:
                    new = float(val) if isinstance(DEFAULT_CFG[key], float) else int(val)
                    lo, hi = CFG_RANGES[key]
                    assert lo <= new <= hi
                    assert key != "near_m" or new < self.cfg["far_m"]
                    assert key != "far_m" or new > self.cfg["near_m"]
                self.cfg[key] = new
            except (ValueError, AssertionError):
                return err("unknown key or value out of range (see PROTOCOL.md)")
            ok(cfg=dict(self.cfg), restarting=False)
        elif name == "calibrate":
            if self.calib or self.autocal:
                return err("a calibration is already running")
            if not self.phones or self.true_distance is None:
                return err("no phone signal right now; pair a phone and bring it close")
            secs = int(tok[1]) if len(tok) > 1 and tok[1].isdigit() else 8
            metres = float(tok[2]) if len(tok) > 2 else 1.0
            self.calib = {"until": now + secs * 1000, "metres": metres, "samples": []}
            ok(calibrating=True, phone=0, seconds=secs, metres=metres)
        elif name == "autocal":
            if len(tok) > 1 and tok[1] == "stop":
                if self.autocal:
                    self.autocal = None
                    self._emit({"t": "autocal", "state": "stopped"})
                return ok(autocal=False)
            try:
                m1 = float(tok[1]) if len(tok) > 1 else 0.5
                m2 = float(tok[2]) if len(tok) > 2 else 2.0
                turns = int(tok[3]) if len(tok) > 3 else 3
                assert 0.2 <= m1 <= 3 and m1 * 1.5 <= m2 <= 10 and 0 <= turns <= 3
            except (ValueError, AssertionError):
                return err("usage: autocal [near metres 0.2-3] [far metres, at least 1.5x the near one, max 10]"
                           " [orientations 0-3] | autocal stop")
            if self.calib or self.autocal:
                return err("a calibration is already running")
            if not self.phones or self.phones[0]["rssi_f"] is None:
                return err("no phone signal right now; pair a phone and bring it close")
            self.autocal = {"state": "place", "step": 0, "retries": 0, "since": now, "m": [m1, m2],
                            "turns": turns, "levels": [], "orient": [], "samples": []}
            ok(autocal=True, phone=0, near_m=m1, far_m=m2, turns=turns)
            self._autocal_place()
        elif name == "stream" and len(tok) > 1 and tok[1] in ("on", "off"):
            self.stream = tok[1] == "on"
            ok(stream=self.stream)
        elif name == "restart":
            ok()
            self._emit({"t": "hello", "fw": "focus-device", "ver": VERSION, "chip": "Simulated ESP32", "mac": "de:mo:00:00:00:01"})
        else:
            err("unknown command (type help)")
