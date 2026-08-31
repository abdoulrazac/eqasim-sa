# Historical Morris: review an old campaign

**Practical example: understand an existing folder that already contains `method: morris`.**

**English** | [Français](../docs-fr/what-when-how-morris.md)

## Simple rule

If you are preparing a new study, **do not use this method**.

- For a small numeric demonstration, use [discrete OAT](what-when-how-discrete_oat.md).
- For scientific screening, use [canonical Morris](what-when-how-canonical_morris.md).

The old `morris` method remains available only for reviewing or reproducing a
historical campaign. Its effects are calculated over adjacent levels using
normalized local steps. They are not the effects of the current canonical
Morris method.

## Practical example: audit an archived campaign

Suppose a colleague gives you:

```text
old-study/
├── configs/morris-2024.yml
└── campaigns/morris-2024/
    ├── plan.parquet
    ├── runs/
    └── archives/
```

Your question is not "which parameters should I study today?" but:

> What exactly did this old campaign measure, and what do its results support?

### 1. Confirm that the provenance already exists

The original YAML must already contain:

```yaml
experiment:
  method: morris
  seed_role: replication
  trajectories: 8
  design_seed: 20260814
  replications:
    seeds: [1301, 1302]
```

The campaign folder must contain its sealed `plan.parquet`. The complete context
also includes the model's Git commit, the data manifest, the lockfile, the
`eqasim-sa` version, the archives, and the run markers. A folder without these
elements is not enough to reproduce the study.

Do not recreate a missing plan or modify the archived plan to make it accept a
different context. That would create a new campaign rather than review the old
one.

### 2. Recover the question, parameters, and indicators

For example, an old YAML may contain:

```yaml
parameters:
  sampling_rate:
    values: [0.001, 0.002, 0.005]
  matching_minimum_observations:
    values: [5, 20, 50]

indicators:
  - pair:
      scalarizer: tv_distance
      artifact: age
      reference: INSEE
```

In this example, the campaign observed local changes in the TV distance between
the synthetic age distribution and INSEE as `sampling_rate` or
`matching_minimum_observations` moved from one neighboring level to the next.

For a move from a `left` value to a `right` value, the historical calculation is:

```text
delta = (right - left) / (domain maximum - domain minimum)
effect = (indicator after - indicator before) / delta
```

The effect is therefore expressed for a normalized factor change over the
declared range. It is neither a continuous derivative nor a variance share.

The domains must be numeric and strictly increasing. Seeds are replications. The
historical method rejects structured factors.

This block describes what the study did; it does not justify the selected values
or recommend reusing this design.

### 3. Check the plan without replacing it

First restore the **archived version** of `eqasim-sa` in its environment. The
current version is not interchangeable because the runner version is part of the
sealed context. In the example below, `HISTORICAL_ENV` points to that restored
environment. The script then reads `campaign_root` from the YAML so it can check
the exact file **before** calling `plan`:

```bash
TARGET=/path/to/old-study/configs/morris-2024.yml
HISTORICAL_ENV=/path/to/archived-eqasim-sa-environment
SA=("$HISTORICAL_ENV/bin/eqasim-sa")
HISTORICAL_PYTHON="$HISTORICAL_ENV/bin/python"

PLAN=$("$HISTORICAL_PYTHON" - "$TARGET" <<'PY'
import sys
from pathlib import Path

import yaml

meta = Path(sys.argv[1]).resolve()
root = Path(yaml.safe_load(meta.read_text(encoding="utf-8"))["campaign_root"])
if not root.is_absolute():
    root = (meta.parent / root).resolve()
print(root / "plan.parquet")
PY
)
[[ -f "$PLAN" && ! -L "$PLAN" ]] || {
  printf '%s\n' "Historical plan is missing; stop the audit." >&2
  exit 1
}

RESULT=$("${SA[@]}" plan --meta-config "$TARGET") || exit 1
printf '%s\n' "$RESULT"
STATUS=$(printf '%s' "$RESULT" | "$HISTORICAL_PYTHON" -c \
  'import json, sys; print(json.load(sys.stdin)["status"])')
[[ "$STATUS" == unchanged ]] || {
  printf '%s\n' "Historical plan is not unchanged; stop the audit." >&2
  exit 1
}
```

Any error means that you must stop and recover the original files rather than
alter the provenance.

### 4. Reanalyze only complete archives

If all required runs and their archives are present, you can rebuild the
analysis outputs:

```bash
"${SA[@]}" analyze --meta-config "$TARGET"
```

`analyze` does not rerun the simulator. If runs are missing, `run` is acceptable
only if the original environment, execution setup, and context remain exactly
reproducible. Do not silently add new seeds, values, or data.

`check` and `estimate` mainly apply to a new execution. They require compatible
resource observations; a calibration created today is not part of the 2024
historical provenance.

### 5. Read the historical ranking

The `morris-ranking.parquet` file includes:

| Column | Historical interpretation |
| --- | --- |
| `mu` | mean local effect with its sign |
| `mu_star` | mean magnitude of the local effects |
| `sigma` | standard deviation of all `T × R` effects; it combines trajectories and seeds |
| `seed_std` | square root of the mean across-seed variance within each trajectory |
| `rank` | descending order by `mu_star` |

Suppose an **old output** ranks `sampling_rate` ahead of
`matching_minimum_observations`. You may write: "In the declared historical
design, `sampling_rate` had the largest normalized local effects on the age
indicator."

A large `sigma` does not by itself prove an interaction or nonlinearity because
seed variation also contributes to it. `sigma` and `seed_std` are both expressed
in normalized-effect units and can be compared with each other.
`replication-dispersion.parquet` remains useful context, but its standard
deviations are in raw TV-indicator units; do not compare them numerically with
the first two columns.

You must not write: "Morris proves that `sampling_rate` is globally the most
important factor." The design is not canonical and does not estimate variance
shares.

If the archived version produces `governed-report.json`, it must remain
`demonstration_noninferential` with `claimAuthorized: false`. Morris versions
from before governed reporting was introduced do not create this file: record
its absence in the audit and do not recreate it with a newer runner, because that
would change the sealed context.

## What you can conclude

You can describe the factors, levels, seeds, indicators, and local effects of
the old campaign, while clearly stating that they are historical and
noncanonical.

You cannot turn this ranking into scientific screening, prioritization, or a
factor-fixing decision. For a new question, create a new campaign with a current
method and a new root.

## Audit checklist

- The YAML and `plan.parquet` existed before the audit.
- The model, Git commit, data, lockfile, and version have been recovered.
- `plan` returns `unchanged`, never `created`.
- The required archives are complete before `analyze`.
- No plan or run marker is edited by hand.
- When this version has governed reporting, it records noninferential status; otherwise the audit records that the report is absent.

## Continue

- New demonstration: [Discrete OAT](what-when-how-discrete_oat.md)
- New scientific screening: [Canonical Morris](what-when-how-canonical_morris.md)
- References: [YAML](yaml-reference.md), [compatibility](compatibility.md), and [workflow and results](workflow-and-results.md)
