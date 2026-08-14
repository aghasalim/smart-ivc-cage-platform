"""Sensor packet builders.

Models 24-h C57BL/6J physiology under a standard 12:12 light/dark cycle.
Lights-on at 07:00, lights-off at 19:00 (typical vivarium schedule). The
dark cycle is the active phase for mice.

Sources (see profiles.py for full citations):
- PMC4792845: VO2 / VCO2 / RER / movement on chow vs HFD, day 3.
- PMC5140024: sleep / activity rhythms.
"""
from __future__ import annotations

import math
import random
from typing import Any

import numpy as np

from profiles import CageProfile

# Vivarium lighting schedule
LIGHTS_ON_HOUR = 7.0    # 07:00 local
LIGHTS_OFF_HOUR = 19.0  # 19:00 local

# Caloric equivalent of O2 for indirect calorimetry (Weir 1949 simplification):
#   EE [kcal/h] = 60 * (3.815 + 1.232 * RER) * VO2_L_per_min
WEIR_A = 3.815
WEIR_B = 1.232


def _is_dark(hour: float) -> bool:
    return hour < LIGHTS_ON_HOUR or hour >= LIGHTS_OFF_HOUR


def _circadian_activity(hour: float, profile: CageProfile) -> float:
    """Smooth 0..1 activity envelope.

    Mice are nocturnal — peak activity around 02:00, trough around 14:00.
    Two narrow secondary peaks at lights-on/off transitions (Diessler et al.).
    """
    rad = ((hour - 14.0) / 24.0) * 2.0 * math.pi
    base = 0.5 + 0.5 * math.sin(rad - math.pi / 2)

    # Lights-off transition burst
    if abs(hour - LIGHTS_OFF_HOUR) < 0.5:
        base = min(1.0, base + 0.25)
    # Lights-on burst (brief feeding/grooming)
    if abs(hour - LIGHTS_ON_HOUR) < 0.5:
        base = min(1.0, base + 0.15)

    if "restless" in profile.quirks:
        base = min(1.0, base + 0.10)
    if "sleeps_late" in profile.quirks and 22 <= hour < 24:
        base = min(1.0, base + 0.15)
    return max(0.0, min(1.0, base * profile.activity_amplitude))


def feeding_state(hour: float, profile: CageProfile, state: dict) -> dict[str, Any]:
    window_open = profile.feeding_window_start <= hour or hour <= (profile.feeding_window_end - 24)
    gate = "open" if window_open else "closed"

    if window_open:
        delivered = round(random.uniform(0.05, 0.20), 3)
        # HFD mice waste less (energy-dense pellets crumble less)
        waste_factor = (0.04, 0.10) if profile.diet == "hfd" else (0.05, 0.15)
        wasted = round(delivered * random.uniform(*waste_factor), 3)
    else:
        delivered = 0.0
        wasted = 0.0

    state["remaining_g"] = max(0.0, state.get("remaining_g", 20.0) - delivered)
    if state["remaining_g"] < 3:
        state["remaining_g"] = 20.0  # refilled at midnight

    return {
        "delivered_g": delivered,
        "remaining_g": round(state["remaining_g"], 2),
        "wasted_g": wasted,
        "gate_state": gate,
    }


def water_state(hour: float, profile: CageProfile, state: dict) -> dict[str, Any]:
    activity = _circadian_activity(hour, profile)
    # Spread the daily ration across ticks weighted by activity. 5s ticks
    # -> 17280 ticks/day; activity weight has mean ~0.5.
    base_flow = (profile.daily_water_ml / (24 * 720)) * activity * 2.0
    flow = max(0.0, base_flow + random.gauss(0, 0.05))
    state["tank_ml"] = max(0.0, state.get("tank_ml", 250.0) - flow)
    if state["tank_ml"] < 20:
        state["tank_ml"] = 250.0
    return {"flow_ml": round(flow, 3), "tank_ml": round(state["tank_ml"], 2)}


def metabolic_state(hour: float, profile: CageProfile) -> dict[str, Any]:
    """VO2 / VCO2 / RER / EE driven by light-dark cycle and diet.

    Numbers calibrated to PMC4792845 day-3 baselines:
      Chow: light VO2 ~52, dark VO2 ~68, RER ~0.85 light / ~0.92 dark.
      HFD:  light VO2 ~55, dark VO2 ~71, RER ~0.74 light / ~0.79 dark.
    """
    activity = _circadian_activity(hour, profile)
    dark = _is_dark(hour)

    # VO2 — light/dark difference is ~25-30% in healthy adults
    cycle_boost = 1.20 if dark else 1.0
    vo2 = profile.base_vo2 * cycle_boost * (0.85 + 0.30 * activity) + random.gauss(0, 1.5)
    vo2 = max(30.0, vo2)

    # RER — depends on diet (fat oxidation lowers RER) and feeding state
    rer = profile.base_rer
    if dark:
        rer += 0.04  # dark cycle = fed = more carb oxidation
    if profile.feeding_window_start <= hour or hour <= (profile.feeding_window_end - 24):
        rer += 0.03  # post-meal lift
    if not dark and not (profile.feeding_window_start <= hour or hour <= (profile.feeding_window_end - 24)):
        rer -= 0.02  # rested fasted state
    rer += random.gauss(0, 0.025)
    # Clamp to physiologically plausible window
    rer = max(0.68, min(1.05, rer))

    vco2 = vo2 * rer

    # Weir equation: EE [kcal/h] = 60 * (3.815 + 1.232 * RER) * VO2_L/min
    vo2_l_min = (vo2 / 1000.0) * profile.body_weight_g  # mL/min/kg * kg = mL/min, /1000 = L/min
    ee = 60.0 * (WEIR_A + WEIR_B * rer) * vo2_l_min / 1000.0  # → kcal/h

    return {
        "vo2_ml_min_kg": round(vo2, 2),
        "vco2_ml_min_kg": round(vco2, 2),
        "rer": round(rer, 3),
        "ee_kcal_h": round(ee, 4),
    }


def behaviour_state(hour: float, profile: CageProfile) -> dict[str, Any]:
    """4x4 IR positioning grid + movement distance.

    Sleep probability follows the light/dark fractions from PMC5140024:
      Light cycle: ~72% sleeping, ~28% wake.
      Dark cycle:  ~28% sleeping, ~72% wake.
    """
    activity = _circadian_activity(hour, profile)
    dark = _is_dark(hour)

    sleep_p = profile.dark_sleep_fraction if dark else profile.light_sleep_fraction
    is_sleeping = random.random() < (sleep_p * (1.0 - activity))

    grid = np.zeros(16, dtype=int)

    if is_sleeping:
        # Curled in a nest corner — one cell active
        grid[random.choice([0, 3, 12, 15])] = 1
        movement = random.uniform(0.0, 0.3)
    else:
        n_active = max(1, int(round(activity * 8 + random.gauss(0, 1))))
        for _ in range(n_active):
            i = random.randint(0, 15)
            grid[i] += 1
        # Movement scales with activity; B6 mice cover 50-200 cm/min at peak
        movement = random.uniform(2.0, 14.0) * activity

    return {
        "grid_cells": grid.tolist(),
        "movement_cm": round(movement, 2),
    }


def weighing_state(hour: float, profile: CageProfile) -> dict[str, Any]:
    drift = random.gauss(0, 0.02)
    return {"animal_g": round(profile.body_weight_g + drift, 2)}
