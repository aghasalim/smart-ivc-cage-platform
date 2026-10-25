# Contributing

Issues and pull requests are welcome. By contributing you agree your work is released under this repository's licence (see `LICENSE`).

## The one rule

Every number in the README comes from an artifact in this repo and is recomputed independently. A change that moves a published number must update the artifact and the README together, and must say why the number moved. Please don't edit a number by hand.

## Before opening a pull request

```bash
cd backend && pip install -r requirements.txt pytest && pytest
```

CI runs the same checks. If a check fails, say so in the pull request rather than weakening it.

## Reporting a result you can't reproduce

Open an issue with the command you ran, the seed, the library versions and the number you got. A non-reproducing result is the most useful kind of report.

## Citing

See `CITATION.cff`.
