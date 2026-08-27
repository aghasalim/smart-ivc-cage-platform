# IVC Cage, Pi camera streaming service

Lightweight MJPEG streamer that exposes USB cameras attached to the Raspberry Pi as
HTTP streams. Powers the`/cameras` page in the dashboard.

## Endpoints (port`8090`)

| Method | Path | Description |
|---|---|---|
|`GET` |`/health` |`{"status":"ok","cameras":N}` |
|`GET` |`/api/cameras` | JSON list of detected USB cameras |
|`GET` |`/stream/<id>` | MJPEG live stream |
|`GET` |`/snapshot/<id>` | Single JPEG frame |
|`GET` |`/` | Simple diagnostic grid |

## Install on the Pi

```bash
# Copy files
scp device/camera-stream/server.py            pi:~/camera-stream/server.py
scp device/camera-stream/ivc-cameras.service  pi:~/

# On the Pi: install the systemd unit
sudo mv ~/ivc-cameras.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ivc-cameras.service
sudo systemctl status ivc-cameras.service
```

## Dependencies

Already present on Raspberry Pi OS Bookworm:
-`python3` (≥ 3.11)
-`python3-flask`
-`ffmpeg`
-`v4l-utils` (`v4l2-ctl`)

No`cv2`/OpenCV needed, capture goes through`ffmpeg` for zero-copy MJPEG passthrough.

## Network layout

Pi is configured at static`192.168.50.2/24` on`eth0`, directly cabled to the
dev Mac's USB-Ethernet adapter (`en7` at`192.168.50.1/24`). The dashboard's
`VITE_PI_STREAM_URL` defaults to`http://192.168.50.2:8090`.

Because this address is on a direct link, the`/cameras` page only works when
running the dashboard locally (Vite dev server) on the Mac that has the LAN
cable plugged in.
