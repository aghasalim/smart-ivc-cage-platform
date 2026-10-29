# Troubleshooting

Symptoms first, then the fix. Software at the top, hardware below. If yours
isn't here, open an issue with the exact command and the exact output.

## Running the stack

| Symptom | Cause | Fix |
|---|---|---|
| Backend exits on boot with a JWT message | default dev secret in production | set a 32+ character `JWT_SECRET` in `.env` |
| `docker compose up` stalls on `backend` health | first boot creates the DB and seeds users; takes ~15 s | wait; `docker compose logs backend` should end in `Application startup complete` |
| Dashboard loads but every cage card is empty | nothing is posting readings | the simulator container should be up (`docker compose ps`); on the manual path, start `device/simulator/simulator.py` |
| Overview shows "API unreachable" | frontend built with the wrong `VITE_API_URL` | match it to where the backend actually listens, then rebuild the frontend |
| Login says invalid credentials on a fresh install | `.env` passwords changed *after* first boot | accounts are seeded once; delete the SQLite file (or the `backend_data` volume) and restart |
| Port 8000 or 5173 already in use | another process | change `BACKEND_PORT` in `.env` or stop the other process |
| Cameras page is offline | no Pi on the network | expected without hardware; otherwise set `VITE_PI_STREAM_URL` to the Pi's `:8090` |
| Behaviour column is always `resting`/`active` and never changes much | no trained model, so the rule-based fallback is in use | `make train`, then restart the backend (Docker: rebuild, it bakes the `.pkl` in) |
| `make verify` fails after you retrained | the README figures no longer match `metrics.json` | that is the point; update the README numbers, or revert the retrain |
| `make verify` says a tool is skipped | R, Go, Ruby or sqlite3 not installed | fine locally; CI has all of them |
| `pip install -r ai/requirements.txt` fails on Python 3.14 | numpy 1.26 has no 3.14 wheel | use Python 3.11 to 3.13 for the ML venv (`uv venv --python 3.12`) |

## Arduino Mega

| Symptom | Cause | Fix |
|---|---|---|
| No `/dev/ttyACM0` on the Pi | cable is power-only, or the Mega is on a clone bootloader | try another USB cable; clones show up as `/dev/ttyUSB0`, set `SERIAL_PORT` in the bridge `.env` |
| Serial shows garbage | baud mismatch | 115 200 8-N-1, no flow control |
| Load cell reads a constant or drifts wildly | not calibrated, or DT/SCK swapped | send `{"tare":1}`, then `{"calibrate":1}` for guided calibration with a known mass; check D4/D5, D6/D7, D8/D9 pairs |
| Load cell reading jumps when the pump runs | ground loop or shared supply sag | star ground to the PSU; pump on its own 12 V rail |
| DHT11 returns the same value forever with a rising `env_age` | checksum failing (cable too long, no pull-up) | keep the data lead under 20 cm; the on-board pull-up is enough at that length |
| Flow sensor counts pulses with no water moving | electrical noise on D2; we measured ~200 phantom pulses/s on the build | this is why dosing is open-loop by time; add a 10 kΩ pull-up and a 100 nF cap, or leave it unused |
| Pump runs but no water comes out | not primed, or valve closed | the valve relay (D13) must be LOW while the pump (D12) is LOW; prime by running `{"pump":"on","dur":3000}` with the outlet held low |
| Pump keeps running | safety timeout not reached | firmware caps a dose at target×4 s (20 to 300 s); if it never stops, the relay is wired to NC instead of NO |
| Relay clicks but the load doesn't switch | COM/NO miswired, or the 12 V rail is off | 12 V supply → COM, load → NO |
| Food servo twitches at boot | normal; `attach()` on D11 and the phantom D10 | harmless |
| CO₂ always 0 or 5000 | MH-Z19C not wired, or ABC left on | CO₂ is not working end to end on the reference build; see `HARDWARE.md` §3.4 |

## Raspberry Pi

| Symptom | Cause | Fix |
|---|---|---|
| O₂ reads 0 | the CH340 adapter's wire has come loose (known issue) | reseat; the bridge finds the port by VID `0x1A86` so renumbering isn't the problem |
| `ivc-arduino-bridge` loops on `401` | API key wrong or revoked | make a new key in the dashboard's Settings → API Keys and put it in `device/arduino-bridge/.env` |
| `ivc-arduino-bridge` loops on `404 Unknown cage` | `CAGE_ID` doesn't exist in the backend | create the cage in the dashboard first; the id must match exactly |
| Camera stream drops when a second viewer connects | the UVC camera is being opened twice | the camera service fans out one reader per camera; make sure nothing else holds `/dev/video*` |
| Dashboard buttons for the GPIO toy do nothing | `gpiozero`/`lgpio` missing from the *system* Python | `pi-deploy.sh` self-heals this on its next tick; or `sudo apt install python3-gpiozero python3-lgpio` |
| Something moves on its own | a stray `realtime_monitor.py` or `mouse_control.py` holding GPIO | `pi-deploy.sh` kills those every tick; check `~/pi-deploy.log` |
| A push to `main` didn't deploy | timer stopped, or the pull failed | `systemctl --user status pi-deploy.timer`; tail `~/pi-deploy.log` |
| Fan at full speed | expected under load (level 4) | `scripts/fan-max.service` pins it; remove the unit if you want it quiet |

## Printed reservoir

| Symptom | Cause | Fix |
|---|---|---|
| Weeps at the seam between layers | under-extrusion or too-thin walls | 4 perimeters, 100 % infill on the body, slight over-extrusion (flow 103 to 105 %) |
| Lid doesn't seal | PLA warped on the flange | print the lid flat side down; a 1 mm silicone or EPDM O-ring in the groove fixes most of it |
| Water tastes of plastic / the mice won't drink | wrong material | PETG or food-safe PLA, and rinse before use |
