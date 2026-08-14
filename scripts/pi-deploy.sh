#!/usr/bin/env bash
# pi-deploy.sh — Pull latest from GitHub and redeploy everything to the live
# directories on the Pi. Idempotent: safe to run on a no-op pull.
#
# Run with no args from cron; or:
#   pi-deploy.sh --force          rebuild + restart everything regardless of diff
#   pi-deploy.sh --notify '<url>'  POST to <url> on success (for webhooks)
set -euo pipefail

REPO=${REPO:-$HOME/IndustryProject}
BACKEND=$HOME/ivc-backend
CAMERAS=$HOME/camera-stream
INGESTER=$HOME/env-ingester
INFERENCE=$HOME/pi-inference
LOG=$HOME/pi-deploy.log

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

log()  { echo "$(date -Is) $*" | tee -a "$LOG"; }
fail() { log "ERROR: $*"; exit 1; }

cd "$REPO" || fail "repo not found at $REPO"

# 00. KILL ROGUE GPIO USERS (runs every tick, BEFORE everything else) ---------
# realtime_monitor.py / mouse_control.py / test_pins_alive.py / behavior_analysis.py
# all touch GPIO 17/22/23/27 independently of the camera service. If one of them
# was launched manually (nohup, screen, tmux, &) it will hold pins HIGH and the
# toy will move 'mindlessly'. The camera service itself runs from
# ~/camera-stream/server.py, so we whitelist exactly that path. Any other
# python3 process matching the rogue patterns gets SIGKILL'd.
ROGUE_PATTERNS=(
  'realtime_monitor\.py'
  'mouse_control\.py'
  'test_pins_alive\.py'
  'behavior_analysis\.py'
  'online_classifier'
)
for pat in "${ROGUE_PATTERNS[@]}"; do
  # -f matches against full command line. We must NOT match our own deploy
  # script, our own grep, or the camera-stream server.
  pids=$(pgrep -f "$pat" 2>/dev/null | grep -v "$$" || true)
  for pid in $pids; do
    # Read the actual cmdline to be extra careful before killing.
    cmd=$(tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null || echo "")
    # Skip our own deploy script, our own grep, and the camera-stream server.
    if [[ "$cmd" == *"pi-deploy.sh"* ]] || [[ "$cmd" == *"pgrep"* ]] || \
       [[ "$cmd" == *"camera-stream/server.py"* ]]; then
      continue
    fi
    log "Killing rogue GPIO user PID=$pid: $cmd"
    kill -9 "$pid" 2>/dev/null || true
  done
done

# 0a. GPIO self-heal (runs every tick, BEFORE the no-op gate) -----------------
# The camera-stream service runs under /usr/bin/python3 (system Python, NOT
# the backend venv). If gpiozero + lgpio aren't importable from THAT python,
# MouseController falls back to dry-run and dashboard buttons don't actually
# drive GPIO. We also handle the case where the libs ARE importable but the
# running service was started before they were installed, so it's still in
# dry-run mode → just restart it.
need_gpio_install=0
need_camera_restart=0
if ! /usr/bin/python3 -c "from gpiozero import DigitalOutputDevice; import lgpio" 2>/dev/null; then
  need_gpio_install=1
  need_camera_restart=1
fi
# Quick probe: ask the live service what backend it's currently using.
running_backend=$(curl -s --max-time 2 http://127.0.0.1:8090/mouse/status 2>/dev/null \
  | /usr/bin/python3 -c "import json,sys;print(json.load(sys.stdin).get('backend',''))" 2>/dev/null \
  || echo "")
if [[ -n "$running_backend" && "$running_backend" != "gpiozero" && "$running_backend" != "rpi.gpio" ]]; then
  need_camera_restart=1
fi

if [[ $need_gpio_install -eq 1 ]]; then
  log "GPIO libs not importable by /usr/bin/python3 — installing"
  # liblgpio1 is the C library python-lgpio binds to. The Python wheel alone
  # isn't enough on Debian Bookworm — the C lib must be present.
  sudo -n apt-get install -y --no-install-recommends liblgpio1 python3-lgpio gpiozero 2>>"$LOG" || \
    log "WARN: apt-get install for GPIO libs failed (no NOPASSWD?)"
  # Belt-and-suspenders: also ensure the Python wheels land in the system
  # site-packages (Bookworm marks the env 'externally managed' → --break-system-packages).
  /usr/bin/python3 -m pip install --quiet --break-system-packages gpiozero lgpio 2>>"$LOG" || \
    log "WARN: pip install for gpiozero/lgpio failed"
  if /usr/bin/python3 -c "from gpiozero import DigitalOutputDevice; import lgpio" 2>/dev/null; then
    log "GPIO libs now available"
  else
    log "ERROR: GPIO libs STILL missing after install — needs manual fix"
    # If we can't fix it, restarting won't help. Skip restart.
    need_camera_restart=0
  fi
fi

if [[ $need_camera_restart -eq 1 ]]; then
  log "Restarting ivc-cameras so MouseController re-probes GPIO backend (was: ${running_backend:-unknown})"
  pkill -9 -f 'python3.*server\.py' 2>/dev/null || true
  sleep 1
  systemctl --user restart ivc-cameras 2>>"$LOG" || \
    log "WARN: could not restart ivc-cameras"
fi

# 0b-pre. NOPASSWD sudoers for power + fan control --------------------------------
# The camera-stream service shells out to several root commands via `sudo -n`:
#   /system/reboot, /system/shutdown        → reboot / shutdown
#   /system/fan (Auto/Off/Low/Med/High)     → systemctl stop/start/disable/mask
#                                              fan-max.service, plus a drop-in
#                                              override written with mkdir + tee.
# Without NOPASSWD entries for ALL of these, `sudo -n` fails with "a password is
# required" and the fan neuter can't run — so fan-max.service keeps forcing the
# fan to max and Auto mode never takes effect.  We scope the entry to the exact
# binaries the service needs.  (Acceptable on this isolated lab device.)
SUDOERS_DROP=/etc/sudoers.d/ivc-power
# v2 marker bumped so existing Pis (which only have the reboot/shutdown line)
# get the expanded set rewritten on the next deploy tick.
SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: /sbin/reboot, /sbin/shutdown, /usr/sbin/reboot, /usr/sbin/shutdown, /usr/bin/systemctl, /bin/systemctl, /usr/bin/mkdir, /bin/mkdir, /usr/bin/tee, /bin/tee, /usr/bin/rm, /bin/rm"
# Detect by a v2-specific token (systemctl) so the old reboot-only line is
# upgraded in place. Use cat (readable without root on Debian) — sudo grep was
# itself writing the log every tick.
if ! cat "$SUDOERS_DROP" 2>/dev/null | grep -qF "/usr/bin/systemctl"; then
  echo "$SUDOERS_LINE" | sudo -n tee "$SUDOERS_DROP" >/dev/null 2>&1 && \
    { sudo -n chmod 0440 "$SUDOERS_DROP" 2>/dev/null || true; \
      log "sudoers drop-in written (v2: power + fan): $SUDOERS_DROP"; } || \
    log "WARN: could not write sudoers drop-in (needs manual: echo '$SUDOERS_LINE' | sudo tee $SUDOERS_DROP)"
fi

# 0b. Fan-max service (runs every tick, BEFORE the no-op gate) ------------------
# Install / refresh the systemd unit idempotently — only rewrites the file
# when it differs from the in-repo copy. Runs every tick so it self-heals
# if the unit gets removed. Uses sudo -n (NOPASSWD); silently skips if not
# allowed.
#
# IMPORTANT: this script no longer FORCE-STARTS fan-max.service. The Pi's
# fan is user-controlled now via the /system/fan endpoint (which can mask
# the service to keep the fan at lower levels). If the cron blindly started
# fan-max here it would override the user's last setting every 60 s.
# We only auto-enable on initial install when the unit doesn't exist yet.
FAN_UNIT_SRC="$REPO/scripts/fan-max.service"
FAN_UNIT_DST="/etc/systemd/system/fan-max.service"
if [[ -f "$FAN_UNIT_SRC" ]]; then
  if [[ ! -f "$FAN_UNIT_DST" ]]; then
    # First install — enable so the fan defaults to "Max" until a user
    # explicitly changes it via the dashboard.
    if sudo -n tee "$FAN_UNIT_DST" >/dev/null < "$FAN_UNIT_SRC" 2>/dev/null; then
      log "fan-max.service installed (first time)"
      sudo -n systemctl daemon-reload 2>/dev/null || true
      sudo -n systemctl enable --now fan-max.service 2>/dev/null || \
        log "WARN: could not enable fan-max.service"
    fi
  elif ! cmp -s "$FAN_UNIT_SRC" "$FAN_UNIT_DST" 2>/dev/null; then
    # Unit file content changed — refresh, but DO NOT force start/enable
    # (preserves user's masked-off state if they chose a lower level).
    if sudo -n tee "$FAN_UNIT_DST" >/dev/null < "$FAN_UNIT_SRC" 2>/dev/null; then
      log "fan-max.service unit updated (not auto-started — user controls state)"
      sudo -n systemctl daemon-reload 2>/dev/null || true
    fi
  fi
  # No "ensure running" branch — the user's last /system/fan call wins.
fi

# 1. Fetch and detect changes ---------------------------------------------------
git fetch --quiet origin main
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)

if [[ "$old" == "$new" && $FORCE -eq 0 ]]; then
  exit 0   # nothing to do, stay quiet (cron-friendly)
fi

log "Pulling $old → $new"
git reset --hard origin/main >/dev/null

# Detect which subsystems changed
CHANGED=$(git diff --name-only "$old" "$new" 2>/dev/null || echo "ALL")
need_backend=0; need_cameras=0; need_ingester=0; need_frontend=0; need_pydeps=0
need_inference=0; need_inference_deps=0; need_bridge=0
if [[ $FORCE -eq 1 ]]; then
  need_backend=1; need_cameras=1; need_ingester=1; need_frontend=1; need_pydeps=1
  need_inference=1; need_inference_deps=1; need_bridge=1
else
  grep -q '^backend/'          <<<"$CHANGED" && need_backend=1
  grep -q '^backend/requirements' <<<"$CHANGED" && need_pydeps=1
  grep -q '^device/camera-stream/' <<<"$CHANGED" && need_cameras=1
  grep -q '^device/env-ingester/' <<<"$CHANGED" && need_ingester=1
  grep -q '^device/pi-inference/' <<<"$CHANGED" && need_inference=1
  grep -q '^device/pi-inference/requirements' <<<"$CHANGED" && need_inference_deps=1
  grep -q '^device/arduino-bridge/' <<<"$CHANGED" && need_bridge=1
  grep -q '^frontend/'        <<<"$CHANGED" && need_frontend=1
fi

log "Subsystem changes: backend=$need_backend cameras=$need_cameras ingester=$need_ingester frontend=$need_frontend pydeps=$need_pydeps inference=$need_inference bridge=$need_bridge"

# 2. Backend --------------------------------------------------------------------
if [[ $need_backend -eq 1 ]]; then
  log "Syncing backend code"
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'data/' \
    "$REPO/backend/app/" "$BACKEND/app/"
  if [[ $need_pydeps -eq 1 ]] && [[ -f "$REPO/backend/requirements.txt" ]]; then
    log "Installing Python deps"
    "$HOME/ivc-venv/bin/pip" install --quiet -r "$REPO/backend/requirements.txt" || log "WARN: pip install failed (continuing)"
  fi
  systemctl --user restart ivc-backend
fi

# 3. Camera service -------------------------------------------------------------
if [[ $need_cameras -eq 1 ]]; then
  log "Syncing camera-stream code"
  rsync -a "$REPO/device/camera-stream/server.py" "$CAMERAS/server.py"
  if [[ -f "$REPO/device/camera-stream/requirements.txt" ]]; then
    rsync -a "$REPO/device/camera-stream/requirements.txt" "$CAMERAS/requirements.txt"
  fi
  # Install camera-stream Python deps system-wide if a requirements.txt exists.
  # This is how gpiozero / lgpio land on the Pi for the new /mouse/* GPIO
  # endpoints. --break-system-packages is required on Debian Bookworm+ to
  # write into the site-packages that /usr/bin/python3 uses (the venv is for
  # the backend only; the camera service runs against system Python).
  if [[ -f "$CAMERAS/requirements.txt" ]]; then
    log "Installing camera-stream Python deps (system-wide)"
    pip install --quiet --break-system-packages \
      -r "$CAMERAS/requirements.txt" 2>&1 | tee -a "$LOG" || \
      log "WARN: camera-stream pip install reported errors (continuing)"
  fi
  # Kill any stale python3 process still holding port 8090 before restart.
  # Without this, systemctl restart may leave the old process running and the
  # new service fails to bind (shows "activating → unknown" in the deploy log).
  pkill -9 -f 'python3.*server\.py' 2>/dev/null || true
  sleep 1
  systemctl --user restart ivc-cameras
fi

# 4. Arduino serial bridge ------------------------------------------------------
BRIDGE=$HOME/arduino-bridge
if [[ $need_bridge -eq 1 ]]; then
  log "Syncing arduino-bridge code"
  mkdir -p "$BRIDGE"
  rsync -a --delete \
    --exclude '.env' --exclude '*.log' --exclude '__pycache__' \
    "$REPO/device/arduino-bridge/" "$BRIDGE/"

  # Install Python deps system-wide (pyserial + requests)
  if [[ -f "$BRIDGE/requirements.txt" ]]; then
    pip install --quiet --break-system-packages \
      -r "$BRIDGE/requirements.txt" 2>&1 | tee -a "$LOG" || \
      log "WARN: arduino-bridge pip install reported errors (continuing)"
  fi

  # Install / refresh the systemd user unit idempotently.
  BRIDGE_UNIT_SRC="$BRIDGE/ivc-arduino-bridge.service"
  BRIDGE_UNIT_DST="$HOME/.config/systemd/user/ivc-arduino-bridge.service"
  if [[ -f "$BRIDGE_UNIT_SRC" ]]; then
    mkdir -p "$(dirname "$BRIDGE_UNIT_DST")"
    if ! cmp -s "$BRIDGE_UNIT_SRC" "$BRIDGE_UNIT_DST" 2>/dev/null; then
      cp "$BRIDGE_UNIT_SRC" "$BRIDGE_UNIT_DST"
      systemctl --user daemon-reload || true
      log "ivc-arduino-bridge.service installed/updated"
    fi
  fi

  # Start only if .env with a real API_KEY exists
  if [[ -f "$BRIDGE/.env" ]] && grep -qE '^API_KEY=ivc_' "$BRIDGE/.env"; then
    systemctl --user enable ivc-arduino-bridge 2>/dev/null || true
    systemctl --user restart ivc-arduino-bridge
  else
    log "  → ivc-arduino-bridge NOT started: drop API_KEY into $BRIDGE/.env first"
    log "    (cp $BRIDGE/env.example $BRIDGE/.env  &&  nano $BRIDGE/.env)"
  fi
fi

# 4b. Env ingester ---------------------------------------------------------------
if [[ $need_ingester -eq 1 ]]; then
  log "Syncing env-ingester code"
  rsync -a "$REPO/device/env-ingester/env_ingester.py" "$INGESTER/env_ingester.py"
  systemctl --user restart ivc-env-ingester
fi

# 4b. On-Pi YOLOv8 inference (replaces Colab roundtrip) -------------------------
if [[ $need_inference -eq 1 ]]; then
  log "Syncing pi-inference code"
  mkdir -p "$INFERENCE"
  # Preserve user-created .env (gitignored) — exclude it from --delete sweep.
  # The small REQUIRED state model (state_classifier_v3.pkl, ~16 MB) now travels
  # via git, so let it sync. The large OPTIONAL stereotypy model
  # (behavior_classifier.pkl, ~85 MB) and NCNN export dirs are deployed
  # out-of-band via scp (too big for git), so they must NOT be wiped.
  rsync -a --delete \
    --exclude '.env' --exclude '*.log' --exclude '__pycache__' \
    --exclude 'behavior_classifier.pkl' --exclude '*_ncnn_model' \
    "$REPO/device/pi-inference/" "$INFERENCE/"

  # Heavy deps (torch CPU ARM, ~400 MB) — only install when requirements.txt
  # changed OR no install marker exists yet.
  # IMPORTANT: install with `/usr/bin/python3 -m pip`, NOT bare `pip`. The
  # ivc-inference.service runs ExecStart=/usr/bin/python3, so deps MUST land in
  # that interpreter's site-packages. Bare `pip` may resolve to a different
  # python (venv / pyenv) → service crashes with ModuleNotFoundError: cv2.
  if [[ $need_inference_deps -eq 1 ]] || \
     ! /usr/bin/python3 -c "import ultralytics, cv2" 2>/dev/null; then
    log "Installing pi-inference Python deps (this may take a few minutes)"
    /usr/bin/python3 -m pip install --quiet --break-system-packages \
      -r "$INFERENCE/requirements.txt" 2>&1 | tee -a "$LOG" || \
      log "WARN: pi-inference pip install reported errors (continuing)"
  fi

  # Install/refresh the systemd user unit idempotently.
  UNIT_SRC="$INFERENCE/ivc-inference.service"
  UNIT_DST="$HOME/.config/systemd/user/ivc-inference.service"
  if [[ -f "$UNIT_SRC" ]]; then
    mkdir -p "$(dirname "$UNIT_DST")"
    if ! cmp -s "$UNIT_SRC" "$UNIT_DST" 2>/dev/null; then
      cp "$UNIT_SRC" "$UNIT_DST"
      systemctl --user daemon-reload || true
      log "ivc-inference.service installed/updated"
    fi
  fi

  # Only start the service if the user has populated .env with an API key.
  if [[ -f "$INFERENCE/.env" ]] && grep -qE '^API_KEY=ivc_' "$INFERENCE/.env"; then
    systemctl --user enable ivc-inference 2>/dev/null || true
    systemctl --user restart ivc-inference
  else
    log "  → ivc-inference NOT started: drop API_KEY into $INFERENCE/.env first"
    log "    (cp $INFERENCE/env.example $INFERENCE/.env  &&  nano $INFERENCE/.env)"
  fi
fi

# 5. Frontend -------------------------------------------------------------------
if [[ $need_frontend -eq 1 ]]; then
  log "Building frontend"
  cd "$REPO/frontend"
  # npm ci is faster than install when lockfile is up-to-date; fall back to install
  if ! npm ci --no-audit --no-fund --prefer-offline 2>>"$LOG" >/dev/null; then
    log "npm ci failed, falling back to npm install"
    npm install --no-audit --no-fund --prefer-offline 2>>"$LOG" >/dev/null
  fi
  npm run build 2>>"$LOG" >/dev/null || fail "frontend build failed — see $LOG"
  log "Syncing frontend dist → static/"
  rsync -a --delete "$REPO/frontend/dist/" "$BACKEND/static/"
  cd "$REPO"
fi

# Wait briefly so services settle, then report status
sleep 2
for svc in ivc-backend ivc-cameras ivc-env-ingester ivc-inference ivc-arduino-bridge; do
  state=$(systemctl --user is-active "$svc" 2>/dev/null || echo unknown)
  log "  $svc: $state"
done
fan_state=$(sudo -n systemctl is-active fan-max.service 2>/dev/null || echo unknown)
log "  fan-max:         $fan_state"

log "Deploy complete ($(git rev-parse --short HEAD))"
