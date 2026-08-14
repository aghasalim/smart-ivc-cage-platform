// IVC Cage — Arduino Mega sensor bridge
// HX711x3 + MH-Z19C CO2 + JXW-02 O2(USB) + DHT11 + Food servo → JSON on USB serial
// Commands: c=calibrate t=tare z=co2zero
//
// PIN MAP (Arduino Mega 2560) — matches physical wiring:
//   Flow sensor (YF-S401) signal → D2  (interrupt)
//   DHT11 data                   → D3
//   HX711 #1 (FOOD)  DT/SCK       → D4 / D5
//   HX711 #2 (WATER) DT/SCK       → D6 / D7
//   HX711 #3 (MOUSE) DT/SCK       → D8 / D9
//   Food servo signal            → D11
//   Pump relay (RELAY1)          → D12  (active-LOW)
//   Solenoid valve relay (RELAY2)→ D13  (active-LOW)
//   CO2 (MH-Z19C) Serial3        → D14(TX3)/D15(RX3)   [optional/unused]
//   O2 (JXW-02)                  → USB CH340 on the Pi (/dev/ttyUSB0), NOT the Mega
#include <Arduino.h>
#include <HX711.h>
#include <EEPROM.h>
#include <Servo.h>
#include <DHT.h>

#define DHT_PIN 3
#define SERVO1_PIN 11     // FOOD servo (single servo, signal on D11)
#define SERVO2_PIN 10     // unused (no servo wired); kept to avoid pin clash
#define RELAY_PUMP_PIN 12
#define RELAY2_PIN 13
#define FLOW_PIN 2        // YF-S401 signal (interrupt-capable)

void co2cmd(byte a, byte b);
int co2read();
void o2drain();
void doCalib();
void loadCalib();
void setup();
void loop();

HX711 scFood, scWater, scMouse;
DHT dht(DHT_PIN, DHT11);
Servo servoFood, servoWater;
// Servos use attach-move-detach so they hold a clean position on command but are
// NOT continuously driven while idle — the HX711 load-cell reads disable
// interrupts, which starves the Servo timer PWM and makes an always-attached
// servo twitch/"blink" every sensor cycle. Detaching when idle removes that.
int gFoodAngle = 0;                 // last commanded angle (source of truth for reporting)
int gWaterAngle = 0;
unsigned long gFoodDetachAt = 0;    // millis() at which to detach (0 = idle/detached)
unsigned long gWaterDetachAt = 0;
const unsigned long SERVO_SETTLE_MS = 900;  // keep driven this long after a move

unsigned long gLastSend = 0;
unsigned long gBoot = 0;
float gO2 = -1;
int gCO2 = -1;
int gCO2err = 0;
bool gWarm = true;
String gO2buf = "";
float gTemp = -127;
float gHum = -127;

// Water pump + flow sensor
// gPulsesPerML and gDoseCoastML are CALIBRATED per pump+tubing and persisted in
// EEPROM (see loadFlowCalib/saveFlowCalib). Defaults match a generic YF-S401.
float gPulsesPerML = 7.5;   // flow-sensor pulses per mL (calibrate with `{"flowcal":N}`)
float gDoseCoastML = 1.5;   // mL that coast out AFTER the relay cuts (early-stop comp)
volatile unsigned long gFlowPulses = 0;
volatile unsigned long gLastPulseMs = 0;   // millis() of last pulse (vice-versa stall detection)
float gFlowML = 0;
bool gPumpOn = false;
// Flow calibration run state ({"flowcal":<seconds>})
bool gCalActive = false;
bool gCalRelayOff = false;
unsigned long gCalRelayOffAt = 0;
unsigned long gCalPulsesAtOff = 0;
unsigned long gPumpStopAt = 0;  // auto-stop after N ms (time mode)
// Volume dosing: pump until this many mL have flowed, then stop.
float gDoseTargetML = 0;        // 0 = not dosing
unsigned long gDoseStartPulses = 0;
unsigned long gDoseTimeoutAt = 0;  // safety: stop even if target never reached
// Stall recovery (vice versa): if pump is on but no pulses for FLOW_STALL_MS, restart pump.
const unsigned long FLOW_STALL_MS   = 2000;  // 2s without a pulse = stall
const int           FLOW_MAX_RETRIES = 3;
int  gFlowStallRetries = 0;
unsigned long gPumpRestartAt = 0;  // non-zero while waiting 300ms before pump-on retry

// ── Siphon / overshoot prevention ─────────────────────────────────────────────
// RELAY2 (pin 13) = solenoid valve on the water line.
//   OPEN  (LOW)  while pump runs  → water flows freely.
//   CLOSED (HIGH) when pump stops → physically blocks gravity/siphon flow.
//
// After pump stops, flow sensor keeps watching:
//   • Any pulse = siphon still flowing → log event with extra mL counted.
//   • Valve stays CLOSED until siphon stops (no pulse for SIPHON_IDLE_MS).
//
// Early-stop compensation for exact dosing:
//   gDoseCoastML = mL that flow AFTER relay cuts (calibrated, see above).
//   Relay cuts early so: pump_mL + coast_mL ≈ target_mL.
const unsigned long  SIPHON_IDLE_MS   = 1500;  // no pulse this long = siphon stopped
unsigned long gSiphonWatchUntil = 0;           // non-zero = actively watching for siphon
float         gSiphonExtraML    = 0;           // mL counted after pump stopped

// ── CO2 (MH-Z19C on Serial3: Pin 14=TX3, Pin 15=RX3) ──────────
void co2cmd(byte a, byte b) {
  byte c[9] = {0xFF, 0x01, a, b, 0, 0, 0, 0, 0};
  byte k = 0;
  for (int i = 1; i < 8; i++) k += c[i];
  c[8] = 0xFF - k + 1;
  while (Serial3.available()) Serial3.read();
  Serial3.write(c, 9);
}

int co2read() {
  if (gWarm) {
    if (millis() - gBoot < 60000UL) return -99;
    gWarm = false;
  }
  co2cmd(0x86, 0x00);
  unsigned long t = millis();
  while (Serial3.available() < 9) {
    if (millis() - t > 1500) { gCO2err = -1; return -1; }  // timeout
  }
  byte r[9];
  Serial3.readBytes(r, 9);
  if (r[0] != 0xFF || r[1] != 0x86) { gCO2err = -2; return -2; }
  byte k = 0;
  for (int i = 1; i < 8; i++) k += r[i];
  if ((byte)(0xFF - k + 1) != r[8]) { gCO2err = -3; return -3; }
  int p = (int(r[2]) << 8) | r[3];
  if (p < 300 || p > 10000) { gCO2err = -4; return -4; }
  gCO2err = 0;
  return p;
}

// ── O2 (JXW-02 on Serial2: Pin 16=TX2, Pin 17=RX2) ────────────
void o2drain() {
  while (Serial2.available()) {
    char c = (char)Serial2.read();
    if (c == '\n') {
      gO2buf.trim();
      for (int i = 0; i < (int)gO2buf.length(); i++) {
        if (isDigit(gO2buf[i]) || gO2buf[i] == '.') {
          float v = gO2buf.substring(i).toFloat();
          if (v > 0 && v <= 25) gO2 = v;
          break;
        }
      }
      gO2buf = "";
    } else if (c != '\r') {
      gO2buf += c;
      if (gO2buf.length() > 40) gO2buf = "";
    }
  }
}

// ── EEPROM calibration ─────────────────────────────────────────
// EEPROM layout:
//   0   uint32  HX711 magic 0x49564341
//   4   float   scFood scale     16  long scFood offset
//   8   float   scWater scale    20  long scWater offset
//   12  float   scMouse scale    24  long scMouse offset
//   28  uint32  flow magic 0x464C4F57 ("FLOW")
//   32  float   gPulsesPerML     36  float gDoseCoastML
void loadCalib() {
  uint32_t m;
  EEPROM.get(0, m);
  if (m == 0x49564341UL) {
    float f1, f2, f3; long o1, o2, o3;
    EEPROM.get(4, f1); EEPROM.get(8, f2); EEPROM.get(12, f3);
    EEPROM.get(16, o1); EEPROM.get(20, o2); EEPROM.get(24, o3);
    scFood.set_scale(f1); scFood.set_offset(o1);
    scWater.set_scale(f2); scWater.set_offset(o2);
    scMouse.set_scale(f3); scMouse.set_offset(o3);
  } else {
    scFood.set_scale(430); scWater.set_scale(430); scMouse.set_scale(430);
  }
}

void loadFlowCalib() {
  uint32_t fm;
  EEPROM.get(28, fm);
  if (fm == 0x464C4F57UL) {
    float ppm, coast;
    EEPROM.get(32, ppm); EEPROM.get(36, coast);
    if (ppm > 0.1 && ppm < 1000.0) gPulsesPerML = ppm;
    if (coast >= 0.0 && coast < 50.0) gDoseCoastML = coast;
  }  // else keep defaults (7.5 / 1.5)
}

void saveFlowCalib() {
  uint32_t fm = 0x464C4F57UL;
  EEPROM.put(28, fm);
  EEPROM.put(32, gPulsesPerML);
  EEPROM.put(36, gDoseCoastML);
}

// Start a calibration run: pump for runMs, then keep counting coast pulses
// until flow stops, and report the totals so the true pulses/mL can be computed.
void flowCalStart(unsigned long runMs) {
  noInterrupts(); gFlowPulses = 0; gLastPulseMs = millis(); interrupts();
  gFlowML = 0;
  gCalActive = true;
  gCalRelayOff = false;
  gCalPulsesAtOff = 0;
  gCalRelayOffAt = millis() + runMs;
  gPumpOn = true;
  valveOpen();
  digitalWrite(RELAY_PUMP_PIN, LOW);  // pump ON
}

void doCalib() {
  HX711 *sc[3] = {&scFood, &scWater, &scMouse};
  const char *nm[3] = {"FOOD", "WATER", "MOUSE"};
  float fac[3]; long off[3];
  for (int j = 0; j < 3; j++) {
    Serial.print(F("-- ")); Serial.print(nm[j]); Serial.println(F(" --"));
    Serial.println(F("Empty scale, send key..."));
    while (!Serial.available()) {} while (Serial.available()) Serial.read();
    sc[j]->tare(10); off[j] = sc[j]->get_offset();
    Serial.println(F("Place weight, type grams + Enter:"));
    String ln = ""; unsigned long t = millis();
    while (ln.length() == 0 && millis() - t < 30000) {
      if (Serial.available()) {
        char ch = (char)Serial.read();
        if (ch == '\n') ln.trim(); else if (ch != '\r') ln += ch;
      }
    }
    float g = ln.toFloat();
    if (g <= 0) { fac[j] = 430; Serial.println(F("skip")); continue; }
    sc[j]->set_scale(1); float raw = sc[j]->get_units(10);
    fac[j] = raw / g; sc[j]->set_scale(fac[j]);
    Serial.print(fac[j]); Serial.print(F(" -> "));
    Serial.print(sc[j]->get_units(5)); Serial.println(F("g"));
  }
  uint32_t m = 0x49564341UL; EEPROM.put(0, m);
  EEPROM.put(4, fac[0]); EEPROM.put(8, fac[1]); EEPROM.put(12, fac[2]);
  EEPROM.put(16, off[0]); EEPROM.put(20, off[1]); EEPROM.put(24, off[2]);
  Serial.println(F("Saved."));
}

void flowPulseISR() { gFlowPulses++; gLastPulseMs = millis(); }

void valveOpen() {
  digitalWrite(RELAY2_PIN, LOW);   // active-LOW: solenoid valve OPEN
}

void valveClose() {
  digitalWrite(RELAY2_PIN, HIGH);  // active-LOW: solenoid valve CLOSED
}

void pumpOn(unsigned long durationMs) {
  gFlowPulses = 0;
  gLastPulseMs = 0;
  gFlowStallRetries = 0;
  gPumpRestartAt = 0;
  gSiphonWatchUntil = 0;
  gSiphonExtraML = 0;
  gPumpOn = true;
  gPumpStopAt = millis() + durationMs;
  valveOpen();                        // open solenoid valve first
  digitalWrite(RELAY_PUMP_PIN, LOW);  // then start pump
}

void pumpOff() {
  digitalWrite(RELAY_PUMP_PIN, HIGH); // stop pump relay immediately
  valveClose();                       // close solenoid valve — cuts siphon physically
  gPumpOn = false;
  gDoseTargetML = 0;
  gPumpRestartAt = 0;
  // Reset flow counter; siphon watch uses its own separate pulse tracking.
  noInterrupts();
  gFlowPulses = 0;
  gLastPulseMs = millis();  // anchor: any pulse AFTER this = siphon
  interrupts();
  gFlowML = 0;
  gSiphonExtraML = 0;
  gSiphonWatchUntil = millis() + 10000UL; // watch up to 10s for residual flow
  Serial.println(F("{\"event\":\"flow_reset\",\"action\":\"pump_stopped\"}"));
}

// Dispense an exact volume (mL) with early-stop compensation.
// Relay cuts at (targetML - DOSE_COAST_ML) so coasting flow reaches the target.
// Safety timeout prevents runaway pump.
void doseVolume(float targetML) {
  gFlowPulses = 0;
  gLastPulseMs = millis();
  gFlowStallRetries = 0;
  gPumpRestartAt = 0;
  gDoseStartPulses = 0;
  gSiphonWatchUntil = 0;
  gSiphonExtraML = 0;
  // Apply early-stop: cut relay before target so coasting covers the rest.
  float cutAt = targetML - gDoseCoastML;
  if (cutAt < 0.5) cutAt = targetML;  // don't go below 0.5 mL
  gDoseTargetML = cutAt;
  gPumpOn = true;
  gPumpStopAt = 0;  // not time-based
  // Safety timeout sized for a SLOW pump (~0.25 mL/s worst case) so a dose is
  // never cut short before the flow sensor reaches the target. The stall-
  // recovery logic handles a genuinely dry/stuck pump separately.
  unsigned long timeoutMs = (unsigned long)(targetML * 4000.0);  // 0.25 mL/s floor
  if (timeoutMs < 20000) timeoutMs = 20000;
  if (timeoutMs > 300000) timeoutMs = 300000;  // 5 min absolute cap
  gDoseTimeoutAt = millis() + timeoutMs;
  valveOpen();                        // open solenoid valve
  digitalWrite(RELAY_PUMP_PIN, LOW);  // start pump
}

// Attach (if needed), move to angle, and schedule a detach after the servo has
// had time to physically reach the target. The light gate holds by gear friction.
void moveServoFood(int angle) {
  gFoodAngle = angle;
  if (!servoFood.attached()) servoFood.attach(SERVO1_PIN);
  servoFood.write(angle);
  gFoodDetachAt = millis() + SERVO_SETTLE_MS;
}
void moveServoWater(int angle) {
  gWaterAngle = angle;
  if (!servoWater.attached()) servoWater.attach(SERVO2_PIN);
  servoWater.write(angle);
  gWaterDetachAt = millis() + SERVO_SETTLE_MS;
}
// Non-blocking: detach a servo once it has settled (called every loop).
void serviceServos() {
  unsigned long now = millis();
  if (gFoodDetachAt && now >= gFoodDetachAt)  { servoFood.detach();  gFoodDetachAt = 0; }
  if (gWaterDetachAt && now >= gWaterDetachAt) { servoWater.detach(); gWaterDetachAt = 0; }
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(9600);   // O2 on Pin 16/17
  Serial3.begin(9600);   // CO2 on Pin 14(TX3)/15(RX3)
  gBoot = millis();
  dht.begin();

  // Relay pins (active-LOW: HIGH = off)
  pinMode(RELAY_PUMP_PIN, OUTPUT); digitalWrite(RELAY_PUMP_PIN, HIGH);
  pinMode(RELAY2_PIN, OUTPUT); digitalWrite(RELAY2_PIN, HIGH);

  // Flow sensor interrupt
  pinMode(FLOW_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), flowPulseISR, RISING);

  // Set both servos to their closed position once, then they auto-detach in loop.
  moveServoFood(0); moveServoWater(0);
  scFood.begin(4, 5); scWater.begin(6, 7); scMouse.begin(8, 9);
  delay(500);
  loadCalib();
  loadFlowCalib();   // restore calibrated pulses/mL + coast from EEPROM
  uint32_t m; EEPROM.get(0, m);
  if (m != 0x49564341UL) {
    if (scFood.is_ready()) scFood.tare(10);
    if (scWater.is_ready()) scWater.tare(10);
    if (scMouse.is_ready()) scMouse.tare(10);
  }
  delay(200);
  co2cmd(0x79, 0x00); delay(100); // disable ABC
  Serial.println(F("{\"event\":\"boot\",\"msg\":\"IVC Arduino ready\"}"));
}

void loop() {
  serviceServos();   // detach idle servos so HX711 reads can't make them twitch
  o2drain();
  if (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '{') {
      String cmd = "{"; unsigned long t = millis();
      while (millis() - t < 200) {
        if (Serial.available()) { char c2 = (char)Serial.read(); cmd += c2; if (c2 == '}') break; }
      }
      // Only act on a COMPLETE JSON frame. Feed-servo actuation induces
      // electrical noise on the serial RX line; a garbled partial fragment
      // must never reach the matcher below, or a phantom "pump"/"on" snippet
      // would switch the water pump on (the "feed triggers water" bug).
      if (!cmd.endsWith("}")) {
        // discard incomplete / noise-corrupted frame
      }
      else if (cmd.indexOf("flowcal") > 0) {
        // {"flowcal":20} → run pump 20 s, count ALL pulses incl. coast, report
        // totals so the true pulses/mL can be computed from measured water.
        int di = cmd.indexOf("flowcal");
        int ci = cmd.indexOf(':', di);
        int secs = cmd.substring(ci + 1).toInt();
        if (secs >= 2 && secs <= 120) {
          flowCalStart((unsigned long)secs * 1000UL);
          Serial.print(F("{\"status\":\"ok\",\"flowcal\":")); Serial.print(secs);
          Serial.println(F(",\"msg\":\"collect ALL water, then send {\\\"setflow\\\":<pulses_per_ml>}\"}"));
        } else {
          Serial.println(F("{\"status\":\"err\",\"msg\":\"flowcal seconds must be 2-120\"}"));
        }
      }
      else if (cmd.indexOf("setflow") > 0) {
        // {"setflow":9.4} → set pulses/mL and persist to EEPROM
        int di = cmd.indexOf("setflow");
        int ci = cmd.indexOf(':', di);
        float v = cmd.substring(ci + 1).toFloat();
        if (v > 0.1 && v < 1000.0) {
          gPulsesPerML = v; saveFlowCalib();
          Serial.print(F("{\"status\":\"ok\",\"pulses_per_ml\":")); Serial.print(gPulsesPerML, 3);
          Serial.print(F(",\"coast_ml\":")); Serial.print(gDoseCoastML, 2); Serial.println(F("}"));
        } else {
          Serial.println(F("{\"status\":\"err\",\"msg\":\"setflow must be 0.1-1000\"}"));
        }
      }
      else if (cmd.indexOf("setcoast") > 0) {
        // {"setcoast":1.2} → set early-stop coast mL and persist
        int di = cmd.indexOf("setcoast");
        int ci = cmd.indexOf(':', di);
        float v = cmd.substring(ci + 1).toFloat();
        if (v >= 0.0 && v < 50.0) {
          gDoseCoastML = v; saveFlowCalib();
          Serial.print(F("{\"status\":\"ok\",\"pulses_per_ml\":")); Serial.print(gPulsesPerML, 3);
          Serial.print(F(",\"coast_ml\":")); Serial.print(gDoseCoastML, 2); Serial.println(F("}"));
        } else {
          Serial.println(F("{\"status\":\"err\",\"msg\":\"setcoast must be 0-50\"}"));
        }
      }
      else if (cmd.indexOf("dose") > 0) {
        // {"dose":100} → dispense exactly 100 mL using flow-sensor feedback
        int di = cmd.indexOf("dose");
        int ci = cmd.indexOf(':', di);
        float target = cmd.substring(ci + 1).toFloat();
        if (target > 0 && target <= 1000) {
          doseVolume(target);
          Serial.print(F("{\"status\":\"ok\",\"dose\":")); Serial.print(target, 1);
          Serial.print(F(",\"pulses_per_ml\":")); Serial.print(gPulsesPerML, 3);
          Serial.print(F(",\"coast_ml\":")); Serial.print(gDoseCoastML, 2); Serial.println(F("}"));
        } else {
          Serial.println(F("{\"status\":\"err\",\"msg\":\"dose must be 1-1000 mL\"}"));
        }
      }
      else if (cmd.indexOf("pump") > 0) {
        if (cmd.indexOf("on") > 0) {
          int durIdx = cmd.indexOf("dur");
          int dur = 5000;
          if (durIdx > 0) { int ci = cmd.indexOf(':', durIdx); dur = cmd.substring(ci+1).toInt(); }
          if (dur < 100) dur = 5000;
          if (dur > 30000) dur = 30000;
          pumpOn(dur);
          Serial.print(F("{\"status\":\"ok\",\"pump\":\"on\",\"dur\":")); Serial.print(dur); Serial.println(F("}"));
        } else {
          pumpOff();
          Serial.println(F("{\"status\":\"ok\",\"pump\":\"off\"}"));
        }
      }
      else if (cmd.indexOf("servo") > 0) {
        int ai = cmd.indexOf("angle");
        if (ai > 0) {
          int ci = cmd.indexOf(':', ai); int angle = constrain(cmd.substring(ci+1).toInt(), 0, 180);
          // Dashboard sends "feed" for the food gate; accept both "feed"/"food".
          if (cmd.indexOf("food") > 0 || cmd.indexOf("feed") > 0) moveServoFood(angle);
          else if (cmd.indexOf("water") > 0) moveServoWater(angle);
        }
        Serial.print(F("{\"status\":\"ok\",\"feed\":")); Serial.print(gFoodAngle);
        Serial.print(F(",\"water\":")); Serial.print(gWaterAngle);
        Serial.print(F(",\"temp\":")); Serial.print(gTemp, 1);
        Serial.print(F(",\"hum\":")); Serial.print(gHum, 1);
        Serial.println(F("}"));
      } else if (cmd.indexOf("status") > 0) {
        Serial.print(F("{\"status\":\"ok\",\"feed\":")); Serial.print(gFoodAngle);
        Serial.print(F(",\"water\":")); Serial.print(gWaterAngle);
        Serial.print(F(",\"temp\":")); Serial.print(gTemp, 1);
        Serial.print(F(",\"hum\":")); Serial.print(gHum, 1);
        Serial.print(F(",\"pulses_per_ml\":")); Serial.print(gPulsesPerML, 3);
        Serial.print(F(",\"coast_ml\":")); Serial.print(gDoseCoastML, 2);
        Serial.println(F("}"));
      }
      // JSON-only replacements for the removed single-char debug commands.
      // These need a full {...} frame, so serial noise can't trigger them.
      else if (cmd.indexOf("tare") > 0) {
        if (scFood.is_ready()) scFood.tare(10);
        if (scWater.is_ready()) scWater.tare(10);
        if (scMouse.is_ready()) scMouse.tare(10);
        Serial.println(F("{\"event\":\"tare\"}"));
      }
      else if (cmd.indexOf("calibrate") > 0) { doCalib(); loadCalib(); }
      else if (cmd.indexOf("co2zero") > 0) {
        co2cmd(0x87, 0x00); Serial.println(F("{\"event\":\"co2zero\"}"));
      }
    } else {
      // Ignore ANY byte that isn't the start of a JSON frame. The legacy
      // single-character debug commands (c/t/z/p/x) were a serious hazard: a
      // lone stray byte induced on the serial RX line by feed-servo electrical
      // noise could read as 'p' and switch the water pump ON — the recurring
      // "feed valve triggers the water relays" bug. Every real command from the
      // bridge is JSON ({...}); a bare byte is noise and must do nothing.
      // Calibrate/tare/co2-zero are now JSON-only: {"calibrate":1} / {"tare":1}
      // / {"co2zero":1} (handled in the JSON matcher above).
      while (Serial.available()) Serial.read();
    }
  }

  // Calculate flow mL from pulses (calibrated gPulsesPerML)
  noInterrupts();
  unsigned long pulses = gFlowPulses;
  unsigned long lastP  = gLastPulseMs;
  interrupts();
  gFlowML = pulses / gPulsesPerML;

  // ── Flow calibration run (takes priority; bypasses dose/siphon logic) ──
  if (gCalActive) {
    unsigned long now = millis();
    if (!gCalRelayOff && now >= gCalRelayOffAt) {
      digitalWrite(RELAY_PUMP_PIN, HIGH);  // pump OFF; valve stays OPEN to count coast
      gCalRelayOff = true;
      gCalPulsesAtOff = pulses;
    }
    if (gCalRelayOff && (now - lastP) >= 2000UL) {
      // Coast finished — report totals and reset.
      valveClose();
      gPumpOn = false;
      gCalActive = false;
      unsigned long coastP = pulses - gCalPulsesAtOff;
      Serial.print(F("{\"event\":\"flowcal\",\"total_pulses\":")); Serial.print(pulses);
      Serial.print(F(",\"pulses_at_relay_off\":")); Serial.print(gCalPulsesAtOff);
      Serial.print(F(",\"coast_pulses\":")); Serial.print(coastP);
      Serial.print(F(",\"current_pulses_per_ml\":")); Serial.print(gPulsesPerML, 3);
      Serial.print(F(",\"est_ml_at_current\":")); Serial.print(pulses / gPulsesPerML, 2);
      Serial.println(F(",\"msg\":\"measure collected mL; setflow = total_pulses / measured_ml\"}"));
      noInterrupts(); gFlowPulses = 0; interrupts();
      gFlowML = 0;
    }
    return;  // skip normal dose/pump/siphon handling during calibration
  }

  // ── Volume dosing: stop when target mL reached (or safety timeout) ──
  if (gDoseTargetML > 0 && gPumpOn) {
    if (gFlowML >= gDoseTargetML) {
      float delivered = gFlowML;  // save before pumpOff() resets the counter
      pumpOff();
      Serial.print(F("{\"event\":\"dose\",\"action\":\"done\",\"delivered_ml\":"));
      Serial.print(delivered, 2); Serial.println(F("}"));
    } else if (millis() >= gDoseTimeoutAt) {
      float delivered = gFlowML;  // save before pumpOff() resets the counter
      pumpOff();
      Serial.print(F("{\"event\":\"dose\",\"action\":\"timeout\",\"delivered_ml\":"));
      Serial.print(delivered, 2); Serial.println(F("}"));
    }
  }

  // Auto-stop pump after duration (time mode only — not during dosing)
  if (gPumpOn && gDoseTargetML == 0 && gPumpStopAt > 0 && millis() >= gPumpStopAt) {
    pumpOff();
    Serial.println(F("{\"event\":\"pump\",\"action\":\"auto_stop\"}"));
  }

  // ── Vice-versa stall recovery: pump on but no pulses → restart pump ──────
  // If pump relay is on and flow sensor has gone silent for FLOW_STALL_MS,
  // briefly cycle the relay off then back on to bring water back.
  if (gPumpOn && gDoseTargetML > 0) {
    // Waiting for the brief off-pause before retry
    if (gPumpRestartAt > 0) {
      if (millis() >= gPumpRestartAt) {
        gPumpRestartAt = 0;
        gLastPulseMs = millis();  // reset stall clock for fresh retry
        digitalWrite(RELAY_PUMP_PIN, LOW);  // pump back ON
        Serial.print(F("{\"event\":\"flow_stall\",\"action\":\"retry\",\"attempt\":"));
        Serial.print(gFlowStallRetries); Serial.println(F("}"));
      }
    } else {
      // Check for stall: pump is physically on but no pulses arriving
      unsigned long now = millis();
      unsigned long lastPulse;
      noInterrupts(); lastPulse = gLastPulseMs; interrupts();
      bool stalled = (lastPulse == 0)
                       ? (now > 3000 && (now - 3000) > 0)   // no pulse since boot grace period
                       : ((now - lastPulse) >= FLOW_STALL_MS);
      if (stalled && gFlowStallRetries < FLOW_MAX_RETRIES) {
        gFlowStallRetries++;
        digitalWrite(RELAY_PUMP_PIN, HIGH);  // pump OFF briefly
        gPumpRestartAt = millis() + 300;     // wait 300ms then back on
        Serial.print(F("{\"event\":\"flow_stall\",\"action\":\"detected\",\"attempt\":"));
        Serial.print(gFlowStallRetries); Serial.println(F("}"));
      } else if (stalled && gFlowStallRetries >= FLOW_MAX_RETRIES) {
        // Gave up — stop dose, report
        pumpOff();
        Serial.print(F("{\"event\":\"flow_stall\",\"action\":\"give_up\",\"delivered_ml\":"));
        Serial.print(gFlowML, 2); Serial.println(F("}"));
      }
    }
  }

  // ── Siphon / post-stop flow monitor (vice versa) ─────────────────────────
  // Pump is OFF but valve is CLOSED. Watch flow sensor: any pulse means water
  // is still moving (gravity/siphon through leaky valve or open line).
  // Log extra mL and keep valve closed until flow is truly zero.
  if (!gPumpOn && gSiphonWatchUntil > 0) {
    noInterrupts();
    unsigned long pulses = gFlowPulses;
    unsigned long lastP  = gLastPulseMs;
    interrupts();

    float extraML = pulses / gPulsesPerML;
    unsigned long now = millis();

    if (pulses > 0) {
      // Still getting pulses — siphon active, valve stays closed
      gSiphonExtraML = extraML;
      if (now >= gSiphonWatchUntil) {
        // Watched long enough — report what leaked through
        Serial.print(F("{\"event\":\"siphon\",\"action\":\"closed\",\"extra_ml\":"));
        Serial.print(gSiphonExtraML, 2); Serial.println(F("}"));
        gSiphonWatchUntil = 0;
        valveClose(); // keep closed
      }
    } else {
      // No pulses — check if idle long enough to confirm flow has stopped
      bool idle = (lastP == 0) || ((now - lastP) >= SIPHON_IDLE_MS);
      if (idle) {
        // Flow truly stopped — report any extra mL, close watch
        if (gSiphonExtraML > 0.05) {
          Serial.print(F("{\"event\":\"siphon\",\"action\":\"stopped\",\"extra_ml\":"));
          Serial.print(gSiphonExtraML, 2); Serial.println(F("}"));
        }
        gSiphonWatchUntil = 0;
        valveClose(); // keep closed until next pump cycle
      }
    }
  }

  if (millis() - gLastSend < 5000UL) return;
  // Defer the slow HX711 sensor cycle while:
  //  • a servo is mid-move — the interrupt-disabling reads starve the Servo PWM
  //    and cause a visible twitch; OR
  //  • the pump is dosing/running — the blocking reads delay the volume-target
  //    check and let the dose OVERSHOOT before it stops. Pausing reads during a
  //    dose keeps the stop tight (exact mL). Doses are short, so the gap is small.
  if (gFoodDetachAt || gWaterDetachAt || gPumpOn || gCalActive) return;
  gLastSend = millis();

  float h = dht.readHumidity(); float t = dht.readTemperature();
  if (!isnan(h)) gHum = h; if (!isnan(t)) gTemp = t;

  float fg = scFood.is_ready() ? scFood.get_units(5) : -1;
  float wg = scWater.is_ready() ? scWater.get_units(5) : -1;
  float mg = scMouse.is_ready() ? scMouse.get_units(5) : -1;

  int co2 = co2read();
  if (co2 > 0) gCO2 = co2;

  // Include co2_err for debugging
  Serial.print(F("{\"food_g\":")); Serial.print(fg, 2);
  Serial.print(F(",\"water_g\":")); Serial.print(wg, 2);
  Serial.print(F(",\"mouse_g\":")); Serial.print(mg, 2);
  Serial.print(F(",\"co2_ppm\":")); Serial.print(gCO2);
  Serial.print(F(",\"co2_ok\":")); Serial.print(co2 > 0 ? "true" : "false");
  Serial.print(F(",\"co2_err\":")); Serial.print(gCO2err);
  Serial.print(F(",\"co2_raw\":")); Serial.print(co2);
  Serial.print(F(",\"o2_pct\":")); Serial.print(gO2, 2);
  Serial.print(F(",\"temp_c\":")); Serial.print(gTemp, 1);
  Serial.print(F(",\"hum_pct\":")); Serial.print(gHum, 1);
  Serial.print(F(",\"servo_food\":")); Serial.print(gFoodAngle);
  Serial.print(F(",\"servo_water\":")); Serial.print(gWaterAngle);
  Serial.print(F(",\"flow_ml\":")); Serial.print(gFlowML, 2);
  Serial.print(F(",\"pump_on\":")); Serial.print(gPumpOn ? "true" : "false");
  Serial.println(F("}"));
}
