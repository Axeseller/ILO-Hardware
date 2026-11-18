# ESP32 control project: notes for AI assistants

Arduino-framework firmware for ESP32 boards, built with PlatformIO **Core (CLI)**. The PlatformIO
editor extension is NOT installed. Build and flash through `pio` in the terminal or the tasks in
`.vscode/tasks.json`.

## Commands
- Build: `pio run`. Flash: `pio run -t upload`. Monitor: `pio device monitor`.
- Talk to the running firmware: `esp <command>` (e.g. `esp led on`, `esp read 4`), or `esp` for a shell.
- Identify an unknown board: `esp --detect`. List ports: `esp --list`.
- `pio` lives at `%USERPROFILE%\.platformio\penv\Scripts\pio.exe`. Cursor's terminal has it on PATH via
  `.vscode/settings.json`. Elsewhere use the `pio.cmd` / `esp.cmd` shims in this folder.
- The serial port can only be open in one program. Close the monitor or `esp` shell before uploading.

## Layout
- `platformio.ini`: one env per chip (`esp32`, `esp32-s3`, `esp32-c3`). `default_envs` picks the target.
  Build output goes to `~/.platformio/workspaces/meiners-esp32`, not `.pio/` (this folder is in OneDrive).
- `src/main.cpp`: serial command shell at 115200 baud. Add a command by writing `static void cmdX()`
  and adding a row to `COMMANDS`. Read arguments from `tok[1..ntok-1]`, which are already lowercased.
  Validate pins with `argPin()`. Finish **every** command with exactly one `ok(...)` or `err(...)`,
  because `tools/esp.py` waits for that final line.
- `tools/esp.py`: host CLI plus an importable `Esp` class (`Esp().cmd("write 4 1")` returns `(ok, lines)`).

## Rules
- The code must compile on Arduino-ESP32 2.x and 3.x. Their LEDC/PWM APIs differ, so use the
  `pwmSet`/`pwmRelease` helpers, and use `#if ESP_ARDUINO_VERSION_MAJOR >= 3` for anything else that differs.
- Never drive flash pins (ESP32 GPIO6-11, S3 GPIO26-32, C3 GPIO12-17). `reservedReason()` blocks them.
- Don't block in `loop()` for long. Use `millis()` timing like the blink code does.
- After changing firmware, run `pio run` (and for other chips, `pio run -e esp32-s3 -e esp32-c3`) to
  confirm it compiles before saying it works.
