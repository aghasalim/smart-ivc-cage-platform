# Reference firmware

Reference Arduino/ESP32 sketch for the cage controller. **Not** required for
the simulator or backend to run, this file documents the embedded contract.

The physical controller is expected to:

1. Read each sensor on its native bus (I²C, SPI, 1-Wire, GPIO).
2. Compose one JSON packet matching`docs/api-reference.md` § Ingest.
3. POST to`${BACKEND_URL}/api/v1/ingest` over HTTPS with a Bearer token
   that uniquely identifies the cage.
4. Drive the feeding-gate stepper motor according to the schedule pulled
   from`${BACKEND_URL}/api/v1/cages/{id}/schedule`.

See`cage_controller.ino` for a working scaffold.
