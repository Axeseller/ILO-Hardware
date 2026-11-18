"""Talk to the focus-device firmware over USB serial (or to the built-in simulator).

The protocol is in PROTOCOL.md: the PC sends text commands, the device answers with one JSON
object per line. Replies are matched to requests with a `#<id>` prefix; everything else is an event.

    link = Link(SerialTransport("COM5"))      # or Link(SimTransport())
    print(link.request("info"))
    msg = link.events.get()                   # {"t": "phone", "zone": "near", ...}
"""
import itertools
import json
import queue
import threading

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # the app explains this to the user
    serial = None
    list_ports = None

BAUD = 115200
USB_CHIPS = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH340/CH9102",
    0x0403: "FTDI",
    0x303A: "ESP32 native USB",
}


def find_ports():
    """All serial ports, ESP32-looking ones first: [(device, description, looks_like_esp32)]."""
    if list_ports is None:
        return []
    out = []
    for p in sorted(list_ports.comports(), key=lambda p: p.device):
        out.append((p.device, p.description or "", p.vid in USB_CHIPS))
    return sorted(out, key=lambda x: not x[2])


def guess_port():
    """The single port that looks like an ESP32, else the only port, else None."""
    ports = find_ports()
    likely = [p for p in ports if p[2]]
    if len(likely) >= 1:
        return likely[0][0]
    if len(ports) == 1:
        return ports[0][0]
    return None


class SerialTransport:
    def __init__(self, port):
        if serial is None:
            raise RuntimeError("pyserial is missing (run setup.cmd)")
        self.name = port
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = port, BAUD, 0.2
        self.ser.dtr = self.ser.rts = False  # don't hold the board in reset
        self.ser.open()

    def write(self, data):
        self.ser.write(data)

    def readline(self):
        return self.ser.readline()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class Link:
    """A connection to the device. Reader thread pushes events onto `events`."""

    def __init__(self, transport, tap=None):
        """tap(direction, text), if given, sees every line: direction is "tx" or "rx".
        It's called from the reader thread, so it must be thread-safe (a deque append is)."""
        self.t = transport
        self.tap = tap
        self.events = queue.Queue()
        self._waiters = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _tap(self, direction, text):
        if self.tap:
            try:
                self.tap(direction, text)
            except Exception:  # noqa: BLE001 - a broken tap must never kill the link
                pass

    @property
    def name(self):
        return getattr(self.t, "name", "?")

    def _reader(self):
        while self.alive:
            try:
                raw = self.t.readline()
            except Exception as e:  # unplugged
                self.alive = False
                self.events.put({"t": "_link_lost", "error": str(e)})
                return
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                self._tap("rx", text)
            if not text.startswith("{"):
                if text:
                    self.events.put({"t": "_raw", "line": text})
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                self.events.put({"t": "_raw", "line": text})
                continue
            if msg.get("t") in ("ok", "err") and "id" in msg:
                with self._lock:
                    waiter = self._waiters.pop(msg["id"], None)
                if waiter:
                    waiter.put(msg)
                    continue
            self.events.put(msg)

    def send(self, cmd):
        """Fire and forget. The reply arrives as an event."""
        self._tap("tx", cmd.strip())
        self.t.write((cmd.strip() + "\n").encode())

    def request(self, cmd, timeout=3.0):
        """Send a command and wait for its reply. Raises TimeoutError or DeviceError."""
        rid = next(self._ids)
        box = queue.Queue(maxsize=1)
        with self._lock:
            self._waiters[rid] = box
        try:
            line = "#%d %s" % (rid, cmd.strip())
            self._tap("tx", line)
            self.t.write((line + "\n").encode())
            msg = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no reply to %r" % cmd) from None
        finally:
            with self._lock:
                self._waiters.pop(rid, None)
        if msg.get("t") == "err":
            raise DeviceError(msg.get("msg", "error"))
        return msg

    def close(self):
        self.alive = False
        self.t.close()


class DeviceError(Exception):
    pass
