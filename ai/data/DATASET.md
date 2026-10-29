# Data card: `synthetic.csv`

The only training set in this repo. It is generated, not recorded. The README
explains at length why the 0.996 macro-F1 on it means very little; this page
is the short version plus the facts a reader needs to use the file.

## Where it comes from

`ai/training/generate_dataset.py`, seeded RNG, so `python generate_dataset.py`
gives you a byte-identical file. The committed copy was made with the defaults
(20 000 rows, seed 42). No animal was involved; the generator writes labels
from hand-written rules (nocturnal activity curve, feeding bouts when the gate
is open, daytime sleep, occasional stereotyped pacing) and then draws features
that match the label.

## What a row is

One minute of one cage. 21 feature columns and a label:

| Column | Type | Meaning |
|---|---|---|
| `grid_0` … `grid_15` | int | occupancy count per cell of the 4×4 IR grid |
| `movement_cm` | float | distance moved in the minute |
| `weight_g` | float | animal mass on the platform load cell |
| `gate_open` | 0/1 | feeding gate state |
| `hour`, `minute` | int | local time, to let the model learn the circadian rule |
| `label` | str | one of the seven classes below |

## Classes and balance

Counts in the committed 20 000-row file:

| Label | Rows | Share |
|---|---:|---:|
| active | 7 639 | 38.2 % |
| sleeping | 3 546 | 17.7 % |
| resting | 3 357 | 16.8 % |
| exploring | 3 163 | 15.8 % |
| drinking | 1 058 | 5.3 % |
| eating | 664 | 3.3 % |
| stereotyped | 573 | 2.9 % |

Imbalanced by design; the generator follows the published time budgets of
laboratory mice, and both training scripts stratify on the label.

## Split

`train.py` uses a stratified 85/15 train/test split, then 85/15 of the
training part for validation (seed 42): 14 450 / 2 550 / 3 000 rows.
`compare_models.py` uses its own 80/20 split with seed 0. Both split **by row**.
There is no animal or session identity in the file to split by, which is one of
the reasons the score can't be read as generalisation.

## Known limits

- The features are separable by construction. `movement_cm` alone puts each
  label in its own band (`ai/reports/figures/synthetic-separability.png`), and
  an unpruned decision tree matches the reported model within 0.003 F1.
- No sensor noise model, no missing readings, no drift, no multi-animal cages.
- `hour` and `minute` leak the circadian rule straight into the features.

## Real recordings in this repo

The camera pipeline under `ai/training/behavioral_monitoring/` and
`multi_animal_tracking/` was run on two real video files (48 h single mouse,
9.78 h five mice). Those videos are not committed; the derived CSVs under
`ai/data/*/outputs/` and the plots under `ai/reports/*/outputs/` are. They have
no ground-truth labels, so there is no accuracy figure for them either.

## Licence

Same as the repo (MIT). If you record real labelled data on this hardware,
please open an issue; a few hours of labelled video would be worth more to the
project than any change to the model.
