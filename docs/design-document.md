# Design Document — Intelligent IVC Cage

**Project:** Integrated Intelligent Precision Metabolic & Behavioral IVC Cage
**Client:** the industry partner (`contact@example.org`)
**Module:** Industry Project (semester 4)
**Document version:** 1.1
**Last updated:** 2026-05-20

---

## 1. Executive summary

This project delivers the software platform for a next-generation IVC (Individually Ventilated Cage) used in mouse/rat research. The platform receives sensor data from a smart cage, persists it in a time-series database, runs ML-based behaviour recognition, and presents researchers with a real-time dashboard.

The hardware is described in the client brief; this repository contains the **complete software stack** plus a faithful **device simulator** that emulates every sensor specified by the client. The simulator can be replaced 1-for-1 with the real device because both speak the same documented HTTP/JSON contract.

---

## 2. Requirements

### 2.1 Functional requirements

| ID | Requirement | Priority | Source |
|---|---|---|---|
| FR-01 | Measure actual food intake = delivered − remaining − wasted | MUST | Brief §4 |
| FR-02 | Restrict feeding to a configurable nocturnal window (default 23:00 – 00:30) | MUST | Brief §7 |
| FR-03 | Measure water intake with 0.01 mL precision and zero evaporation loss | MUST | Brief §4 |
| FR-04 | Capture infrared video + grid position; classify behaviour | MUST | Brief §5 |
| FR-05 | Measure VO₂, VCO₂, RER, and EE in a closed-circuit configuration | MUST | Brief §6 |
| FR-05b | Measure cage environment (T/RH) with DHT11; display live in header | SHOULD | UX research |
| FR-06 | Authenticate researchers; only authenticated calls accepted | MUST | Rubric — Connect |
| FR-07 | Provide a researcher dashboard with real-time + historical views | MUST | Rubric — Design |
| FR-08 | Alert researcher when daily intake falls below threshold | SHOULD | UX research |
| FR-09 | Multi-cage support (1–N cages on one platform) | SHOULD | UX research |
| FR-10 | Export data for downstream statistical analysis (CSV) | SHOULD | UX research |

### 2.2 Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-01 | Ingestion latency (sensor → DB) | ≤ 250 ms p95 (measured: < 40 ms) |
| NFR-02 | Dashboard time-to-first-paint | ≤ 2 s on broadband |
| NFR-03 | Data retention | ≥ 90 days online, exportable |
| NFR-04 | Recover from a backend restart with no data loss | 100% (SQLite WAL) |
| NFR-05 | Run on a Raspberry Pi 5 (8 GB) as a single self-contained device | RAM ≤ 2 GB |
| NFR-06 | OWASP Top-10 controls applied (input validation, JWT, security headers) | All applicable items |
| NFR-07 | Auto-deploy from `main` without operator intervention | ≤ 60 s from push to live |
| NFR-08 | Publicly reachable without exposing inbound ports | Cloudflare Tunnel |

### 2.3 Out of scope

- Physical manufacture of the cage and sensors (hardware vendor's responsibility).
- Animal-welfare regulatory certification.
- Cloud-scale multi-tenant operation (single-lab deployment only).

---

## 3. User personas

### 3.1 Dr. Elena — Principal Investigator
- Wants a daily overview of every cage.
- Cares about anomalies (low intake, abnormal RER, unusual activity).
- Will export data for SPSS / R.

### 3.2 Marc — Research Technician
- Operates the cages day-to-day.
- Configures feeding windows, refills feed/water.
- Will use the dashboard the most.

### 3.3 Sofia — Lab Auditor
- Periodically checks that data is being recorded correctly.
- Needs read-only access and an audit log.

---

## 4. UX & visual design

### 4.1 House style

- **Primary palette:** slate-900 background, emerald-500 accent for live data, amber-500 for warnings, rose-500 for alerts.
- **Typography:** Inter for UI, JetBrains Mono for numeric readings.
- **Density:** information-dense but never crowded — 24 px gutters, 8 px line-height baseline.
- **Iconography:** Lucide icons (consistent stroke width).
- **Components:** shadcn/ui — Radix-based, accessible by default.
- **Dark mode:** primary surface; light mode supported.

### 4.2 Key screens

1. **Overview** — grid of cage cards (one per cage) with key metrics: last intake, last activity, RER, alerts.
2. **Cage detail** — tabs for Feeding, Water, Metabolic, Behaviour, History, Settings.
3. **Live feed** — real-time chart (last 15 min) of activity, RER, intake delta.
4. **Schedule** — visual editor for feeding windows.
5. **Reports** — daily/weekly summary, CSV export.
6. **Settings** — users, alert thresholds, sensor calibration.

### 4.3 Responsive behaviour

- Single-cage view: optimised down to 768 px.
- Overview grid: 1 / 2 / 3 / 4 columns at 480 / 768 / 1280 / 1920 px breakpoints.
- All interactive elements ≥ 44 × 44 px touch target.

### 4.4 Accessibility

- WCAG 2.1 AA contrast on all text.
- Keyboard navigation: tab order matches reading order; visible focus rings.
- ARIA labels on every icon-only button.
- All charts have a "View as table" alternative.

---

## 5. Architecture (summary)

The full software architecture is in [architecture.md](architecture.md);
the hardware analysis is in [HARDWARE.md](HARDWARE.md). At a glance:

- **Device** — Raspberry Pi 5 + Arduino Mega 2560 + USB cameras + DHT11.
- **Device → Backend** — HTTPS / JSON; sensor packets every 5 s; JWT-authenticated.
- **Backend** — FastAPI (Python 3.13), SQLite (WAL mode), background aggregator, WebSocket fan-out.
- **AI** — *two* models running in the aggregator loop:
  1. Behaviour classifier (scikit-learn HistGradientBoosting, macro-F1 0.996).
  2. Streaming anomaly detector (MAD-based, per-cage online baseline).
- **Frontend** — React 18 + Vite + Tailwind + shadcn/ui; REST + WebSocket; TanStack Query.
- **Public exposure** — Cloudflare Tunnel: `example.org` + `cam.example.org`,
  no inbound ports on the Pi.
- **Deployment** — GitHub `main` → 60 s poll → `pi-deploy.sh` → subsystem-aware
  rsync + systemd restart. No Docker, no cloud hosts.

---

## 6. Data model

```
User            (id, username, password_hash, role, created_at)
Cage            (id, name, location, animal_strain, animal_age_days,
                 device_token_hash, last_seen, created_at)
Reading         (id, cage_id, ts, sensor, seq, payload_json, ingested_at)
                 sensor ∈ {feeding, water, metabolic, behaviour, weighing, environment}
                 UNIQUE (cage_id, sensor, seq)              ← replay protection
BehaviourLabel  (id, cage_id, ts, label, confidence)        ← classifier output
Alert           (id, cage_id, ts, severity, rule, message, acknowledged_at)
                 rule ∈ {rer_out_of_range, intake_low_24h, no_activity,
                         anomaly_rer, anomaly_movement_cm, anomaly_water_flow_ml}
ScheduleWindow  (id, cage_id, start_local, end_local, days_of_week, active)
AuditLog        (id, user_id, action, target, ip, ts)
```

A normalised schema with one append-only `Reading` table keeps ingestion fast and lets us add sensors without migrations.

---

## 7. AI design — two complementary models

The aggregator loop runs **two** AI models on every 30 s tick:

### 7.1 Model 1 — Behaviour classifier (supervised, discriminative)

**Goal:** classify each window into one of
`{sleeping, resting, active, exploring, eating, drinking, stereotyped}`.

**Inputs:** grid-cell occupancy histogram (16 cells), movement distance,
time-of-day, weight delta, recent feeding-gate state.

**Model:** gradient-boosted decision tree (scikit-learn
`HistGradientBoostingClassifier`) — small, fast, interpretable; comparable
accuracy to small NNs on tabular behavioural data.

**Training:** synthetic dataset (20 000 rows, seed = 42 → reproducible).
70/15/15 train/val/test split. Measured macro-F1 on the held-out 15 % test
split: **0.996** (HistGradientBoostingClassifier).

> **Read that 0.996 with care.** Both the training and the test split come from
> `ai/training/generate_dataset.py`, a rule-based generator that seeds its own
> labels. The classifier is therefore recovering rules the generator wrote, and
> the score measures how separable the generator made its classes — not accuracy
> on a real animal. The comparison table below is the evidence: two unrelated
> model families tie at 0.996 and even logistic regression reaches 0.935, which
> is what a saturated benchmark looks like. No labelled real-animal data was
> collected (live animals were out of scope), so the inference path is validated
> as *working*, not as *accurate*.

**Comparison:** three candidates benchmarked in
`ai/training/compare_models.py`. Latest measurements (held-out 20 % split):

| Model | Macro-F1 |
|---|---:|
| `random_forest` | 0.996 |
| `hist_gradient_boosting` | 0.996 |
| `logistic_regression` | 0.935 |

HistGradientBoosting is the production choice: nearly tied with RandomForest
on accuracy, smaller artefact, and faster inference.

**Code:** `backend/app/ai/classifier.py` — falls back to a deterministic
rule-based classifier if the model artefact is missing, so the platform
remains useful on a fresh clone.

### 7.2 Model 2 — Streaming anomaly detector (unsupervised, online)

**Goal:** flag windows whose RER / movement / water-flow profile is
statistically unusual *for that specific cage*, before they cross a hard
rule threshold.

**Why a second model?** The classifier always picks a label — even
unhealthy windows come back as e.g. "active" with low confidence. The
anomaly detector asks the orthogonal question: *is this normal **for this
animal**?* This catches early metabolic stress, dehydration onset and
equipment drift that the classifier alone would miss.

**Algorithm:** EWMA + **MAD** (Median Absolute Deviation) for robust
per-cage z-scoring. Each `(cage, metric)` pair has its own sliding window
of 240 samples (≈ 2 h of history). |z| ≥ 3 → *info*; |z| ≥ 4.5 → *warning*.

**Why MAD instead of Isolation Forest / one-class SVM?**

* No `scikit-learn` dependency to ship — runs natively on the Pi.
* Streaming-friendly — no batch retraining cycle.
* Resistant to the very anomalies we're trying to catch (median, not mean).
* Inspectable: a researcher can read the baseline from
  `/api/v1/system/anomaly` and reason about every score.

**Code:** `backend/app/ai/anomaly.py`. Per-cage, per-metric state; alerts
written to the existing `Alert` table with rule names like
`anomaly_rer` so they appear in the existing Alerts UI.

### 7.3 Future-proofing

Both models are loaded by string reference, so swapping in a fine-tuned
NN classifier or an Isolation Forest anomaly detector is a one-file
change.

---

## 8. Security design

| Concern | Mitigation |
|---|---|
| Sensor spoofing | Each device gets a per-device API token (rotatable). |
| Replay attacks | Reading payload includes a monotonic `seq`; backend rejects duplicates per cage. |
| SQL injection | Parameterised queries only (SQLAlchemy ORM). |
| XSS | React escapes by default; no `dangerouslySetInnerHTML`. |
| CSRF | Cookie auth disabled; all auth via `Authorization: Bearer …`. |
| Password storage | bcrypt with cost 12. |
| Transport | HTTPS in production (terminated at reverse proxy). |
| Audit | Every privileged action recorded in `AuditLog`. |
| Secret management | All secrets via `.env`; never committed; `.env.example` lists keys without values. |

---

## 9. Testing strategy

- **Backend:** pytest, ≥ 70% line coverage on `app/services/*` (the business-logic layer).
- **Frontend:** Vitest + React Testing Library for components; Playwright smoke test for the happy path.
- **Integration:** `scripts/dev-up.sh` brings up backend + frontend + simulator;
  the test suite posts sensor packets and asserts the dashboard reflects them.
- **AI (classifier):** held-out test set; macro-F1 ≥ 0.85 enforced in CI.
- **AI (anomaly detector):** synthetic anomaly injection — the test suite
  injects an out-of-baseline RER and asserts that the detector raises an
  `anomaly_rer` alert within one tick after warm-up.
- **Manual:** UX walk-through against the personas before each demo.
- **Production smoke test:** `/health` + `/api/v1/system/status` are
  monitored every minute by the deploy timer's status check.

---

## 10. Roll-out

The Industry Project module is a 1-semester prototype. Roll-out is staged:

1. **Sprint 1–2:** simulator + backend skeleton, schema, auth.
2. **Sprint 3:** dashboard MVP (overview + cage detail).
3. **Sprint 4:** ML behaviour classifier wired in.
4. **Sprint 5:** alerts, scheduling, reports.
5. **Sprint 6 (final):** Pi deployment, Cloudflare Tunnel, DHT11 sensor,
   second AI model (anomaly detector), auto-deploy pipeline, System ops
   page, accessibility audit, presentation, hand-off.

See [HARDWARE.md](HARDWARE.md) for the device reproducibility steps. (The sprint
plan, backlog and burndown were course-process artefacts and are not part of this
public extract.)
