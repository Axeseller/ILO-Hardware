// proximity.h: turns noisy Bluetooth signal strength (RSSI) into a distance, a zone and focus stats.
//
// Pure logic with no Arduino/Bluetooth dependencies, so tests/ can compile and check it on a PC.
// Times are milliseconds from a monotonic clock (millis() on the device); wrap-around is handled
// by unsigned subtraction.
//
// Approach (after ESPresense, a mature open-source BLE presence project):
//   - keep the last few seconds of RSSI samples, drop outliers with a Tukey fence (IQR) and average
//     the rest as power (milliwatts), not as dB. Fading only ever pulls single readings down, so a
//     dB average reads low by an amount that depends on the room; a power average doesn't.
//   - smooth that level with a filter that is slow while the phone lies still and fast when it moves.
//   - zones are decided in dB below the 1 m reference, with a 6 dB gap between the "enter" and
//     "leave" thresholds and a dwell time, so they don't flicker.
//   - calibration measures the phone at two known distances and fits both model constants. It
//     can also measure the phone in several orientations: a phone's antenna is stronger in some
//     directions than others, and one receiver can't tell "turned" from "further away". The
//     reference is then centred on the average orientation and the zone hysteresis is widened to
//     half the spread, so turning the phone in place can't flip its zone.
#pragma once
#include <math.h>
#include <stdint.h>
#include <string.h>

namespace prox {

enum Zone : uint8_t { NEAR = 0, ROOM = 1, FAR = 2, AWAY = 3 };

inline const char *zoneName(Zone z) {
  switch (z) {
    case NEAR: return "near";
    case ROOM: return "room";
    case FAR: return "far";
    default: return "away";
  }
}

// Log-distance path-loss model: rssi = ref - 10 n log10(d).
// lossDb = ref - rssi = how many dB weaker than at 1 m.
inline float lossForDistance(float metres, float pathN) {
  if (metres < 0.05f) metres = 0.05f;
  return 10.0f * pathN * log10f(metres);
}
inline float distanceForLoss(float lossDb, float pathN) {
  if (pathN < 1.0f) pathN = 1.0f;
  float d = powf(10.0f, lossDb / (10.0f * pathN));
  if (d < 0.05f) d = 0.05f;
  if (d > 50.0f) d = 50.0f;
  return d;
}
inline float distanceFromRssi(float rssi, float refAt1m, float pathN) {
  return distanceForLoss(refAt1m - rssi, pathN);
}

// 10^(-d/10) for a whole number of dB d >= 0 (0 once it is 50 dB down: too weak to matter).
inline float powerRatio(int dBelow) {
  static const float tenth[10] = {1.0f,      0.794328f, 0.630957f, 0.501187f, 0.398107f,
                                  0.316228f, 0.251189f, 0.199526f, 0.158489f, 0.125893f};
  static const float decade[5] = {1.0f, 0.1f, 0.01f, 0.001f, 0.0001f};
  if (dBelow < 0) dBelow = 0;
  if (dBelow >= 50) return 0.0f;
  return tenth[dBelow % 10] * decade[dBelow / 10];
}

inline float quantile(const int8_t *sorted, uint16_t c, float q) {
  float pos = q * (c - 1);
  uint16_t i = (uint16_t)pos;
  float frac = pos - i;
  return i + 1 < c ? sorted[i] + frac * (sorted[i + 1] - sorted[i]) : sorted[i];
}

// The signal level (dBm) of a batch of RSSI samples. Sorts them in place.
// Samples outside the Tukey fence [Q1 - 1.5 IQR, Q3 + 1.5 IQR] are dropped (single blocked or
// reflected packets); the rest are averaged as power and converted back to dB.
// The live filter and calibration both use this, so they agree on what "the level" is.
inline float robustLevel(int8_t *s, uint16_t c) {
  if (!c) return NAN;
  for (uint16_t i = 1; i < c; i++)  // insertion sort, c is small
    for (uint16_t j = i; j > 0 && s[j - 1] > s[j]; j--) {
      int8_t t = s[j];
      s[j] = s[j - 1];
      s[j - 1] = t;
    }
  float q1 = quantile(s, c, 0.25f), q3 = quantile(s, c, 0.75f), iqr = q3 - q1;
  float lo = q1 - 1.5f * iqr, hi = q3 + 1.5f * iqr;
  int top = 0;
  uint16_t kept = 0;
  for (uint16_t i = 0; i < c; i++)
    if (s[i] >= lo && s[i] <= hi) {
      top = s[i];  // sorted: the last one kept is the strongest
      kept++;
    }
  if (!kept) return quantile(s, c, 0.5f);
  float sum = 0;  // power relative to the strongest kept sample
  for (uint16_t i = 0; i < c; i++)
    if (s[i] >= lo && s[i] <= hi) sum += powerRatio(top - s[i]);
  return top + 10.0f * log10f(sum / kept);
}

// A time window of RSSI samples. value() = robustLevel() of the recent ones.
class RssiWindow {
 public:
  static const uint8_t kCap = 64;

  void reset() { n_ = head_ = 0; lastMs_ = 0; }
  RssiWindow() { reset(); }

  // Ignores readings that aren't real (127 = "not available", positive, or absurdly weak).
  void add(int rssi, uint32_t nowMs) {
    if (rssi >= 0 || rssi < -110) return;
    rssi_[head_] = (int8_t)rssi;
    ms_[head_] = nowMs;
    head_ = (head_ + 1) % kCap;
    if (n_ < kCap) n_++;
    lastMs_ = nowMs;
  }

  // Number of samples no older than windowMs.
  uint8_t count(uint32_t nowMs, uint32_t windowMs) const {
    uint8_t c = 0;
    for (uint8_t i = 0; i < n_; i++)
      if (nowMs - ms_[i] <= windowMs) c++;
    return c;
  }

  // Level of the samples whose age is between minAgeMs and maxAgeMs.
  // Returns false if fewer than minSamples qualify.
  bool level(uint32_t nowMs, uint32_t minAgeMs, uint32_t maxAgeMs, uint8_t minSamples, float &out) const {
    int8_t s[kCap];
    uint8_t c = 0;
    for (uint8_t i = 0; i < n_; i++) {
      uint32_t age = nowMs - ms_[i];
      if (age >= minAgeMs && age <= maxAgeMs) s[c++] = rssi_[i];
    }
    if (c == 0 || c < minSamples) return false;
    out = robustLevel(s, c);
    return true;
  }

  // Level of the samples no older than windowMs.
  bool value(uint32_t nowMs, uint32_t windowMs, uint8_t minSamples, float &out) const {
    return level(nowMs, 0, windowMs, minSamples, out);
  }

  // True when the last halfMs and the halfMs before it have the same level (within tolDb):
  // the phone is lying still. Each half needs minSamples.
  bool steady(uint32_t nowMs, uint32_t halfMs, uint8_t minSamples, float tolDb) const {
    float a, b;
    return level(nowMs, 0, halfMs, minSamples, a) && level(nowMs, halfMs + 1, 2 * halfMs, minSamples, b) &&
           fabsf(a - b) <= tolDb;
  }

  uint32_t lastMs() const { return lastMs_; }
  bool empty() const { return n_ == 0; }

 private:
  int8_t rssi_[kCap];
  uint32_t ms_[kCap];
  uint8_t n_, head_;
  uint32_t lastMs_;
};

// Smooths a level (dB) over time: slowly while it only jitters, quickly when it really moves.
// Within kQuietDb of the current estimate a reading is treated as noise and averaged with time
// constant tauS; the further off it is, the more it is trusted, and at kMoveDb it is taken as is.
// That steadies the distance of a phone lying still without making a pickup slow to show.
class LevelSmoother {
 public:
  static constexpr float kQuietDb = 1.5f, kMoveDb = 5.0f;
  static const uint32_t kGapMs = 3000;  // no readings for this long: start again from the next one

  void reset() { has_ = false; }
  bool has() const { return has_; }

  float update(float z, uint32_t nowMs, float tauS) {
    if (!has_ || tauS <= 0 || nowMs - lastMs_ > kGapMs) {
      has_ = true;
      lastMs_ = nowMs;
      return x_ = z;
    }
    float dt = (nowMs - lastMs_) / 1000.0f;
    lastMs_ = nowMs;
    float err = z - x_;
    float slow = 1.0f - expf(-dt / tauS);
    float k = (fabsf(err) - kQuietDb) / (kMoveDb - kQuietDb);
    if (k < 0) k = 0;
    if (k > 1) k = 1;
    x_ += (slow + (1.0f - slow) * k) * err;
    return x_;
  }

 private:
  bool has_ = false;
  float x_ = 0;
  uint32_t lastMs_ = 0;
};

// Two-point calibration: the model rssi = ref - 10 n log10(d) through a level measured at each of
// two distances. n is kept inside [nMin, nMax] (clamped = true if it had to be), and ref is then
// the best fit for that n. Returns false if the distances are too close together to tell.
struct PathFit {
  float ref, n;
  bool clamped;
};
inline bool fitPathLoss(float d1, float r1, float d2, float r2, float nMin, float nMax, PathFit &out) {
  if (d1 <= 0 || d2 <= 0) return false;
  float span = log10f(d2 / d1);  // decades of distance between the two points
  if (fabsf(span) < 0.17f) return false;  // less than a factor of 1.5 apart
  float n = (r1 - r2) / (10.0f * span);
  out.clamped = n < nMin || n > nMax;
  if (n < nMin) n = nMin;
  if (n > nMax) n = nMax;
  out.n = n;
  out.ref = 0.5f * ((r1 + 10.0f * n * log10f(d1)) + (r2 + 10.0f * n * log10f(d2)));
  return true;
}

// What turning the phone does to its signal, from the level measured in each of n orientations.
// levels[0] is the orientation the distance model was calibrated in.
//   spreadDb: strongest minus weakest orientation.
//   offsetDb: the average orientation relative to levels[0]; adding it to the 1 m reference centres
//             the model, so any orientation is off by at most about half the spread.
struct OrientFit {
  float spreadDb, offsetDb;
};
inline OrientFit fitOrientation(const float *levels, uint8_t n) {
  OrientFit f{0, 0};
  if (!n) return f;
  float lo = levels[0], hi = levels[0], sum = 0;
  for (uint8_t i = 0; i < n; i++) {
    if (levels[i] < lo) lo = levels[i];
    if (levels[i] > hi) hi = levels[i];
    sum += levels[i];
  }
  f.spreadDb = hi - lo;
  f.offsetDb = sum / n - levels[0];
  return f;
}

// The hysteresis to use around each zone boundary. A phone whose signal swings by orientDb when it
// is turned needs half of that on each side, or turning it in place could flip its zone.
inline float effectiveHyst(float hystDb, float orientDb) {
  float h = 0.5f * orientDb;
  if (h > 8.0f) h = 8.0f;
  return h > hystDb ? h : hystDb;
}

struct ZoneConfig {
  float nearM = 1.0f;            // closer than this = NEAR
  float farM = 4.0f;             // further than this = FAR
  float pathN = 2.7f;            // path-loss exponent
  float hystDb = 3.0f;           // enter a zone 3 dB inside its boundary, leave 3 dB outside (6 dB gap)
  uint32_t enterNearMs = 3000;   // must look NEAR continuously this long before it counts
  uint32_t leaveNearMs = 5000;   // must look not-NEAR continuously this long before NEAR ends
  uint32_t otherMs = 5000;       // ROOM <-> FAR
};

// Debounced zone from a filtered loss (dB below the 1 m reference).
//
// Two clocks:
//   - "away from the current zone since": runs while readings point anywhere other than the
//     current zone. It does NOT restart when they flip between other zones (e.g. ROOM/FAR noise
//     while leaving NEAR), which used to keep a phone stuck in NEAR.
//   - "NEAR since": entering NEAR needs NEAR readings continuously.
class ZoneTracker {
 public:
  void reset() {
    zone_ = target_ = AWAY;
    outSinceMs_ = nearSinceMs_ = 0;
    out_ = nearRun_ = false;
  }
  ZoneTracker() { reset(); }

  // Returns true when the zone changed.
  bool update(float lossDb, uint32_t nowMs, const ZoneConfig &c) {
    Zone target = targetZone(lossDb, c);
    target_ = target;
    if (zone_ == AWAY) {  // first reading after being away: take it as it is
      clearPending();
      return set(target);
    }
    if (target == zone_) {
      clearPending();
      return false;
    }
    if (!out_) {
      out_ = true;
      outSinceMs_ = nowMs;
    }
    if (target == NEAR) {
      if (!nearRun_) {
        nearRun_ = true;
        nearSinceMs_ = nowMs;
      }
    } else {
      nearRun_ = false;
    }
    if (nowMs - sinceFor(target) >= needFor(target, c)) {
      clearPending();
      return set(target);
    }
    return false;
  }

  // No signal any more.
  bool goAway() {
    clearPending();
    target_ = AWAY;
    return set(AWAY);
  }
  // Signal lost but probably still around: stop claiming NEAR (used right after a disconnect).
  bool capAtFar() {
    if (zone_ == NEAR || zone_ == ROOM) {
      clearPending();
      target_ = FAR;
      return set(FAR);
    }
    return false;
  }

  Zone zone() const { return zone_; }
  // The zone the readings point to while a change is waiting out its dwell time (else zone()).
  Zone pending() const { return out_ ? target_ : zone_; }
  // How long until pending() takes over if readings stay as they are (0 if nothing pending).
  uint32_t pendingLeftMs(uint32_t nowMs, const ZoneConfig &c) const {
    if (!out_) return 0;
    uint32_t waited = nowMs - sinceFor(target_), need = needFor(target_, c);
    return waited >= need ? 0 : need - waited;
  }

 private:
  bool set(Zone z) {
    if (z == zone_) return false;
    zone_ = z;
    return true;
  }
  void clearPending() { out_ = nearRun_ = false; }
  uint32_t sinceFor(Zone target) const { return target == NEAR ? nearSinceMs_ : outSinceMs_; }
  uint32_t needFor(Zone target, const ZoneConfig &c) const {
    return target == NEAR ? c.enterNearMs : (zone_ == NEAR ? c.leaveNearMs : c.otherMs);
  }
  Zone targetZone(float loss, const ZoneConfig &c) const {
    float nearT = lossForDistance(c.nearM, c.pathN), farT = lossForDistance(c.farM, c.pathN);
    float h = zone_ == AWAY ? 0 : c.hystDb;
    bool isNear = zone_ == NEAR ? loss < nearT + h : loss < nearT - h;
    if (isNear) return NEAR;
    bool isFar = zone_ == FAR ? loss > farT - h : loss > farT + h;
    return isFar ? FAR : ROOM;
  }

  Zone zone_, target_;
  uint32_t outSinceMs_, nearSinceMs_;
  bool out_, nearRun_;
};

// A focus session: counts time the phone spends NEAR and "pickups" (stays NEAR longer than grace).
class FocusSession {
 public:
  enum Event : uint8_t { NONE, DISTRACTED, RECOVERED, DONE };

  void start(uint32_t nowMs, uint32_t limitS) {
    active_ = true;
    startMs_ = lastTickMs_ = nowMs;
    limitS_ = limitS;
    nearMs_ = pickups_ = nearSinceMs_ = 0;
    inNear_ = distracted_ = false;
  }
  void stop(uint32_t nowMs) {
    if (active_) lastTickMs_ = nowMs;
    active_ = distracted_ = false;
  }

  // Call often (every loop is fine) with the nearest zone of any known phone.
  Event tick(uint32_t nowMs, Zone nearest, uint32_t graceMs) {
    if (!active_) return NONE;
    uint32_t dt = nowMs - lastTickMs_;
    lastTickMs_ = nowMs;
    Event ev = NONE;
    if (nearest == NEAR) {
      nearMs_ += dt;
      if (!inNear_) {
        inNear_ = true;
        nearSinceMs_ = nowMs;
      }
      if (!distracted_ && nowMs - nearSinceMs_ >= graceMs) {
        distracted_ = true;
        pickups_++;
        ev = DISTRACTED;
      }
    } else {
      inNear_ = false;
      if (distracted_) {
        distracted_ = false;
        ev = RECOVERED;
      }
    }
    if (limitS_ && elapsedS(nowMs) >= limitS_) {
      active_ = distracted_ = false;
      return DONE;
    }
    return ev;
  }

  bool active() const { return active_; }
  bool distracted() const { return distracted_; }
  uint32_t limitS() const { return limitS_; }
  uint32_t elapsedS(uint32_t nowMs) const { return ((active_ ? nowMs : lastTickMs_) - startMs_) / 1000; }
  int32_t leftS(uint32_t nowMs) const {
    if (!limitS_) return -1;
    uint32_t e = elapsedS(nowMs);
    return e >= limitS_ ? 0 : (int32_t)(limitS_ - e);
  }
  uint32_t nearS() const { return nearMs_ / 1000; }
  uint32_t pickups() const { return pickups_; }

 private:
  bool active_ = false, inNear_ = false, distracted_ = false;
  uint32_t startMs_ = 0, lastTickMs_ = 0, limitS_ = 0, nearMs_ = 0, pickups_ = 0, nearSinceMs_ = 0;
};

}  // namespace prox
