// focus-device: an ESP32 that senses how close your phone is, so you can focus.
//
// How it works
//   1. Pairing (button held 3 s, or `pair` over USB): the phone pairs with "Focus Device" from its
//      own Bluetooth settings. No app. The device keeps the phone's identity key (IRK).
//   2. Measuring, two ways:
//        - connection RSSI: in `hid` mode the phone stays connected (it sees a media remote), and
//          the device reads the link's signal strength 10 times a second. Works on iPhone and Android.
//        - advertisements: the device listens to the phone's own Bluetooth broadcasts and
//          recognises them with the IRK, even though the phone's address changes every ~15 min.
//          Mostly iPhones. This is the only source in `scan` mode.
//   3. Signal strength -> robust power average -> adaptive smoothing -> distance -> zone
//      (near / room / far / away). `autocal` fits the distance model to the room from two spots
//      and measures how much the phone's orientation changes its signal, which the zones allow for.
//   4. Focus session (short button press, or `focus start`): if the phone stays `near` for longer
//      than grace_s, it counts as a pickup, the LED flashes and the optional buzzer beeps.
//
// The PC talks to it over USB serial; see PROTOCOL.md. Pure logic lives in proximity.h and rpa.h
// so it can be unit-tested on a PC (tests/).

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <Preferences.h>
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "proximity.h"
#include "rpa.h"

#define FW_NAME "focus-device"
#define FW_VERSION "0.2.0"
#define BLE_NAME "Focus Device"

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
#ifndef BUTTON_PIN
#  define BUTTON_PIN 0
#endif

static const uint8_t MAX_PHONES = 3;  // NimBLE keeps 3 bonds by default
static const uint32_t SAMPLE_MS = 100;  // each reading comes from a different radio channel, so more = steadier
static const uint32_t CONN_WINDOW_MS = 2000, ADV_WINDOW_MS = 15000;
static const uint8_t MIN_CONN_SAMPLES = 5, MIN_ADV_SAMPLES = 3;
static const uint32_t FAST_ADV_MS = 30000;  // advertise fast this long after boot/disconnect
static const uint32_t UNAUTH_TIMEOUT_MS = 20000;
static const uint32_t IRK_WAIT_MS = 10000;

enum Mode : uint8_t { MODE_HID = 0, MODE_SCAN = 1 };
enum Src : uint8_t { SRC_NONE = 0, SRC_CONN = 1, SRC_ADV = 2 };
static const char *srcName(uint8_t s) { return s == SRC_CONN ? "conn" : s == SRC_ADV ? "adv" : "none"; }

// ---------------------------------------------------------------------------
// Settings (saved in flash)
// ---------------------------------------------------------------------------

struct Config {
  float nearM = 1.0f, farM = 4.0f, pathN = 2.7f;
  float refConn = -60.0f, refAdv = -65.0f;
  float hystDb = 3.0f, nearInS = 3.0f, nearOutS = 5.0f, zoneS = 5.0f;  // zone stability (see proximity.h)
  float smoothS = 4.0f;  // smoothing while the phone lies still (0 = off)
  float orientDb = 0.0f;  // signal change between the phone's best and worst orientation (set by autocal)
  uint16_t awayS = 30, graceS = 5, focusMin = 25, streamMs = 500;
  int8_t buzzerPin = -1;
  uint8_t mode = MODE_HID;
};
static Config cfg;
static Preferences prefs;

static void saveConfig() {
  prefs.putFloat("near_m", cfg.nearM);
  prefs.putFloat("far_m", cfg.farM);
  prefs.putFloat("path_n", cfg.pathN);
  prefs.putFloat("ref_conn", cfg.refConn);
  prefs.putFloat("ref_adv", cfg.refAdv);
  prefs.putFloat("hyst_db", cfg.hystDb);
  prefs.putFloat("near_in_s", cfg.nearInS);
  prefs.putFloat("near_out_s", cfg.nearOutS);
  prefs.putFloat("zone_s", cfg.zoneS);
  prefs.putFloat("smooth_s", cfg.smoothS);
  prefs.putFloat("orient_db", cfg.orientDb);
  prefs.putUShort("away_s", cfg.awayS);
  prefs.putUShort("grace_s", cfg.graceS);
  prefs.putUShort("focus_min", cfg.focusMin);
  prefs.putUShort("stream_ms", cfg.streamMs);
  prefs.putChar("buzzer_pin", cfg.buzzerPin);
  prefs.putUChar("mode", cfg.mode);
}

static void loadConfig() {
  Config d;
  cfg.nearM = prefs.getFloat("near_m", d.nearM);
  cfg.farM = prefs.getFloat("far_m", d.farM);
  cfg.pathN = prefs.getFloat("path_n", d.pathN);
  cfg.refConn = prefs.getFloat("ref_conn", d.refConn);
  cfg.refAdv = prefs.getFloat("ref_adv", d.refAdv);
  cfg.hystDb = prefs.getFloat("hyst_db", d.hystDb);
  cfg.nearInS = prefs.getFloat("near_in_s", d.nearInS);
  cfg.nearOutS = prefs.getFloat("near_out_s", d.nearOutS);
  cfg.zoneS = prefs.getFloat("zone_s", d.zoneS);
  cfg.smoothS = prefs.getFloat("smooth_s", d.smoothS);
  cfg.orientDb = prefs.getFloat("orient_db", d.orientDb);
  cfg.awayS = prefs.getUShort("away_s", d.awayS);
  cfg.graceS = prefs.getUShort("grace_s", d.graceS);
  cfg.focusMin = prefs.getUShort("focus_min", d.focusMin);
  cfg.streamMs = prefs.getUShort("stream_ms", d.streamMs);
  cfg.buzzerPin = prefs.getChar("buzzer_pin", d.buzzerPin);
  cfg.mode = prefs.getUChar("mode", d.mode) == MODE_SCAN ? MODE_SCAN : MODE_HID;
}

static prox::ZoneConfig zoneConfig() {
  prox::ZoneConfig z;
  z.nearM = cfg.nearM;
  z.farM = cfg.farM;
  z.pathN = cfg.pathN;
  z.hystDb = prox::effectiveHyst(cfg.hystDb, cfg.orientDb);
  z.enterNearMs = (uint32_t)(cfg.nearInS * 1000);
  z.leaveNearMs = (uint32_t)(cfg.nearOutS * 1000);
  z.otherMs = (uint32_t)(cfg.zoneS * 1000);
  return z;
}

// ---------------------------------------------------------------------------
// JSON output
// ---------------------------------------------------------------------------

class Json {
 public:
  Json() : s_("{") {}
  explicit Json(const char *type) : s_("{") { str("t", type); }
  Json &str(const char *k, const String &v) {
    key(k);
    s_ += '"';
    for (unsigned i = 0; i < v.length(); i++) {
      char c = v[i];
      if (c == '"' || c == '\\') {
        s_ += '\\';
        s_ += c;
      } else if ((uint8_t)c < 0x20) {
        s_ += ' ';
      } else {
        s_ += c;
      }
    }
    s_ += '"';
    return *this;
  }
  Json &num(const char *k, long v) {
    key(k);
    s_ += String(v);
    return *this;
  }
  Json &flt(const char *k, float v, uint8_t dec = 1) {
    key(k);
    if (isnan(v) || isinf(v)) s_ += "null";
    else s_ += String(v, (unsigned int)dec);
    return *this;
  }
  Json &bol(const char *k, bool v) {
    key(k);
    s_ += v ? "true" : "false";
    return *this;
  }
  Json &raw(const char *k, const String &json) {
    key(k);
    s_ += json;
    return *this;
  }
  String done() const { return s_ + "}"; }

 private:
  void key(const char *k) {
    if (!first_) s_ += ',';
    first_ = false;
    s_ += '"';
    s_ += k;
    s_ += "\":";
  }
  String s_;
  bool first_ = true;
};

// Serial ports we talk on. S3/C3 boards listen on native USB and on the UART ("COM") pins.
static Stream *const PORTS[] = {
  &Serial,
#if ARDUINO_USB_CDC_ON_BOOT
  &Serial0,
#endif
};
static const uint8_t NPORTS = sizeof(PORTS) / sizeof(PORTS[0]);

static bool portWritable(uint8_t i) {
#if ARDUINO_USB_CDC_ON_BOOT
  if (i == 0) return (bool)Serial;  // native USB: skip when no PC has the port open
#endif
  (void)i;
  return true;
}

static void emit(const String &line) {
  for (uint8_t i = 0; i < NPORTS; i++)
    if (portWritable(i)) PORTS[i]->println(line);
}

static void logMsg(const String &msg) { emit(Json("log").str("msg", msg).done()); }

// ---------------------------------------------------------------------------
// Phones
// ---------------------------------------------------------------------------

struct Phone {
  bool used = false;
  ble_addr_t id{};             // identity address (stable, learned when pairing)
  bool hasIrk = false;
  rpa::Resolver resolver;      // recognises the phone's rotating addresses
  uint16_t conn = BLE_HS_CONN_HANDLE_NONE;
  prox::RssiWindow connWin, advWin;
  prox::ZoneTracker zone;
  prox::LevelSmoother smooth;
  int lastRssi = 0;
  uint8_t src = SRC_NONE;
  float rssiF = NAN, distM = NAN;

  // Empty the slot. (Not `= Phone()`: the AES context inside the resolver can't be copied.)
  void reset() {
    used = false;
    id = ble_addr_t{};
    hasIrk = false;
    resolver.clear();
    conn = BLE_HS_CONN_HANDLE_NONE;
    connWin.reset();
    advWin.reset();
    zone.reset();
    smooth.reset();
    lastRssi = 0;
    src = SRC_NONE;
    rssiF = distM = NAN;
  }
};
static Phone phones[MAX_PHONES];
static SemaphoreHandle_t phonesLock;  // the BLE task reads id/irk while matching advertisements

static bool sameAddr(const ble_addr_t &a, const ble_addr_t &b) {
  return a.type == b.type && memcmp(a.val, b.val, 6) == 0;
}
static String addrStr(const ble_addr_t &a) { return String(NimBLEAddress(a).toString().c_str()); }

static int findPhone(const ble_addr_t &id) {
  for (int i = 0; i < MAX_PHONES; i++)
    if (phones[i].used && sameAddr(phones[i].id, id)) return i;
  return -1;
}
static int findPhoneByConn(uint16_t conn) {
  for (int i = 0; i < MAX_PHONES; i++)
    if (phones[i].used && phones[i].conn == conn) return i;
  return -1;
}
static int phoneCount() {
  int n = 0;
  for (auto &p : phones) n += p.used;
  return n;
}

// Reads the phone's IRK from NimBLE's bond store. False if the phone didn't share one (yet).
static bool readIrk(const ble_addr_t &id, uint8_t irk[16]) {
  ble_store_key_sec key{};
  key.peer_addr = id;
  key.idx = 0;
  ble_store_value_sec val{};
  if (ble_store_read_peer_sec(&key, &val) != 0 || !val.irk_present) return false;
  memcpy(irk, val.irk, 16);
  return true;
}

// Recently seen random addresses and which phone they belong to (-1 = none), so each address is
// checked with AES once rather than for every advertisement. Only touched by the BLE task, except
// clearAddrCache() which runs under phonesLock.
struct AddrCacheEntry {
  uint8_t val[6];
  int8_t phone;
  bool used;
};
static AddrCacheEntry addrCache[24];
static uint8_t addrCacheNext = 0;
static void clearAddrCache() { memset(addrCache, 0, sizeof(addrCache)); }

static void savePhones() {
  for (int i = 0; i < MAX_PHONES; i++) {
    char k[4] = {'p', char('0' + i), 0};
    if (phones[i].used) prefs.putBytes(k, &phones[i].id, sizeof(ble_addr_t));
    else prefs.remove(k);
  }
}

static void setPhoneIrk(Phone &p, const uint8_t irk[16]) {
  p.resolver.setIrk(irk);
  p.hasIrk = p.resolver.ready();
}

// Rebuild the phone list from flash, keeping only phones NimBLE still has a bond for.
static void loadPhones() {
  xSemaphoreTake(phonesLock, portMAX_DELAY);
  for (int i = 0; i < MAX_PHONES; i++) {
    char k[4] = {'p', char('0' + i), 0};
    ble_addr_t id{};
    if (prefs.getBytes(k, &id, sizeof(id)) == sizeof(id) && NimBLEDevice::isBonded(NimBLEAddress(id))) {
      phones[i].used = true;
      phones[i].id = id;
    }
  }
  // Bonds we don't have a slot for (e.g. flash was cleared): adopt them.
  for (int b = 0; b < NimBLEDevice::getNumBonds(); b++) {
    NimBLEAddress a = NimBLEDevice::getBondedAddress(b);
    if (findPhone(*a.getBase()) >= 0) continue;
    for (int i = 0; i < MAX_PHONES; i++)
      if (!phones[i].used) {
        phones[i].used = true;
        phones[i].id = *a.getBase();
        break;
      }
  }
  for (auto &p : phones) {
    uint8_t irk[16];
    if (p.used && readIrk(p.id, irk)) setPhoneIrk(p, irk);
  }
  clearAddrCache();
  xSemaphoreGive(phonesLock);
  savePhones();
}

// ---------------------------------------------------------------------------
// Bluetooth -> main loop events (BLE callbacks run in the NimBLE task)
// ---------------------------------------------------------------------------

enum EvType : uint8_t { EV_ADV, EV_CONNECT, EV_DISCONNECT, EV_AUTH };
struct Ev {
  EvType type;
  int8_t phone;
  int8_t rssi;
  bool encrypted;
  uint16_t conn;
  int reason;
  uint32_t ms;
  ble_addr_t addr;
};
static QueueHandle_t evq;

static void post(const Ev &e) { xQueueSend(evq, &e, 0); }  // drop if the loop is behind

// Which phone sent this advertisement? -1 if none.
static int matchAdvertiser(const NimBLEAddress &a) {
  const ble_addr_t *b = a.getBase();
  int found = -1;
  if (xSemaphoreTake(phonesLock, pdMS_TO_TICKS(5)) != pdTRUE) return -1;
  // NimBLE may already have replaced the rotating address with the identity address.
  for (int i = 0; i < MAX_PHONES && found < 0; i++)
    if (phones[i].used && sameAddr(phones[i].id, *b)) found = i;
  if (found < 0 && b->type == BLE_ADDR_RANDOM && rpa::isResolvable(b->val)) {
    bool cached = false;
    for (auto &c : addrCache)
      if (c.used && memcmp(c.val, b->val, 6) == 0) {
        found = c.phone;
        cached = true;
        break;
      }
    if (!cached) {
      for (int i = 0; i < MAX_PHONES && found < 0; i++)
        if (phones[i].used && phones[i].hasIrk && phones[i].resolver.matches(b->val)) found = i;
      AddrCacheEntry &c = addrCache[addrCacheNext];
      addrCacheNext = (addrCacheNext + 1) % (sizeof(addrCache) / sizeof(addrCache[0]));
      memcpy(c.val, b->val, 6);
      c.phone = (int8_t)found;
      c.used = true;
    }
  }
  xSemaphoreGive(phonesLock);
  return found;
}

class ScanCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice *d) override {
    int i = matchAdvertiser(d->getAddress());
    if (i < 0) return;
    Ev e{};
    e.type = EV_ADV;
    e.phone = (int8_t)i;
    e.rssi = d->getRSSI();
    e.ms = millis();
    post(e);
  }
};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *, NimBLEConnInfo &ci) override {
    Ev e{};
    e.type = EV_CONNECT;
    e.conn = ci.getConnHandle();
    e.addr = *ci.getAddress().getBase();
    e.ms = millis();
    post(e);
  }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &ci, int reason) override {
    Ev e{};
    e.type = EV_DISCONNECT;
    e.conn = ci.getConnHandle();
    e.addr = *ci.getIdAddress().getBase();
    e.reason = reason;
    e.ms = millis();
    post(e);
  }
  void onAuthenticationComplete(NimBLEConnInfo &ci) override {
    Ev e{};
    e.type = EV_AUTH;
    e.conn = ci.getConnHandle();
    e.addr = *ci.getIdAddress().getBase();
    e.encrypted = ci.isEncrypted() && ci.isBonded();
    e.ms = millis();
    post(e);
  }
};

// ---------------------------------------------------------------------------
// Bluetooth setup and advertising
// ---------------------------------------------------------------------------

static NimBLEServer *server;
static bool bleOk = false;

// Media-remote report map (play/pause, volume). We never send key presses; the HID service just
// lets the phone stay connected from its Bluetooth settings without an app.
static const uint8_t HID_REPORT_MAP[] = {
  0x05, 0x0C,  // Usage Page (Consumer)
  0x09, 0x01,  // Usage (Consumer Control)
  0xA1, 0x01,  // Collection (Application)
  0x85, 0x01,  //   Report ID (1)
  0x15, 0x00,  //   Logical Minimum (0)
  0x25, 0x01,  //   Logical Maximum (1)
  0x75, 0x01,  //   Report Size (1)
  0x95, 0x03,  //   Report Count (3)
  0x09, 0xCD,  //   Usage (Play/Pause)
  0x09, 0xE9,  //   Usage (Volume Up)
  0x09, 0xEA,  //   Usage (Volume Down)
  0x81, 0x02,  //   Input (Data, Variable, Absolute)
  0x95, 0x05,  //   Report Count (5)
  0x81, 0x01,  //   Input (Constant) padding
  0xC0,        // End Collection
};

static void buildGatt() {
  server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  server->advertiseOnDisconnect(false);  // advertising is managed in updateAdvertising()
  if (cfg.mode == MODE_HID) {
    NimBLEHIDDevice *hid = new NimBLEHIDDevice(server);
    hid->setManufacturer("Meiners");
    hid->setPnp(0x02, 0x303A, 0x8F01, 0x0100);
    hid->setHidInfo(0x00, 0x01);
    hid->setReportMap((uint8_t *)HID_REPORT_MAP, sizeof(HID_REPORT_MAP));
    hid->getInputReport(1);  // encrypted characteristic: reading it makes the phone pair
    hid->setBatteryLevel(100);
  } else {
    // Same trick ESPresense uses for iPhones: look like a heart-rate monitor whose
    // characteristics need encryption, so the phone pairs (and hands over its IRK).
    NimBLEService *hrs = server->createService("180D");
    NimBLECharacteristic *hrm = hrs->createCharacteristic(
        "2A37", NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ_ENC);
    uint8_t bpm[2] = {0x00, 60};
    hrm->setValue(bpm, 2);
    NimBLEService *dis = server->createService("180A");
    dis->createCharacteristic("2A29", NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::READ_ENC)->setValue("Meiners");
    dis->createCharacteristic("2A24", NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::READ_ENC)->setValue(BLE_NAME);
  }
  server->start();
}

static bool pairing = false;
static uint32_t pairUntilMs = 0;
static uint32_t fastAdvUntilMs = 0;
static int8_t advState = -1;  // -1 off, 0 slow, 1 fast (what's currently running)

static void configureAdvertising(bool fast) {
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->stop();
  adv->reset();
  adv->setName(BLE_NAME);
  if (cfg.mode == MODE_HID) {
    adv->setAppearance(0x03C0);  // generic HID
    adv->addServiceUUID(NimBLEUUID((uint16_t)0x1812));
  } else {
    adv->setAppearance(0x0340);  // heart-rate sensor
    adv->addServiceUUID(NimBLEUUID((uint16_t)0x180D));
  }
  // Units of 0.625 ms: 32 = 20 ms, 48 = 30 ms (fast); 668 = 417.5 ms (Apple-recommended slow step).
  adv->setMinInterval(fast ? 32 : 668);
  adv->setMaxInterval(fast ? 48 : 668);
}

// Advertise while pairing, and in hid mode while a known phone is not connected (so it reconnects).
static void updateAdvertising(uint32_t now) {
  bool want = pairing;
  if (!want && cfg.mode == MODE_HID && server->getConnectedCount() < CONFIG_BT_NIMBLE_MAX_CONNECTIONS) {
    for (auto &p : phones)
      if (p.used && p.conn == BLE_HS_CONN_HANDLE_NONE) want = true;
  }
  bool fast = pairing || (int32_t)(fastAdvUntilMs - now) > 0;
  int8_t target = want ? (fast ? 1 : 0) : -1;
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  bool running = adv->isAdvertising();
  if (target == advState && (target < 0) == !running) return;
  if (target < 0) {
    adv->stop();
  } else {
    configureAdvertising(target == 1);
    adv->start();
  }
  advState = target;
}

// ---------------------------------------------------------------------------
// Pending connections and pairing
// ---------------------------------------------------------------------------

struct Pending {
  uint16_t conn;
  uint32_t sinceMs;
  bool used;
};
static Pending pending[CONFIG_BT_NIMBLE_MAX_CONNECTIONS];

// A newly paired phone whose IRK we are still waiting for.
static int irkWaitPhone = -1;
static uint16_t irkWaitConn = BLE_HS_CONN_HANDLE_NONE;
static uint32_t irkWaitSinceMs = 0;

static void disconnect(uint16_t conn) {
  if (conn != BLE_HS_CONN_HANDLE_NONE) server->disconnect(conn);
}

static void pairOpen(uint32_t seconds) {
  pairing = true;
  pairUntilMs = millis() + seconds * 1000;
  advState = -2;  // force a restart with pairing settings
  emit(Json("pair").str("state", "open").num("left_s", seconds).str("mode", cfg.mode == MODE_HID ? "hid" : "scan").done());
}

static void pairClose(const char *why) {
  if (!pairing) return;
  pairing = false;
  advState = -2;
  emit(Json("pair").str("state", "closed").str("why", why).done());
}

static void finishPairing(int idx, bool gotIrk) {
  Phone &p = phones[idx];
  emit(Json("pair").str("state", "bonded").num("id", idx).str("addr", addrStr(p.id)).bol("irk", gotIrk).done());
  if (!gotIrk && cfg.mode == MODE_SCAN)
    logMsg("This phone didn't share its identity key, so scan mode can't recognise it. Try hid mode.");
  if (cfg.mode == MODE_SCAN && p.conn != BLE_HS_CONN_HANDLE_NONE) disconnect(p.conn);  // only needed the key
  pairing = false;
  advState = -2;
  emit(Json("pair").str("state", "closed").str("why", "bonded").done());
}

// ---------------------------------------------------------------------------
// Calibration state (the logic is further down, after the LED and buzzer it uses)
// ---------------------------------------------------------------------------

// Raw readings of one phone, collected while a calibration is measuring.
static struct {
  bool on = false;
  int8_t phone = -1;
  int8_t conn[200], adv[100];
  uint8_t nConn = 0, nAdv = 0;
} cap;

static void captureStart(int phone) {
  cap.on = true;
  cap.phone = (int8_t)phone;
  cap.nConn = cap.nAdv = 0;
}

// `calibrate`: one spot, for a fixed time.
static struct {
  bool active = false;
  uint32_t untilMs = 0;
  float metres = 1.0f;
} calib;

// `autocal`: two spots. For each one the device waits until the phone lies still, measures it,
// and at the end fits both the 1 m reference and the path-loss exponent. Then, at the second spot,
// the phone is turned up to three times to see how much its orientation changes the signal.
enum AcState : uint8_t { AC_IDLE, AC_PLACE, AC_TURN, AC_MEASURE };
static const uint32_t AC_PLACE_TIMEOUT_MS = 90000, AC_MEASURE_MS = 6000;
static const uint32_t AC_TURN_MIN_MS = 7000, AC_TURN_MAX_MS = 20000, AC_TURN_MEASURE_MS = 4000;
static const uint8_t AC_MAX_RETRIES = 4;
static const float AC_MOVED_DB = 4.0f;  // the second spot must be at least this much weaker
static const float AC_STILL_DB = 3.0f;  // the two halves of one measurement must agree this well
static struct {
  AcState state = AC_IDLE;
  int8_t phone = -1;
  uint8_t step = 0, retries = 0;                    // step 0, 1: the spots; 2..: orientations at spot 2
  float m[2] = {0.5f, 2.0f};                        // distance of each spot
  float conn[2] = {NAN, NAN}, adv[2] = {NAN, NAN};  // level measured at each spot (NAN = none)
  uint8_t turns = 3, nOrient = 0;                   // orientations asked for / levels in orient[]
  bool orientConn = true;                           // which source orient[] was measured with
  float orient[4];                                  // level per orientation; [0] = as at spot 2
  uint32_t sinceMs = 0;
} ac;

// ---------------------------------------------------------------------------
// Focus, LED, buzzer, button
// ---------------------------------------------------------------------------

static prox::FocusSession focus;
static bool streamOn = true;

static String focusJson(uint32_t now) {
  return Json()
      .bol("active", focus.active())
      .num("limit_s", focus.limitS())
      .num("elapsed_s", focus.elapsedS(now))
      .num("left_s", focus.leftS(now))
      .num("near_s", focus.nearS())
      .num("pickups", focus.pickups())
      .bol("distracted", focus.distracted())
      .done();
}

static void emitFocus(const char *state, uint32_t now) {
  Json j("focus");
  j.str("state", state)
      .bol("active", focus.active())
      .num("limit_s", focus.limitS())
      .num("elapsed_s", focus.elapsedS(now))
      .num("left_s", focus.leftS(now))
      .num("near_s", focus.nearS())
      .num("pickups", focus.pickups())
      .bol("distracted", focus.distracted());
  emit(j.done());
}

static void ledWrite(bool on) {
  static int8_t last = -1;  // only touch the pin on a change (S3 boards drive an RGB LED, which is slow)
  if (last == (int8_t)on) return;
  last = on;
  digitalWrite(LED_PIN, (on != (bool)LED_ACTIVE_LOW) ? HIGH : LOW);
}

// Non-blocking beeper: a short pattern of tones.
static struct {
  uint8_t left = 0;
  uint16_t freq = 0, onMs = 0, gapMs = 0;
  uint32_t nextMs = 0;
} beeper;
static uint32_t lastNagMs = 0;

static void beep(uint8_t count, uint16_t freq, uint16_t onMs, uint16_t gapMs) {
  if (cfg.buzzerPin < 0) return;
  beeper.left = count;
  beeper.freq = freq;
  beeper.onMs = onMs;
  beeper.gapMs = gapMs;
  beeper.nextMs = millis();
}

static void beeperTick(uint32_t now) {
  if (!beeper.left || cfg.buzzerPin < 0 || (int32_t)(now - beeper.nextMs) < 0) return;
  tone(cfg.buzzerPin, beeper.freq, beeper.onMs);
  beeper.left--;
  beeper.nextMs = now + beeper.onMs + beeper.gapMs;
}

static prox::Zone nearestZone() {
  prox::Zone z = prox::AWAY;
  for (auto &p : phones)
    if (p.used && p.zone.zone() < z) z = p.zone.zone();
  return z;
}

static void ledTick(uint32_t now) {
  bool on;
  if (pairing) on = (now % 1000) < 100 || ((now % 1000) >= 200 && (now % 1000) < 300);  // double blink
  else if (ac.state == AC_MEASURE) on = true;                                            // measuring: hold still
  else if (ac.state == AC_PLACE) on = (now / 250) % 2;                                   // waiting for the phone
  else if (ac.state == AC_TURN) on = (now / 100) % 2;                                    // turn the phone now
  else if (focus.active() && focus.distracted()) on = (now / 100) % 2;                  // fast flash
  else if (nearestZone() == prox::NEAR) on = true;                                       // phone close
  else if (focus.active()) on = (now % 2000) < 60;                                       // heartbeat
  else on = false;
  ledWrite(on);
}

static void focusStart(uint32_t minutes, uint32_t now) {
  focus.start(now, minutes * 60);
  lastNagMs = 0;
  beep(1, 2400, 80, 0);
  emitFocus("started", now);
}

static void focusStop(uint32_t now) {
  if (!focus.active()) return;
  focus.stop(now);
  beep(2, 1800, 80, 80);
  emitFocus("stopped", now);
}

static void buttonTick(uint32_t now) {
  static bool down = false, longDone = false;
  static uint32_t downMs = 0, changeMs = 0;
  bool pressed = digitalRead(BUTTON_PIN) == LOW;
  if (pressed != down && now - changeMs > 30) {  // debounce
    changeMs = now;
    down = pressed;
    if (down) {
      downMs = now;
      longDone = false;
    } else if (!longDone && now - downMs < 1500) {
      if (focus.active()) focusStop(now);
      else focusStart(cfg.focusMin, now);
    }
  }
  if (down && !longDone && now - downMs >= 3000) {
    longDone = true;
    if (phoneCount() >= MAX_PHONES) {
      logMsg("All phone slots are used. Forget a phone first (PC app, or `forget <id>`).");
      beep(3, 600, 120, 80);
    } else {
      pairOpen(120);
      beep(1, 3000, 150, 0);
    }
  }
}

// ---------------------------------------------------------------------------
// Calibration
// ---------------------------------------------------------------------------

static float clampRef(float r) { return r < -100 ? -100 : r > -20 ? -20 : r; }

// The phone with the strongest signal right now (-1 = none).
static int strongestPhone() {
  int best = -1;
  float bestR = -200;
  for (int i = 0; i < MAX_PHONES; i++)
    if (phones[i].used && !isnan(phones[i].rssiF) && phones[i].rssiF > bestR) {
      bestR = phones[i].rssiF;
      best = i;
    }
  return best;
}

static void calibrationFinish() {
  calib.active = cap.on = false;
  float loss = prox::lossForDistance(calib.metres, cfg.pathN);
  bool okConn = cap.nConn >= 5, okAdv = cap.nAdv >= 3;
  Json j("calibrated");
  j.bol("ok", okConn || okAdv).num("id", cap.phone).flt("metres", calib.metres, 2);
  j.num("samples_conn", cap.nConn).num("samples_adv", cap.nAdv);
  if (okConn) {
    cfg.refConn = clampRef(prox::robustLevel(cap.conn, cap.nConn) + loss);  // rssi = ref - loss
    j.flt("ref_conn", cfg.refConn);
  }
  if (okAdv) {
    cfg.refAdv = clampRef(prox::robustLevel(cap.adv, cap.nAdv) + loss);
    j.flt("ref_adv", cfg.refAdv);
  }
  if (okConn || okAdv) saveConfig();
  else j.str("msg", "No signal from the phone. Is it paired and nearby?");
  emit(j.done());
}

static void autocalEmitPlace(bool retry) {
  Json j("autocal");
  j.str("state", "place").num("step", ac.step + 1).flt("metres", ac.m[ac.step], 2);
  j.num("left_s", AC_PLACE_TIMEOUT_MS / 1000);
  if (retry) j.bol("retry", true);
  emit(j.done());
}

static void autocalFail(const char *msg) {
  ac.state = AC_IDLE;
  cap.on = false;
  beep(3, 600, 120, 80);
  emit(Json("autocal").str("state", "failed").str("msg", msg).done());
}

static void autocalStop() {
  if (ac.state == AC_IDLE) return;
  ac.state = AC_IDLE;
  cap.on = false;
  emit(Json("autocal").str("state", "stopped").done());
}

// Both spots are measured: fit the model and save it.
static void autocalFinish() {
  ac.state = AC_IDLE;
  bool byConn = !isnan(ac.conn[0]) && !isnan(ac.conn[1]);
  bool byAdv = !isnan(ac.adv[0]) && !isnan(ac.adv[1]);
  if (!byConn && !byAdv)
    return autocalFail("The connection changed between the two spots, so they can't be compared. Try again.");
  const float *r = byConn ? ac.conn : ac.adv;
  prox::PathFit fit;
  if (r[0] - r[1] < 3.0f || !prox::fitPathLoss(ac.m[0], r[0], ac.m[1], r[1], 1.5f, 5.0f, fit))
    return autocalFail("The signal was not clearly weaker at the second spot. Check both distances and try again.");
  float oldConn = cfg.refConn, oldAdv = cfg.refAdv;
  cfg.pathN = fit.n;
  if (byConn) {
    cfg.refConn = clampRef(fit.ref);
    float sum = 0;  // broadcasts: use whatever was heard at either spot, else keep their old offset
    int k = 0;
    for (int i = 0; i < 2; i++)
      if (!isnan(ac.adv[i])) {
        sum += ac.adv[i] + prox::lossForDistance(ac.m[i], fit.n);
        k++;
      }
    cfg.refAdv = clampRef(k ? sum / k : oldAdv + (cfg.refConn - oldConn));
  } else {
    cfg.refAdv = clampRef(fit.ref);
    cfg.refConn = clampRef(oldConn + (cfg.refAdv - oldAdv));
  }
  // Orientations: centre the reference on the average one and remember the spread for the zones.
  bool oriented = ac.nOrient >= 2;
  prox::OrientFit of{0, 0};
  if (oriented) {
    of = prox::fitOrientation(ac.orient, ac.nOrient);
    cfg.orientDb = of.spreadDb > 20 ? 20 : of.spreadDb;
    cfg.refConn = clampRef(cfg.refConn + of.offsetDb);
    cfg.refAdv = clampRef(cfg.refAdv + of.offsetDb);
  }
  saveConfig();
  beep(2, 2400, 80, 80);
  Json j("autocal");
  j.str("state", "done").num("id", ac.phone).str("src", byConn ? "conn" : "adv");
  j.flt("path_n", cfg.pathN, 2).flt("ref_conn", cfg.refConn).flt("ref_adv", cfg.refAdv);
  j.flt("rssi1", r[0]).flt("rssi2", r[1]).bol("clamped", fit.clamped);
  if (oriented) j.num("orientations", ac.nOrient).flt("orient_db", cfg.orientDb).flt("orient_offset", of.offsetDb);
  emit(j.done());
}

static void autocalEmitTurn(bool retry) {
  uint8_t k = ac.step - 1;  // turn number, 1..ac.turns
  Json j("autocal");
  j.str("state", "turn").num("turn", k).num("of", ac.turns).str("how", k == 3 ? "face_down" : "quarter");
  if (retry) j.bol("retry", true);
  emit(j.done());
}

// One spot (6 s) or one orientation (4 s) has been measured.
static void autocalMeasured(uint32_t now) {
  cap.on = false;
  bool turn = ac.step >= 2;  // steps 0 and 1 are the two spots, 2.. are the orientations at spot 2
  bool okConn = cap.nConn >= 20, okAdv = cap.nAdv >= 3;
  if (!okConn && !okAdv) {
    if (turn) return autocalFinish();  // the spots are measured: keep them
    return autocalFail("Lost the phone's signal while measuring. Is it still connected?");
  }
  // Did the phone (or you) move meanwhile? Then the first and second half disagree: measure again.
  int8_t *buf = okConn ? cap.conn : cap.adv;
  uint8_t n = okConn ? cap.nConn : cap.nAdv;
  if (n >= 10) {
    float a = prox::robustLevel(buf, n / 2), b = prox::robustLevel(buf + n / 2, n - n / 2);
    if (fabsf(a - b) > AC_STILL_DB) {
      if (++ac.retries > AC_MAX_RETRIES) {
        if (turn) return autocalFinish();
        return autocalFail("The signal kept changing while measuring. Keep the phone and yourself still, then try again.");
      }
      ac.state = turn ? AC_TURN : AC_PLACE;
      ac.sinceMs = now;
      if (turn) autocalEmitTurn(true);
      else autocalEmitPlace(true);
      return;
    }
  }
  float lc = okConn ? prox::robustLevel(cap.conn, cap.nConn) : NAN;
  float la = okAdv ? prox::robustLevel(cap.adv, cap.nAdv) : NAN;
  if (ac.step == 0) {
    ac.conn[0] = lc;
    ac.adv[0] = la;
    ac.step = 1;
    ac.retries = 0;
    ac.state = AC_PLACE;
    ac.sinceMs = now;
    beep(2, 2400, 80, 80);
    autocalEmitPlace(false);
    return;
  }
  if (ac.step == 1) {
    ac.conn[1] = lc;
    ac.adv[1] = la;
    ac.orientConn = okConn;  // the orientations are compared within one source
    ac.orient[0] = okConn ? lc : la;
    ac.nOrient = 1;
  } else {
    float l = ac.orientConn ? lc : la;
    if (!isnan(l)) ac.orient[ac.nOrient++] = l;
  }
  if (ac.step - 1 < ac.turns) {  // another orientation to go
    ac.step++;
    ac.retries = 0;
    ac.state = AC_TURN;
    ac.sinceMs = now;
    beep(2, 2400, 80, 80);
    autocalEmitTurn(false);
  } else {
    autocalFinish();
  }
}

static void autocalStartMeasure(uint32_t now) {
  bool turn = ac.step >= 2;
  ac.state = AC_MEASURE;
  ac.sinceMs = now;
  captureStart(ac.phone);
  beep(1, 2400, 80, 0);
  Json j("autocal");
  j.str("state", "measuring").num("step", turn ? 2 : ac.step + 1).flt("metres", ac.m[turn ? 1 : ac.step], 2);
  if (turn) j.num("turn", ac.step - 1);
  j.num("seconds", (turn ? AC_TURN_MEASURE_MS : AC_MEASURE_MS) / 1000);
  emit(j.done());
}

static void autocalTick(uint32_t now) {
  if (ac.state == AC_IDLE) return;
  uint32_t waited = now - ac.sinceMs;
  if (ac.state == AC_MEASURE) {
    if (waited >= (ac.step >= 2 ? AC_TURN_MEASURE_MS : AC_MEASURE_MS)) autocalMeasured(now);
    return;
  }
  Phone &p = phones[ac.phone];
  bool conn = p.src == SRC_CONN;
  const prox::RssiWindow &w = conn ? p.connWin : p.advWin;
  uint32_t half = conn ? 2000 : 6000;
  uint8_t need = conn ? 10 : 3;
  bool steady = p.src != SRC_NONE && w.steady(now, half, need, conn ? 2.0f : 3.0f);
  if (ac.state == AC_TURN) {
    // Time to turn the phone and let go, then measure once it lies still. Turning need not change
    // the level, so there is nothing to wait for beyond that: after a while, measure anyway.
    if (waited >= AC_TURN_MIN_MS && (steady || waited >= AC_TURN_MAX_MS)) autocalStartMeasure(now);
    return;
  }
  // AC_PLACE: wait until the phone lies still at the spot, then measure by ourselves.
  if (waited >= AC_PLACE_TIMEOUT_MS)
    return autocalFail(ac.step == 0
                           ? "The signal never settled. Lay the phone down at the first spot, step back and try again."
                           : "The signal never got weaker and settled. Lay the phone down further from the board and try again.");
  if (!steady) return;
  if (ac.step == 1) {  // the second spot only counts once the phone has clearly moved away
    float first = conn ? ac.conn[0] : ac.adv[0], cur;
    if (isnan(first) || !w.value(now, half, need, cur) || cur > first - AC_MOVED_DB) return;
  }
  autocalStartMeasure(now);
}

// ---------------------------------------------------------------------------
// Measurement
// ---------------------------------------------------------------------------

static void emitZone(int i, prox::Zone prev) {
  Phone &p = phones[i];
  emit(Json("zone")
           .num("id", i)
           .str("zone", prox::zoneName(p.zone.zone()))
           .str("prev", prox::zoneName(prev))
           .flt("dist_m", p.distM, 2)
           .done());
}

static void addSample(int i, uint8_t src, int rssi, uint32_t now) {
  Phone &p = phones[i];
  if (rssi >= 0 || rssi < -110) return;
  p.lastRssi = rssi;
  (src == SRC_CONN ? p.connWin : p.advWin).add(rssi, now);
  if (cap.on && cap.phone == i) {
    if (src == SRC_CONN && cap.nConn < sizeof(cap.conn)) cap.conn[cap.nConn++] = (int8_t)rssi;
    if (src == SRC_ADV && cap.nAdv < sizeof(cap.adv)) cap.adv[cap.nAdv++] = (int8_t)rssi;
  }
}

// Re-evaluate every phone's distance and zone. Called every SAMPLE_MS.
static void evaluate(uint32_t now) {
  prox::ZoneConfig zc = zoneConfig();
  for (int i = 0; i < MAX_PHONES; i++) {
    Phone &p = phones[i];
    if (!p.used) continue;
    if (p.conn != BLE_HS_CONN_HANDLE_NONE) {
      int8_t r = 0;
      if (ble_gap_conn_rssi(p.conn, &r) == 0) addSample(i, SRC_CONN, r, now);
    }
    float r;
    uint8_t src = SRC_NONE;
    if (p.conn != BLE_HS_CONN_HANDLE_NONE && p.connWin.value(now, CONN_WINDOW_MS, MIN_CONN_SAMPLES, r)) src = SRC_CONN;
    else if (p.advWin.value(now, ADV_WINDOW_MS, MIN_ADV_SAMPLES, r)) src = SRC_ADV;
    p.src = src;
    prox::Zone prev = p.zone.zone();
    if (src != SRC_NONE) {
      // Smoothed as loss (dB below the 1 m reference), which means the same for both sources.
      float ref = src == SRC_CONN ? cfg.refConn : cfg.refAdv;
      float loss = p.smooth.update(ref - r, now, cfg.smoothS);
      p.rssiF = ref - loss;
      p.distM = prox::distanceForLoss(loss, cfg.pathN);
      if (p.zone.update(loss, now, zc)) emitZone(i, prev);
    } else {
      p.rssiF = p.distM = NAN;  // no fresh signal: report no distance rather than a stale one
      uint32_t last = max(p.connWin.lastMs(), p.advWin.lastMs());
      bool everHeard = !p.connWin.empty() || !p.advWin.empty();
      if (!everHeard || now - last >= (uint32_t)cfg.awayS * 1000) {
        if (p.zone.goAway()) emitZone(i, prev);
      }
    }
  }
}

static void streamPhones(uint32_t now) {
  prox::ZoneConfig zc = zoneConfig();
  for (int i = 0; i < MAX_PHONES; i++) {
    Phone &p = phones[i];
    if (!p.used) continue;
    uint32_t last = max(p.connWin.lastMs(), p.advWin.lastMs());
    bool heard = !p.connWin.empty() || !p.advWin.empty();
    Json j("phone");
    j.num("id", i).str("src", srcName(p.src));
    if (heard) j.num("rssi", p.lastRssi);
    j.flt("rssi_f", p.rssiF).flt("dist_m", p.distM, 2).str("zone", prox::zoneName(p.zone.zone()));
    j.num("age_ms", heard ? (long)(now - last) : -1).bol("connected", p.conn != BLE_HS_CONN_HANDLE_NONE);
    if (p.zone.pending() != p.zone.zone())  // a zone change is waiting out its dwell time
      j.str("pending", prox::zoneName(p.zone.pending())).num("pending_ms", p.zone.pendingLeftMs(now, zc));
    emit(j.done());
  }
}

static String phonesJson() {
  String s = "[";
  bool first = true;
  for (int i = 0; i < MAX_PHONES; i++) {
    Phone &p = phones[i];
    if (!p.used) continue;
    if (!first) s += ',';
    first = false;
    s += Json()
             .num("id", i)
             .str("addr", addrStr(p.id))
             .bol("irk", p.hasIrk)
             .bol("connected", p.conn != BLE_HS_CONN_HANDLE_NONE)
             .str("src", srcName(p.src))
             .str("zone", prox::zoneName(p.zone.zone()))
             .flt("dist_m", p.distM, 2)
             .flt("rssi_f", p.rssiF)
             .done();
  }
  return s + "]";
}

static String cfgJson() {
  return Json()
      .flt("near_m", cfg.nearM, 2)
      .flt("far_m", cfg.farM, 2)
      .flt("path_n", cfg.pathN, 2)
      .flt("ref_conn", cfg.refConn)
      .flt("ref_adv", cfg.refAdv)
      .flt("hyst_db", cfg.hystDb)
      .flt("near_in_s", cfg.nearInS)
      .flt("near_out_s", cfg.nearOutS)
      .flt("zone_s", cfg.zoneS)
      .flt("smooth_s", cfg.smoothS)
      .flt("orient_db", cfg.orientDb)
      .num("away_s", cfg.awayS)
      .num("grace_s", cfg.graceS)
      .num("focus_min", cfg.focusMin)
      .num("stream_ms", cfg.streamMs)
      .num("buzzer_pin", cfg.buzzerPin)
      .str("mode", cfg.mode == MODE_HID ? "hid" : "scan")
      .done();
}

// ---------------------------------------------------------------------------
// Handling Bluetooth events in the main loop
// ---------------------------------------------------------------------------

static void addPending(uint16_t conn, uint32_t now) {
  for (auto &p : pending)
    if (!p.used) {
      p = {conn, now, true};
      return;
    }
}
static void dropPending(uint16_t conn) {
  for (auto &p : pending)
    if (p.used && p.conn == conn) p.used = false;
}

static void handleEvent(const Ev &e, uint32_t now) {
  switch (e.type) {
    case EV_ADV:
      // Stamped with the loop's clock, not e.ms: a sample must never be "newer" than `now`.
      if (e.phone >= 0 && e.phone < MAX_PHONES && phones[e.phone].used) addSample(e.phone, SRC_ADV, e.rssi, now);
      break;

    case EV_CONNECT:
      if (cfg.mode == MODE_SCAN && !pairing) {  // scan mode only accepts connections for pairing
        disconnect(e.conn);
        break;
      }
      addPending(e.conn, now);
      // Ask the phone to encrypt now: a bonded phone does so with its saved keys (and we learn who
      // it is); a new phone gets the pairing prompt.
      NimBLEDevice::startSecurity(e.conn);
      break;

    case EV_AUTH: {
      dropPending(e.conn);
      if (!e.encrypted) {
        logMsg("Pairing failed or was cancelled on the phone.");
        if (pairing) emit(Json("pair").str("state", "failed").done());
        disconnect(e.conn);
        break;
      }
      int i = findPhone(e.addr);
      if (i >= 0) {  // a known phone (re)connected
        phones[i].conn = e.conn;
        emit(Json("conn").num("id", i).str("addr", addrStr(e.addr)).str("state", "connected").done());
        if (cfg.mode == MODE_SCAN && !pairing) disconnect(e.conn);
        else if (cfg.mode == MODE_HID)  // 30-45 ms interval, no latency, 4 s timeout (Apple-compatible)
          server->updateConnParams(e.conn, 24, 36, 0, 400);
        if (pairing) pairClose("already paired");
        break;
      }
      if (!pairing || phoneCount() >= MAX_PHONES) {  // a stranger: drop the bond it just created
        logMsg(String("Rejected pairing from ") + addrStr(e.addr) + (pairing ? " (no free slot)" : " (pairing is off)"));
        NimBLEDevice::deleteBond(NimBLEAddress(e.addr));
        disconnect(e.conn);
        break;
      }
      for (i = 0; i < MAX_PHONES && phones[i].used; i++) {
      }
      xSemaphoreTake(phonesLock, portMAX_DELAY);
      phones[i].reset();
      phones[i].used = true;
      phones[i].id = e.addr;
      phones[i].conn = e.conn;
      clearAddrCache();
      xSemaphoreGive(phonesLock);
      savePhones();
      pairing = false;  // one phone per pairing window; "closed" is reported by finishPairing()
      advState = -2;
      irkWaitPhone = i;  // the IRK can land in the store a moment after this event
      irkWaitConn = e.conn;
      irkWaitSinceMs = now;
      if (cfg.mode == MODE_HID) server->updateConnParams(e.conn, 24, 36, 0, 400);
      break;
    }

    case EV_DISCONNECT: {
      dropPending(e.conn);
      int i = findPhoneByConn(e.conn);
      if (i < 0) break;
      Phone &p = phones[i];
      p.conn = BLE_HS_CONN_HANDLE_NONE;
      fastAdvUntilMs = now + FAST_ADV_MS;
      prox::Zone prev = p.zone.zone();
      p.rssiF = p.distM = NAN;
      p.smooth.reset();
      if (p.zone.capAtFar()) emitZone(i, prev);  // the link dropped: probably not on the desk any more
      emit(Json("conn").num("id", i).str("addr", addrStr(p.id)).str("state", "disconnected").num("reason", e.reason).done());
      if (irkWaitPhone == i) irkWaitConn = BLE_HS_CONN_HANDLE_NONE;
      break;
    }
  }
}

static void housekeeping(uint32_t now) {
  if (pairing && (int32_t)(now - pairUntilMs) >= 0) pairClose("timeout");

  // Waiting for a new phone's IRK
  if (irkWaitPhone >= 0) {
    uint8_t irk[16];
    Phone &p = phones[irkWaitPhone];
    if (readIrk(p.id, irk)) {
      xSemaphoreTake(phonesLock, portMAX_DELAY);
      setPhoneIrk(p, irk);
      clearAddrCache();
      xSemaphoreGive(phonesLock);
      int i = irkWaitPhone;
      irkWaitPhone = -1;
      finishPairing(i, true);
    } else if (now - irkWaitSinceMs >= IRK_WAIT_MS) {
      int i = irkWaitPhone;
      irkWaitPhone = -1;
      finishPairing(i, false);
    }
  }

  // Connections that never encrypted with a known phone
  for (auto &p : pending)
    if (p.used && now - p.sinceMs >= (pairing ? 60000u : UNAUTH_TIMEOUT_MS)) {
      p.used = false;
      disconnect(p.conn);
    }

  // Keep the scanner running
  NimBLEScan *scan = NimBLEDevice::getScan();
  if (!scan->isScanning()) scan->start(0, false, true);

  updateAdvertising(now);

  if (calib.active && (int32_t)(now - calib.untilMs) >= 0) calibrationFinish();
  autocalTick(now);
}

// ---------------------------------------------------------------------------
// Serial commands
// ---------------------------------------------------------------------------

static const uint8_t MAX_TOKENS = 6;
static String tok[MAX_TOKENS];
static uint8_t ntok = 0;
static Stream *replyTo = &Serial;
static long cmdId = -1;
static String cmdName;

static Json okReply() {
  Json j("ok");
  j.str("cmd", cmdName);
  if (cmdId >= 0) j.num("id", cmdId);
  return j;
}
static void sendReply(const Json &j) { replyTo->println(j.done()); }
static void err(const String &msg) {
  Json j("err");
  j.str("cmd", cmdName);
  if (cmdId >= 0) j.num("id", cmdId);
  sendReply(j.str("msg", msg));
}

static bool parseLong(const String &s, long &out) {
  if (!s.length()) return false;
  char *end;
  out = strtol(s.c_str(), &end, 10);
  return *end == '\0';
}
static bool parseFloat(const String &s, float &out) {
  if (!s.length()) return false;
  char *end;
  out = strtof(s.c_str(), &end);
  return *end == '\0' && !isnan(out) && !isinf(out);
}

// A GPIO the buzzer may use: a real output pin, not flash/USB/serial/LED/button.
static const char *buzzerPinProblem(long pin) {
  if (pin == -1) return nullptr;
  if (pin < 0 || pin >= SOC_GPIO_PIN_COUNT || !GPIO_IS_VALID_OUTPUT_GPIO((gpio_num_t)pin)) return "not an output pin on this chip";
  if (pin == LED_PIN || pin == BUTTON_PIN) return "used by the LED or button";
#if CONFIG_IDF_TARGET_ESP32
  if (pin >= 6 && pin <= 11) return "wired to the flash chip";
  if (pin == 1 || pin == 3) return "the USB serial line";
#elif CONFIG_IDF_TARGET_ESP32S3
  if (pin >= 26 && pin <= 32) return "wired to the flash chip";
  if (pin == 19 || pin == 20 || pin == 43 || pin == 44) return "a USB or serial line";
#elif CONFIG_IDF_TARGET_ESP32C3
  if (pin >= 12 && pin <= 17) return "wired to the flash chip";
  if (pin == 18 || pin == 19 || pin == 20 || pin == 21) return "a USB or serial line";
#endif
  return nullptr;
}

static void cmdInfo(uint32_t now) {
  Json j = okReply();
  j.str("fw", FW_NAME).str("ver", FW_VERSION).str("chip", ESP.getChipModel());
  j.str("mac", String(NimBLEDevice::getAddress().toString().c_str()));
  j.num("uptime_s", now / 1000).bol("ble", bleOk).bol("rpa_selftest", rpa::selfTest());
  j.bol("pairing", pairing).num("max_phones", MAX_PHONES);
  j.raw("phones", phonesJson()).raw("cfg", cfgJson()).raw("focus", focusJson(now));
  sendReply(j);
}

static void cmdPair() {
  if (ntok > 1 && tok[1] == "stop") {
    pairClose("stopped");
    sendReply(okReply().bol("pairing", false));
    return;
  }
  long s = 120;
  if (ntok > 1 && (!parseLong(tok[1], s) || s < 10 || s > 600)) return err("usage: pair [10-600 seconds] | pair stop");
  if (!bleOk) return err("Bluetooth failed to start");
  if (phoneCount() >= MAX_PHONES) return err("all phone slots are used; forget a phone first");
  pairOpen(s);
  sendReply(okReply().bol("pairing", true).num("left_s", s));
}

static void forgetPhone(int i) {
  Phone &p = phones[i];
  if (p.conn != BLE_HS_CONN_HANDLE_NONE) disconnect(p.conn);
  NimBLEDevice::deleteBond(NimBLEAddress(p.id));
  xSemaphoreTake(phonesLock, portMAX_DELAY);
  p.reset();
  clearAddrCache();
  xSemaphoreGive(phonesLock);
  if (irkWaitPhone == i) irkWaitPhone = -1;
  if (calib.active && cap.phone == i) calib.active = cap.on = false;
  if (ac.state != AC_IDLE && ac.phone == i) autocalStop();
}

static void cmdForget() {
  if (ntok < 2) return err("usage: forget <id> | forget all");
  int n = 0;
  if (tok[1] == "all") {
    for (int i = 0; i < MAX_PHONES; i++)
      if (phones[i].used) {
        forgetPhone(i);
        n++;
      }
    NimBLEDevice::deleteAllBonds();
  } else {
    long id;
    if (!parseLong(tok[1], id) || id < 0 || id >= MAX_PHONES || !phones[id].used) return err("no phone with that id");
    forgetPhone(id);
    n = 1;
  }
  savePhones();
  sendReply(okReply().num("forgot", n));
}

static void cmdFocus(uint32_t now) {
  String a = ntok > 1 ? tok[1] : "";
  if (a == "start") {
    long m = cfg.focusMin;
    if (ntok > 2 && (!parseLong(tok[2], m) || m < 0 || m > 1440)) return err("usage: focus start [0-1440 minutes]");
    focusStart(m, now);
  } else if (a == "stop") {
    focusStop(now);
  } else if (a != "") {
    return err("usage: focus | focus start [minutes] | focus stop");
  }
  sendReply(okReply().raw("focus", focusJson(now)));
}

static void cmdCfg() {
  if (ntok == 1) return sendReply(okReply().raw("cfg", cfgJson()));
  if (tok[1] == "reset") {
    uint8_t mode = cfg.mode;
    cfg = Config();
    cfg.mode = mode;  // changing the mode needs a restart; keep it
    saveConfig();
    return sendReply(okReply().raw("cfg", cfgJson()));
  }
  if (ntok < 3) return err("usage: cfg | cfg <key> <value> | cfg reset");
  const String &k = tok[1], &v = tok[2];
  float f;
  long n;
  bool restart = false;
  if (k == "near_m" && parseFloat(v, f) && f >= 0.1f && f < cfg.farM) cfg.nearM = f;
  else if (k == "far_m" && parseFloat(v, f) && f > cfg.nearM && f <= 30) cfg.farM = f;
  else if (k == "path_n" && parseFloat(v, f) && f >= 1.5f && f <= 5) cfg.pathN = f;
  else if (k == "ref_conn" && parseFloat(v, f) && f >= -100 && f <= -20) cfg.refConn = f;
  else if (k == "ref_adv" && parseFloat(v, f) && f >= -100 && f <= -20) cfg.refAdv = f;
  else if (k == "hyst_db" && parseFloat(v, f) && f >= 0 && f <= 10) cfg.hystDb = f;
  else if (k == "near_in_s" && parseFloat(v, f) && f >= 0 && f <= 30) cfg.nearInS = f;
  else if (k == "near_out_s" && parseFloat(v, f) && f >= 0 && f <= 60) cfg.nearOutS = f;
  else if (k == "zone_s" && parseFloat(v, f) && f >= 0 && f <= 60) cfg.zoneS = f;
  else if (k == "smooth_s" && parseFloat(v, f) && f >= 0 && f <= 20) cfg.smoothS = f;
  else if (k == "orient_db" && parseFloat(v, f) && f >= 0 && f <= 20) cfg.orientDb = f;
  else if (k == "away_s" && parseLong(v, n) && n >= 5 && n <= 600) cfg.awayS = n;
  else if (k == "grace_s" && parseLong(v, n) && n >= 0 && n <= 600) cfg.graceS = n;
  else if (k == "focus_min" && parseLong(v, n) && n >= 1 && n <= 1440) cfg.focusMin = n;
  else if (k == "stream_ms" && parseLong(v, n) && n >= 100 && n <= 10000) cfg.streamMs = n;
  else if (k == "buzzer_pin" && parseLong(v, n)) {
    if (const char *why = buzzerPinProblem(n)) return err(String("GPIO") + n + " can't be used: " + why);
    if (cfg.buzzerPin >= 0) noTone(cfg.buzzerPin);
    cfg.buzzerPin = n;
  } else if (k == "mode" && (v == "hid" || v == "scan")) {
    uint8_t m = v == "hid" ? MODE_HID : MODE_SCAN;
    restart = m != cfg.mode;
    cfg.mode = m;
  } else {
    return err("unknown key or value out of range (see PROTOCOL.md)");
  }
  saveConfig();
  sendReply(okReply().raw("cfg", cfgJson()).bol("restarting", restart));
  if (restart) {
    replyTo->flush();
    delay(200);
    ESP.restart();
  }
}

static void cmdCalibrate(uint32_t now) {
  long s = 8;
  float m = 1.0f;
  if (ntok > 1 && (!parseLong(tok[1], s) || s < 3 || s > 60)) return err("usage: calibrate [3-60 seconds] [metres]");
  if (ntok > 2 && (!parseFloat(tok[2], m) || m < 0.1f || m > 5)) return err("usage: calibrate [3-60 seconds] [0.1-5 metres]");
  if (calib.active || ac.state != AC_IDLE) return err("a calibration is already running");
  int best = strongestPhone();  // calibrate against the phone with the strongest signal right now
  if (best < 0) return err("no phone signal right now; pair a phone and bring it close");
  calib.active = true;
  calib.untilMs = now + s * 1000;
  calib.metres = m;
  captureStart(best);
  // "phone", not "id": a reply's "id" is the request id that the PC matches on.
  sendReply(okReply().bol("calibrating", true).num("phone", best).num("seconds", s).flt("metres", m, 2));
}

static void cmdAutocal(uint32_t now) {
  if (ntok > 1 && tok[1] == "stop") {
    autocalStop();
    return sendReply(okReply().bol("autocal", false));
  }
  float m1 = 0.5f, m2 = 2.0f;
  long turns = 3;
  if ((ntok > 1 && !parseFloat(tok[1], m1)) || (ntok > 2 && !parseFloat(tok[2], m2)) ||
      (ntok > 3 && !parseLong(tok[3], turns)) || m1 < 0.2f || m1 > 3 || m2 > 10 || m2 < 1.5f * m1 || turns < 0 ||
      turns > 3)
    return err("usage: autocal [near metres 0.2-3] [far metres, at least 1.5x the near one, max 10] "
               "[orientations 0-3] | autocal stop");
  if (calib.active || ac.state != AC_IDLE) return err("a calibration is already running");
  int best = strongestPhone();
  if (best < 0) return err("no phone signal right now; pair a phone and bring it close");
  ac.phone = (int8_t)best;
  ac.step = ac.retries = ac.nOrient = 0;
  ac.turns = (uint8_t)turns;
  ac.m[0] = m1;
  ac.m[1] = m2;
  ac.conn[0] = ac.conn[1] = ac.adv[0] = ac.adv[1] = NAN;
  ac.state = AC_PLACE;
  ac.sinceMs = now;
  sendReply(okReply().bol("autocal", true).num("phone", best).flt("near_m", m1, 2).flt("far_m", m2, 2).num("turns", turns));
  autocalEmitPlace(false);
}

static void cmdHelp() {
  sendReply(okReply().raw("cmds",
                          "[\"info\",\"phones\",\"pair [seconds]\",\"pair stop\",\"forget <id>|all\","
                          "\"focus\",\"focus start [minutes]\",\"focus stop\",\"cfg\",\"cfg <key> <value>\","
                          "\"cfg reset\",\"calibrate [seconds] [metres]\",\"autocal [near_m] [far_m] [turns]\",\"autocal stop\","
                          "\"stream on|off\",\"restart\"]"));
}

static void runLine(String line, uint32_t now) {
  line.trim();
  if (!line.length()) return;
  cmdId = -1;
  if (line[0] == '#') {
    int sp = line.indexOf(' ');
    long id;
    if (sp > 1 && parseLong(line.substring(1, sp), id)) cmdId = id;
    line = sp > 0 ? line.substring(sp + 1) : "";
    line.trim();
  }
  ntok = 0;
  int i = 0;
  while (i < (int)line.length() && ntok < MAX_TOKENS) {
    while (i < (int)line.length() && line[i] == ' ') i++;
    int start = i;
    while (i < (int)line.length() && line[i] != ' ') i++;
    if (i > start) {
      tok[ntok] = line.substring(start, i);
      tok[ntok].toLowerCase();
      ntok++;
    }
  }
  if (!ntok) return;
  cmdName = tok[0];
  const String &c = tok[0];
  if (c == "help") cmdHelp();
  else if (c == "info") cmdInfo(now);
  else if (c == "phones") sendReply(okReply().raw("phones", phonesJson()));
  else if (c == "pair") cmdPair();
  else if (c == "forget") cmdForget();
  else if (c == "focus") cmdFocus(now);
  else if (c == "cfg") cmdCfg();
  else if (c == "calibrate") cmdCalibrate(now);
  else if (c == "autocal") cmdAutocal(now);
  else if (c == "stream" && ntok > 1 && (tok[1] == "on" || tok[1] == "off")) {
    streamOn = tok[1] == "on";
    sendReply(okReply().bol("stream", streamOn));
  } else if (c == "restart") {
    sendReply(okReply());
    replyTo->flush();
    delay(100);
    ESP.restart();
  } else err("unknown command (type help)");
}

struct LineReader {
  Stream *port;
  String buf;
  void poll(uint32_t now) {
    while (port->available()) {
      char ch = (char)port->read();
      if (ch == '\n' || ch == '\r') {
        if (buf.length()) {
          replyTo = port;
          runLine(buf, now);
          buf = "";
        }
      } else if (ch == 8 || ch == 127) {
        if (buf.length()) buf.remove(buf.length() - 1);
      } else if (buf.length() < 120 && ch >= 32) {
        buf += ch;
      }
    }
  }
};
static LineReader readers[NPORTS];

// ---------------------------------------------------------------------------

void setup() {
  // A TX buffer so printing status lines never stalls the loop at 115200 baud.
#if ARDUINO_USB_CDC_ON_BOOT
  Serial0.setTxBufferSize(2048);
#else
  Serial.setTxBufferSize(2048);
#endif
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  Serial0.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) delay(10);
#endif
  for (uint8_t i = 0; i < NPORTS; i++) readers[i].port = PORTS[i];

  pinMode(LED_PIN, OUTPUT);
  ledWrite(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  prefs.begin("focus", false);
  loadConfig();
  if (buzzerPinProblem(cfg.buzzerPin)) cfg.buzzerPin = -1;

  phonesLock = xSemaphoreCreateMutex();
  evq = xQueueCreate(64, sizeof(Ev));

  bleOk = NimBLEDevice::init(BLE_NAME);
  if (bleOk) {
    NimBLEDevice::setPower(9);
    NimBLEDevice::setSecurityAuth(true, false, true);  // bond, no PIN ("Just Works"), secure connections
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    NimBLEDevice::setSecurityInitKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
    NimBLEDevice::setSecurityRespKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
    buildGatt();
    loadPhones();

    NimBLEScan *scan = NimBLEDevice::getScan();
    scan->setScanCallbacks(new ScanCallbacks(), true);  // every advertisement, duplicates included
    scan->setActiveScan(false);
    scan->setInterval(100);  // ms
    scan->setWindow(60);     // leave air time for the phone connection
    scan->setMaxResults(0);  // don't store results, the callback handles them
    scan->start(0, false, true);
    fastAdvUntilMs = millis() + FAST_ADV_MS;
  }

  String mac = bleOk ? String(NimBLEDevice::getAddress().toString().c_str()) : String("");
  for (uint8_t i = 0; i < NPORTS; i++) PORTS[i]->println();
  emit(Json("hello").str("fw", FW_NAME).str("ver", FW_VERSION).str("chip", ESP.getChipModel()).str("mac", mac).done());
  if (!bleOk) logMsg("Bluetooth failed to start");
  if (!rpa::selfTest()) logMsg("RPA self-test FAILED: phones won't be recognised from their broadcasts");
  logMsg(String("mode ") + (cfg.mode == MODE_HID ? "hid" : "scan") + ", " + phoneCount() + " phone(s) paired");
}

void loop() {
  uint32_t now = millis();
  for (auto &r : readers) r.poll(now);

  Ev e;
  while (xQueueReceive(evq, &e, 0) == pdTRUE) handleEvent(e, now);

  static uint32_t lastSample = 0, lastStream = 0, lastHouse = 0;
  if (bleOk && now - lastHouse >= 250) {
    lastHouse = now;
    housekeeping(now);
  }
  if (now - lastSample >= SAMPLE_MS) {
    lastSample = now;
    if (bleOk) evaluate(now);
  }
  if (streamOn && now - lastStream >= cfg.streamMs) {
    lastStream = now;
    streamPhones(now);
  }

  prox::FocusSession::Event fe = focus.tick(now, nearestZone(), (uint32_t)cfg.graceS * 1000);
  if (fe == prox::FocusSession::DISTRACTED) {
    emitFocus("distracted", now);
    beep(3, 2600, 120, 90);
    lastNagMs = now;
  } else if (fe == prox::FocusSession::RECOVERED) {
    emitFocus("recovered", now);
  } else if (fe == prox::FocusSession::DONE) {
    emitFocus("done", now);
    beep(2, 1500, 400, 200);
  }
  if (focus.active() && focus.distracted() && now - lastNagMs >= 15000) {  // keep nagging gently
    beep(1, 2600, 120, 0);
    lastNagMs = now;
  }

  buttonTick(now);
  beeperTick(now);
  ledTick(now);
  delay(5);
}
