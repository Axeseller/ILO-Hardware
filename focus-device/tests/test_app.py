"""Tests for the PC side: the protocol client against the simulator, and the app window in demo mode.

Run: tests\\run_tests.cmd   (or: python -m unittest discover -s tests -p "test_*.py")
"""
import os
import queue
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from device import DeviceError, Link  # noqa: E402
from simulator import SimTransport  # noqa: E402


def wait_for(link, pred, timeout):
    """Return the first event matching pred, or None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            m = link.events.get(timeout=0.1)
        except queue.Empty:
            continue
        if pred(m):
            return m
    return None


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.sim = SimTransport()
        self.link = Link(self.sim)

    def tearDown(self):
        self.link.close()

    def test_hello_and_info(self):
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "hello", 2))
        info = self.link.request("info")
        for key in ("fw", "ver", "chip", "mac", "uptime_s", "pairing", "phones", "cfg", "focus"):
            self.assertIn(key, info)
        self.assertEqual(info["fw"], "focus-device")
        self.assertEqual(info["cmd"], "info")
        for key in ("near_m", "far_m", "path_n", "ref_conn", "ref_adv", "away_s", "grace_s", "focus_min",
                    "stream_ms", "buzzer_pin", "mode", "smooth_s", "orient_db"):
            self.assertIn(key, info["cfg"])

    def test_errors_raise(self):
        with self.assertRaises(DeviceError):
            self.link.request("bogus")
        with self.assertRaises(DeviceError):
            self.link.request("cfg near_m banana")

    def test_phone_near_then_away_and_focus(self):
        self.link.close()  # start this one with the phone already across the room (no race with setUp's 0.5 m)
        self.sim = SimTransport(distance=3.0)
        self.link = Link(self.sim)
        self.link.request("cfg away_s 5")
        self.link.request("cfg grace_s 2")
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["zone"] == "room", 4))
        r = self.link.request("focus start 1")
        self.assertTrue(r["focus"]["active"])
        self.sim.set_distance(0.3)
        z = wait_for(self.link, lambda m: m["t"] == "zone" and m["zone"] == "near", 8)
        self.assertIsNotNone(z, "phone never became near")
        d = wait_for(self.link, lambda m: m["t"] == "focus" and m["state"] == "distracted", 5)
        self.assertIsNotNone(d, "no distraction after grace period")
        self.assertEqual(d["pickups"], 1)
        self.sim.set_distance(None)  # leaves the room
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "conn" and m["state"] == "disconnected", 2))
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "focus" and m["state"] == "recovered", 2))
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["zone"] == "away", 8))
        r = self.link.request("focus stop")
        self.assertFalse(r["focus"]["active"])
        self.assertEqual(r["focus"]["pickups"], 1)

    def test_pair_and_forget(self):
        self.link.request("forget all")
        self.assertEqual(self.link.request("phones")["phones"], [])
        self.link.request("pair 30")
        b = wait_for(self.link, lambda m: m["t"] == "pair" and m["state"] == "bonded", 8)
        self.assertIsNotNone(b)
        self.assertTrue(b["irk"])
        self.assertEqual(len(self.link.request("phones")["phones"]), 1)

    def test_no_stale_distance_and_pending_zone(self):
        self.link.request("cfg near_out_s 2")
        self.sim.set_distance(0.3)
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["zone"] == "near", 5))
        self.sim.set_distance(6.0)
        p = wait_for(self.link, lambda m: m["t"] == "phone" and m.get("pending") in ("room", "far"), 5)
        self.assertIsNotNone(p, "no pending zone reported while leaving NEAR")
        self.assertGreater(p["pending_ms"], 0)
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["prev"] == "near", 6))
        self.sim.set_distance(None)  # out of range: the zone is held (capped at far) but no distance
        lost = wait_for(self.link, lambda m: m["t"] == "phone" and not m["connected"], 3)
        self.assertIsNotNone(lost)
        self.assertEqual(lost["zone"], "far")
        self.assertIsNone(lost["dist_m"])
        self.assertIsNone(lost["rssi_f"])

    def test_tuning_keys_and_limits(self):
        cfg = self.link.request("cfg")["cfg"]
        for key in ("hyst_db", "near_in_s", "near_out_s", "zone_s"):
            self.assertIn(key, cfg)
        self.assertEqual(self.link.request("cfg near_in_s 1.5")["cfg"]["near_in_s"], 1.5)
        with self.assertRaises(DeviceError):
            self.link.request("cfg hyst_db 99")
        with self.assertRaises(DeviceError):
            self.link.request("cfg near_m 9")  # must stay below far_m (4)

    def test_calibrate(self):
        self.sim.set_distance(1.0)
        time.sleep(1.0)
        r = self.link.request("calibrate 3 1.0")
        self.assertTrue(r["calibrating"])
        c = wait_for(self.link, lambda m: m["t"] == "calibrated", 6)
        self.assertTrue(c and c["ok"])
        self.assertAlmostEqual(c["ref_conn"], -60, delta=6)  # sim phone: -60 dBm at 1 m plus noise

    def test_autocal_fits_the_room(self):
        """Two spots: the device finds both when to measure and the model (the sim's room is -60 dBm, n 2.7)."""
        self.sim.autocal_measure_ms = 3000
        self.link.request("cfg path_n 2.0")   # start from a wrong model
        self.link.request("cfg ref_conn -50")
        self.sim.set_distance(0.5)
        time.sleep(1.0)
        with self.assertRaises(DeviceError):
            self.link.request("autocal 1 1.2")  # spots too close together
        r = self.link.request("autocal 0.5 2 0")  # 0 = spots only, no orientation check
        self.assertTrue(r["autocal"])
        with self.assertRaises(DeviceError):
            self.link.request("autocal")  # already running
        with self.assertRaises(DeviceError):
            self.link.request("calibrate")
        first = wait_for(self.link, lambda m: m["t"] == "autocal", 2)
        self.assertEqual((first["state"], first["step"], first["metres"]), ("place", 1, 0.5))
        m1 = wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] != "place", 12)
        self.assertEqual((m1["state"], m1["step"]), ("measuring", 1), "spot 1 was never measured")
        nxt = wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] == "place" and m["step"] == 2, 12)
        self.assertIsNotNone(nxt, "never asked for spot 2")
        # The phone hasn't moved yet: spot 2 must not be measured where spot 1 was.
        self.assertIsNone(wait_for(self.link, lambda m: m["t"] == "autocal", 3))
        self.sim.set_distance(2.0)
        done = wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] in ("done", "failed"), 25)
        self.assertEqual(done and done["state"], "done", done)
        self.assertAlmostEqual(done["path_n"], 2.7, delta=0.45)
        self.assertAlmostEqual(done["ref_conn"], -60, delta=3)
        cfg = self.link.request("cfg")["cfg"]
        self.assertEqual((cfg["path_n"], cfg["ref_conn"]), (done["path_n"], done["ref_conn"]))

    def test_autocal_measures_orientation(self):
        """After the two spots the phone is turned three times; the reference is centred on the
        average orientation and the spread widens the zone hysteresis."""
        import simulator
        self.sim.autocal_measure_ms = self.sim.autocal_turn_measure_ms = 3000
        self.sim.autocal_turn_min_ms = 1000
        self.sim.set_distance(0.5)
        time.sleep(1.0)
        self.assertEqual(self.link.request("autocal 0.5 2")["turns"], 3)  # the default
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] == "place"
                                      and m["step"] == 2, 20), "never asked for spot 2")
        self.sim.set_distance(2.0)
        losses = {1: 6, 2: 2, 3: 9}  # what each orientation costs, in dB
        seen, done = [], None
        end = time.monotonic() + 60
        while time.monotonic() < end and not done:
            m = wait_for(self.link, lambda m: m["t"] == "autocal", 1)
            if not m:
                continue
            if m["state"] == "turn" and not m.get("retry"):
                seen.append((m["turn"], m["how"]))
                self.sim.set_orientation_loss(losses[m["turn"]])
            elif m["state"] in ("done", "failed"):
                done = m
        self.assertEqual(done and done["state"], "done", done)
        self.assertEqual(seen, [(1, "quarter"), (2, "quarter"), (3, "face_down")])
        self.assertEqual(done["orientations"], 4)
        self.assertAlmostEqual(done["orient_db"], 9, delta=2.5)
        self.assertAlmostEqual(done["orient_offset"], -4.25, delta=1.5)   # mean of 0, -6, -2, -9
        self.assertAlmostEqual(done["path_n"], 2.7, delta=0.45)           # the spots are unaffected
        self.assertAlmostEqual(done["ref_conn"], -60 - 4.25, delta=3.5)   # centred on the average orientation
        cfg = self.link.request("cfg")["cfg"]
        self.assertEqual(cfg["orient_db"], done["orient_db"])
        self.assertEqual(simulator.effective_hyst(3.0, 9.0), 4.5)
        self.assertEqual(simulator.effective_hyst(3.0, 2.0), 3.0)
        self.assertEqual(simulator.effective_hyst(3.0, 30.0), 8.0)

    def test_turning_the_phone_keeps_its_zone(self):
        """A phone at 0.92 m that loses 10 dB by being turned reads 4 dB past the NEAR boundary, which
        used to drop it out of NEAR (3 dB hysteresis). With the spread known it stays (5 dB)."""
        self.link.request("cfg near_out_s 1")
        self.link.request("cfg orient_db 10")
        self.link.request("cfg ref_conn -65")  # centred: 5 dB below the best orientation
        self.sim.set_distance(0.92)
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["zone"] == "near", 6))
        for loss in (10, 0, 10):
            self.sim.set_orientation_loss(loss)
            self.assertIsNone(wait_for(self.link, lambda m: m["t"] == "zone", 5), "turning the phone changed its zone")
        self.sim.set_orientation_loss(0)
        self.sim.set_distance(3.0)  # really moving away still registers
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "zone" and m["prev"] == "near", 8))

    def test_autocal_stop_and_timeout(self):
        self.sim.autocal_place_timeout_ms = 1500
        self.sim.set_distance(1.0)
        time.sleep(1.0)
        before = self.link.request("cfg")["cfg"]
        self.link.request("autocal")
        self.assertFalse(self.link.request("autocal stop")["autocal"])
        self.assertIsNotNone(wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] == "stopped", 2))
        self.sim.set_distance(3.0)  # the signal has just changed, so it can't settle within 1.5 s
        self.link.request("autocal")
        failed = wait_for(self.link, lambda m: m["t"] == "autocal" and m["state"] == "failed", 5)
        self.assertIsNotNone(failed)
        self.assertIn("settled", failed["msg"])
        self.assertEqual(self.link.request("cfg")["cfg"], before)  # nothing saved

    def test_still_phone_reads_steady_and_true(self):
        """Accuracy at rest: a phone lying at 1 m reads close to 1 m and hardly wanders."""
        self.sim.set_distance(1.0)
        time.sleep(6.0)  # let the filter settle
        while not self.link.events.empty():
            self.link.events.get()
        dists = []
        end = time.monotonic() + 8
        while time.monotonic() < end:
            m = wait_for(self.link, lambda m: m["t"] == "phone" and m["dist_m"] is not None, 1)
            if m:
                dists.append(m["dist_m"])
        self.assertGreater(len(dists), 10)
        self.assertLess(max(dists) - min(dists), 0.25)
        self.assertAlmostEqual(sum(dists) / len(dists), 1.0, delta=0.15)


class TempNotes:
    """Point agent_notes at a temp folder so tests never touch the real AGENT_NOTES.md."""

    def __enter__(self):
        import tempfile
        import agent_notes
        self.mod, self.dir = agent_notes, tempfile.mkdtemp()
        self.saved = agent_notes.NOTES_PATH, agent_notes.ARCHIVE_PATH
        agent_notes.NOTES_PATH = os.path.join(self.dir, "AGENT_NOTES.md")
        agent_notes.ARCHIVE_PATH = os.path.join(self.dir, "AGENT_NOTES_ARCHIVE.md")
        return agent_notes

    def __exit__(self, *exc):
        self.mod.NOTES_PATH, self.mod.ARCHIVE_PATH = self.saved


class AgentNotesTest(unittest.TestCase):
    def test_add_load_done_archive_delete(self):
        with TempNotes() as notes:
            notes.add("bug", "Phone stuck in NEAR at 5 m", snapshot="- Board: test")
            notes.add("idea", "First line\n## 2026-01-01 00:00 | bug | open\nnot a heading")
            got = notes.load()
            self.assertEqual([n["kind"] for n in got], ["bug", "idea"])
            self.assertEqual({n["status"] for n in got}, {"open"})
            self.assertIn("<details><summary>Board snapshot</summary>", got[0]["body"])
            self.assertIn("not a heading", got[1]["body"])  # the fake heading stayed inside the note
            notes.set_status(0, "done")
            self.assertEqual(notes.archive_done(), 1)
            self.assertEqual([n["kind"] for n in notes.load()], ["idea"])
            self.assertEqual([n["status"] for n in notes.load(notes.ARCHIVE_PATH)], ["done"])
            notes.delete(0)
            self.assertEqual(notes.load(), [])
            with open(notes.NOTES_PATH, encoding="utf-8") as f:
                self.assertTrue(f.read().startswith("# Notes for AI agents"))
            with self.assertRaises(ValueError):
                notes.add("bug", "   ")


class ChipDetectTest(unittest.TestCase):
    def test_esptool_output(self):
        import focus_app
        f = focus_app.chip_from_esptool
        self.assertEqual(f("Detecting chip type... ESP32\nChip type:          ESP32-D0WD-V3 (revision v3.1)"), "ESP32")
        self.assertEqual(f("Chip is ESP32-D0WDQ6 (revision 1)"), "ESP32")
        self.assertEqual(f("Detecting chip type... ESP32-S3\nChip type:          ESP32-S3 (QFN56) (revision v0.2)"), "ESP32-S3")
        self.assertEqual(f("Chip type:          ESP32-C3 (QFN32) (revision v0.4)"), "ESP32-C3")
        self.assertIsNone(f("A fatal error occurred: Failed to connect to Espressif device: No serial data received."))


class AppWindowTest(unittest.TestCase):
    """Drives the real Tk windows in demo mode. Any exception inside a Tk callback fails the test
    (in the real app those would be invisible, since it runs without a console)."""

    def _root(self):
        import tkinter as tk
        import focus_app
        focus_app.SESSIONS_CSV = os.path.join(os.environ.get("TEMP", "."), "focus-test-sessions.csv")
        root = tk.Tk()
        self.tk_errors = []
        root.report_callback_exception = lambda *exc: self.tk_errors.append(exc)
        return root, focus_app

    def _close(self, root):
        for job in root.tk.splitlist(root.tk.call("after", "info")):  # stop pending timers quietly
            root.after_cancel(job)
        root.destroy()

    def _pump(self, root, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            time.sleep(0.02)

    def test_dev_panel(self):
        root, focus_app = self._root()
        try:
            with TempNotes() as notes:
                app = focus_app.App(root)
                app._auto_connect = lambda: None
                app.toggle_demo()
                app.sim.set_distance(0.4)
                self._pump(root, 2.0)
                app.open_dev_panel()
                panel = app.dev_panel
                self._pump(root, 1.0)
                self.assertEqual(panel.board_vals["fw"].cget("text"), "focus-device 0.2.0-sim")
                # Console: a typed command and its reply both show up
                panel._send("help")
                self._pump(root, 1.0)
                text = panel.console.get("1.0", "end")
                self.assertIn("> help", text)
                self.assertIn('"cmd":"help"', text)
                self.assertNotIn('{"t":"phone"', text)  # phone stream hidden by default
                # Config: one setting via its Set button
                panel.cfg_rows["near_out_s"][1].set("4")
                panel._set_one("near_out_s")
                self._pump(root, 1.5)
                self.assertEqual(app.cfg["near_out_s"], 4.0)
                self.assertEqual(panel.cfg_rows["near_out_s"][0].cget("text"), "4")
                # Preset
                panel._apply(dict(focus_app.dev_panel.PRESETS[0][1]), "Snappy")
                self._pump(root, 1.5)
                self.assertEqual(app.cfg["near_out_s"], 2.0)
                # Signal tab has data and draws
                self.assertTrue(panel.sig)
                panel.nb.select(2)
                self._pump(root, 0.6)
                # Agent note with a snapshot
                panel.note_text.insert("1.0", "Leaving NEAR felt slow on the real board")
                panel._add_note()
                saved = notes.load()
                self.assertEqual(len(saved), 1)
                self.assertIn("Leaving NEAR felt slow", saved[0]["body"])
                self.assertIn("- Board: focus-device 0.2.0-sim", saved[0]["body"])
                self.assertIn("near_out_s=2.0", saved[0]["body"])
                panel.notes_tree.selection_set("0")
                panel._set_status("done")
                self.assertEqual(notes.load()[0]["status"], "done")
                panel.close()
                self.assertIsNone(app.dev_panel)
            self.assertEqual(self.tk_errors, [])
        finally:
            self._close(root)

    def test_auto_calibrate_dialog(self):
        root, focus_app = self._root()
        try:
            app = focus_app.App(root)
            app._auto_connect = lambda: None
            app.toggle_demo()
            app.sim.autocal_measure_ms = app.sim.autocal_turn_measure_ms = 3000
            app.sim.autocal_turn_min_ms = 800
            app.sim.set_distance(0.5)
            self._pump(root, 2.0)
            app.request("cfg path_n 2.0")
            self._pump(root, 0.5)
            app.open_autocal()
            dlg = app.autocal_dialog
            dlg.near.set("3")
            dlg.far.set("3.5")
            dlg.start()  # spots too close: refused in the dialog, nothing sent
            self.assertIn("1.5 times", dlg.msg.cget("text"))
            dlg.near.set("0.5")
            dlg.far.set("2")
            dlg.start()

            def pump_until(pred, seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end and not pred():
                    root.update()
                    time.sleep(0.02)
                return pred()

            self.assertTrue(pump_until(lambda: dlg.state == "measuring" and dlg.step == 1, 12))
            self.assertIn("Measuring spot 1", dlg.text.cget("text"))
            self.assertTrue(pump_until(lambda: dlg.state == "place" and dlg.step == 2, 12))
            self.assertIn("2 m", dlg.text.cget("text"))
            app.sim.set_distance(2.0)
            self.assertTrue(pump_until(lambda: dlg.state == "turn", 20), "never asked to turn the phone")
            self.assertIn("quarter turn", dlg.text.cget("text"))
            app.sim.set_orientation_loss(7)
            self.assertTrue(pump_until(lambda: dlg.state in ("done", "failed"), 50))
            self.assertEqual(dlg.state, "done", dlg.msg.cget("text"))
            self.assertIn("Room factor", dlg.text.cget("text"))
            self.assertIn("Orientation", dlg.text.cget("text"))
            self._pump(root, 1.0)  # the app re-reads the settings
            self.assertAlmostEqual(app.cfg["path_n"], 2.7, delta=0.45)
            self.assertAlmostEqual(app.cfg["orient_db"], 7, delta=2.5)
            dlg.close()
            self.assertIsNone(app.autocal_dialog)
            # Closing mid-run stops it on the board
            app.open_autocal()
            dlg = app.autocal_dialog
            dlg.start()
            self.assertTrue(pump_until(lambda: dlg.running, 3))
            dlg.close()
            self._pump(root, 0.5)
            self.assertIsNone(app.sim.autocal)
            self.assertEqual(self.tk_errors, [])
        finally:
            self._close(root)

    def test_fast_slider_no_stale_display(self):
        root, focus_app = self._root()
        try:
            app = focus_app.App(root)
            app._auto_connect = lambda: None
            app.toggle_demo()
            app.sim.set_distance(0.3)
            self._pump(root, 3.0)
            for d in [0.1, 10, 0.3, 8, 0.1, 10, 6] * 4:  # whip the slider
                app.demo_dist.set(d)
                app._demo_moved()
                self._pump(root, 0.05)
            self._pump(root, 9.0)  # settle at 6 m: must have left NEAR (5 s + filter lag)
            self.assertIn(app.phones[0]["zone"], ("room", "far"))
            app.demo_gone.set(True)
            app._demo_moved()
            self._pump(root, 1.5)
            self.assertIsNone(app.phones[0]["dist_m"])
            self.assertEqual(app.history[0][-1][1], None)  # the chart breaks the line, no flat stale value
            app.demo_gone.set(False)
            app.demo_auto.set(True)
            app._demo_auto()
            self._pump(root, 1.0)
            self.assertAlmostEqual(app.demo_dist.get(), min(10, app.sim.true_distance or 10), delta=0.6)
            self.assertEqual(self.tk_errors, [])
        finally:
            self._close(root)

    def test_demo_session(self):
        root, focus_app = self._root()
        try:
            app = focus_app.App(root)
            app._auto_connect = lambda: None  # don't touch real serial ports
            app.toggle_demo()
            app.sim.set_distance(0.3)

            def pump(seconds):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    root.update()
                    time.sleep(0.02)

            pump(2.5)
            self.assertEqual(app.info.get("fw"), "focus-device")
            self.assertIn(0, app.phones)
            self.assertEqual(app.phones[0].get("zone"), "near")
            app.minutes.set("1")
            app.toggle_focus()
            pump(1.0)
            self.assertTrue(app.focus.get("active"))
            self.assertEqual(app.focus_btn.cget("text"), "Stop")
            pump(5.5)  # grace 5 s -> distracted
            self.assertTrue(app.focus.get("distracted"))
            self.assertIn("Put it away", app.banner.cget("text"))
            app.sim.set_distance(8.0)
            pump(4.0)
            self.assertTrue(app.focus.get("distracted"))  # leaving NEAR takes 8 s on purpose
            pump(7.0)
            self.assertFalse(app.focus.get("distracted"))
            self.assertIn("out of reach", app.banner.cget("text"))
            app.toggle_focus()
            pump(1.0)
            self.assertFalse(app.focus.get("active"))
            self.assertGreater(len(app.history[0]), 5)
            if "--screenshot" in sys.argv or os.environ.get("FOCUS_SCREENSHOT"):
                self._screenshot(root, app)
            self.assertEqual(self.tk_errors, [])
        finally:
            self._close(root)

    def _screenshot(self, root, app):
        try:
            from PIL import ImageGrab
        except ImportError:
            return
        app.sim.set_distance(0.4)
        end = time.monotonic() + 4
        while time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.environ["FOCUS_SCREENSHOT"])


if __name__ == "__main__":
    unittest.main()
