# Bill of Materials (BOM) — IVC Cage

**Project:** Integrated Intelligent Precision Metabolic & Behavioural IVC Cage
**Revision:** 2026-06 (as-built, verified against deployed firmware & hardware)
**Currency:** EUR (indicative retail, ex-VAT; prices vary by supplier/quantity)

> This BOM reflects the **as-built** system that is live on `https://example.org`.
> Every pin/interface here was verified against the running firmware
> (`device/arduino/ivc_sensors/ivc_sensors.ino`) and the Pi bridge
> (`device/arduino-bridge/bridge.py`). See [HARDWARE.md](HARDWARE.md) for the
> full wiring diagrams and protocol details.

---

## 1. Compute & control

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 1.1 | Raspberry Pi 5 Model B | 1 | 8 GB RAM, quad-core Cortex-A76, aarch64 | — (host) | Edge compute: Arduino bridge, camera service, AI inference, Cloudflare tunnel, backend proxy | 80 |
| 1.2 | Arduino Mega 2560 R3 | 1 | ATmega2560, 54 digital I/O, 4× UART, 16 MHz | USB-B → Pi `/dev/ttyACM0` | Real-time sensor acquisition + actuator control | 35 |
| 1.3 | Raspberry Pi 5 PSU | 1 | 27 W USB-C PD (5.1 V / 5 A) | USB-C | Pi power | 13 |
| 1.4 | microSD card | 1 | 32 GB A2, Class 10 | microSD slot | Pi OS + local data | 9 |
| 1.5 | Active cooler / heatsink-fan | 1 | Official Pi 5 active cooler | Pi fan header | Thermal management (fan runs ~level 4 under load) | 6 |
| 1.6 | USB-A → USB-B cable | 1 | 0.5–1 m | Pi ↔ Mega | Serial link + Mega power | 3 |

---

## 2. Mass sensing (3× scales)

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 2.1 | HX711 24-bit load-cell ADC | 3 | 24-bit, 10/80 SPS, gain 128 | Mega DT/SCK — **D4/D5** (food), **D6/D7** (water), **D8/D9** (mouse); VCC 5 V | Amplify + digitise each strain gauge | 3×2 = 6 |
| 2.2 | Load cell — food hopper | 1 | Strain-gauge bar, ~1 kg | 4-wire (E+/E−/A+/A−) → HX711 #1 | Food mass / consumption | 4 |
| 2.3 | Load cell — water bottle | 1 | Strain-gauge bar, ~1 kg | 4-wire → HX711 #2 | Water mass / consumption | 4 |
| 2.4 | Load cell — mouse platform | 1 | Strain-gauge bar, ~500 g–1 kg | 4-wire → HX711 #3 | Animal body-weight | 4 |

> Calibration is stored in the Mega's EEPROM (magic `0x49564341`); `t` tares,
> `c` runs a guided calibration with a known reference weight.

---

## 3. Environmental sensing

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 3.1 | DHT11 temp/humidity module | 1 | 0–50 °C ±2 °C, 20–90 %RH; 3-pin (on-board pull-up) | 1-wire data → Mega **D3**; 5 V | Cage temperature & humidity | 3 |
| 3.2 | MH-Z19C NDIR CO₂ sensor | 1 | 0–5000 ppm, UART 9600, 3.3 V logic, 5 V supply | UART — Mega Serial3 **D14/D15** *or* Pi `/dev/serial0` (GPIO14/15) | Metabolic CO₂ (VCO₂ proxy) | 22 |
| 3.3 | JXW-02 O₂ sensor + CH340 USB board | 1 | Electrochemical, 0–25 % O₂, UART 9600 | **USB** → Pi (`/dev/ttyUSB*`, CH340 VID `0x1A86`, auto-detected) | Metabolic O₂ (VO₂ proxy); reads ~20.9 % in air | 35 |

> O₂ frame: `FF 01 07 01 <hi> <lo> 00 00 00 <csum>` → `O₂% = (hi<<8 | lo) / 10`.
> The bridge validates header **and** checksum before accepting a reading.

---

## 4. Water delivery & flow (closed-loop dosing)

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 4.1 | 2-channel relay module | 1 | 5 V coil, opto-isolated, **active-LOW**, 10 A contacts | IN1 → Mega **D12** (pump), IN2 → **D13** (valve); VCC 5 V | Switch 12 V pump + solenoid | 4 |
| 4.2 | DC water pump | 1 | 12 V, self-priming diaphragm | 12 V via relay CH1 (COM/NO) | Deliver dosed water | 9 |
| 4.3 | Solenoid valve (normally-closed) | 1 | 12 V, NC | 12 V via relay CH2 (COM/NO) | Siphon prevention — closes the instant the pump stops | 7 |
| 4.4 | YF-S401 flow sensor | 1 | 0.3–6 L/min, **7.5 pulses/mL**, Hall-effect | Signal → Mega **D2** (interrupt); 5 V | Measure delivered volume; closed-loop dose feedback | 5 |
| 4.5 | 12 V power supply | 1 | ≥ 2 A (3 A recommended) | Barrel/screw → relay commons | Pump + valve power | 10 |
| 4.6 | Silicone tubing + reservoir + fittings | 1 set | food-grade, ~6 mm ID | inline: reservoir → pump → valve → flow sensor → cage | Water path | 8 |

> Dosing is exact-volume with **early-stop coast compensation**
> (`target − DOSE_COAST_ML`) and a safety timeout; the valve closes on stop to
> defeat gravity/siphon overshoot.

---

## 5. Feeding

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 5.1 | Micro servo (food gate) | 1 | SG90/MG90-class, 5 V PWM | Signal → Mega **D11**; 5 V | Open/close the food dispenser gate | 3 |

---

## 6. Vision / behaviour

| # | Component | Qty | Key spec | Interface / connection | Purpose | Est. € |
|---|-----------|:--:|----------|------------------------|---------|------:|
| 6.1 | USB camera (UVC) | 3 | Generic UVC (`0bda:3035`), MJPEG | USB → powered hub → Pi | Multi-angle behaviour capture for YOLO detection + tracking | 3×12 = 36 |
| 6.2 | Powered USB 2.0 hub | 1 | 4-port, externally powered (Genesys `05e3`) | Pi USB | Supply + aggregate the cameras | 12 |

---

## 7. Wiring & misc

| # | Component | Qty | Key spec | Purpose | Est. € |
|---|-----------|:--:|----------|---------|------:|
| 7.1 | Dupont jumper wires | 1 pack | M-M / M-F / F-F | Signal wiring | 5 |
| 7.2 | Breadboard / perfboard + rails | 1 | 830-pt + power rails | 5 V/GND distribution to sensors | 4 |
| 7.3 | Enclosure / mounting hardware | 1 set | standoffs, brackets | Mount Pi/Mega/relays/sensors to cage | 10 |
| 7.4 | Ferrules / heat-shrink / cable ties | 1 set | — | Reliable, serviceable terminations | 4 |

---

## 8. Hardware subtotal (indicative)

| Subsystem | Est. € |
|-----------|------:|
| Compute & control | 146 |
| Mass sensing | 22 |
| Environmental sensing | 60 |
| Water delivery & flow | 43 |
| Feeding | 3 |
| Vision | 48 |
| Wiring & misc | 27 |
| **Hardware total** | **≈ 349** |

---

## 9. Cloud / recurring (OPEX)

**Everything is self-hosted on the Raspberry Pi** — the FastAPI backend, the
SQLite database, the React dashboard (served by FastAPI as static files), and
the camera service all run as `systemd` units on the Pi, exposed via a
Cloudflare Tunnel. There is **no Vercel and no Railway** in the stack.

| Service | Plan | Purpose | Est. €/mo |
|---------|------|---------|----------:|
| Cloudflare | Free + Tunnel | `example.org` DNS/TLS + zero-inbound-port tunnel to the Pi | 0 |
| Domain | example.org | annual ~ €10/yr | ~1 |
| Google Colab | Pro (optional) | YOLO training / live inference offload | ~10 |
| **Recurring** | | | **≈ 11/mo** |

> Self-hosting on the Pi means **no monthly hosting bill** — the only hard
> recurring cost is the domain. The shipped device is **Pi 5 + Arduino Mega
> 2560** with the full sensor suite above (not the original Pi 4 + single IR
> USB-camera concept), and it hosts the entire software stack itself.

---

## 10. Pin map cross-reference (Arduino Mega 2560)

| Pin | Net | Component |
|-----|-----|-----------|
| D2 | Flow pulse (INT) | YF-S401 |
| D3 | 1-wire | DHT11 |
| D4 / D5 | HX711 DT / SCK | Food load cell |
| D6 / D7 | HX711 DT / SCK | Water load cell |
| D8 / D9 | HX711 DT / SCK | Mouse load cell |
| D11 | Servo PWM | Food gate servo |
| D12 | Relay IN1 (active-LOW) | Water pump |
| D13 | Relay IN2 (active-LOW) | Solenoid valve |
| D14 / D15 | Serial3 TX/RX | MH-Z19C CO₂ (optional path) |
| 5V / GND | Power rails | All 5 V sensors/modules |

O₂ (JXW-02) and the cameras attach to the **Raspberry Pi via USB**, not the Mega.
See [HARDWARE.md](HARDWARE.md) §3 for full wiring and §6 for power/safety analysis.
