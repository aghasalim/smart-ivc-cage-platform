"""Per-cage personality profiles.

Baseline numerics are taken from published indirect-calorimetry studies on
C57BL/6J mice — primarily:

- PMC4792845 (Senn et al., Data in Brief 2016): VO2, VCO2, RER and movement
  for C57BL/6J females on day 3 of standard vs high-fat diet, fasted/non-fasted.
- PMC5140024 (Diessler et al., PLOS Biology 2018): sleep and activity rhythms
  in C57BL/6J under a standard 12:12 light/dark cycle.
- OSF dataset hwxgv: food intake / body-weight time series.

Approximate adult C57BL/6J reference values (≈8–12 weeks):
  body weight, female : 18–22 g
  body weight, male   : 23–28 g
  daily intake, chow  : 3.5–4.5 g
  daily intake, HFD   : 2.5–3.2 g (energy-dense, so less mass)
  daily water         : 4.5–6.5 mL
  VO2 (light/rest)    : 50–58 mL O2 / min / kg
  VO2 (dark/active)   : 62–75 mL O2 / min / kg
  RER, chow           : ~0.85–0.95 (fed dark), ~0.78–0.85 (light/rested)
  RER, HFD            : ~0.72–0.82 (chronic fat oxidation)
  RER, fasted         : drops to ~0.70–0.75
  EE                  : ~0.35–0.55 kcal / h for a 20 g mouse
"""
from dataclasses import dataclass, field
from typing import List, Literal

Diet = Literal["chow", "hfd"]
Sex = Literal["male", "female"]


@dataclass
class CageProfile:
    cage_id: str

    # Animal
    sex: Sex = "male"
    body_weight_g: float = 25.0

    # Diet (drives both intake mass and metabolic baselines)
    diet: Diet = "chow"

    # Feeding schedule (dark cycle hours, local time)
    feeding_window_start: float = 23.0   # 23:00
    feeding_window_end: float = 24.5     # 00:30 next day

    # Intake / water budgets (24-hour totals)
    daily_intake_g: float = 4.0
    daily_water_ml: float = 5.5

    # Metabolic baselines (light cycle / resting values)
    base_vo2: float = 52.0    # mL O2 / min / kg, light cycle
    base_rer: float = 0.85    # chow-fed light cycle

    # Activity multiplier (1.0 = average B6 mouse)
    activity_amplitude: float = 1.0

    # Sleep behaviour
    light_sleep_fraction: float = 0.72  # ~70-80% sleep during light (Diessler et al.)
    dark_sleep_fraction: float = 0.28   # ~25-35% during dark

    quirks: List[str] = field(default_factory=list)


# Three cohorts that approximate the experimental design in PMC4792845:
# - cage-001 and cage-002: female C57BL/6J, standard chow
# - cage-003: female C57BL/6J, high-fat diet (HFD day 3)
PROFILES: dict[str, CageProfile] = {
    "cage-001": CageProfile(
        cage_id="cage-001",
        sex="female",
        body_weight_g=20.4,
        diet="chow",
        daily_intake_g=4.2,
        daily_water_ml=5.6,
        base_vo2=53.5,
        base_rer=0.88,
        activity_amplitude=1.05,
    ),
    "cage-002": CageProfile(
        cage_id="cage-002",
        sex="female",
        body_weight_g=21.1,
        diet="chow",
        daily_intake_g=4.0,
        daily_water_ml=5.4,
        base_vo2=51.8,
        base_rer=0.87,
        activity_amplitude=0.95,
        quirks=["sleeps_late"],
    ),
    "cage-003": CageProfile(
        cage_id="cage-003",
        sex="female",
        body_weight_g=22.6,
        diet="hfd",
        daily_intake_g=2.9,           # HFD: energy-dense, less mass
        daily_water_ml=5.0,
        base_vo2=55.0,                # slightly higher EE on HFD
        base_rer=0.77,                # shifted toward fat oxidation
        activity_amplitude=0.90,      # mild HFD-induced hypoactivity
        quirks=["restless"],
    ),
}


def get_profile(cage_id: str) -> CageProfile:
    return PROFILES.get(cage_id, CageProfile(cage_id=cage_id))
