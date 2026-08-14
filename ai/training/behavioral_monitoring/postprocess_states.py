"""
postprocess_states.py
Temporal smoothing for window-level state predictions.

Standalone module used by:
  - train_classifier_v2.py  (offline evaluation on held-out test set)
  - realtime_monitor.py     (live pipeline — state_history deque)

Run directly to test on outputs/sgd_v2_learning_log.csv:
    python postprocess_states.py
"""

import os
import sys

import pandas as pd

CONSECUTIVE_QUIET_FOR_SLEEP = 3   # windows of Quiet/Rest  -> Possible Sleep
CONSECUTIVE_ACTIVE_TO_WAKE  = 2   # min Wake/Active windows needed to exit sleep
TEMPORAL_WINDOW             = 450  # rolling context window (frames)

_QUIET = "Quiet / Rest"
_SLEEP = "Possible Sleep"
_WAKE  = "Wake / Active"


def smooth_state_predictions(predictions: list) -> list:
    """
    Apply temporal smoothing to a sequence of window-level state predictions.

    Rule 1 — sleep induction:
        If CONSECUTIVE_QUIET_FOR_SLEEP or more consecutive windows are
        Quiet/Rest, reclassify all of them as Possible Sleep.

    Rule 2 — sleep persistence:
        Short Wake/Active runs (< CONSECUTIVE_ACTIVE_TO_WAKE) sandwiched
        between Possible Sleep periods are absorbed back into Possible Sleep,
        so a single noisy active window does not prematurely end a sleep bout.

    Args:
        predictions: list of state strings per window
    Returns:
        smoothed list of state strings (same length)
    """
    if not predictions:
        return []

    out = list(predictions)
    n   = len(out)

    # ── Pass 1: sustained Quiet/Rest runs → Possible Sleep ───────────────────
    i = 0
    while i < n:
        if out[i] == _QUIET:
            j = i
            while j < n and out[j] == _QUIET:
                j += 1
            if j - i >= CONSECUTIVE_QUIET_FOR_SLEEP:
                for k in range(i, j):
                    out[k] = _SLEEP
            i = j
        else:
            i += 1

    # ── Pass 2: short Wake gaps inside Possible Sleep → absorb ───────────────
    i = 0
    while i < n:
        if out[i] == _WAKE:
            j = i
            while j < n and out[j] == _WAKE:
                j += 1
            wake_run   = j - i
            prev_sleep = i > 0 and out[i - 1] == _SLEEP
            next_sleep = j < n and out[j] == _SLEEP
            if prev_sleep and next_sleep and wake_run < CONSECUTIVE_ACTIVE_TO_WAKE:
                for k in range(i, j):
                    out[k] = _SLEEP
            i = j
        else:
            i += 1

    return out


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    LOG_CSV = "outputs/sgd_v2_learning_log.csv"
    if not os.path.exists(LOG_CSV):
        print(f"Not found: {LOG_CSV}")
        sys.exit(1)

    df = pd.read_csv(LOG_CSV)
    print(f"Loaded {LOG_CSV}: {len(df)} windows")
    print(f"Columns: {list(df.columns)}\n")

    rf_labels  = df["rf_label"].tolist()
    sgd_labels = df["sgd_label"].tolist()

    smoothed = smooth_state_predictions(sgd_labels)

    # ── How many windows changed ──────────────────────────────────────────────
    changed_total    = sum(s != o for s, o in zip(smoothed, sgd_labels))
    qr_to_sleep      = sum(
        1 for o, s in zip(sgd_labels, smoothed)
        if o == _QUIET and s == _SLEEP
    )
    wake_to_sleep    = sum(
        1 for o, s in zip(sgd_labels, smoothed)
        if o == _WAKE and s == _SLEEP
    )

    print(f"=== Smoothing rule results ===")
    print(f"Total windows          : {len(smoothed)}")
    print(f"Windows changed        : {changed_total}  "
          f"({changed_total / len(smoothed) * 100:.1f}%)")
    print(f"  Quiet/Rest -> Sleep  : {qr_to_sleep}")
    print(f"  Wake -> Sleep (gap)  : {wake_to_sleep}")

    # ── Agreement with RF before / after ────────────────────────────────────
    agree_before = sum(s == r for s, r in zip(sgd_labels, rf_labels))
    agree_after  = sum(s == r for s, r in zip(smoothed, rf_labels))
    n = len(rf_labels)
    print(f"\nAgreement with RF")
    print(f"  Before smoothing     : {agree_before}/{n}  "
          f"({agree_before / n * 100:.1f}%)")
    print(f"  After  smoothing     : {agree_after}/{n}  "
          f"({agree_after / n * 100:.1f}%)")

    # ── Possible Sleep recall estimate ───────────────────────────────────────
    rf_sleep_mask = [r == _SLEEP for r in rf_labels]
    n_rf_sleep    = sum(rf_sleep_mask)
    if n_rf_sleep > 0:
        recall_before = sum(
            s == _SLEEP for s, m in zip(sgd_labels, rf_sleep_mask) if m
        ) / n_rf_sleep
        recall_after  = sum(
            s == _SLEEP for s, m in zip(smoothed, rf_sleep_mask) if m
        ) / n_rf_sleep
        print(f"\nPossible Sleep recall (using RF as ground truth)")
        print(f"  Before smoothing     : {recall_before:.3f}")
        print(f"  After  smoothing     : {recall_after:.3f}")
        print(f"  Delta                : {recall_after - recall_before:+.3f}")
    else:
        print("\nNo Possible Sleep windows in RF labels — cannot estimate recall.")

    # ── State distribution before / after ────────────────────────────────────
    print(f"\nState distribution in SGD predictions")
    all_states = [_WAKE, _QUIET, _SLEEP]
    print(f"{'State':<18}  {'Before':>8}  {'After':>8}  {'Delta':>8}")
    for st in all_states:
        b = sgd_labels.count(st)
        a = smoothed.count(st)
        print(f"  {st:<16}  {b:>8}  {a:>8}  {a-b:>+8}")
