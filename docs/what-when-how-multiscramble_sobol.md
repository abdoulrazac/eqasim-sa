# Multi-scramble Sobol: prioritize factors or test factor fixing

**Practical case: deciding which matching setting to learn more accurately first.**

**English** | [Français](../docs-fr/what-when-how-multiscramble_sobol.md)

## At a glance

After Morris screening, suppose that two parameters remain uncertain:
`matching_minimum_observations` and `matching_minimum_age`. You no longer want
only to know whether they have an effect. You want to decide where to invest the
next data-collection or modeling effort.

**Factor prioritization** (FP) asks:

> Which factor should we learn more accurately first to reduce expected output uncertainty?

**Factor fixing** (FF) asks a different question:

> Does a factor's total effect, including interactions, remain below a declared practical tolerance?

Both objectives use multi-scramble Sobol, but they require separate charters,
budgets, and campaigns. This guide first walks through the FP case, then presents
the FF variant.

## Essential concepts

- The **first-order index** is the share of variation associated with the factor alone.
- The **total-order index** includes the factor and all its interactions.
- A **scramble** is an independent randomization of the Sobol sequence.
- A **checkpoint** is a sample size at which stability is assessed.
- An **actionable** factor can be the subject of a decision; the simulator seed cannot.

Several independent scrambles show how estimates change from one Sobol
randomization to another. Resampling rows from a single scramble does not provide
this evidence; this is why [historical Sobol](what-when-how-sobol.md) remains
non-inferential.

## Prerequisite: completed screening

The Nantes protocol permits FP or FF only after canonical Morris production that
is:

- complete;
- converged according to the declared criteria;
- assigned `conditional` status;
- supported by a sealed plan and a sealed governed report.

The renderer checks this gate. Do not replace the screening campaign with a
fictitious path, and do not remove a factor simply because its Morris rank seemed
low: screening never proves that a factor can be fixed.

The steps below assume that you completed the
[canonical Morris example](what-when-how-canonical_morris.md), that `$ROOT` and
the Bash array `SA` are defined, and that `mon-analyse-sa/` contains the
manifest, scratch directory, and a complete resource calibration.

## The end-to-end FP case

The complete case is in
[`03-multiscramble-fp.yml`](../protocols/nantes-scientific/03-multiscramble-fp.yml).
It is a concrete protocol, not a directly executable file: the renderer must
first replace its machine placeholders and record the screening provenance. This
guide does not claim that an actual Nantes campaign has already produced the
fictitious results shown below.

### 1. State the decision

The charter asks which of the two numerical settings should be learned more
accurately first to reduce variation in the age indicator:

```yaml
decision:
  claim: Prioritize which of the two declared Nantes numerical factors should be learned more accurately first
  use: diagnostic
  setting: Loire-Atlantique department 44 population synthesis using INSEE RP 2022 and EDGT 44
  objective: fp
  estimand: First-order Sobol variance shares for age total-variation distance over the two declared numerical factors and finite seed support
  primary_indicator: tv_distance(age, ref=INSEE)
```

An FP decision ranks actionable factors by their mean first-order index. It does
not yet say that a factor can be fixed.

### 2. Declare the factors, including simulator noise

```yaml
parameters:
  matching_minimum_observations:
    values: [5, 20, 50]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Production screening report sha256:<actual screening digest>
  matching_minimum_age:
    values: [0, 5, 12]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Production screening report sha256:<actual screening digest>
  seed:
    values: [44011, 44012, 44013, 44014]
    distribution:
      family: discrete_uniform
      transform: identity
      provenance: Declared finite simulator-seed support
```

The two matching settings are actionable. `seed` represents a source of random
variation needed for the calculation, but nobody will "learn the seed more
accurately." It therefore remains diagnostic and receives no actionable FP rank.

Every ordinary factor must already exist under `config:` in the sealed model.
The special `seed` key is the only exception and must not exist there. Never
declare `random_seed` as a factor.

The support and equal probabilities must be justified by the study. A convenient
list of values is not scientific evidence by itself.

### 3. Declare the primary indicator

As in canonical Morris, the protocol calculates age TV against INSEE, distance
TV against HTS, and number-of-vehicles TV against HTS. Only
`tv_distance(age, ref=INSEE)` governs the FP decision. The other indicators help
identify secondary effects; they must not silently change the question after the
results have been seen.

For Nantes/EDGT 44, the distance indicator is asymmetric: synthetic bins use
Euclidean activity-to-activity distance, whereas HTS bins use routed distance.
It remains a secondary diagnostic.

### 4. Declare the scrambles and checkpoints

```yaml
experiment:
  method: multiscramble_sobol
  seed_role: factor
  base_sample_sizes: [8, 16, 32]
  scramble_seeds: [44101, 44102, 44103, 44104]

analysis:
  completeness_threshold: 1.0
  confidence_level: 0.95
```

The four scramble seeds create four independent randomizations. Within each
scramble, sizes `8`, `16`, and `32` are nested: checkpoint 16 reuses the first 8
points, and checkpoint 32 reuses the first 16.

The maximum number of positions uses `S = 4` scrambles, `N = 32`, and `D = 3`
factors, including the seed:

```text
S × N × (2D+2) = 4 × 32 × 8 = 1 024 positions
```

The budget field is 1,280 with a 20% reserve, leaving exactly **1,024 structural
positions** available for the design. After projection onto the discrete
`3 × 3 × 4` supports, these positions represent **36 unique `runId` values**.
The scientific budget is deliberately checked against positions, while `run`
executes each `runId` only once. This equality is an explicit protocol choice,
not a general rule.

### 5. Render the FP campaign

From the `openmole-eqasim` root, after canonical Morris production:

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/03-multiscramble-fp.yml" \
  "$ROOT/rendered/multiscramble-fp.yml" \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/multiscramble-fp" \
  --screening-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production"
```

The renderer refuses to produce the YAML if the screening evidence is missing,
incomplete, unstable, or incompatible.

### 6. Plan, estimate, and run

```bash
TARGET="$ROOT/rendered/multiscramble-fp.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$TARGET"
"${SA[@]}" check --meta-config "$TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$TARGET"
"${SA[@]}" analyze --meta-config "$TARGET"
```

The specified calibration must already contain at least one complete run and be
compatible with the resources for this execution. Without a compatible
observation, follow your site's procedure before executing the 36 unique runs
represented by the 1,024 positions.

### 7. Read an FP decision

The `multiscramble-sobol-indices.parquet` file aggregates the four scrambles at
the final checkpoint. For FP, examine the first-order columns, rank, and
`is_actionable`.

**Fictitious** illustration:

| Factor | Mean first-order index | Across-scramble interval | Actionable | FP rank |
| --- | ---: | ---: | --- | ---: |
| `matching_minimum_observations` | `0.41` | `0.34-0.48` | yes | `1` |
| `matching_minimum_age` | `0.16` | `0.09-0.23` | yes | `2` |
| `seed` | `0.27` | `0.18-0.36` | no | blank |

In this fictitious example, the conditional learning priority is
`matching_minimum_observations` because it has the largest mean first-order index
among actionable factors. The seed may explain visible variation, but it does
not become a learning target.

Before writing that sentence, check:

- `multiscramble-sobol-by-scramble.parquet` to see the four independent estimates;
- `multiscramble-sobol-convergence.parquet` for stability across checkpoints;
- `multiscramble-sobol-diagnostics.parquet` for out-of-range values, contrasts, and duplicates;
- `convergence-evidence.parquet` to see whether the `ci_width` criterion passed;
- `governed-report.json` for the campaign status.

Raw indices are not clipped to `[0, 1]`. An out-of-range value from a finite
sample remains visible in the diagnostics and suggests increasing the budget or
revisiting the design.

An `unstable` status prohibits naming a priority. A `conditional` status permits
only a priority conditional on the declared setting. The current contract leaves
robustness checks pending and has `claimAuthorized: false`; it therefore does not
produce an authorized, robust final claim.

## FF variant: test whether a factor can be fixed

FF must be a separate campaign, with objective `ff` and a tolerance declared
**before** `plan`. The Nantes protocol
[`04-multiscramble-ff.yml`](../protocols/nantes-scientific/04-multiscramble-ff.yml)
uses `practical_tolerance: 0.05`, checkpoints `[16, 32, 64]`, and four other
scramble seeds.

Call the renderer in the same way, replacing the source, output, and root:

```bash
uv run --project "$ROOT/eqasim-sa" --locked python \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/render_protocol.py" \
  "$ROOT/eqasim-sa/protocols/nantes-scientific/04-multiscramble-ff.yml" \
  "$ROOT/rendered/multiscramble-ff.yml" \
  --model-config "$ROOT/eqasim-france/config_local_nantes.yml" \
  --model-root "$ROOT/eqasim-france" \
  --data-manifest "$ROOT/mon-analyse-sa/manifest.json" \
  --lockfile "$ROOT/eqasim-france/uv.lock" \
  --scratch "$ROOT/mon-analyse-sa/scratch" \
  --data-path "$ROOT/eqasim-data" \
  --campaign-root "$ROOT/mon-analyse-sa/campaigns/multiscramble-ff" \
  --screening-campaign-root "$ROOT/mon-analyse-sa/campaigns/canonical-morris-production"
```

Then run the complete FF campaign separately from FP:

```bash
FF_TARGET="$ROOT/rendered/multiscramble-ff.yml"
CALIBRATION="$ROOT/mon-analyse-sa/configs/00-calibration.yml"

"${SA[@]}" plan --meta-config "$FF_TARGET"
"${SA[@]}" check --meta-config "$FF_TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" estimate --meta-config "$FF_TARGET" --calibration-meta-config "$CALIBRATION"
"${SA[@]}" run --meta-config "$FF_TARGET"
"${SA[@]}" analyze --meta-config "$FF_TARGET"
```

The FF plan contains `4 × 64 × 8 = 2,048` structural positions projected onto
the same **36 unique runs**. Then read
`$ROOT/mon-analyse-sa/campaigns/multiscramble-ff/multiscramble-sobol-indices.parquet`.

For the primary indicator, FF compares the **upper bound of the total-order
interval**, column `total_order_ci_high`, with `0.05`. This tolerance is a
dimensionless total variance share; it is not `0.05` TV-distance units. The
`within_practical_tolerance` column records the comparison for actionable factors
of the primary indicator. If the bound is fictitiously `0.04`, the factor is
conditionally within tolerance, including interactions. If it is `0.08`, the
factor is not. A point estimate below `0.05` is insufficient if its upper bound
exceeds the tolerance.

Use this classification only when `convergence-evidence.parquet` confirms that
the `ci_width` criterion passed and `governed-report.json` has `conditional`
status. With `unstable`, the computed column remains diagnostic and supports no
FF decision.

The seed and secondary indicators never receive an FF decision.

## What you can conclude

For FP, after convergence, you may name a conditional learning priority among
the actionable factors. For FF, **after convergence**, you may say that a factor
is conditionally within the declared tolerance for the primary indicator,
including interactions.

You may not extend this conclusion to other supports, dependence structures,
indicators, or scenarios. You may neither rank nor fix the seed. With
`claimAuthorized: false`, the statement remains conditional and is not
authorized as a robust final claim.

## Checklist

- A complete, converged canonical Morris production campaign with `conditional` status exists.
- The charter chooses exactly one objective, FP or FF.
- Supports, probabilities, and independence are justified.
- At least four independent scrambles and doubling sample sizes are declared.
- `seed` is included as a finite non-actionable factor.
- For FF, the tolerance is declared before `plan`, and total order is used.
- Convergence, diagnostics, and the governed report are read before any conclusion is written.

## Next steps

- Previous step: [Canonical Morris](what-when-how-canonical_morris.md)
- Legacy workflow only: [Historical Sobol](what-when-how-sobol.md)
- Complete protocol: [Scientific Nantes](../protocols/nantes-scientific/README.md)
- References: [YAML](yaml-reference.md), [parameters and methods](parameters-and-methods.md), and [workflow and results](workflow-and-results.md)
