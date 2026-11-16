# Named structured configurations tutorial

**English** | [Français](../docs-fr/structured-configurations-tutorial.md)

This tutorial compares three ordered `config.matching_attributes` lists in a
small Grid campaign. It is an end-to-end demonstration of configuration,
execution, storage, and analysis plumbing. It is not a scientific campaign and
does not support inferential or decision claims.

## 1. Prepare the model before initialization

The active eqasim-france YAML referenced by `--model-config` must already contain
`config.matching_attributes` as a list of strings. For example:

```yaml
config:
  matching_attributes:
    - sex
    - any_cars
    - age_class
    - socioprofessional_class
    - departement_id
```

Add this key to the tracked YAML that eqasim-france actually loads, not to an
untracked local copy. Do this before `init` or any `plan`, then commit it and
leave the complete model repository clean:

```bash
git -C "$ROOT/eqasim-france" add config_local_nantes.yml
git -C "$ROOT/eqasim-france" commit -m "Declare matching attributes"
git -C "$ROOT/eqasim-france" status --short
```

The final command must print nothing. Adapt the filename if `init` uses another
`--model-config`. The model commit is part of `contextHash`; changing it after a
plan has been sealed requires a new project or, at minimum, a new `campaign` (and so a new,
empty campaign folder). Never place a changed context over an existing campaign.

## 2. Initialize or enter a generated project

Initialize after the model commit:

```bash
ROOT="$(pwd -P)"
SA=(uv run --project "$ROOT/eqasim-sa" --locked eqasim-sa)
PY=(uv run --project "$ROOT/eqasim-sa" --locked python)
"${SA[@]}" init \
  --project-name matching-structures \
  --model-root "$ROOT/eqasim-france" \
  --data-path "$ROOT/eqasim-data"
cd "$ROOT/matching-structures"
```

An existing generated project is also usable if its model paths point to this
tracked YAML and no campaign root is being reused after the context change. Use
the `SA=(...)` array rendered in its `README.md` if it differs from the command
above. For the inspection snippets, set `PY=(python)` when eqasim-sa and PyArrow
are installed in the active Python environment.

The target references resource calibration. If it has not already been run:

```bash
"${SA[@]}" plan --meta-config configs/00-calibration.yml
"${SA[@]}" run --meta-config configs/00-calibration.yml
```

## 3. Derive a separate Grid target

Keep the generated starter intact:

```bash
cp configs/01-grid.yml configs/04-structured-grid.yml
```

In `configs/04-structured-grid.yml`, make these exact replacements:

1. Replace the value of `campaign:` with `matching-structures-grid-structured`.
   The campaign folder follows by default:
   `campaigns/matching-structures-grid-structured/`.
2. Replace the complete `parameters:` block, up to but not including
   `outputs:`, with the block below.

```yaml
# structured-grid-tutorial-replacement
campaign: matching-structures-grid-structured
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
```

The `campaign` line replaces the existing top-level line; do not insert a
second copy. The `parameters` block replaces both starter factors. Keep all
other blocks from `01-grid.yml` unchanged, including:

- `method: grid` and the two replication seeds `[1201, 1202]`;
- the three `outputs`, which give by default the age/INSEE, distance/HTS, and
  number-of-vehicles/HTS indicators;
- the `calibration.meta_configs` block;
- the `model` and `execution` blocks, including `machine_options`.

`"${SA[@]}" show-config configs/04-structured-grid.yml` prints the resulting
configuration with every default written out.

There are `3 configurations × 2 replication seeds = 6` design positions and,
because all three ordered payloads differ, 6 expected unique runs. The
`age_priority` variant uses the same EDGT/census columns as the default but
changes their relaxation priority; it does not assume an unavailable field.

## 4. Plan, check, estimate, run, and analyze

```bash
TARGET=configs/04-structured-grid.yml
"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET"
"${SA[@]}" estimate --meta-config "$TARGET"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

On first planning, verify `designPositions: 6` and `uniqueRuns: 6`. `check` and
`estimate` require at least one complete run in the referenced calibration.

## 5. Inspect the sealed plan with PyArrow

Run this from the generated project. It uses PyArrow and the standard library,
not pandas:

```bash
"${PY[@]}" - <<'PY'
import json
import pyarrow.parquet as pq

path = "campaigns/matching-structures-grid-structured/plan.parquet"
plan = pq.read_table(path)
metadata = dict(plan.schema.metadata or {})

assert metadata[b"planSchemaVersion"] == b"3"
assert metadata[b"structuredFactorsVersion"] == b"1"
raw = metadata[b"structuredFactors"]
structured = json.loads(raw)
canonical = json.dumps(
    {name: structured[name] for name in sorted(structured)},
    separators=(",", ":"),
    ensure_ascii=False,
).encode("utf-8")
assert raw == canonical

print(plan.select(["runId", "seed", "matching_attributes"]).to_pylist())
print(json.dumps(structured, indent=2, ensure_ascii=False))
PY
```

The `matching_attributes` plan column contains the labels `default`, `reduced`,
and `age_priority` for readability. The canonical `structuredFactors` metadata binds
those labels to the exact ordered lists. Labels are not execution values:
`runId` is computed from the fully resolved configuration, including the ordered
list, and the simulator receives that list through its per-run YAML snapshot.
It is deliberately not passed as a scalar `--matching_attributes` CLI option.

Order has model meaning: eqasim matching relaxes the last matching attribute
first. Reordering one payload therefore changes both behavior and `runId`.

## 6. Join indicators to readable aliases

The generation selected by `analysis-generation.json` contains
`indicators.parquet`, with `runId`, indicator name, and value. Resolve its
published path and join the aliases from the plan without pandas:

```bash
"${PY[@]}" - <<'PY'
import json
from pathlib import Path

import pyarrow.parquet as pq

root = Path("campaigns/matching-structures-grid-structured")
manifest = json.loads((root / "analysis-generation.json").read_text())
plan = pq.read_table(root / "plan.parquet").select(
    ["runId", "seed", "matching_attributes"]
)
indicators = pq.read_table(root / manifest["outputs"]["indicators"]["path"])
joined = indicators.join(plan, keys="runId", join_type="left outer")
order = [
    ("matching_attributes", "ascending"),
    ("seed", "ascending"),
    ("indicator", "ascending"),
]
print(joined.sort_by(order).to_pylist())
PY
```

Interpret the analysis files narrowly:

- `replication-dispersion.parquet` groups each named configuration and indicator
  across the two seeds and reports mean, standard deviation, CV, and a simple
  normal interval.
- `output-summary.parquet` summarizes all planned indicator observations; it is
  descriptive, not a structured-factor effect estimate.
- `governed-report.json` records this campaign as
  `demonstration_noninferential`, with no authorized claim.

These are demonstration comparisons. Grid does not estimate first-order or
total-order variance shares and cannot establish factor prioritization or factor
fixing. A per-configuration `matched_counts` diagnostic is recommended before
interpreting matching changes, but eqasim-sa does not currently compute or
archive `matched_counts`; arrange that evidence in the model workflow separately.

## 7. Scientific multi-scramble parameter fragment

For a separately authored scientific FP/FF campaign, a structured factor may be
used with `method: multiscramble_sobol`. The following is only a parameter
fragment, not a complete scientific charter or a runnable meta-configuration:

```yaml
parameters:
  matching_attributes:
    configurations:
      default: [sex, any_cars, age_class, socioprofessional_class, departement_id]
      reduced: [sex, age_class, socioprofessional_class]
      age_priority: [age_class, any_cars, sex, socioprofessional_class, departement_id]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Study justification for equal probability over three named matching structures
experiment:
  method: multiscramble_sobol
  seeds: [44011, 44012, 44013, 44014]
  seeds_provenance: Declared finite simulator-seed nuisance support
```

The finite seed support is written in `experiment.seeds`, never under
`parameters`; `seeds_provenance` states where it comes from. The fragment
omits the other required experiment keys (`base_sample_sizes`,
`scramble_seeds`).

Here `discrete_uniform` assigns equal probability to the three labels; it does
not assign probability independently to attributes inside a list. The finite
seed support is a non-actionable nuisance factor and does not represent
unrestricted intrinsic simulator noise. A real campaign still needs the full
scientific decision, uncertainty, estimand, budget, convergence, robustness,
stochastic `finite_seed_factor`, independent-scramble, and provenance contract.
Do not fabricate these fields from this fragment. See the
[YAML reference](yaml-reference.md),
[parameters and methods](parameters-and-methods.md#named-structured-list-factors),
and the study-authored [Nantes protocol](../protocols/nantes-scientific/README.md).

## Refusals and safe recovery

- `discrete_oat` and `canonical_morris` both reject structured factors.
  They cannot treat named lists as ordered numeric trajectory levels. Use Grid
  for a demonstration or a fully governed multi-scramble Sobol campaign when
  its probability model is defensible.
- Configuration keys must be exactly `configurations` for a demonstration, or
  `configurations` plus `distribution` for a scientific campaign.
- Every payload must be a non-empty ordered list of unique, non-empty strings.
  Labels must be non-empty, and payloads must be distinct; aliases are rejected.
- The base model value must already be a list of strings. eqasim-sa will not
  create or convert the model key.
- Schema-v3 metadata is sealed and canonical. Never edit labels, rows,
  `structuredFactors`, versions, `runId`, or completion markers by hand;
  validation rejects missing, reordered, aliased, or noncanonical metadata.
- Any design, payload, model commit, data manifest, lockfile, or package-version
  change requires a new campaign name (and so a new campaign folder). Preserve old plans as immutable
  provenance rather than repairing them in place.

See [Troubleshooting](troubleshooting.md#named-structured-factor-errors) for the
corresponding error families.
