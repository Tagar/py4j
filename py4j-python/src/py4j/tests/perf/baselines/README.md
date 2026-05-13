# Reference baselines

Compact JSON snapshots of the perf-framework output, paired with their
human-readable markdown rendering. Each file pair captures one run of
the full scenario suite on one machine — committed so that future
optimization PRs can `--compare` against a known point rather than
having to capture a fresh baseline on every contributor's laptop.

## Naming convention

```
YYYY-MM-DD-os-arch-cpu.json    # compact, --n-runs 3, used by --compare
YYYY-MM-DD-os-arch-cpu.md      # human-readable summary
```

Date is the capture date. `os-arch-cpu` is enough hardware detail to
tell baselines apart at a glance. Examples:

- `2026-05-12-macos-arm64-m4pro.{json,md}`
- `2026-09-01-linux-x86_64-xeon-gold-6240.{json,md}`

## Adding a new baseline

```bash
cd py4j-python
python -m py4j.tests.perf \
  --n-runs 3 \
  --save src/py4j/tests/perf/baselines/$(date +%F)-linux-x86_64-<cpu>.json \
  --save-compact \
  --output-dir /tmp/perf-baseline-tmp
```

Then render the markdown from the same JSON:

```python
import json
from py4j.tests.perf.report import write_markdown
with open("src/py4j/tests/perf/baselines/...json") as f:
    write_markdown(json.load(f), "src/py4j/tests/perf/baselines/...md")
```

Keep the machine idle during capture (close browser, IDE, chat apps).
The framework prints environment warnings for battery / load / thermal
state — heed them. The inaugural baseline was captured at noise levels
well under each scenario's `expected_cv` budget; new baselines should
aim for the same.

## Using a baseline for comparison

```bash
python -m py4j.tests.perf \
  --compare src/py4j/tests/perf/baselines/2026-05-12-macos-arm64-m4pro.json \
  --output-dir /tmp/perf-diff
```

The framework will warn (loudly) if the baseline's environment doesn't
match the current run's — different OS, JVM, or Python version makes
the comparison much harder to interpret. Cross-hardware comparisons
are sometimes useful for spotting *direction* of change but not for
quantifying it.

## When to refresh

- After a significant perf-affecting change merges to master, capture
  a fresh baseline on the same hardware to keep the reference current.
- When adding support for a new platform (e.g. arm64 Linux), add its
  baseline alongside the existing ones; don't replace.
- Bump the file pair (delete old, add new with newer date) once a
  hardware-specific baseline is more than a few months old or no
  longer reflects current code.
