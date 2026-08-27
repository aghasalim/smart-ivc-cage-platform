# On-Pi YOLOv8 inference

Local replacement for the Colab notebook. Runs the trained mouse detector
(`best.pt`) directly on the Raspberry Pi so the dashboard's "Live AI Detection"
panel updates with **loopback** latency instead of a Colab cloud round-trip.

## Why

End-to-end latency comparison:

| Path                       | Inference | Per-frame | End-to-end |
|----------------------------|-----------|-----------|------------|
| Colab pipeline             | T4 GPU    | ~10 ms    | 1 to 3 s (cloud round-trip dominates) |
| On-Pi torch CPU (.pt @ 640)| Pi 5 CPU  | ~250 ms   | ~400 ms |
| **On-Pi NCNN (@ 480)** ✅  | Pi 5 CPU  | **~70 ms**| **~150 ms** |
| On-Pi NCNN (@ 320, max fps)| Pi 5 CPU  | ~40 ms    | ~120 ms |

`inference.py` auto-exports`.pt → NCNN` on first boot, so this is the
default path, no extra setup. NCNN is Tencent's ARM-optimised engine; on a
Pi 5's Cortex-A76 it has hand-tuned NEON kernels that the torch CPU backend
doesn't use, hence the 3-4× speedup.

## First-time setup (run on the Pi)

```bash
cd ~/pi-inference   # rsynced here automatically by pi-deploy.sh

# Install Python deps system-wide. First install pulls torch CPU ARM
# (~400 MB) and takes a few minutes on a Pi 5.
pip install --break-system-packages -r requirements.txt

# Configure the API key
cp env.example .env
nano .env   # set API_KEY=ivc_…  (Settings → API Keys → Create)

# Install the systemd user unit (one-time)
mkdir -p ~/.config/systemd/user
cp ivc-inference.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ivc-inference

# Check it
systemctl --user status ivc-inference
tail -f ~/pi-inference/inference.log
```

After the first deploy,`pi-deploy.sh` rsyncs new code into`~/pi-inference/`
and restarts the service automatically when anything in`device/pi-inference/`
changes on`main`.

## Configuration

All knobs are environment variables, set them in`.env`:

| Var            | Default                  | Notes |
|----------------|--------------------------|-------|
|`API_KEY`      | _(required)_             | Backend API key, starts with`ivc_` |
|`CAMERA_URL`   |`http://127.0.0.1:8090`  | Local camera-stream service |
|`BACKEND_URL`  |`https://example.org`    | Dashboard backend, Cloudflare Tunnel → Pi |
|`MODEL_PATH`   |`./best.pt`              | Path to trained weights. NCNN dir auto-derived as`best_ncnn_model/` |
|`CONF_THRESH`  |`0.40`                   | YOLO confidence threshold |
|`YOLO_IMGSZ`   |`480`                    | Inference resolution, drop to`320` for ~2× speed; bump to`640` for smallest mice |
|`USE_NCNN`     |`1`                      | Set to`0` to force the slower torch backend |
|`LOOP_DELAY_S` |`0`                      | Min seconds between camera-loop passes |
