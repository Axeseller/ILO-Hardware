// esp32-control: a serial command shell for driving an ESP32 by hand or from a script.
//
// Open a serial monitor at 115200 baud and type `help`. Every command replies with
// zero or more data lines and then exactly one final line starting with "OK" or "ERR",
// so tools/esp.py (or any other program) knows when the reply is complete.
//
// To add your own command: write a `static void cmdThing()` below and add it to COMMANDS.

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include "soc/soc_caps.h"

#define FW_NAME "esp32-control"
#define FW_VERSION "0.1.0"

#ifndef LED_PIN
#  ifdef LED_BUILTIN
#    define LED_PIN LED_BUILTIN
#  else
#    define LED_PIN 2
#  endif
#endif
#ifndef LED_ACTIVE_LOW
#  define LED_ACTIVE_LOW 0
#endif

static const uint32_t BAUD = 115200;
static const uint8_t MAX_TOKENS = 6;
static const uint8_t PIN_UNSET = 0xFF;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static Stream *io = &Serial;          // where the current command came from / replies go
static String tok[MAX_TOKENS];
static uint8_t ntok = 0;

static bool ledOn = false;
static uint32_t blinkMs = 0, lastBlink = 0;

static uint8_t pinModes[SOC_GPIO_PIN_COUNT];   // last mode we set, or PIN_UNSET
static uint32_t pwmFreq[SOC_GPIO_PIN_COUNT];   // 0 = pin not driven by PWM
static uint8_t pwmBits[SOC_GPIO_PIN_COUNT];

// ---------------------------------------------------------------------------
// Reply helpers
// ---------------------------------------------------------------------------

static void ok(const String &msg = "") { io->println(msg.length() ? "OK " + msg : String("OK")); }
static void err(const String &msg) { io->println("ERR " + msg); }

static bool parseNum(const String &s, long &out) {
  if (s.length() == 0) return false;
  char *end = nullptr;
  out = strtol(s.c_str(), &end, 0);   // accepts 12, 0x1F, ...
  return *end == '\0';
}

// ---------------------------------------------------------------------------
// Pin safety: refuse pins that would crash the chip or cut the serial link
// ---------------------------------------------------------------------------

static const char *reservedReason(int pin) {
#if CONFIG_IDF_TARGET_ESP32
  if (pin >= 6 && pin <= 11) return "is wired to the flash chip (touching it crashes the ESP32)";
  if (pin == 1 || pin == 3) return "is the USB-serial TX/RX line this shell talks over";
#elif CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3
  if (pin >= 26 && pin <= 32) return "is wired to the flash/PSRAM chip";
  if (pin == 43 || pin == 44) return "is the UART TX/RX line this shell talks over";
  if (pin == 19 || pin == 20) return "is a native USB data line";
#elif CONFIG_IDF_TARGET_ESP32C3
  if (pin >= 12 && pin <= 17) return "is wired to the flash chip";
  if (pin == 20 || pin == 21) return "is the UART TX/RX line this shell talks over";
  if (pin == 18 || pin == 19) return "is a native USB data line";
#endif
  return nullptr;
}

// Parses tok[idx] as a GPIO number usable for input or output. Prints ERR and returns -1 if not.
static int argPin(uint8_t idx, bool output) {
  long p;
  if (idx >= ntok || !parseNum(tok[idx], p)) { err("expected a GPIO number"); return -1; }
  if (p < 0 || p >= SOC_GPIO_PIN_COUNT || !((SOC_GPIO_VALID_GPIO_MASK >> p) & 1ULL)) {
    err("GPIO" + String(p) + " doesn't exist on this chip");
    return -1;
  }
  if (output && !((SOC_GPIO_VALID_OUTPUT_GPIO_MASK >> p) & 1ULL)) {
    err("GPIO" + String(p) + " is input-only");
    return -1;
  }
  if (const char *why = reservedReason(p)) { err("GPIO" + String(p) + " " + why); return -1; }
  return (int)p;
}

// ---------------------------------------------------------------------------
// PWM (works on both Arduino-ESP32 2.x and 3.x, whose LEDC APIs differ)
// ---------------------------------------------------------------------------

#if ESP_ARDUINO_VERSION_MAJOR >= 3
static bool pwmAttach(int pin, uint32_t freq, uint8_t bits) { return ledcAttach(pin, freq, bits); }
static void pwmWrite(int pin, uint32_t duty) { ledcWrite(pin, duty); }
static void pwmDetach(int pin) { ledcDetach(pin); }
#else
static int8_t pwmChan[SOC_GPIO_PIN_COUNT];  // LEDC channel + 1, 0 = none
static uint32_t chanUsed = 0;
static bool pwmAttach(int pin, uint32_t freq, uint8_t bits) {
  int ch = pwmChan[pin] - 1;
  if (ch < 0) {
    // Even channels only: neighbouring channels share a timer, i.e. a frequency.
    for (int c = 0; c < SOC_LEDC_CHANNEL_NUM; c += 2)
      if (!(chanUsed & (1u << c))) { ch = c; break; }
    if (ch < 0) return false;
  }
  if (ledcSetup(ch, freq, bits) == 0) return false;
  ledcAttachPin(pin, ch);
  chanUsed |= 1u << ch;
  pwmChan[pin] = ch + 1;
  return true;
}
static void pwmWrite(int pin, uint32_t duty) { ledcWrite(pwmChan[pin] - 1, duty); }
static void pwmDetach(int pin) {
  if (!pwmChan[pin]) return;
  ledcDetachPin(pin);
  chanUsed &= ~(1u << (pwmChan[pin] - 1));
  pwmChan[pin] = 0;
}
#endif

static bool pwmSet(int pin, uint32_t freq, uint8_t bits, uint32_t duty) {
  if (pwmFreq[pin] != freq || pwmBits[pin] != bits) {
    if (pwmFreq[pin]) pwmDetach(pin);
    pwmFreq[pin] = 0;
    if (!pwmAttach(pin, freq, bits)) return false;
    pwmFreq[pin] = freq;
    pwmBits[pin] = bits;
    pinModes[pin] = PIN_UNSET;
  }
  pwmWrite(pin, duty);
  return true;
}

static void pwmRelease(int pin) {
  if (pin < SOC_GPIO_PIN_COUNT && pwmFreq[pin]) {
    pwmDetach(pin);
    pwmFreq[pin] = 0;
  }
}

// Make sure a pin is a plain GPIO in the given mode (undoing PWM if needed).
static void ensureMode(int pin, uint8_t mode) {
  pwmRelease(pin);
  if (pin < SOC_GPIO_PIN_COUNT && pinModes[pin] == mode) return;
  pinMode(pin, mode);
  if (pin < SOC_GPIO_PIN_COUNT) pinModes[pin] = mode;
}

// ---------------------------------------------------------------------------
// LED
// ---------------------------------------------------------------------------

static void ledWrite(bool on) {
  ledOn = on;
  digitalWrite(LED_PIN, (on != (bool)LED_ACTIVE_LOW) ? HIGH : LOW);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

static void cmdHelp();

static void cmdInfo() {
  uint64_t mac = ESP.getEfuseMac();
  io->printf("firmware  %s %s\n", FW_NAME, FW_VERSION);
  io->printf("chip      %s rev %d, %d core(s) @ %lu MHz\n", ESP.getChipModel(), (int)ESP.getChipRevision(),
             (int)ESP.getChipCores(), (unsigned long)ESP.getCpuFreqMHz());
  io->printf("flash     %lu KB\n", (unsigned long)(ESP.getFlashChipSize() / 1024));
  io->printf("ram       %lu KB free of %lu KB\n", (unsigned long)(ESP.getFreeHeap() / 1024),
             (unsigned long)(ESP.getHeapSize() / 1024));
  if (ESP.getPsramSize()) io->printf("psram     %lu KB\n", (unsigned long)(ESP.getPsramSize() / 1024));
  io->printf("mac       %02X:%02X:%02X:%02X:%02X:%02X\n", (int)(mac & 0xFF), (int)((mac >> 8) & 0xFF),
             (int)((mac >> 16) & 0xFF), (int)((mac >> 24) & 0xFF), (int)((mac >> 32) & 0xFF),
             (int)((mac >> 40) & 0xFF));
#if !CONFIG_IDF_TARGET_ESP32
  io->printf("temp      %.1f C (chip die, rough)\n", temperatureRead());
#endif
  io->printf("led       GPIO%d (%s)\n", (int)(LED_PIN % SOC_GPIO_PIN_COUNT), ledOn ? "on" : "off");
  io->printf("uptime    %lu s\n", (unsigned long)(millis() / 1000));
  io->printf("sdk       ESP-IDF %s, Arduino core %d.%d.%d\n", ESP.getSdkVersion(), ESP_ARDUINO_VERSION_MAJOR,
             ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH);
  ok();
}

static void cmdLed() {
  String a = ntok > 1 ? tok[1] : "";
  if (a == "") { ok(String("led ") + (ledOn ? "on" : "off")); return; }
  blinkMs = 0;
  ensureMode(LED_PIN, OUTPUT);
  if (a == "on" || a == "1") ledWrite(true);
  else if (a == "off" || a == "0") ledWrite(false);
  else if (a == "toggle") ledWrite(!ledOn);
  else { err("usage: led on|off|toggle"); return; }
  ok(String("led ") + (ledOn ? "on" : "off"));
}

static void cmdBlink() {
  long ms;
  if (ntok > 1 && tok[1] == "off") { blinkMs = 0; ledWrite(false); ok("blink off"); return; }
  if (ntok < 2 || !parseNum(tok[1], ms) || ms < 20 || ms > 60000) { err("usage: blink <20-60000 ms>|off"); return; }
  ensureMode(LED_PIN, OUTPUT);
  blinkMs = ms;
  lastBlink = millis();
  ok("blinking every " + String(ms) + " ms");
}

static void cmdMode() {
  int pin = argPin(1, ntok > 2 && tok[2] == "out");
  if (pin < 0) return;
  String m = ntok > 2 ? tok[2] : "";
  uint8_t mode;
  if (m == "in") mode = INPUT;
  else if (m == "out") mode = OUTPUT;
  else if (m == "pullup") mode = INPUT_PULLUP;
  else if (m == "pulldown") mode = INPUT_PULLDOWN;
  else { err("usage: mode <pin> in|out|pullup|pulldown"); return; }
  ensureMode(pin, mode);
  ok("GPIO" + String(pin) + " " + m);
}

static void cmdWrite() {
  int pin = argPin(1, true);
  if (pin < 0) return;
  String v = ntok > 2 ? tok[2] : "";
  int level;
  if (v == "1" || v == "high" || v == "on") level = HIGH;
  else if (v == "0" || v == "low" || v == "off") level = LOW;
  else { err("usage: write <pin> 0|1"); return; }
  ensureMode(pin, OUTPUT);
  digitalWrite(pin, level);
  ok("GPIO" + String(pin) + " = " + String(level));
}

static void cmdRead() {
  int pin = argPin(1, false);
  if (pin < 0) return;
  if (pinModes[pin] == PIN_UNSET) ensureMode(pin, INPUT);  // never configured: make it an input
  ok("GPIO" + String(pin) + " = " + String(digitalRead(pin)));
}

static void cmdAdc() {
  int pin = argPin(1, false);
  if (pin < 0) return;
  if (digitalPinToAnalogChannel(pin) < 0) { err("GPIO" + String(pin) + " has no ADC"); return; }
  pwmRelease(pin);
  pinModes[pin] = PIN_UNSET;
  int raw = analogRead(pin);
  uint32_t mv = analogReadMilliVolts(pin);
  ok("GPIO" + String(pin) + " = " + String(raw) + " raw, " + String(mv) + " mV");
}

static void cmdPwm() {
  int pin = argPin(1, true);
  if (pin < 0) return;
  if (ntok > 2 && tok[2] == "off") { ensureMode(pin, OUTPUT); digitalWrite(pin, LOW); ok("GPIO" + String(pin) + " pwm off"); return; }
  long duty, freq = 5000;
  if (ntok < 3 || !parseNum(tok[2], duty) || duty < 0 || duty > 255 ||
      (ntok > 3 && (!parseNum(tok[3], freq) || freq < 1 || freq > 40000000))) {
    err("usage: pwm <pin> <0-255> [freq Hz]   or   pwm <pin> off");
    return;
  }
  if (!pwmSet(pin, freq, 8, duty)) { err("couldn't start PWM (out of PWM channels, or frequency too high)"); return; }
  ok("GPIO" + String(pin) + " pwm " + String(duty) + "/255 @ " + String(freq) + " Hz");
}

static void cmdServo() {
  int pin = argPin(1, true);
  if (pin < 0) return;
  long angle;
  if (ntok < 3 || !parseNum(tok[2], angle) || angle < 0 || angle > 180) { err("usage: servo <pin> <0-180>"); return; }
  // 50 Hz, 14-bit: 20 ms period = 16384 ticks. 0..180 deg maps to 500..2500 us pulses.
  uint32_t us = 500 + (uint32_t)angle * 2000 / 180;
  if (!pwmSet(pin, 50, 14, us * 16384 / 20000)) { err("couldn't start PWM"); return; }
  ok("GPIO" + String(pin) + " servo " + String(angle) + " deg");
}

#ifdef RGB_BUILTIN
static void cmdRgb() {
  long r, g, b;
  if (ntok < 4 || !parseNum(tok[1], r) || !parseNum(tok[2], g) || !parseNum(tok[3], b) ||
      r < 0 || r > 255 || g < 0 || g > 255 || b < 0 || b > 255) {
    err("usage: rgb <r> <g> <b>   (each 0-255)");
    return;
  }
  blinkMs = 0;
#  if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(RGB_BUILTIN, r, g, b);
#  else
  neopixelWrite(RGB_BUILTIN, r, g, b);
#  endif
  ok("rgb " + String(r) + " " + String(g) + " " + String(b));
}
#endif

static void cmdScan() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  int n = WiFi.scanNetworks();
  if (n < 0) { err("wifi scan failed"); return; }
  for (int i = 0; i < n; i++) {
    io->printf("%4d dBm  ch%-2d  %-6s %s\n", (int)WiFi.RSSI(i), (int)WiFi.channel(i),
               WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "locked", WiFi.SSID(i).c_str());
  }
  WiFi.scanDelete();
  ok(String(n) + " network(s)");
}

static void cmdI2c() {
  long sda = SDA, scl = SCL;
  if (ntok == 3 && (!parseNum(tok[1], sda) || !parseNum(tok[2], scl))) { err("usage: i2c [sda scl]"); return; }
  Wire.end();
  if (!Wire.begin((int)sda, (int)scl)) { err("couldn't start I2C on those pins"); return; }
  Wire.setTimeOut(20);
  int found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) { io->printf("0x%02X\n", addr); found++; }
  }
  ok(String(found) + " device(s) on SDA=GPIO" + String(sda) + " SCL=GPIO" + String(scl));
}

static void cmdRestart() {
  ok("restarting");
  io->flush();
  delay(50);
  ESP.restart();
}

struct Command {
  const char *name, *args, *help;
  void (*fn)();
};

static const Command COMMANDS[] = {
  {"help", "", "this list", cmdHelp},
  {"info", "", "chip, memory, MAC, uptime", cmdInfo},
  {"led", "[on|off|toggle]", "onboard LED", cmdLed},
  {"blink", "<ms>|off", "blink the onboard LED", cmdBlink},
#ifdef RGB_BUILTIN
  {"rgb", "<r> <g> <b>", "onboard RGB LED colour (0-255 each)", cmdRgb},
#endif
  {"mode", "<pin> in|out|pullup|pulldown", "set a pin's mode", cmdMode},
  {"write", "<pin> 0|1", "drive a pin low/high", cmdWrite},
  {"read", "<pin>", "read a pin (0/1)", cmdRead},
  {"adc", "<pin>", "read an analog voltage", cmdAdc},
  {"pwm", "<pin> <0-255> [Hz]|off", "PWM a pin (LED dimming, motors via a driver)", cmdPwm},
  {"servo", "<pin> <0-180>", "point a hobby servo", cmdServo},
  {"scan", "", "list nearby Wi-Fi networks", cmdScan},
  {"i2c", "[sda scl]", "find I2C devices (sensors, displays)", cmdI2c},
  {"restart", "", "reboot the board", cmdRestart},
};

static void cmdHelp() {
  for (const Command &c : COMMANDS) {
    String usage = String(c.name) + " " + c.args;
    io->printf("  %-34s %s\n", usage.c_str(), c.help);
  }
  ok();
}

// ---------------------------------------------------------------------------
// Line input, one buffer per serial port
// ---------------------------------------------------------------------------

static void runLine(String line) {
  line.trim();
  if (!line.length()) return;
  ntok = 0;
  int i = 0;
  while (i < (int)line.length() && ntok < MAX_TOKENS) {
    while (i < (int)line.length() && line[i] == ' ') i++;
    int start = i;
    while (i < (int)line.length() && line[i] != ' ') i++;
    if (i > start) tok[ntok++] = line.substring(start, i);
  }
  for (uint8_t t = 0; t < ntok; t++) tok[t].toLowerCase();
  for (const Command &c : COMMANDS) {
    if (tok[0] == c.name) { c.fn(); return; }
  }
  err("unknown command '" + tok[0] + "' (type help)");
}

struct LineReader {
  Stream *port;
  String buf;
  void poll() {
    while (port->available()) {
      char ch = (char)port->read();
      if (ch == '\n' || ch == '\r') {
        if (buf.length()) { io = port; runLine(buf); buf = ""; }
      } else if (ch == 8 || ch == 127) {  // backspace
        if (buf.length()) buf.remove(buf.length() - 1);
      } else if (buf.length() < 120 && ch >= 32) {
        buf += ch;
      }
    }
  }
};

static LineReader readers[] = {
  {&Serial, ""},
#if ARDUINO_USB_CDC_ON_BOOT
  {&Serial0, ""},  // also listen on the UART pins / "COM" USB port
#endif
};

// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(BAUD);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial0.begin(BAUD);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) delay(10);  // give native USB a moment to enumerate
#endif
  memset(pinModes, PIN_UNSET, sizeof(pinModes));
  pinMode(LED_PIN, OUTPUT);
  ledWrite(false);

  String banner = String("READY ") + FW_NAME + " " + FW_VERSION + " on " + ESP.getChipModel() + " (type help)";
  for (LineReader &r : readers) { r.port->println(); r.port->println(banner); }
}

void loop() {
  for (LineReader &r : readers) r.poll();
  if (blinkMs && millis() - lastBlink >= blinkMs) {
    lastBlink = millis();
    ledWrite(!ledOn);
  }
  delay(1);
}
