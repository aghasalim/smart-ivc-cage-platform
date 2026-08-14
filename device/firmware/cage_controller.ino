/*
 * IVC Cage Controller — reference firmware
 * Hardware target: ESP32 (Arduino core)
 *
 * Reads each sensor, composes a JSON packet, and POSTs it to the backend.
 * Drives the timed feeding gate according to the schedule pulled from the
 * backend at boot and refreshed every 6 hours.
 *
 * Wiring (suggested):
 *   - I2C @ SDA=21, SCL=22  → SCD30 gas sensor (VO₂/VCO₂ proxy)
 *   - HX711 #1 @ DT=4,  SCK=5    → upper feed bin load cell
 *   - HX711 #2 @ DT=16, SCK=17   → waste tray load cell
 *   - HX711 #3 @ DT=18, SCK=19   → floor scale
 *   - Liquid flow sensor          → GPIO 25  (pulse input)
 *   - IR grid                     → 16 × GPIO via MCP23017 expander
 *   - Camera (OV2640)             → ESP32-CAM dedicated lines
 *   - Stepper for feeding gate    → GPIO 32, 33, 26, 27
 *
 * Author: Industry Project team
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID   = "lab-net";
const char* WIFI_PASS   = "set-in-secrets";
const char* CAGE_ID     = "cage-001";
const char* BACKEND_URL = "https://backend.lab.example/api/v1/ingest";
const char* DEVICE_TOKEN = "set-via-provisioning";

const uint32_t PUBLISH_INTERVAL_MS = 5000;
uint32_t lastPublish = 0;
uint32_t seq = 0;

void setupWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(500); }
}

void readSensors(JsonDocument& doc) {
  JsonObject sensors = doc.createNestedObject("sensors");

  JsonObject feeding = sensors.createNestedObject("feeding");
  feeding["delivered_g"]  = readUpperLoadCellGrams();
  feeding["remaining_g"]  = readRemainingBinGrams();
  feeding["wasted_g"]     = readWasteTrayGrams();
  feeding["gate_state"]   = gateIsOpen() ? "open" : "closed";

  JsonObject water = sensors.createNestedObject("water");
  water["flow_ml"] = readWaterFlowSinceLastTick();
  water["tank_ml"] = readWaterTankLevel();

  JsonObject metabolic = sensors.createNestedObject("metabolic");
  metabolic["vo2_ml_min_kg"]  = readVO2();
  metabolic["vco2_ml_min_kg"] = readVCO2();
  metabolic["rer"]            = readRER();
  metabolic["ee_kcal_h"]      = readEE();

  JsonObject behaviour = sensors.createNestedObject("behaviour");
  JsonArray grid = behaviour.createNestedArray("grid_cells");
  for (int i = 0; i < 16; i++) grid.add(readGridCell(i));
  behaviour["movement_cm"] = readMovementSinceLastTick();

  JsonObject weighing = sensors.createNestedObject("weighing");
  weighing["animal_g"] = readFloorScale();
}

bool publishPacket() {
  StaticJsonDocument<1024> doc;
  doc["cage_id"] = CAGE_ID;
  doc["ts"] = isoTimestamp();
  doc["seq"] = ++seq;
  readSensors(doc);

  String body;
  serializeJson(doc, body);

  HTTPClient http;
  http.begin(BACKEND_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);
  int code = http.POST(body);
  http.end();
  return code == 202;
}

void driveFeedingGate() {
  if (insideScheduledWindow()) openGate();
  else closeGate();
}

void setup() {
  Serial.begin(115200);
  initSensors();
  setupWifi();
  pullScheduleFromBackend();
}

void loop() {
  driveFeedingGate();
  if (millis() - lastPublish >= PUBLISH_INTERVAL_MS) {
    if (!publishPacket()) {
      Serial.println("publish failed — buffering for retry");
      bufferPacket();
    }
    lastPublish = millis();
  }
}

/* --- Sensor stubs — replace with vendor SDK calls ------------------------ */
float readUpperLoadCellGrams() { return 0.0f; }
float readRemainingBinGrams()  { return 20.0f; }
float readWasteTrayGrams()     { return 0.0f; }
bool  gateIsOpen()             { return false; }
float readWaterFlowSinceLastTick() { return 0.0f; }
float readWaterTankLevel()     { return 250.0f; }
float readVO2()                { return 38.0f; }
float readVCO2()               { return 31.0f; }
float readRER()                { return 0.82f; }
float readEE()                 { return 0.42f; }
int   readGridCell(int i)      { return 0; }
float readMovementSinceLastTick() { return 0.0f; }
float readFloorScale()         { return 25.0f; }
String isoTimestamp()          { return "2026-01-01T00:00:00Z"; }
void initSensors()             {}
void pullScheduleFromBackend() {}
bool insideScheduledWindow()   { return false; }
void openGate()                {}
void closeGate()               {}
void bufferPacket()            {}
