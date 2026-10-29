# Contributing

Thanks for looking. This is a cleaned-up student project, so the bar is low and
the welcome is real. Bug reports, hardware notes from your own build, and
real-mouse recordings are the most useful things you could send.

## Before you open a PR

```bash
make test      # backend tests (18)
make verify    # recomputes every published number; CI fails if one disagrees
```

If you touched the frontend, `cd frontend && npx tsc -b --noEmit && npm run build`.

## What goes where

| Change | Place | Also update |
|---|---|---|
| API endpoint | `backend/app/api/` | `docs/api-reference.md`, a test in `backend/tests/` |
| Firmware pin or protocol | `device/arduino/ivc_sensors/` | the pin table in `docs/HARDWARE.md` §3.1 |
| Training code | `ai/training/` | `ai/README.md`; rerun `make train` and commit `ai/reports/` |
| A number in the README | wherever it comes from | `verify/` must still pass |
| Printed part | `device/<part>/` with the STL and a README | `docs/BUILD_GUIDE.md` |

## Rules of thumb

- The published metrics are reproducible by construction. Don't edit
  `ai/reports/metrics.json` or `model-comparison.md` by hand; rerun the script.
- Don't commit model weights (`*.pkl`, `*.onnx`). `.gitignore` already blocks them.
- Don't add a dependency for something a few lines of stdlib can do.
- Keep the README's tone: say what was measured and how, and say what wasn't.

## Commit messages

One line, imperative, says what changed and why if it isn't obvious. The git log
is the project history, so a message like "fix" doesn't help anyone later.

## Reporting a bug

Use the issue template. The two things that make a bug fixable are the exact
command you ran and the exact output. For hardware, add the firmware line from
`/api/v1/meta` and a photo of the wiring.
