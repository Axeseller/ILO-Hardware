#!/usr/bin/env python3
"""Talk to the esp32-control firmware over USB.

  esp                       interactive shell (type commands, Ctrl+C to quit)
  esp led on                send one command, print the reply
  esp --list                show serial ports and which ones look like an ESP32
  esp --detect              ask the chip what it is and which build env to use
  esp --port COM5 info      use a specific port

From Python code:   from esp import Esp;  Esp().cmd("write 4 1")
"""
import argparse
import re
import subprocess
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing. Run this through esp.cmd or Cursor's terminal "
             "(they use PlatformIO's Python, which has it), or: pip install pyserial")

BAUD = 115200
USB_CHIPS = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH340/CH9102",
    0x0403: "FTDI",
    0x303A: "ESP32 native USB",
}
SLOW_COMMANDS = {"scan": 20, "i2c": 15}
CHIP_TO_ENV = {"ESP32": "esp32", "ESP32-S3": "esp32-s3", "ESP32-C3": "esp32-c3"}
ROM_BOOT_LINE = re.compile(r"^(ets |rst:|ESP-ROM|configsip|clk_drv|mode:|load:|entry |SPIWP|Build:)")


def find_ports():
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    return ports, [p for p in ports if p.vid in USB_CHIPS]


def pick_port(explicit=None):
    if explicit:
        return explicit
    ports, likely = find_ports()
    if len(likely) == 1:
        return likely[0].device
    if len(likely) > 1:
        sys.exit("Several boards found (%s). Pick one with --port." % ", ".join(p.device for p in likely))
    if len(ports) == 1:
        return ports[0].device
    sys.exit("No ESP32 found. Plug it in with a DATA usb cable (many are charge-only), then run: esp --list")


class Esp:
    """A connection to a board running esp32-control."""

    def __init__(self, port=None):
        self.port = pick_port(port)
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = self.port, BAUD, 0.1
        self.ser.dtr = self.ser.rts = False  # keep the auto-reset circuit from rebooting / holding the chip
        try:
            self.ser.open()
        except serial.SerialException as e:
            sys.exit(f"Can't open {self.port}: {e}\n"
                     "Something else is using it (serial monitor, upload, Arduino IDE?). Close that and retry.")

    def close(self):
        self.ser.close()

    def _send(self, line):
        self.ser.write((line.strip() + "\n").encode())

    def cmd(self, line, timeout=None, echo=False):
        """Send one command. Returns (ok, lines) where lines includes the final OK/ERR line."""
        timeout = timeout or SLOW_COMMANDS.get(line.split()[0].lower() if line.split() else "", 4)
        self.ser.reset_input_buffer()
        self._send(line)
        out, booting, resent = [], False, False
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ser.readline()
            if not raw:
                continue
            text = raw.decode(errors="replace").rstrip("\r\n")
            if ROM_BOOT_LINE.match(text):  # the board rebooted when we opened the port
                booting = True
                continue
            if text.startswith("READY "):  # firmware finished booting: our command was lost, send again
                if not resent:
                    resent, booting = True, False
                    self._send(line)
                    deadline = time.time() + timeout
                continue
            if booting or not text.strip():
                continue
            out.append(text)
            if echo:
                print(text)
            if text == "OK" or text.startswith(("OK ", "ERR")):
                return text.startswith("OK"), out
        raise TimeoutError(f"No reply within {timeout}s. Is the esp32-control firmware flashed? "
                           "(Upload it with: pio run -t upload)")

    def shell(self):
        print(f"Connected to {self.port}. Type 'help' for commands, Ctrl+C to quit.")
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    raw = self.ser.readline()
                except serial.SerialException:
                    print("\n[board disconnected]")
                    stop.set()
                    return
                if raw:
                    print(raw.decode(errors="replace").rstrip("\r\n"), flush=True)

        threading.Thread(target=reader, daemon=True).start()
        try:
            while not stop.is_set():
                line = input()
                if line.strip().lower() in ("exit", "quit"):
                    break
                if line.strip():
                    self._send(line)
        except (KeyboardInterrupt, EOFError):
            pass
        stop.set()


def list_cmd():
    ports, _ = find_ports()
    if not ports:
        print("No serial ports at all.\n"
              "  - Try another USB cable (charge-only cables are very common) and another USB port.\n"
              "  - Run scripts\\setup.ps1 -UsbOnly to check for a missing USB driver.")
        return
    for p in ports:
        chip = USB_CHIPS.get(p.vid)
        tag = f"<- {chip}" if chip else ""
        ids = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
        print(f"{p.device:8} {ids}  {p.description[:45]:45} {tag}")


def detect_cmd(port):
    port = pick_port(port)
    print(f"Asking the chip on {port} what it is (it will reboot)...")
    try:
        r = subprocess.run([sys.executable, "-m", "esptool", "--port", port, "flash-id"],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        sys.exit("esptool not found; run scripts\\setup.ps1")
    text = r.stdout + r.stderr
    if r.returncode != 0:
        print(text.strip())
        sys.exit("\nCouldn't talk to the bootloader. Hold the BOOT button, tap EN/RST, release BOOT, and retry.\n"
                 "If the port is busy, close any serial monitor first.")
    m = re.search(r"ESP32-(S2|S3|C2|C3|C5|C6|C61|H2|P4)\b", text)
    chip = f"ESP32-{m.group(1)}" if m else "ESP32"
    for pattern in (r"Chip type:\s*(.+)", r"Chip is (.+)", r"Features:\s*(.+)", r"Crystal frequency:\s*(.+)",
                    r"Detected flash size:\s*(.+)", r"MAC:\s*(.+)"):
        mm = re.search(pattern, text)
        if mm:
            print("  " + mm.group(0).strip())
    p = next((x for x in list_ports.comports() if x.device == port), None)
    if p is not None and p.vid == 0x303A:
        print("  Connected through the chip's own USB (no USB-serial adapter).")
    env = CHIP_TO_ENV.get(chip)
    if env:
        print(f"\nThis is an {chip}. In platformio.ini set:   default_envs = {env}")
    else:
        print(f"\nThis is an {chip}; there's no env for it yet. Add one to platformio.ini "
              f"(board list: pio boards {chip.lower().replace('-', '')}).")


def main():
    ap = argparse.ArgumentParser(description="Control an ESP32 running esp32-control.",
                                 epilog="Commands: run 'esp help' with the board plugged in.")
    ap.add_argument("--port", "-p", help="serial port, e.g. COM5 (default: auto-detect)")
    ap.add_argument("--list", action="store_true", help="list serial ports")
    ap.add_argument("--detect", action="store_true", help="identify the chip via its bootloader")
    ap.add_argument("command", nargs="*", help="command to send (omit for interactive shell)")
    a = ap.parse_args()

    if a.list:
        return list_cmd()
    if a.detect:
        return detect_cmd(a.port)

    esp = Esp(a.port)
    try:
        if not a.command:
            esp.shell()
            return 0
        try:
            ok, _ = esp.cmd(" ".join(a.command), echo=True)
        except TimeoutError as e:
            print(e, file=sys.stderr)
            return 2
        return 0 if ok else 1
    finally:
        esp.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
