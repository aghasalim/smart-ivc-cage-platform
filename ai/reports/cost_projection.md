# Deployment Cost Projection, IVC Cage Behavioral Monitoring

## Per-cage hardware cost
| Component | Estimated cost |
|---|---|
| Raspberry Pi 4 (4GB) | €65 |
| Infrared USB camera (1280×720) | €45 |
| MicroSD 64GB (model storage) | €12 |
| Power supply + case | €15 |
| **Total per cage** | **€137** |

## Software cost
All software is open source, €0 licensing cost.
Dependencies: Python, OpenCV, scikit-learn, pandas, pytesseract.

## Storage cost
Per cage per month: ~200MB CSV data, negligible on any lab server.

## Scalability
Each additional cage adds €137 hardware + one additional realtime_monitor.py instance.
No additional software cost. Dashboard aggregates all cage CSVs automatically.

## Processing requirements
Raspberry Pi 4 handles real-time inference at 25 FPS comfortably.
No GPU required, all models are CPU-only (sklearn).

---

## UPDATE, Shipped hardware reconciliation (2026-06-11)

> **Note:** The original projection above was written against the early Pi 4 + IR USB camera design.
> The device that was actually built and shipped differs on hardware and adds cloud OPEX.

### Shipped device (actual)

| Component | Shipped part | Change vs. original plan |
|---|---|---|
| SBC | Raspberry Pi 5 (4 GB) | Upgraded from Pi 4 |
| Microcontroller | Arduino Mega 2560 | New, handles all sensor I/O |
| Load cells | 3× HX711 + load cell (food/water/animal weight) | New, replaces camera-based weight estimation |
| Flow sensor | YF-S401 (water flow) | New |
| Temperature/humidity | DHT11 | New |
| Actuators | Pump relay, valve relay, food servo | New |
| O2 sensor | USB electrochemical O2 sensor | New, replaces IR USB camera |
| Camera | Removed from shipped unit | Pi 4 + IR USB camera plan abandoned |

The per-cage hardware BOM cost is higher than the original €137 estimate; a revised BOM is tracked in`docs/HARDWARE.md`.

### Recurring OPEX (self-hosted on the Pi)

The entire software stack, FastAPI backend, SQLite database, React dashboard,
and camera service, is **self-hosted on the Raspberry Pi** and exposed via a
Cloudflare Tunnel. There is **no Vercel and no Railway** (both were removed); the
Pi is the only host.

| Service | Plan | Estimated monthly cost |
|---|---|---|
| Cloudflare (tunnel + DNS) | Free tier | €0 |
| Domain (example.org) | annual ~€10/yr | ~€1 |
| Google Colab (optional, YOLO training) | Pro | ~€10 |
| **Total OPEX (per month)** | | **~€1,€11** |

These costs are per-project (not per cage) and are constant regardless of the
number of cages monitored. Self-hosting on the Pi eliminates the previous
Vercel + Railway hosting bill.
