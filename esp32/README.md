# ESP32 control kit

Plug in an ESP32, flash it once, and then control its pins from a serial shell, from the command
line, or from Python. Everything builds with PlatformIO's command-line tools, so the PlatformIO
extension, which is awkward to install in Cursor, isn't needed.

## Already installed on this PC
- PlatformIO Core and esptool, in `%USERPROFILE%\.platformio\penv`
- The ESP32 / ESP32-S3 / ESP32-C3 compilers and the Arduino core

On another computer, run `.\scripts\setup.ps1` once. It needs Python 3 from python.org.

## First run (about 5 minutes)

1. **Plug the board in** with a USB cable that carries data. Many cables only charge. Then check
   that Windows sees it:
   ```
   .\scripts\setup.ps1 -UsbOnly
   ```
   If it says *DRIVER MISSING*, install the driver from the link it prints, then replug the board.
2. **Open this folder in Cursor:** `cursor .` (or File > Open Folder > `esp32`).
3. **Find out which chip you have.** Press `Ctrl+Shift+P`, choose *Tasks: Run Task*, then
   **ESP32: Detect Board**. If it says something other than `esp32`, change `default_envs` in
   `platformio.ini` to match.
4. **Flash the firmware:** run the task **ESP32: Upload + Monitor**. The first build takes
   1-2 minutes. If the upload hangs at `Connecting.....`, hold the **BOOT** button on the board until it
   starts writing.
5. **Try it.** Type into the monitor:
   ```
   help
   led on
   blink 200
   info
   ```
   Press `Ctrl+C` to leave the monitor.

## Controlling the board

From Cursor's terminal (`` Ctrl+` ``), where `esp` and `pio` already work:

```
esp led on            # one command, then exit
esp read 4
esp pwm 2 64          # dim the LED to 25%
esp                   # interactive shell
esp --list            # which COM port is the board on?
```

From your own Python code (use the Python in Cursor's terminal, which has pyserial):

```python
import sys; sys.path.insert(0, "tools")
from esp import Esp
board = Esp()                      # finds the port by itself
ok, lines = board.cmd("adc 34")
print(lines[-1])                   # OK GPIO34 = 1872 raw, 1517 mV
```

### Firmware commands

| Command | What it does |
|---|---|
| `help` | list commands |
| `info` | chip model, flash, RAM, MAC address, uptime |
| `led on\|off\|toggle` | onboard LED |
| `blink <ms>` / `blink off` | blink the LED in the background |
| `rgb <r> <g> <b>` | onboard RGB LED (only on boards that have one, e.g. S3 DevKitC) |
| `mode <pin> in\|out\|pullup\|pulldown` | configure a pin |
| `write <pin> 0\|1` | set a pin low or high |
| `read <pin>` | read a pin (0/1). Buttons: `mode 4 pullup`, then `read 4` |
| `adc <pin>` | read an analog voltage (raw value and millivolts) |
| `pwm <pin> <0-255> [Hz]` / `pwm <pin> off` | PWM output (dimming; motors need a driver board) |
| `servo <pin> <0-180>` | move a hobby servo (power it from 5V, not the 3.3V pin) |
| `scan` | list nearby Wi-Fi networks |
| `i2c [sda scl]` | find I2C sensors and displays (default pins: ESP32 21/22) |
| `restart` | reboot |

Every reply ends with a line that starts with `OK` or `ERR`. The firmware refuses pins that would crash
the chip, such as the flash pins (GPIO6-11 on a classic ESP32), and pins that would cut the USB link.

## Cursor tasks (`Ctrl+Shift+P` > Tasks: Run Task)

| Task | Same as |
|---|---|
| ESP32: Build (`Ctrl+Shift+B`) | `pio run` |
| ESP32: Upload | `pio run -t upload` |
| ESP32: Upload + Monitor | `pio run -t upload -t monitor` |
| ESP32: Serial Monitor | `pio device monitor` |
| ESP32: Control Shell | `esp` |
| ESP32: Detect Board / List Ports | `esp --detect` / `esp --list` |
| ESP32: Erase Flash | wipe the chip completely |
| ESP32: Refresh IntelliSense | regenerate code-completion config |

**Code completion:** when Cursor offers a C/C++ extension, install it. It reads
`.vscode/c_cpp_properties.json`, which *Refresh IntelliSense* generates.

## Troubleshooting

| Problem | Fix |
|---|---|
| No COM port at all | Try a different cable and USB port, then run `.\scripts\setup.ps1 -UsbOnly` |
| `could not open port` / `Access is denied` | Another program has the port open (monitor, `esp` shell, Arduino IDE). Close it |
| Upload stuck at `Connecting.....` | Hold **BOOT** while the upload starts. On some boards: hold BOOT, tap EN/RST, release BOOT |
| `This chip is ESP32-S3, not ESP32` | Wrong `default_envs`. Run *Detect Board* |
| Monitor shows garbage | The baud rate must be 115200 (already set in `platformio.ini`) |
| S3/C3: nothing in the monitor after upload | Press RST once. Native USB re-enumerates after flashing, and the COM number can change |
| `Brownout detector was triggered` | Not enough power. Use a better cable or port, or power servos/motors separately |
| `esp` says no reply | The firmware isn't flashed yet, or the board is still booting. Run *Upload + Monitor* |

## Project layout

```
platformio.ini      board selection and build settings
src/main.cpp        the firmware (command shell). Add your own commands here
tools/esp.py        PC-side control tool and Python library
scripts/setup.ps1   installer and USB check for a new machine
esp.cmd, pio.cmd    shortcuts for terminals outside Cursor
AGENTS.md           project notes that Cursor/Claude read automatically
```
