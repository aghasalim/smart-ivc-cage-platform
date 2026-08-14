"""Generate synthetic Timed Restricted Feeding (TRF) data.

System description (Feature 7 — Timed Restricted Feeding Function):
  Feeding window: 23:00 - 00:30 (90 min permitted access)
  Gate is closed/locked at all other times
  Matches nocturnal feeding rhythm of mice; prevents ad-libitum biting

Schema (one row = one 5-min sample for one cage):
  ts                       ISO 8601 UTC timestamp of the sample
  cage_id                  Cage identifier
  mouse_id                 Mouse identifier in that cage
  schedule_id              Schedule template applied (TRF-23-0030)
  within_window            True iff ts falls inside [23:00, 00:30)
  minutes_to_window_open   How many minutes until the next opening (0 if open)
  gate_state               "open" | "closed" | "locked"
                             open   - actively accessible (within window)
                             closed - shut but unlocked (within 60 min of next open)
                             locked - fully locked (>60 min from next open)
  servo_angle_deg          Servo angle: 90 = open, 0 = closed/locked
  mouse_at_gate            Camera detected mouse near the gate during this slot
  feeding_event            True iff the mouse ate during this 5-min slot
  feed_duration_s          Seconds of confirmed feeding within this slot
  food_consumed_mg         Milligrams consumed during this slot
  hopper_weight_g          Food hopper weight (decreases as mouse eats; auto-refilled <5 g)
  cage_temp_c              Cage air temperature
  cage_humidity_pct        Cage relative humidity
  notes                    Free-text marker for occasional events (mostly null)

Modelling assumptions:
  - 3 cages, each with one mouse of slightly different appetite
  - 5-min sampling -> 288 samples/day/cage
  - Each night: 95 % chance mouse eats; if so, 1-3 visits inside the window
  - Each visit: 5-25 min duration; total nightly intake 3-5 g (scaled by appetite)
  - Diurnal temperature swing of ~ +/- 0.4 C; humidity inverse to temperature
  - Hopper refills to 50 g when below 5 g (simulated tech check)
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Config ───────────────────────────────────────────────────────────────────
N_ROWS         = 11_000
INTERVAL_MIN   = 5
START_TS       = datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc)
SCHEDULE_ID    = "TRF-23-0030"
DEFAULT_OUT    = Path("/Users/salim/Downloads/trf_feeding_synthetic.json")
SEED           = 42

CAGES = [
    {"cage_id": "CAGE-01", "mouse_id": "M-A12",
     "baseline_temp_c": 22.1, "baseline_hum_pct": 47.0,
     "appetite": 1.00, "hopper_g": 50.0},
    {"cage_id": "CAGE-02", "mouse_id": "M-B07",
     "baseline_temp_c": 22.3, "baseline_hum_pct": 46.5,
     "appetite": 0.85, "hopper_g": 50.0},
    {"cage_id": "CAGE-03", "mouse_id": "M-C19",
     "baseline_temp_c": 22.0, "baseline_hum_pct": 48.2,
     "appetite": 1.15, "hopper_g": 50.0},
]

random.seed(SEED)


# ── Feeding window predicates ────────────────────────────────────────────────
def in_feeding_window(t: datetime) -> bool:
    """True if t is inside [23:00, 00:30) — the 90-min open window."""
    if t.hour == 23:
        return True
    if t.hour == 0 and t.minute < 30:
        return True
    return False


def minutes_to_window_open(t: datetime) -> int:
    """Minutes until the next 23:00 opening (0 while inside the window)."""
    if in_feeding_window(t):
        return 0
    today_23 = t.replace(hour=23, minute=0, second=0, microsecond=0)
    next_23 = today_23 if t < today_23 else today_23 + timedelta(days=1)
    return int((next_23 - t).total_seconds() / 60)


# ── Per-cage per-night feeding plan ──────────────────────────────────────────
def plan_visits(window_start: datetime, appetite: float):
    """Return list of (visit_start_dt, visit_end_dt, mg_consumed).

    Each cage gets its own plan per night; visits never overlap (we split the
    90-min window into N equal sub-windows, one per visit).
    """
    if random.random() < 0.05:          # ~5 % skipped nights
        return []
    n_visits     = random.choices([1, 2, 3], weights=[30, 55, 15])[0]
    target_mg    = int(random.uniform(3000, 5500) * appetite)
    per_visit_mg = target_mg // n_visits
    sub_min      = 90 // n_visits        # size of each sub-window

    visits = []
    for i in range(n_visits):
        sub_start = i * sub_min
        # Pick start within first ~70 % of the sub-window so duration fits
        latest_start = sub_start + max(0, sub_min - 25)
        start_off    = random.randint(sub_start, latest_start)
        duration     = random.randint(5, min(25, sub_min - 1))
        s = window_start + timedelta(minutes=start_off)
        e = s + timedelta(minutes=duration)
        visits.append((s, e, per_visit_mg))
    return visits


def precompute_plans() -> dict:
    """Plan all nights once so feeding_intensity() can look up by (cage, date)."""
    end_est = START_TS + timedelta(
        minutes=INTERVAL_MIN * (N_ROWS // len(CAGES) + 100)
    )
    out: dict[str, dict] = {c["cage_id"]: {} for c in CAGES}
    d = START_TS.date()
    while datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc) <= end_est:
        window_start = datetime(d.year, d.month, d.day, 23, 0, 0, tzinfo=timezone.utc)
        for c in CAGES:
            out[c["cage_id"]][d] = plan_visits(window_start, c["appetite"])
        d += timedelta(days=1)
    return out


PLANS = precompute_plans()


def feeding_intensity(cage_id: str, t: datetime) -> tuple[int, int]:
    """Return (seconds_eating_in_this_5min_slot, food_consumed_mg)."""
    # The 23:00 - 00:30 window straddles midnight, so the "plan date" is
    # today if hour >= 23, or yesterday if hour == 0.
    if t.hour >= 23:
        plan_date = t.date()
    elif t.hour == 0 and t.minute < 30:
        plan_date = (t - timedelta(days=1)).date()
    else:
        return (0, 0)

    slot_end = t + timedelta(minutes=INTERVAL_MIN)
    total_s, total_mg = 0, 0
    for v_start, v_end, mg in PLANS[cage_id].get(plan_date, []):
        o_start, o_end = max(t, v_start), min(slot_end, v_end)
        if o_end > o_start:
            ovl_s = (o_end - o_start).total_seconds()
            total_s += int(ovl_s)
            v_total_s = (v_end - v_start).total_seconds()
            total_mg += int(mg * (ovl_s / v_total_s))
    return (min(total_s, INTERVAL_MIN * 60), total_mg)


# ── Environmental noise ──────────────────────────────────────────────────────
def diurnal_temp(t: datetime, baseline: float) -> float:
    # Cooler around 03:00, warmer around 15:00 — small ±0.4 °C swing
    rad = (t.hour * 60 + t.minute) / (24 * 60) * 2 * math.pi
    return round(baseline + 0.4 * math.sin(rad - math.pi / 2) + random.uniform(-0.2, 0.2), 2)


def diurnal_hum(t: datetime, baseline: float) -> float:
    rad = (t.hour * 60 + t.minute) / (24 * 60) * 2 * math.pi
    return round(baseline - 1.5 * math.sin(rad - math.pi / 2) + random.uniform(-0.6, 0.6), 1)


# ── Row builder ──────────────────────────────────────────────────────────────
hopper = {c["cage_id"]: c["hopper_g"] for c in CAGES}


def make_row(t: datetime, cage: dict) -> dict:
    window = in_feeding_window(t)
    m2open = minutes_to_window_open(t)
    eating_s, food_mg = feeding_intensity(cage["cage_id"], t) if window else (0, 0)
    feeding = eating_s > 0

    if food_mg > 0:
        hopper[cage["cage_id"]] -= food_mg / 1000.0
        if hopper[cage["cage_id"]] < 5.0:
            hopper[cage["cage_id"]] = 50.0

    if window:
        gate_state, servo = "open", 90
    elif m2open <= 60:
        gate_state, servo = "closed", 0
    else:
        gate_state, servo = "locked", 0

    mouse_at_gate = feeding or (window and random.random() < 0.05)

    note = None
    if hopper[cage["cage_id"]] == 50.0 and food_mg > 0:
        note = "hopper auto-refill"

    return {
        "ts": t.isoformat().replace("+00:00", "Z"),
        "cage_id": cage["cage_id"],
        "mouse_id": cage["mouse_id"],
        "schedule_id": SCHEDULE_ID,
        "within_window": window,
        "minutes_to_window_open": m2open,
        "gate_state": gate_state,
        "servo_angle_deg": servo,
        "mouse_at_gate": mouse_at_gate,
        "feeding_event": feeding,
        "feed_duration_s": eating_s,
        "food_consumed_mg": food_mg,
        "hopper_weight_g": round(hopper[cage["cage_id"]] + random.uniform(-0.02, 0.02), 3),
        "cage_temp_c": diurnal_temp(t, cage["baseline_temp_c"]),
        "cage_humidity_pct": diurnal_hum(t, cage["baseline_hum_pct"]),
        "notes": note,
    }


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    rows: list[dict] = []
    t = START_TS
    while len(rows) < N_ROWS:
        for cage in CAGES:
            if len(rows) >= N_ROWS:
                break
            rows.append(make_row(t, cage))
        t += timedelta(minutes=INTERVAL_MIN)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(rows, f, indent=2)

    # Quick stats
    feeding_rows = sum(1 for r in rows if r["feeding_event"])
    total_food_g = sum(r["food_consumed_mg"] for r in rows) / 1000.0
    days_span    = (datetime.fromisoformat(rows[-1]["ts"].replace("Z", "+00:00"))
                    - datetime.fromisoformat(rows[0]["ts"].replace("Z", "+00:00"))
                    ).total_seconds() / 86400.0
    print(f"Wrote {len(rows):,} rows -> {out_path}", file=sys.stderr)
    print(f"  span:          {days_span:.2f} days", file=sys.stderr)
    print(f"  feeding rows:  {feeding_rows:,} ({feeding_rows/len(rows)*100:.1f}%)", file=sys.stderr)
    print(f"  food consumed: {total_food_g:.1f} g across all cages", file=sys.stderr)


if __name__ == "__main__":
    main()
