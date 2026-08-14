/*
 * IVC Cage – Valve + Environment controller for Arduino Mega
 *
 * Two servos:
 *   Pin 8  → Feed valve  (0° = closed, 90° = open)
 *   Pin 9  → Water valve (0° = closed, 90° = open)
 *
 * DHT11 humidity / temperature sensor:
 *   Pin 7  → DHT11 data line (with internal pull-up)
 *
 * Serial protocol (115200 baud, newline-terminated JSON):
 *   Servo set    → {"servo":"feed","angle":90}
 *   Servo set    → {"servo":"water","angle":0}
 *   Status query → {"cmd":"status"}
 *   Env query    → {"cmd":"env"}
 *   Response     → {"status":"ok","feed":90,"water":0,"temp":24,"hum":55,"env_age":1234}
 *
 * All responses always include the full state (servos + last env reading)
 * so the Pi cache only needs one round-trip per refresh.
 */

#include <Servo.h>

// --- Pins ------------------------------------------------------------------
const int FEED_PIN  = 8;
const int WATER_PIN = 9;
const int DHT_PIN   = 7;

// --- Servo state -----------------------------------------------------------
Servo feedServo;
Servo waterServo;
int feedAngle  = 0;
int waterAngle = 0;

// --- DHT11 cache -----------------------------------------------------------
// DHT11 needs >=1s between reads, so we cache the last good reading. -127 = no
// valid data yet (sentinel: in real hardware the temp can never reach -127).
int  dhtTempC      = -127;
int  dhtHumPct     = -127;
unsigned long dhtLastReadMs = 0;       // millis() at last successful read
const unsigned long DHT_MIN_INTERVAL_MS = 2000;

// --- DHT11 protocol --------------------------------------------------------
// Minimal inline implementation — no library needed. Returns true on success.
// Layout: data[0]=humidity int, data[1]=humidity dec, data[2]=temp int,
// data[3]=temp dec, data[4]=checksum (= sum of first 4 bytes mod 256).
// DHT11 returns integer values; data[1] and data[3] are usually 0.
bool dhtRead(int &temp, int &hum) {
  uint8_t data[5] = {0, 0, 0, 0, 0};

  // Start signal: pull data line low for >=18ms, then release high for ~40us
  pinMode(DHT_PIN, OUTPUT);
  digitalWrite(DHT_PIN, LOW);
  delay(20);
  digitalWrite(DHT_PIN, HIGH);
  delayMicroseconds(40);
  pinMode(DHT_PIN, INPUT_PULLUP);

  // DHT responds: ~80us low, then ~80us high
  unsigned long t0 = micros();
  while (digitalRead(DHT_PIN) == HIGH) {
    if (micros() - t0 > 200) return false;
  }
  t0 = micros();
  while (digitalRead(DHT_PIN) == LOW) {
    if (micros() - t0 > 200) return false;
  }
  t0 = micros();
  while (digitalRead(DHT_PIN) == HIGH) {
    if (micros() - t0 > 200) return false;
  }

  // 40 data bits. Each bit: 50us low + (26-28us high = 0, 70us high = 1).
  for (int i = 0; i < 40; i++) {
    t0 = micros();
    while (digitalRead(DHT_PIN) == LOW) {
      if (micros() - t0 > 100) return false;
    }
    unsigned long hiStart = micros();
    while (digitalRead(DHT_PIN) == HIGH) {
      if (micros() - hiStart > 100) return false;
    }
    data[i / 8] <<= 1;
    if ((micros() - hiStart) > 40) data[i / 8] |= 1;
  }

  // Checksum
  uint8_t sum = data[0] + data[1] + data[2] + data[3];
  if (sum != data[4]) return false;

  hum  = data[0];
  temp = data[2];
  // Sanity: DHT11 ranges are 0..50°C and 20..90%RH. Allow a tiny margin.
  if (hum < 0 || hum > 100) return false;
  if (temp < -10 || temp > 80) return false;
  return true;
}

void dhtRefreshIfStale() {
  unsigned long now = millis();
  if (dhtLastReadMs != 0 && (now - dhtLastReadMs) < DHT_MIN_INTERVAL_MS) return;
  int t, h;
  if (dhtRead(t, h)) {
    dhtTempC      = t;
    dhtHumPct     = h;
    dhtLastReadMs = now;
  } else {
    // On failure, leave previous cached values intact (don't blank them out).
    // Just don't update dhtLastReadMs — next call will retry immediately.
  }
}

// --- Serial response -------------------------------------------------------
void sendStatus() {
  dhtRefreshIfStale();
  unsigned long age = dhtLastReadMs == 0 ? 0 : (millis() - dhtLastReadMs);

  Serial.print(F("{\"status\":\"ok\",\"feed\":"));
  Serial.print(feedAngle);
  Serial.print(F(",\"water\":"));
  Serial.print(waterAngle);
  Serial.print(F(",\"temp\":"));
  Serial.print(dhtTempC);
  Serial.print(F(",\"hum\":"));
  Serial.print(dhtHumPct);
  Serial.print(F(",\"env_age\":"));
  Serial.print(age);
  Serial.println(F("}"));
}

// --- Setup / loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  feedServo.attach(FEED_PIN);
  waterServo.attach(WATER_PIN);
  feedServo.write(feedAngle);
  waterServo.write(waterAngle);

  // First DHT read takes ~1s after power-up; do it asynchronously
  pinMode(DHT_PIN, INPUT_PULLUP);
  delay(1100);
  dhtRefreshIfStale();

  sendStatus();
}

void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;

  // Determine what's being asked
  bool isFeed   = line.indexOf("\"feed\"")  >= 0;
  bool isWater  = line.indexOf("\"water\"") >= 0;
  bool isStatus = line.indexOf("status") >= 0;
  bool isEnv    = line.indexOf("\"env\"") >= 0 || line.indexOf("env") >= 0;

  // Bare query (status or env) — always returns the full state
  if (isStatus || (isEnv && !isFeed && !isWater)) {
    sendStatus();
    return;
  }

  if (!isFeed && !isWater) {
    sendStatus();
    return;
  }

  // Parse angle value
  int idx = line.indexOf("\"angle\":");
  if (idx < 0) { sendStatus(); return; }
  int angle = line.substring(idx + 8).toInt();
  angle = constrain(angle, 0, 180);

  if (isFeed) {
    feedServo.write(angle);
    feedAngle = angle;
  } else if (isWater) {
    waterServo.write(angle);
    waterAngle = angle;
  }

  sendStatus();
}
