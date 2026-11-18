// Host unit tests for firmware/proximity.h. Run: tests\run_tests.cmd
#include <stdio.h>
#include "../firmware/proximity.h"

using namespace prox;
static int failures = 0;
#define CHECK(cond)                                                \
  do {                                                             \
    if (!(cond)) {                                                 \
      printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond);       \
      failures++;                                                  \
    }                                                              \
  } while (0)
#define NEAR_EQ(a, b, tol) CHECK(fabsf((a) - (b)) <= (tol))

static void testDistanceModel() {
  NEAR_EQ(distanceFromRssi(-59, -59, 2.7f), 1.0f, 0.001f);
  NEAR_EQ(distanceFromRssi(-86, -59, 2.7f), 10.0f, 0.01f);  // 27 dB = 10 * 2.7 * log10(10)
  NEAR_EQ(lossForDistance(2.0f, 2.7f), 8.13f, 0.01f);        // each doubling costs ~8.1 dB
  NEAR_EQ(distanceForLoss(lossForDistance(3.3f, 2.7f), 2.7f), 3.3f, 0.001f);
  CHECK(distanceFromRssi(-20, -59, 2.7f) >= 0.05f);   // clamped
  CHECK(distanceFromRssi(-110, -59, 2.0f) <= 50.0f);  // clamped
}

static void testWindowIgnoresOutliersAndJunk() {
  RssiWindow w;
  uint32_t t = 0;
  for (int i = 0; i < 14; i++) w.add(i % 2 ? -60 : -62, t += 200);
  w.add(-95, t += 200);  // one blocked packet
  w.add(127, t += 1);    // "not available"
  w.add(5, t += 1);      // nonsense
  float v;
  CHECK(w.value(t, 3000, 3, v));
  NEAR_EQ(v, -61.0f, 0.3f);  // the -95 is outside the Tukey fence
  CHECK(w.count(t, 3000) == 15);
}

static void testWindowNeedsEnoughRecentSamples() {
  RssiWindow w;
  float v;
  CHECK(!w.value(1000, 3000, 1, v));  // empty
  w.add(-70, 1000);
  w.add(-70, 1200);
  CHECK(!w.value(1300, 3000, 3, v));  // only 2
  w.add(-70, 1400);
  CHECK(w.value(1500, 3000, 3, v));
  CHECK(!w.value(1400 + 3001, 3000, 1, v));  // all expired
  // Old samples age out of the average
  for (int i = 0; i < 20; i++) w.add(-50, 10000 + i * 200);
  CHECK(w.value(14000, 3000, 3, v));
  NEAR_EQ(v, -50.0f, 0.01f);
}

static void testWindowWrapsCapacity() {
  RssiWindow w;
  for (int i = 0; i < 200; i++) w.add(-40 - (i % 7), i * 10);
  float v;
  CHECK(w.value(2000, 100000, 3, v));
  CHECK(w.count(2000, 100000) == RssiWindow::kCap);
  CHECK(v <= -40 && v >= -46);
}

// A small repeatable random source for the statistical tests.
static uint32_t rngState = 12345;
static float rnd() {  // uniform in (0, 1)
  rngState = rngState * 1664525u + 1013904223u;
  return ((rngState >> 8) + 0.5f) / 16777216.0f;
}
static float gauss() { return sqrtf(-2.0f * logf(rnd())) * cosf(6.2831853f * rnd()); }
// One RSSI reading of a signal whose true average level is `level`, with Rician fading
// (k = direct-path power / reflected power; 0 = worst case) and 1 dB rounding.
static int fadedRssi(float level, float k) {
  float los = sqrtf(k / (k + 1)), sc = sqrtf(0.5f / (k + 1));
  float re = los + sc * gauss(), im = sc * gauss();
  return (int)lroundf(level + 10.0f * log10f(re * re + im * im));
}

static void testLevelAveragesPower() {
  // Two packets, one 10 dB weaker: the average power is 0.55x the strong one = -2.6 dB.
  int8_t a[] = {-50, -60};
  NEAR_EQ(robustLevel(a, 2), -52.6f, 0.05f);
  int8_t b[] = {-70, -70, -70, -70};
  NEAR_EQ(robustLevel(b, 4), -70.0f, 0.001f);
  int8_t c[] = {-64};
  NEAR_EQ(robustLevel(c, 1), -64.0f, 0.001f);
  NEAR_EQ(powerRatio(0), 1.0f, 1e-6f);
  NEAR_EQ(powerRatio(3), 0.501f, 0.001f);
  NEAR_EQ(powerRatio(27), 0.001995f, 0.00001f);
  CHECK(powerRatio(60) == 0.0f);
}

// Fading must not shift the level: the same true signal reads the same whether the room gives
// a clean path (k = 8) or pure reflections (k = 0). A plain dB average is ~2 dB lower in the second.
static void testLevelUnbiasedUnderFading() {
  const float ks[] = {8.0f, 0.0f};
  for (float k : ks) {
    float sum = 0, sumDb = 0;
    const int runs = 400;
    for (int r = 0; r < runs; r++) {
      int8_t s[20];
      float db = 0;
      for (int i = 0; i < 20; i++) {
        s[i] = (int8_t)fadedRssi(-60, k);
        db += s[i];
      }
      sumDb += db / 20;
      sum += robustLevel(s, 20);
    }
    NEAR_EQ(sum / runs, -60.0f, 0.7f);
    if (k == 0.0f) CHECK(sumDb / runs < -61.8f);  // what the old dB average would have said
  }
}

static void testWindowLevelByAgeAndSteady() {
  RssiWindow w;
  uint32_t t = 0;
  for (int i = 0; i < 20; i++) w.add(-50, t += 100);  // 2 s at -50
  for (int i = 0; i < 20; i++) w.add(-66, t += 100);  // then 2 s at -66
  float v;
  CHECK(w.level(t, 0, 1900, 3, v));
  NEAR_EQ(v, -66.0f, 0.01f);
  CHECK(w.level(t, 2000, 3900, 3, v));
  NEAR_EQ(v, -50.0f, 0.01f);
  CHECK(!w.level(t, 5000, 9000, 1, v));          // nothing that old
  CHECK(!w.steady(t, 2000, 8, 2.0f));            // it just moved
  for (int i = 0; i < 20; i++) w.add(-66, t += 100);
  CHECK(w.steady(t, 2000, 8, 2.0f));             // 4 s at one level
  CHECK(!w.steady(t, 2000, 30, 2.0f));           // not enough samples per half
  RssiWindow e;
  CHECK(!e.steady(1000, 2000, 1, 2.0f));
}

static void testSmootherQuietAtRestQuickOnMoves() {
  // At rest: jitter of +-1 dB around -60 comes out much smaller.
  LevelSmoother s;
  uint32_t t = 0;
  float lo = 0, hi = -200;
  for (int i = 0; i < 600; i++) {
    float x = s.update(-60.0f + (i % 2 ? 1.0f : -1.0f), t += 100, 4.0f);
    if (i >= 100) {
      if (x < lo) lo = x;
      if (x > hi) hi = x;
    }
  }
  CHECK(hi - lo < 0.2f);
  NEAR_EQ(0.5f * (hi + lo), -60.0f, 0.1f);
  // A real 20 dB move is followed at once...
  float x = s.update(-40.0f, t += 100, 4.0f);
  NEAR_EQ(x, -40.0f, 0.01f);
  // ...and a ramp of 5 dB/s is tracked within 3 dB.
  float z = -40.0f;
  for (int i = 0; i < 40; i++) {
    z -= 0.5f;
    x = s.update(z, t += 100, 4.0f);
    CHECK(fabsf(x - z) < 3.0f);
  }
  // A small step still gets there, just slowly (time constant 4 s).
  LevelSmoother q;
  t = 0;
  q.update(-60.0f, t, 4.0f);
  for (int i = 0; i < 40; i++) x = q.update(-59.0f, t += 100, 4.0f);  // 4 s
  NEAR_EQ(x, -59.37f, 0.05f);  // 1 - e^-1 of the way
  // tau 0 = no smoothing; a gap in the readings starts again.
  LevelSmoother off;
  off.update(-60.0f, 0, 0.0f);
  NEAR_EQ(off.update(-59.0f, 100, 0.0f), -59.0f, 0.001f);
  NEAR_EQ(q.update(-59.9f, t += LevelSmoother::kGapMs + 1, 4.0f), -59.9f, 0.001f);
  q.reset();
  CHECK(!q.has());
}

static void testTwoPointFit() {
  PathFit f;
  // Exact data from ref -58, n 3.1 comes back.
  float r1 = -58.0f - lossForDistance(0.5f, 3.1f), r2 = -58.0f - lossForDistance(2.0f, 3.1f);
  CHECK(fitPathLoss(0.5f, r1, 2.0f, r2, 1.5f, 5.0f, f));
  NEAR_EQ(f.n, 3.1f, 0.001f);
  NEAR_EQ(f.ref, -58.0f, 0.001f);
  CHECK(!f.clamped);
  CHECK(fitPathLoss(2.0f, r2, 0.5f, r1, 1.5f, 5.0f, f));  // either order
  NEAR_EQ(f.n, 3.1f, 0.001f);
  // The signal barely dropped: n is held at the minimum and ref splits the difference.
  CHECK(fitPathLoss(0.5f, -50.0f, 2.0f, -53.0f, 1.5f, 5.0f, f));
  CHECK(f.clamped);
  NEAR_EQ(f.n, 1.5f, 0.001f);
  NEAR_EQ(f.ref, -51.5f, 0.01f);  // the points sit symmetrically around 1 m
  CHECK(!fitPathLoss(1.0f, -60.0f, 1.2f, -62.0f, 1.5f, 5.0f, f));  // too close together
  CHECK(!fitPathLoss(0.0f, -60.0f, 2.0f, -62.0f, 1.5f, 5.0f, f));
}

static void testOrientation() {
  // Four orientations: as calibrated, then 6, 2 and 9 dB weaker.
  float levels[] = {-66.0f, -72.0f, -68.0f, -75.0f};
  OrientFit f = fitOrientation(levels, 4);
  NEAR_EQ(f.spreadDb, 9.0f, 0.001f);
  NEAR_EQ(f.offsetDb, -4.25f, 0.001f);  // the average orientation is 4.25 dB below the first
  f = fitOrientation(levels, 1);
  NEAR_EQ(f.spreadDb, 0.0f, 0.001f);
  NEAR_EQ(f.offsetDb, 0.0f, 0.001f);
  NEAR_EQ(effectiveHyst(3.0f, 0.0f), 3.0f, 0.001f);   // nothing measured: as configured
  NEAR_EQ(effectiveHyst(3.0f, 4.0f), 3.0f, 0.001f);   // small spread: already covered
  NEAR_EQ(effectiveHyst(3.0f, 9.0f), 4.5f, 0.001f);   // half the spread
  NEAR_EQ(effectiveHyst(3.0f, 30.0f), 8.0f, 0.001f);  // capped

  // A phone at 0.95 m whose signal swings +-4.5 dB as it is turned: with the plain 3 dB hysteresis
  // it drops out of NEAR; with the hysteresis the spread calls for, it stays.
  for (int widened = 0; widened < 2; widened++) {
    ZoneConfig c;
    c.hystDb = widened ? effectiveHyst(3.0f, 9.0f) : 3.0f;
    ZoneTracker z;
    uint32_t t = 0;
    float centre = lossForDistance(0.95f, 2.7f);  // -0.6 dB: just inside the 1 m boundary
    z.update(centre - 4.5f, t, c);
    CHECK(z.zone() == NEAR);
    bool left = false;
    for (int i = 0; i < 40; i++) left |= z.update(centre + ((i / 10) % 2 ? -4.5f : 4.5f), t += 1000, c);
    CHECK(left == !widened);
  }
}

// helper: loss in dB that corresponds to a distance with the default model
static float L(float m) { return lossForDistance(m, 2.7f); }

static void testZoneDwellAndHysteresis() {
  ZoneConfig c;  // near 1 m, far 4 m, +-3 dB, enter NEAR 3 s, other 5 s
  c.leaveNearMs = 8000;
  ZoneTracker z;
  uint32_t t = 1000;
  CHECK(z.update(L(2.0f), t, c));  // first sample after AWAY: immediate
  CHECK(z.zone() == ROOM);
  CHECK(!z.update(L(0.4f), t += 1000, c));  // candidate NEAR (clock starts here)
  CHECK(!z.update(L(0.4f), t += 1000, c));  // 1 s
  CHECK(!z.update(L(0.4f), t += 1000, c));  // 2 s
  CHECK(z.update(L(0.4f), t += 1000, c));   // 3 s -> NEAR
  CHECK(z.zone() == NEAR);
  // 1.2 m is past near_m but inside the hysteresis band (+3 dB ~ 1.29 m): stays NEAR forever
  for (int i = 0; i < 20; i++) CHECK(!z.update(L(1.2f), t += 1000, c));
  CHECK(z.zone() == NEAR);
  // Leaving NEAR needs 8 s from the first reading outside
  for (int i = 0; i < 8; i++) CHECK(!z.update(L(2.0f), t += 1000, c));
  CHECK(z.update(L(2.0f), t += 1000, c));
  CHECK(z.zone() == ROOM);
  // From ROOM, 0.9 m is not NEAR yet (needs 3 dB inside: < ~0.77 m)
  for (int i = 0; i < 10; i++) CHECK(!z.update(L(0.9f), t += 1000, c));
  CHECK(z.zone() == ROOM);
}

// Regression: leaving NEAR while the readings flip between ROOM and FAR (a phone at ~5 m, right at
// the far boundary) must still leave NEAR on time. The old tracker restarted its clock on every
// flip and stayed NEAR forever.
static void testLeavingNearWhileTargetFlips() {
  ZoneConfig c;  // leave NEAR after 5 s
  ZoneTracker z;
  uint32_t t = 0;
  z.update(L(0.4f), t, c);
  CHECK(z.zone() == NEAR);
  bool left = false;
  uint32_t leftAt = 0;
  for (int i = 1; i <= 30 && !left; i++) {
    float d = (i % 2) ? 4.6f : 7.0f;  // ROOM, FAR, ROOM, FAR...
    if (z.update(L(d), t += 500, c)) {
      left = true;
      leftAt = t;
    }
  }
  CHECK(left);
  CHECK(leftAt == 500 + 5000);  // first non-NEAR reading at 500 ms, then 5 s
  CHECK(z.zone() == ROOM || z.zone() == FAR);
}

static void testPendingReporting() {
  ZoneConfig c;
  ZoneTracker z;
  uint32_t t = 0;
  z.update(L(2.0f), t, c);
  CHECK(z.pending() == ROOM);
  CHECK(z.pendingLeftMs(t, c) == 0);
  z.update(L(0.3f), t += 1000, c);  // NEAR readings start
  CHECK(z.zone() == ROOM);
  CHECK(z.pending() == NEAR);
  CHECK(z.pendingLeftMs(t, c) == 3000);
  z.update(L(0.3f), t += 1000, c);
  CHECK(z.pendingLeftMs(t, c) == 2000);
  z.update(L(2.0f), t += 1000, c);  // back to ROOM readings: nothing pending
  CHECK(z.pending() == ROOM);
  CHECK(z.pendingLeftMs(t, c) == 0);
}

static void testEnterNearNeedsContinuousNear() {
  ZoneConfig c;
  ZoneTracker z;
  uint32_t t = 0;
  z.update(L(2.0f), t, c);  // ROOM
  // FAR readings for 4 s keep the "away from ROOM" clock running...
  for (int i = 0; i < 4; i++) z.update(L(8.0f), t += 1000, c);
  CHECK(z.zone() == ROOM);
  // ...but one NEAR reading must not switch straight to NEAR on the strength of that clock
  CHECK(!z.update(L(0.3f), t += 1000, c));
  CHECK(z.zone() == ROOM);
}

static void testZoneShortExcursionIgnored() {
  ZoneConfig c;
  ZoneTracker z;
  uint32_t t = 0;
  z.update(L(2.0f), t, c);
  CHECK(!z.update(L(0.3f), t += 1000, c));
  CHECK(!z.update(L(0.3f), t += 1000, c));
  CHECK(!z.update(L(2.0f), t += 1000, c));  // back before 3 s: candidate cleared
  CHECK(!z.update(L(0.3f), t += 1000, c));  // restarts the clock
  CHECK(!z.update(L(0.3f), t += 1000, c));
  CHECK(z.zone() == ROOM);
}

static void testZoneFar() {
  ZoneConfig c;
  ZoneTracker z;
  uint32_t t = 0;
  CHECK(z.update(L(8.0f), t, c));
  CHECK(z.zone() == FAR);
  for (int i = 0; i < 5; i++) CHECK(!z.update(L(2.0f), t += 1000, c));
  CHECK(z.update(L(2.0f), t += 1000, c));  // 5 s after the first ROOM reading
  CHECK(z.zone() == ROOM);
}

static void testAwayAndCap() {
  ZoneConfig c;
  ZoneTracker z;
  z.update(L(0.3f), 0, c);
  CHECK(z.zone() == NEAR);
  CHECK(z.capAtFar());
  CHECK(z.zone() == FAR);
  CHECK(!z.capAtFar());
  CHECK(z.goAway());
  CHECK(z.zone() == AWAY);
  CHECK(!z.goAway());
  CHECK(z.update(L(0.3f), 100, c));  // back: immediate
  CHECK(z.zone() == NEAR);
}

static void testFocusSession() {
  FocusSession s;
  uint32_t t = 10000;
  s.start(t, 60);
  CHECK(s.tick(t += 1000, ROOM, 5000) == FocusSession::NONE);
  for (int i = 0; i < 4; i++) CHECK(s.tick(t += 1000, NEAR, 5000) == FocusSession::NONE);
  CHECK(s.tick(t += 1000, ROOM, 5000) == FocusSession::NONE);
  CHECK(s.pickups() == 0);
  int distracted = 0;
  for (int i = 0; i < 6; i++)
    if (s.tick(t += 1000, NEAR, 5000) == FocusSession::DISTRACTED) distracted++;
  CHECK(distracted == 1);
  CHECK(s.pickups() == 1);
  CHECK(s.distracted());
  CHECK(s.tick(t += 1000, FAR, 5000) == FocusSession::RECOVERED);
  CHECK(s.nearS() == 10);
  CHECK(s.leftS(t) == 60 - 13);
  FocusSession::Event last = FocusSession::NONE;
  for (int i = 0; i < 60 && s.active(); i++) last = s.tick(t += 1000, AWAY, 5000);
  CHECK(last == FocusSession::DONE);
  CHECK(!s.active());
  CHECK(s.elapsedS(t + 99999) == 60);  // frozen after the end
}

static void testFocusStopFreezesAndUnlimited() {
  FocusSession s;
  s.start(0, 0);
  CHECK(s.leftS(5000) == -1);
  CHECK(s.tick(10u * 3600 * 1000, AWAY, 5000) == FocusSession::NONE);
  CHECK(s.active());
  s.stop(10u * 3600 * 1000 + 500);
  CHECK(!s.active());
  CHECK(s.elapsedS(20u * 3600 * 1000) == 36000);
}

static void testMillisWrap() {
  RssiWindow w;
  uint32_t t = 0xFFFFFF00u;
  for (int i = 0; i < 5; i++) w.add(-60, t + i * 100);  // crosses 2^32
  float v;
  CHECK(w.value(t + 450, 3000, 3, v));
  NEAR_EQ(v, -60.0f, 0.01f);
  FocusSession s;
  s.start(0xFFFFF000u, 60);
  s.tick(0x00000400u, ROOM, 5000);  // 5.1 s later, after wrap
  CHECK(s.elapsedS(0x00000400u) == 5);
}

int main() {
  testDistanceModel();
  testWindowIgnoresOutliersAndJunk();
  testWindowNeedsEnoughRecentSamples();
  testWindowWrapsCapacity();
  testLevelAveragesPower();
  testLevelUnbiasedUnderFading();
  testWindowLevelByAgeAndSteady();
  testSmootherQuietAtRestQuickOnMoves();
  testTwoPointFit();
  testOrientation();
  testZoneDwellAndHysteresis();
  testLeavingNearWhileTargetFlips();
  testPendingReporting();
  testEnterNearNeedsContinuousNear();
  testZoneShortExcursionIgnored();
  testZoneFar();
  testAwayAndCap();
  testFocusSession();
  testFocusStopFreezesAndUnlimited();
  testMillisWrap();
  if (failures) {
    printf("%d check(s) FAILED\n", failures);
    return 1;
  }
  printf("all proximity tests passed\n");
  return 0;
}
