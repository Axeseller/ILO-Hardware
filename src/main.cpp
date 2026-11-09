#include <Arduino.h>

// DOIT ESP32 DEVKIT V1 defines LED_BUILTIN as GPIO 2, the small user LED.
// The power LED next to the USB port stays lit and does not blink.
// GPIO 2 is a strapping pin, so it must stay low while the chip resets. The
// onboard LED is wired so the pin idles low; this sketch drives it only after boot.
constexpr uint8_t kOnboardLedPin = LED_BUILTIN;
constexpr uint32_t kBlinkIntervalMs = 500;
constexpr uint32_t kSerialBaud = 115200;

void setup() {
  pinMode(kOnboardLedPin, OUTPUT);
  Serial.begin(kSerialBaud);
  Serial.println("blink: onboard LED on GPIO 2");
}

void loop() {
  digitalWrite(kOnboardLedPin, HIGH);
  Serial.println("on");
  delay(kBlinkIntervalMs);

  digitalWrite(kOnboardLedPin, LOW);
  Serial.println("off");
  delay(kBlinkIntervalMs);
}
