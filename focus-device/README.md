# Focus Device

An ESP32 that senses **how close your phone is**, so you can focus. Put the board on your desk,
start a focus session, and put your phone away. If the phone comes back within reach and stays
there, the board flashes (and beeps, if you add a buzzer), and the PC app counts it as a pickup.

No phone app is needed. The phone pairs once from its normal Bluetooth settings.

## Start here

1. **Double-click `Focus Device.cmd`** (or the *Focus Device* shortcut on your desktop).
   The app opens. Everything it needs is already installed on this PC.
2. **No board yet?** Click **Demo mode**. A simulated phone appears. Drag the slider on the *Demo*
   tab to move it closer or further away, and try a focus session.
3. **Plug in the ESP32** with a USB cable that carries data (many cables only charge). The app finds
   it by itself. The first time, it offers to **install the firmware**: click *Yes* and wait 1-3 minutes.
4. **Pair your phone:** *Phones* tab, then **Pair a phone...**, and follow the steps. You'll tap
   "Focus Device" in your phone's Bluetooth settings.
5. **Calibrate:** on the *Phones* tab click **Auto calibrate...**. Lay the phone 0.5 m from the
   board, click *Start*, and move it to 2 m when the window says so. There it asks you to turn the
   phone three times (two quarter turns, then face down). The board notices by itself when the phone
   lies still, measures, and works out how the signal fades in your room and how much the phone's
   orientation matters. It takes about two minutes.
   (*Quick calibrate...* measures a single spot instead: faster, less accurate.)
6. **Focus:** pick the minutes and click **Start focus**.

## Developer panel

Click **Developer** in the top bar (or press **Ctrl+D**). It opens a separate window:

| Tab | What it's for |
|---|---|
| **Board** | What's connected (chip, firmware, Bluetooth address, self-test). Restart, pairing, forget phones, factory reset. Install firmware, with a board-type override if auto-detect fails |
| **Config** | Every setting on the board, each with its own *Set* button, and *Snappy / Balanced / Steady* presets for how quickly zones change |
| **Signal** | Raw vs filtered signal strength per phone, with the zone thresholds drawn in. Use it to tune distances and calibration |
| **Console** | Everything sent to and from the board. Type any command (`help` lists them); Up/Down recalls history; *Save log...* keeps a copy |
| **Agent notes** | Notes for the AI that works on this project (see below) |

### Notes for AI agents

Write a note in **Developer > Agent notes**, such as a bug you saw, a result from testing the real
board, an idea or a standing instruction. Tick *Attach a board snapshot* to include the firmware
version, settings, phones and recent events automatically. Notes are saved to
[AGENT_NOTES.md](AGENT_NOTES.md), which Claude Code loads at the start of every session (through
CLAUDE.md) and Cursor is told to read (through AGENTS.md). When the AI has handled a note, it marks
it `done`. *Archive done notes* then moves those to `AGENT_NOTES_ARCHIVE.md`, which keeps the
AI's reading short.

App errors, which would otherwise be invisible, are shown on the Board tab and in the Console, and
saved to `%APPDATA%\FocusDevice\errors.log`.

## Using the board without the PC

Once the firmware is installed and a phone is paired, the board works on its own (USB power only).

| Do this | What happens |
|---|---|
| Short press on **BOOT** | Start or stop a 25-minute focus session |
| Hold **BOOT** for 3 s | Pairing mode for 2 minutes (LED double-blinks) |

| LED | Meaning |
|---|---|
| Short blip every 2 s | Focusing, phone is away. Good. |
| Solid on | Phone is within reach |
| Fast flashing | You picked the phone up during a focus session |
| Double blink | Pairing mode |

**Buzzer (optional):** connect a small buzzer between a free GPIO pin (e.g. **GPIO 25** on a classic
ESP32) and **GND**. Then enter that number under *Settings > Buzzer GPIO* and click *Save to device*.

## Two ways to track the phone

| | **Live** (default) | **Quiet** |
|---|---|---|
| Phones | iPhone and Android | iPhone only |
| How | The phone stays connected (it sees a small media remote) and the board reads the signal strength 10x a second | The board listens to the iPhone's own Bluetooth broadcasts and recognises them with a key the phone shares when pairing |
| Speed | Fast and steady | Slower, especially while the phone is locked |
| Catch | iPhones may hide the on-screen keyboard while a Bluetooth "remote" is connected | None |

Switch on the *Settings* tab. The board restarts, and you'll need to pair the phone again.

## How accurate is it?

The board estimates distance from Bluetooth signal strength. That reliably tells **"phone on or
next to the desk"** apart from **"phone across the room or in another room"**. In-between distances
(1-4 m) are rough, often off by 50-100%. Bodies, pockets and walls weaken the signal, so a phone in
your pocket at the desk can look 2-4 m away. **Auto calibrate** helps most: it fits the estimate to
your room from two measured spots, where the defaults are only a guess. Run it again if you move the
board to another room. The board averages the signal as power and smooths it while the phone lies
still, so a resting phone's distance stays put, and a real move still shows straight away.

**Orientation.** A phone sends more strongly in some directions than others, so the same phone on
the same spot reads nearer or further when you turn it or lay it face down. The board has one
antenna and cannot tell "turned" from "moved". Auto calibrate measures how big that effect is for
your phone and desk, centres the estimate on the average orientation, and widens the margin the
zones use, so turning the phone no longer flips it between *near* and *room*. The distance number
still shifts when you turn the phone. Standing the board upright or raising it a few centimetres off
the desk may reduce the effect; removing it would take a second board listening from another angle.

The zones are stabilised so they don't flicker: the phone has to stay near for 3 s before it counts as
near, and away for 5 s before it stops counting. While a change is pending, the dial says
"changing to ... in N s". These times can be adjusted under *Developer > Config*.

## Troubleshooting

| Problem | Fix |
|---|---|
| App says "No board found" | Try another USB cable and port. Run `scripts\setup.ps1 -UsbOnly` to check for a missing USB driver |
| Firmware install fails at "Connecting..." | Hold **BOOT**, tap **EN/RST**, release **BOOT**, and click *Install / update firmware* again |
| "Focus Device" doesn't show on the phone | Make sure pairing mode is on (LED double-blinks) and the phone is close. iPhone: *Settings > Bluetooth*, under *Other Devices* |
| Phone paired but shows "away" in Quiet mode | Unlock the iPhone for a moment (locked iPhones broadcast rarely), or switch to Live mode |
| iPhone keyboard disappeared | That's Live mode. Use Quiet mode, or tap the keyboard icon on the iPhone |
| Distance looks wrong | Run **Auto calibrate** again in the room where you use it (it sets *Room factor* for you). Keep the phone flat and yourself still while it measures |
| Zone changes when the phone is only turned or flipped | Run **Auto calibrate** with *Also measure the phone's orientation* ticked. *Developer > Config > Orientation spread* shows what it found |
| Auto calibrate says the signal never settled | Lay the phone down (don't hold it), keep your hands and body away from the line between phone and board, and try again |
| `could not open port` | Another program has the serial port open (Arduino IDE, a serial monitor). Close it |

Focus sessions are logged to `%APPDATA%\FocusDevice\sessions.csv`.

## Project layout

```
Focus Device.cmd     double-click to start the app
app/                 the PC app (Python + tkinter)
  focus_app.py         window: live distance, focus timer, chart, pairing, calibration, firmware install
  dev_panel.py         Developer window: board, config, signal, console, agent notes
  agent_notes.py       reads/writes AGENT_NOTES.md
  device.py            serial protocol client
  simulator.py         fake board for Demo mode and tests
AGENT_NOTES.md       your notes for AI assistants (written from the Developer panel)
firmware/            the ESP32 firmware (PlatformIO, Arduino framework, NimBLE-Arduino)
  main.cpp             Bluetooth, phones, focus sessions, LED/button/buzzer, serial commands
  proximity.h          signal -> distance -> zone, focus counting (pure logic, unit-tested on PC)
  rpa.h                recognising a phone's rotating Bluetooth address from its identity key
tests/               run_tests.cmd runs everything that doesn't need hardware
scripts/setup.ps1    installs the tools on a new PC (and adds the desktop shortcut)
platformio.ini       board types: esp32 (default), esp32-s3, esp32-c3
PROTOCOL.md          the USB serial protocol between board and PC
```

## For developers

```
tests\run_tests.cmd                                  all tests + firmware build for the 3 chips
%USERPROFILE%\.platformio\penv\Scripts\pio.exe run   build (default: classic ESP32)
... pio.exe run -e esp32-s3 -t upload                build + flash an S3 board
```

You can also talk to the board directly in any serial monitor at 115200 baud: type `help`, `info`,
`focus start 25` and so on (see PROTOCOL.md).

### Status

Built and tested on this PC without hardware: the firmware compiles for all three chips, the logic
has unit tests (including the Bluetooth spec's address-resolution test vector), and the app was
tested end to end against the simulator. **Not yet tested with a real board and phone.** The first
real test should check:

- that pairing works and the IRK is received on current iOS (`irk: yes` in the Phones tab)
- whether Live mode hides the iPhone keyboard
- how quickly the phone reconnects after leaving and coming back
- what real connection and broadcast RSSI values look like at 0.5, 1 and 3 m, so the defaults can be tuned
