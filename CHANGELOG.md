# Changelog

Dates are release dates. Each version is also a GitHub release and the DOI on
the README points at the Zenodo archive of it.

## Unreleased

- 3D-printable mouse-shaped water reservoir (`device/mouse-reservoir/`): body
  and lid STLs with a four-colour slicer paint guide.
- `Makefile` with `make train`, `make test`, `make verify`, `make up`.
- Build guide (`docs/BUILD_GUIDE.md`): print, wire, flash, deploy, check, with
  a wiring diagram and a checkpoint after every stage.
- Data card for the synthetic training set (`ai/data/DATASET.md`).
- Results table generated from `metrics.json` (`ai/reports/RESULTS.md`).
- Colab notebook that reproduces the behaviour classifier end to end.
- `examples/`: a curl walkthrough and a 60-line Python client for the API.
- Standalone troubleshooting page (`docs/TROUBLESHOOTING.md`).
- `CONTRIBUTING.md` and this changelog.

## v1.1.0 — 2026-10-25

- Rewrote the README in plainer words, with the real hardware and dashboard
  photos at the top.
- `verify/report.py` now rejects a wrong per-class support and a missing class.
- Read naive SQLite timestamps as UTC in the state timeline.
- Keep one copy of `behavior_analysis.py`.

## v1.0.0 — 2026-09-27

- First public extract of the Industry Project repo, archived on Zenodo
  ([10.5281/zenodo.23003680](https://doi.org/10.5281/zenodo.23003680)).
  Client identifiers, minutes, hostnames and credentials removed.
