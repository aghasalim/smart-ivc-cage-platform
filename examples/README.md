# Examples

Small, runnable, and the same shapes the real devices use.

| File | What it shows |
|---|---|
| `quickstart.sh` | health → login → create a cage → ingest → read back → CSV export, in curl |
| `client.py` | the same in 60 lines of Python, standard library only |
| `ingest_packet.json` | one full sensor packet, every optional key present |

```bash
make up                      # in another terminal; the stack with the simulator
./examples/quickstart.sh     # needs jq
python examples/client.py
```

Both default to `http://localhost:8000`. The shell script logs in as `admin`
because creating a cage needs that role; the Python client uses `researcher`,
which can do everything else. Set `API=` and `PASS=` to point them elsewhere.

The ingest endpoint is the whole device contract: the Arduino bridge
(`device/arduino-bridge/bridge.py`), the env ingester and the simulator all
post this packet. Every key under `sensors` is optional, so a device can send
only what it has. Full reference in [`docs/api-reference.md`](../docs/api-reference.md).
