# Focus Device: notes for AI assistants

**First, read `AGENT_NOTES.md`.** It holds the user's open notes (bugs, hardware test results,
ideas, instructions), written from the app's developer panel. When you've handled one, change
`open` to `done` in its heading and add a line saying what you did.

Product prototype: an ESP32 estimates how close the owner's phone is (Bluetooth LE RSSI) and runs
focus sessions. Firmware in `firmware/` (PlatformIO CLI, Arduino framework, NimBLE-Arduino 2.5.2,
`src_dir = firmware`). PC app in `app/` (Python + tkinter, run with PlatformIO's penv Python, which
has pyserial). The protocol between them is `PROTOCOL.md`; keep firmware, `app/simulator.py` and
`PROTOCOL.md` in sync when changing it.

## Verify changes
- `tests\run_tests.cmd` runs everything: C++ logic tests compiled for the PC with zig
  (`%USERPROFILE%\.focus-tools\venv`), Python tests against the simulator, and `pio run` for
  esp32 / esp32-s3 / esp32-c3. Run it before saying something works.
- `pio` = `%USERPROFILE%\.platformio\penv\Scripts\pio.exe`. Build output goes to
  `~/.platformio/workspaces/meiners-focus-device` (this folder is in OneDrive).

## Design rules
- Pure logic goes in `proximity.h` / `rpa.h` (no Arduino includes outside `#ifdef ARDUINO`) so it stays host-testable.
- BLE callbacks run in the NimBLE task: they only post to `evq`; all state changes happen in `loop()`.
  `phonesLock` guards what the scan callback reads (`used`, `id`, `hasIrk`, `resolver`).
- Every serial command gets exactly one `ok`/`err` JSON reply. Never put an `id` field in a reply's
  data (it's the request id the PC matches on).
- IRKs from NimBLE's store are little-endian; `rpa.h` documents the byte order (tested against the
  Core Spec ah() vector).
- Signal chain (0.2.0): `prox::robustLevel` (Tukey fence, then a power average) -> `prox::LevelSmoother`
  (applied to loss, so it survives a conn/adv switch) -> distance and zone. Calibration (`calibrate`,
  `autocal`) must use `robustLevel` on raw samples too, so the reference and the live level agree.
  `app/simulator.py` mirrors all of it; its phone follows a fixed room (`SIM_REF`, `SIM_N`), not the settings.
- Orientation (user report, 2026-11-18: detection depends on how the iPhone is turned). RSSI from one
  antenna can't separate orientation from distance. `autocal` measures up to 4 orientations, stores
  the spread in `orient_db`, centres the refs on the average orientation, and `prox::effectiveHyst`
  widens the zone hysteresis to half the spread (max 8 dB). Don't promise more than that in software;
  the real fix is a second receiver (Axel has a second board).
- No stale data on the wire: with no fresh signal, `rssi_f`/`dist_m` are null. Zone changes that
  are waiting out their dwell time are reported as `pending`/`pending_ms` so the UI can explain lag.
- The app runs under pythonw (no console). Tk callback errors go to `App.on_tk_error`; tests set
  `report_callback_exception` and fail on any. Keep new UI code covered by `tests/test_app.py`.
- Developer panel (`app/dev_panel.py`): when you add a firmware setting, add it to `CFG_SPEC` there,
  to `DEFAULT_CFG`/`CFG_RANGES` in the simulator, and to the cfg table in PROTOCOL.md.
- Validated on hardware (2026-11-18, classic ESP32-D0WD-V3 with a CP2102 USB chip, `hid` mode, iPhone):
  the iPhone bonded with `irk: true` and live `conn` RSSI was sampled at 5 Hz, with `phone` events every
  `stream_ms` (about -59 to -61 dBm filtered, zone `room`). Uploads use 115200 baud because 460800 stalled
  on Axel's CP2102 board; the user's board was only ever flashed at 115200.
- Hardware, 2026-11-18, firmware 0.2.0 on the same board: after a reflash the iPhone reconnected by
  itself and `conn` RSSI was sampled at 10 Hz; with no link, `adv` tracking (IRK match) took over and
  reached `near`. On a recording of the phone at rest (old firmware, 5 Hz raw), replayed through both
  chains: old 1.61-2.70 m, new 1.71-2.04 m.
- Incident, same day: about 30 s after that reconnect, an encryption event for the iPhone's own link
  carried its rotating address instead of its identity. `EV_AUTH` took it for a stranger and called
  `NimBLEDevice::deleteBond`, and `ble_gap_unpair` maps a rotating address back to the identity, so
  the iPhone's bond was deleted (0 phones after restart; the phone must be forgotten and paired again).
  Fixed with `phoneForAuth()` (connection, then identity, then IRK). The fix is not yet confirmed on
  hardware, and what makes iOS send that second encryption event is not known. Never delete a bond
  by an address that has not been checked against the known phones' IRKs.
- Not yet validated on hardware: `autocal` end to end with a phone moved between two measured spots
  and turned (its thresholds, 2 dB steady / 4 dB moved / 3 dB still, come from the fading model, not
  from the board), and how many dB the iPhone's orientation really costs.
- Not yet validated on hardware: iOS keyboard behaviour in `hid` mode, reconnect latency, `scan`
  mode, Android, calibrated RSSI levels (`ref_conn` -60 / `ref_adv` -65 are placeholders until calibrated).
